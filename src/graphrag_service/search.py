"""Retrieval: vector + keyword search fused by rank, and Personalized PageRank over the graph.

Three modes:
  vector  passages only, by embedding similarity
  hybrid  passages, claims, edges and entities; vector and keyword lists fused by RRF
  graph   hybrid, then a HippoRAG-2-style Personalized PageRank over entities and passages
          re-ranks everything, so evidence one or two hops from the matched entities surfaces

No LLM runs here. The caller is expected to be one.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field

import numpy as np
from scipy import sparse

from .store import Store, unj
from .text import RE_CJK, approx_tokens, fts_query, norm

RRF_K = 60
DAMPING = 0.5            # probability of following an edge; HippoRAG 2's setting
PASSAGE_SEED = 0.05      # passage seed weight relative to entity seeds; HippoRAG 2's setting
VIA_WEIGHT = {"page": 1.0, "wikilink": 0.5, "link": 0.5, "source": 0.3}
WEAK_EDGE = 0.5          # weight of the catch-all predicate and of mention edges
HUB_ALPHA = 0.5          # walk scores are divided by degree**alpha, so a hub does not win on size
PPR_WEIGHT = 0.5         # weight of the walk against plain retrieval when the two are blended
WEAK_PREDICATES = {"related_to"}


@dataclass
class Filters:
    types: list[str] | None = None
    after: str | None = None
    domain: str | None = None
    markers: list[str] | None = None
    include_raw: bool = True
    include_superseded: bool = False


@dataclass
class Snapshot:
    """Everything small enough to hold in memory, rebuilt when the store's generation moves."""
    generation: int
    nodes: dict = field(default_factory=dict)        # id -> row dict
    node_by_rid: dict = field(default_factory=dict)
    alias: dict = field(default_factory=dict)        # alias_norm -> node id
    alias_re: re.Pattern | None = None
    edges: dict = field(default_factory=dict)        # rid -> row dict
    claims: dict = field(default_factory=dict)       # rid -> row dict (with text)
    passages: dict = field(default_factory=dict)     # rid -> meta (no text)
    docs: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    node_ix: dict = field(default_factory=dict)      # node id -> matrix index
    passage_ix: dict = field(default_factory=dict)   # passage rid -> matrix index
    matrix: sparse.csr_matrix | None = None
    degree: np.ndarray | None = None                 # weighted degree per matrix index
    adj: dict = field(default_factory=dict)          # node id -> [(other id, label, edge rid)]


def build_snapshot(store: Store) -> Snapshot:
    db, s = store.db, Snapshot(store.generation)
    for r in db.execute("select * from nodes"):
        d = dict(r)
        d["aliases"], d["attrs"] = unj(d["aliases"]), unj(d["attrs"], {})
        s.nodes[d["id"]] = d
        s.node_by_rid[d["rid"]] = d
    for r in db.execute("select node_id, alias_norm from aliases"):
        # a page's own name wins over another page's alias
        if r["alias_norm"] not in s.alias or norm(s.nodes[r["node_id"]]["name"]) == r["alias_norm"]:
            s.alias[r["alias_norm"]] = r["node_id"]
    names = sorted((a for a in s.alias if len(a) >= 2), key=len, reverse=True)
    if names:
        parts = [re.escape(a) if RE_CJK.search(a) else rf"(?<!\w){re.escape(a)}(?!\w)" for a in names]
        s.alias_re = re.compile("|".join(parts))
    for r in db.execute("select * from edges"):
        d = dict(r)
        d["authors"], d["sources"] = unj(d["authors"]), unj(d["sources"])
        s.edges[d["rid"]] = d
    for r in db.execute("select * from claims"):
        d = dict(r)
        d["authors"], d["sources"], d["mentions"] = unj(d["authors"]), unj(d["sources"]), unj(d["mentions"])
        s.claims[d["rid"]] = d
    for r in db.execute("select * from documents"):
        s.docs[r["id"]] = dict(r)
    for r in db.execute("select * from sources"):
        d = dict(r)
        d["origins"], d["entities"] = unj(d["origins"]), unj(d["entities"])
        s.sources[d["id"]] = d
    for r in db.execute("select rid, id, doc_id, ord, heading, tokens from passages"):
        s.passages[r["rid"]] = dict(r)

    # ---- graph: entities and passages in one matrix
    s.node_ix = {nid: i for i, nid in enumerate(s.nodes)}
    n = len(s.node_ix)
    s.passage_ix = {rid: n + i for i, rid in enumerate(s.passages)}
    size = n + len(s.passage_ix)
    weights: dict[tuple[int, int], float] = defaultdict(float)

    def link(a, b, w):
        if a != b:
            weights[(a, b)] = max(weights[(a, b)], w)
            weights[(b, a)] = max(weights[(b, a)], w)

    for e in s.edges.values():
        if e["superseded"] or e["src"] not in s.node_ix or e["dst"] not in s.node_ix:
            continue
        link(s.node_ix[e["src"]], s.node_ix[e["dst"]], WEAK_EDGE if e["predicate"] in WEAK_PREDICATES else 1.0)
        s.adj.setdefault(e["src"], []).append((e["dst"], e["predicate"], e["rid"]))
        s.adj.setdefault(e["dst"], []).append((e["src"], e["inverse"] or "^" + e["predicate"], e["rid"]))
    for c in s.claims.values():
        if c["superseded"] or c["subject"] not in s.node_ix:
            continue
        for m in c["mentions"]:
            if m in s.node_ix:
                link(s.node_ix[c["subject"]], s.node_ix[m], WEAK_EDGE * 0.6)
    for r in db.execute("select passage_rid, node_id, via from passage_nodes"):
        if r["node_id"] in s.node_ix and r["passage_rid"] in s.passage_ix:
            link(s.passage_ix[r["passage_rid"]], s.node_ix[r["node_id"]], VIA_WEIGHT.get(r["via"], 0.3))
    if size and weights:
        rows, cols = zip(*weights)
        m = sparse.csr_matrix((list(weights.values()), (rows, cols)), shape=(size, size), dtype=np.float64)
        colsum = np.asarray(m.sum(axis=0)).ravel()
        colsum[colsum == 0] = 1.0
        s.degree = np.maximum(colsum, 1.0)
        s.matrix = (m @ sparse.diags(1.0 / colsum)).tocsr()   # column-stochastic
    return s


def pagerank(matrix, seed: np.ndarray, iters: int = 30) -> np.ndarray:
    total = seed.sum()
    if matrix is None or total <= 0:
        return seed
    p = seed / total
    x = p.copy()
    for _ in range(iters):
        nxt = DAMPING * (matrix @ x) + (1 - DAMPING) * p
        if np.abs(nxt - x).sum() < 1e-9:
            x = nxt
            break
        x = nxt
    return x


def rrf(*ranked_lists, weights=None) -> dict:
    out: dict = defaultdict(float)
    for i, lst in enumerate(ranked_lists):
        w = weights[i] if weights else 1.0
        for rank, key in enumerate(lst):
            out[key] += w / (RRF_K + rank + 1)
    return out


class Searcher:
    def __init__(self, store: Store, embedder, ppr_weight: float = PPR_WEIGHT, hub_alpha: float = HUB_ALPHA):
        self.store, self.embedder = store, embedder
        self.ppr_weight, self.hub_alpha = ppr_weight, hub_alpha
        self._snap: Snapshot | None = None
        self._lock = threading.Lock()

    @property
    def snap(self) -> Snapshot:
        with self._lock:
            if self._snap is None or self._snap.generation != self.store.generation:
                self._snap = build_snapshot(self.store)
            return self._snap

    # ---- candidate lists
    def _vec(self, kind, qvec, k):
        rows = self.store.db.execute(
            f"select rowid, distance from vec_{kind} where embedding match ? and k = ?",
            (qvec.tobytes(), k)).fetchall()
        return [(r[0], 1.0 - r[1]) for r in rows]

    def _fts(self, kind, query, k):
        if not query:
            return []
        if kind == "nodes":
            sql = ("select rowid from fts_nodes where fts_nodes match ? "
                   "order by bm25(fts_nodes, 10.0, 6.0, 1.0) limit ?")
        else:
            sql = f"select rowid from fts_{kind} where fts_{kind} match ? order by rank limit ?"
        return [r[0] for r in self.store.db.execute(sql, (query, k))]

    def alias_hits(self, snap: Snapshot, query: str) -> list[str]:
        """Entities named in the query, longest match first, each span used once."""
        if not snap.alias_re:
            return []
        out, q = [], norm(query)
        for m in snap.alias_re.finditer(q):
            nid = snap.alias[m.group(0)]
            if nid not in out:
                out.append(nid)
        return out

    # ---- filters
    def _ok_node(self, n, f: Filters):
        return not ((f.types and n["type"] not in f.types) or (f.domain and n["domain"] != f.domain))

    def _ok_fact(self, snap, row, ends, f: Filters):
        if row["superseded"] and not f.include_superseded:
            return False
        if f.markers and (row["marker"] or "fact") not in f.markers:
            return False
        when = row["date"] or row["since"]
        if f.after and when and when < f.after[: len(when)]:
            return False
        nodes = [snap.nodes.get(e) for e in ends]
        if any(n is None for n in nodes):
            return False
        if f.domain and any(n["domain"] != f.domain for n in nodes):
            return False
        if f.types and not any(n["type"] in f.types for n in nodes):
            return False
        return True

    def _ok_passage(self, snap, p, f: Filters):
        d = snap.docs.get(p["doc_id"])
        if d is None:
            return False
        if d["kind"] == "raw" and not f.include_raw:
            return False
        if f.domain and d["domain"] != f.domain:
            return False
        if f.after and d["note_date"] and d["note_date"] < f.after[: len(d["note_date"])]:
            return False
        return True

    # ---- main entry
    def search(self, query: str, mode: str = "hybrid", top_k: int = 8, max_tokens: int = 3000,
               filters: Filters | None = None) -> dict:
        f = filters or Filters()
        snap = self.snap
        qvec = self.embedder.encode_query(query)
        fq = fts_query(query)
        wide = 4 if (f.types or f.markers or f.after or f.domain) else 1

        # passages
        pvec = [(r, s) for r, s in self._vec("passages", qvec, 60 * wide)
                if r in snap.passages and self._ok_passage(snap, snap.passages[r], f)]
        psim = dict(pvec)
        if mode == "vector":
            ranked = [(r, s) for r, s in pvec][:top_k]
            return self._respond(snap, f, max_tokens, passages=ranked, mode=mode)
        pfts = [r for r in self._fts("passages", fq, 60 * wide)
                if r in snap.passages and self._ok_passage(snap, snap.passages[r], f)]
        pfused = rrf([r for r, _ in pvec], pfts)

        # claims and edges
        def facts(kind, table, ends):
            vec = [r for r, _ in self._vec(kind, qvec, 120 * wide)]
            fts = self._fts(kind, fq, 120 * wide)
            keep = lambda r: r in table and self._ok_fact(snap, table[r], ends(table[r]), f)
            return rrf([r for r in vec if keep(r)], [r for r in fts if keep(r)])

        cfused = facts("claims", snap.claims, lambda c: [c["subject"]])
        efused = facts("edges", snap.edges, lambda e: [e["src"], e["dst"]])

        # entities: an entity named in the query is a top hit
        named = [n for n in self.alias_hits(snap, query) if n in snap.nodes]
        nvec = [snap.node_by_rid[r]["id"] for r, _ in self._vec("nodes", qvec, 30) if r in snap.node_by_rid]
        nfts = [snap.node_by_rid[r]["id"] for r in self._fts("nodes", fq, 30) if r in snap.node_by_rid]
        nfused = rrf(named, nvec, nfts, weights=[3.0, 1.0, 1.0])

        if mode == "hybrid" or snap.matrix is None:
            nodes = sorted(nfused, key=nfused.get, reverse=True)
            return self._respond(
                snap, f, max_tokens, mode=mode,
                claims=sorted(cfused.items(), key=lambda kv: -kv[1])[: top_k * 2],
                edges=sorted(efused.items(), key=lambda kv: -kv[1])[: top_k * 2],
                entities=[(n, nfused[n]) for n in nodes if self._ok_node(snap.nodes[n], f)][:top_k],
                passages=sorted(pfused.items(), key=lambda kv: -kv[1])[:top_k])

        # ---- graph mode: seed, walk, re-rank
        seed = np.zeros(snap.matrix.shape[0])
        top = max(nfused.values(), default=0.0)
        for nid, sc in nfused.items():
            seed[snap.node_ix[nid]] = max(seed[snap.node_ix[nid]], sc / top)
        for fused, table, ends in ((cfused, snap.claims, lambda c: [c["subject"]]),
                                   (efused, snap.edges, lambda e: [e["src"], e["dst"]])):
            best = sorted(fused.items(), key=lambda kv: -kv[1])[:20]
            for rid, sc in best:
                for nid in ends(table[rid]):
                    i = snap.node_ix[nid]
                    seed[i] = max(seed[i], 0.7 * sc / best[0][1])
        for nid in named:
            seed[snap.node_ix[nid]] = 1.5
        for rid, sim in pvec:
            seed[snap.passage_ix[rid]] = max(sim, 0.0) * PASSAGE_SEED
        # A hub collects walk mass from every direction. Dividing by degree**alpha ranks an
        # entity or passage by how much more than its size it drew from this query.
        pr = pagerank(snap.matrix, seed) / snap.degree ** self.hub_alpha

        # Every kind is ranked the same way: reciprocal-rank fusion of the plain retrieval
        # order and the walk's order, so a simple lookup still returns what retrieval alone
        # would, and the walk adds what is one or two hops away.
        n_nodes = len(snap.node_ix)
        ids = list(snap.node_ix)
        w = self.ppr_weight
        lift = {ids[i]: float(pr[i]) for i in range(n_nodes) if pr[i] > 0}

        walk_nodes = sorted(lift, key=lift.get, reverse=True)[:60]
        nfinal = rrf(sorted(nfused, key=nfused.get, reverse=True), walk_nodes, weights=[1.0, w])
        # an entity the query names outright stays on top, whatever the walk favours
        node_rank_ids = named + [n for n in sorted(nfinal, key=nfinal.get, reverse=True) if n not in named]

        rids = list(snap.passage_ix)
        pscores = pr[n_nodes:]
        walk_passages = [rids[i] for i in np.argsort(-pscores)[:80] if pscores[i] > 0
                         and self._ok_passage(snap, snap.passages[rids[i]], f)]
        pfinal = rrf(sorted(pfused, key=pfused.get, reverse=True), walk_passages, weights=[1.0, w])
        for rid in pfinal:  # the synthesis outranks raw text at equal score
            if snap.docs[snap.passages[rid]["doc_id"]]["kind"] == "raw":
                pfinal[rid] *= 0.9

        # a claim is as close to the query as its subject is
        walk_claims = sorted(cfused, key=lambda r: lift.get(snap.claims[r]["subject"], 0.0), reverse=True)
        cfinal = rrf(sorted(cfused, key=cfused.get, reverse=True), walk_claims, weights=[1.0, w])

        # an edge is as close as its farther end: both ends must have drawn the walk
        reach = {rid: min(lift.get(e["src"], 0.0), lift.get(e["dst"], 0.0)) for rid, e in snap.edges.items()}
        walk_edges = [rid for rid in sorted(reach, key=reach.get, reverse=True)[:60] if reach[rid] > 0
                      and self._ok_fact(snap, snap.edges[rid], [snap.edges[rid]["src"], snap.edges[rid]["dst"]], f)]
        efinal = rrf(sorted(efused, key=efused.get, reverse=True), walk_edges, weights=[1.0, w])

        entities = [(n, nfinal.get(n, 0.0)) for n in node_rank_ids if self._ok_node(snap.nodes[n], f)][:top_k]
        return self._respond(
            snap, f, max_tokens, mode=mode,
            claims=sorted(cfinal.items(), key=lambda kv: -kv[1])[: top_k * 2],
            edges=sorted(efinal.items(), key=lambda kv: -kv[1])[: top_k * 2],
            entities=entities,
            passages=sorted(pfinal.items(), key=lambda kv: -kv[1])[:top_k],
            paths=self._paths(snap, named or node_rank_ids[:2], node_rank_ids[:6]))

    # ---- paths between the entities the query named and the ones the walk ranked highest
    def _paths(self, snap, starts, targets, max_hops=3, limit=5):
        out, seen = [], set()
        for s in starts[:3]:
            prev = {s: None}
            queue = deque([(s, 0)])
            while queue:
                cur, d = queue.popleft()
                if d == max_hops:
                    continue
                for other, label, _ in snap.adj.get(cur, []):
                    if other not in prev:
                        prev[other] = (cur, label)
                        queue.append((other, d + 1))
            for t in targets:
                if t == s or t not in prev:
                    continue
                path, cur = [t], t
                while prev[cur]:
                    cur, label = prev[cur][0], prev[cur][1]
                    path += [label, cur]
                path.reverse()
                key = frozenset(path[::2])
                if len(path) >= 5 and key not in seen:   # two hops or more; single edges are in `edges`
                    seen.add(key)
                    out.append(path)
        return sorted(out, key=len)[:limit]

    # ---- shaping
    def claim_json(self, c):
        out = {"subject": c["subject"], "text": c["text"], "marker": c["marker"] or "fact"}
        for k in ("date", "since", "section"):
            if c[k]:
                out[k] = c[k]
        if c["authors"]:
            out["authors"] = c["authors"]
        out["sources"] = c["sources"]
        out["superseded"] = c["superseded"] or False
        return out

    def edge_json(self, e):
        out = {"subject": e["src"], "predicate": e["predicate"], "object": e["dst"], "marker": e["marker"] or "fact"}
        for k in ("text", "date", "since"):
            if e[k]:
                out[k] = e[k]
        if e["authors"]:
            out["authors"] = e["authors"]
        out["sources"] = e["sources"]
        out["superseded"] = e["superseded"] or False
        return out

    def entity_json(self, n, full=False):
        out = {"name": n["name"], "type": n["type"], "summary": n["summary"] or "", "path": n["path"]}
        if n["aliases"]:
            out["aliases"] = n["aliases"]
        if n["url"]:
            out["url"] = n["url"]
        if full:
            out["domain"] = n["domain"]
            out.update(n["attrs"])
        return out

    def passage_json(self, snap, rid, score=None, text=None):
        p, d = snap.passages[rid], snap.docs[snap.passages[rid]["doc_id"]]
        if text is None:
            text = self.store.db.execute("select text from passages where rid=?", (rid,)).fetchone()[0]
        out = {"text": text, "path": d["path"], "kind": d["kind"], "title": d["title"], "heading": p["heading"] or ""}
        if d["note_date"]:
            out["note_date"] = d["note_date"]
        if score is not None:
            out["score"] = round(float(score), 4)
        if d["url"]:
            out["url"] = d["url"]
        return out

    def index_info(self):
        state = unj(self.store.meta("client_state"), {})
        return {"revision": state.get("revision"), "indexed_at": self.store.meta("indexed_at"),
                "export_ok": state.get("export_ok", True), "pending_embeddings": self.store.pending()}

    def _respond(self, snap, f, max_tokens, mode, claims=(), edges=(), entities=(), passages=(), paths=()):
        out = {"claims": [], "edges": [], "entities": [], "passages": [], "paths": [], "sources": {}}
        used = 0
        # Claims and edges first: they are short and carry marker, date and source. They may
        # take up to 60% of the budget so passages are never squeezed out entirely.
        fact_budget = max_tokens if not passages else int(max_tokens * 0.6)
        ci, ei = iter(claims), iter(edges)
        while True:
            progressed = False
            for it, table, shape, key in ((ci, snap.claims, self.claim_json, "claims"),
                                          (ci, snap.claims, self.claim_json, "claims"),
                                          (ei, snap.edges, self.edge_json, "edges")):
                nxt = next(it, None)
                if nxt is None:
                    continue
                item = shape(table[nxt[0]])
                cost = approx_tokens(item.get("text", "")) + 12
                if used + cost > fact_budget:
                    continue
                used += cost
                out[key].append(item)
                progressed = True
            if not progressed:
                break
        for nid, _ in entities:
            item = self.entity_json(snap.nodes[nid])
            if len(item["summary"]) > 400:
                item["summary"] = item["summary"][:400].rsplit(" ", 1)[0] + "…"
            cost = approx_tokens(item["summary"]) + 10
            if used + cost > (max_tokens if not passages else int(max_tokens * 0.75)):
                break
            used += cost
            out["entities"].append(item)
        for rid, score in passages:
            item = self.passage_json(snap, rid, score)
            cost = approx_tokens(item["text"]) + 12
            if used + cost > max_tokens:
                room = max_tokens - used - 12
                if out["passages"] or room < 60:
                    continue
                # the best passage is cut to fit rather than dropped: a small budget still gets one
                text = item["text"]
                while text and approx_tokens(text) > room:
                    text = text[: int(len(text) * min(0.9, room / approx_tokens(text)))]
                if not text:
                    continue
                item["text"], item["truncated"] = text.rstrip() + " …", True
                cost = approx_tokens(item["text"]) + 12
            used += cost
            out["passages"].append(item)
        out["paths"] = list(paths)
        for item in out["claims"] + out["edges"]:
            for name in item["sources"]:
                src = snap.sources.get(name)
                if src and name not in out["sources"]:
                    out["sources"][name] = {k: src[k] for k in ("path", "url", "date", "origins") if src[k]}
        out["tokens"] = used
        out["mode"] = mode
        out["index"] = self.index_info()
        return out

    # ---- entity endpoints
    def resolve(self, name: str):
        snap = self.snap
        if name in snap.nodes:
            return snap.nodes[name]
        nid = snap.alias.get(norm(name))
        return snap.nodes.get(nid) if nid else None

    def entity(self, name, f: Filters):
        snap, n = self.snap, self.resolve(name)
        if not n or not self._ok_node(n, Filters(domain=f.domain)):
            return None
        out = self.entity_json(n, full=True)
        out["claims"] = [self.claim_json(c) for c in snap.claims.values()
                         if c["subject"] == n["id"] and self._ok_fact(snap, c, [c["subject"]], f)]
        out["edges"] = [self.edge_json(e) for e in snap.edges.values()
                        if n["id"] in (e["src"], e["dst"]) and self._ok_fact(snap, e, [e["src"], e["dst"]], f)]
        return out

    def neighbors(self, name, predicate, f: Filters):
        snap, n = self.snap, self.resolve(name)
        if not n or not self._ok_node(n, Filters(domain=f.domain)):
            return None
        out = []
        for e in snap.edges.values():
            if n["id"] not in (e["src"], e["dst"]) or not self._ok_fact(snap, e, [e["src"], e["dst"]], f):
                continue
            if predicate and predicate not in (e["predicate"], e["inverse"]):
                continue
            other = snap.nodes[e["dst"] if e["src"] == n["id"] else e["src"]]
            out.append({"entity": self.entity_json(other), "edge": self.edge_json(e)})
        return out

    def open_items(self, entity=None, owner=None, older_than_days=None, f: Filters | None = None):
        snap, f = self.snap, f or Filters()
        f = Filters(domain=f.domain, markers=["open"])
        subject = None
        if entity:
            n = self.resolve(entity)
            if not n:
                return []
            subject = n["id"]
        today, out = dt.date.today(), []
        for c in snap.claims.values():
            if (subject and c["subject"] != subject) or not self._ok_fact(snap, c, [c["subject"]], f):
                continue
            if owner and norm(owner) not in [norm(a) for a in c["authors"]]:
                continue
            item, since, age = self.claim_json(c), c["since"] or c["date"], None
            if since:
                try:
                    y, m, d = ([int(x) for x in since.split("-")] + [1, 1])[:3]
                    age = (today - dt.date(y, m, d)).days
                except ValueError:
                    pass
            item["age_days"] = age
            if older_than_days is not None and (age is None or age < older_than_days):
                continue
            out.append(item)
        return sorted(out, key=lambda i: -(i["age_days"] if i["age_days"] is not None else -1))

    def document(self, path, f: Filters):
        snap = self.snap
        d = snap.docs.get(path) or next((x for x in snap.docs.values() if x["path"] == path), None)
        if not d or (f.domain and d["domain"] != f.domain):
            return None
        rows = self.store.db.execute("select rid, text from passages where doc_id=? order by ord", (d["id"],)).fetchall()
        return {"path": d["path"], "kind": d["kind"], "title": d["title"], "note_date": d["note_date"],
                "url": d["url"], "passages": [{"heading": snap.passages[r["rid"]]["heading"] or "", "text": r["text"]}
                                              for r in rows if r["rid"] in snap.passages]}

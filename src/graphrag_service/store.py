"""SQLite storage: graph tables, FTS5 and sqlite-vec, plus the generic ingest operations.

Every row is addressed by a stable string id chosen by the client. The service stores the
client's `hash` for each row and returns it from the manifest, so a client can work out what
changed without the service knowing anything about where the rows came from.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import sqlite_vec

from .text import approx_tokens, fts_text, norm

SCHEMA_VERSION = "1"
KINDS = ("nodes", "edges", "claims", "sources", "documents", "passages")
EMBEDDED = ("nodes", "edges", "claims", "passages")  # kinds with a vector and an FTS table

TABLES = """
create table if not exists meta(key text primary key, value text);

create table if not exists nodes(
  rid integer primary key, id text unique not null, name text not null, type text, domain text,
  summary text, path text, url text, aliases text, attrs text,
  hash text, embed_want text, embed_have text);
create table if not exists aliases(node_rid integer not null, node_id text not null, alias_norm text not null);
create index if not exists aliases_norm on aliases(alias_norm);
create index if not exists aliases_node on aliases(node_rid);

create table if not exists edges(
  rid integer primary key, id text unique not null, src text not null, predicate text not null,
  dst text not null, inverse text, text text, marker text, date text, since text, authors text,
  sources text, superseded text, hash text, embed_want text, embed_have text);
create index if not exists edges_src on edges(src);
create index if not exists edges_dst on edges(dst);

create table if not exists claims(
  rid integer primary key, id text unique not null, subject text not null, text text not null,
  section text, marker text, date text, since text, authors text, sources text, mentions text,
  superseded text, hash text, embed_want text, embed_have text);
create index if not exists claims_subject on claims(subject);

create table if not exists sources(
  id text primary key, path text, url text, domain text, date text, summary text,
  origins text, entities text, hash text);

create table if not exists documents(
  id text primary key, path text, kind text, title text, note_date text, domain text, url text,
  hash text);

create table if not exists passages(
  rid integer primary key, id text unique not null, doc_id text not null, ord integer,
  heading text, text text not null, tokens integer, hash text, embed_want text, embed_have text);
create index if not exists passages_doc on passages(doc_id);
create table if not exists passage_nodes(passage_rid integer not null, node_id text not null, via text);
create index if not exists passage_nodes_p on passage_nodes(passage_rid);

create virtual table if not exists fts_nodes using fts5(name, aliases, summary, tokenize='unicode61 remove_diacritics 2');
create virtual table if not exists fts_edges using fts5(body, tokenize='unicode61 remove_diacritics 2');
create virtual table if not exists fts_claims using fts5(body, tokenize='unicode61 remove_diacritics 2');
create virtual table if not exists fts_passages using fts5(body, tokenize='unicode61 remove_diacritics 2');
"""

J = json.dumps


def _j(v):
    return J(v, ensure_ascii=False) if v else None


def unj(s, default=None):
    return json.loads(s) if s else ([] if default is None else default)


def row_hash(row: dict) -> str:
    return hashlib.sha1(J(row, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


class BadRow(ValueError):
    pass


def _need(row, kind, *keys):
    for k in keys:
        if row.get(k) in (None, ""):
            raise BadRow(f"{kind}: row {row.get('id')!r} is missing '{k}'")


# ---- what each kind is embedded and keyword-indexed as

def embed_text(kind: str, r: dict) -> str:
    if kind == "nodes":
        al = ", ".join(r.get("aliases") or [])
        return f"{r['name']}" + (f" ({al})" if al else "") + (f"\n{r.get('summary')}" if r.get("summary") else "")
    if kind == "claims":
        return f"{r['subject']}: {r['text']}"
    if kind == "edges":
        return f"{r['src']} {r['predicate'].replace('_', ' ')} {r['dst']}." + (f" {r['text']}" if r.get("text") else "")
    head = " > ".join(x for x in (r.get("title"), r.get("heading")) if x)
    return (head + "\n" if head else "") + r["text"]


class Store:
    def __init__(self, path: Path | str, dim: int, model_id: str):
        self.path, self.dim, self.model_id = str(path), dim, model_id
        self._local = threading.local()
        self.write_lock = threading.RLock()
        self.generation = 0
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.write_lock, self.db as db:
            db.executescript(TABLES)
            for kind in EMBEDDED:
                db.execute(f"create virtual table if not exists vec_{kind} using "
                           f"vec0(embedding float[{dim}] distance_metric=cosine)")
            have = self.meta("model")
            if have and have != f"{model_id}/{dim}":
                # A different model or size: every stored vector is meaningless. Re-embed.
                for kind in EMBEDDED:
                    db.execute(f"drop table vec_{kind}")
                    db.execute(f"create virtual table vec_{kind} using "
                               f"vec0(embedding float[{dim}] distance_metric=cosine)")
                    db.execute(f"update {kind} set embed_have = null")
            self.set_meta("model", f"{model_id}/{dim}", db)
            self.set_meta("schema", SCHEMA_VERSION, db)

    @property
    def db(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=60, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.enable_load_extension(True)
            sqlite_vec.load(con)
            con.enable_load_extension(False)
            con.execute("pragma journal_mode=wal")
            con.execute("pragma synchronous=normal")
            self._local.con = con
        return con

    # ---- meta
    def meta(self, key, default=None):
        row = self.db.execute("select value from meta where key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value, db=None):
        (db or self.db).execute("insert into meta(key,value) values(?,?) "
                                "on conflict(key) do update set value=excluded.value", (key, value))

    # ---- manifest
    def manifest(self) -> dict:
        return {kind: {r[0]: r[1] for r in self.db.execute(f"select id, hash from {kind}")} for kind in KINDS}

    def counts(self) -> dict:
        out = {kind: self.db.execute(f"select count(*) from {kind}").fetchone()[0] for kind in KINDS}
        out["pending_embeddings"] = self.pending()
        return out

    def pending(self) -> int:
        return sum(self.db.execute(f"select count(*) from {kind} where embed_have is not embed_want").fetchone()[0]
                   for kind in EMBEDDED)

    # ---- ingest
    def apply(self, upsert: dict | None = None, delete: dict | None = None, replace: bool = False,
              state: dict | None = None) -> dict:
        """One transaction: upserts, deletes and the client's state note, or nothing at all."""
        upsert, delete = upsert or {}, delete or {}
        for kind in list(upsert) + list(delete):
            if kind not in KINDS:
                raise BadRow(f"unknown kind '{kind}'; expected one of {', '.join(KINDS)}")
        stats = {kind: {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0} for kind in KINDS}
        with self.write_lock:
            db = self.db
            try:
                db.execute("begin immediate")
                titles = {r["id"]: r["title"] for r in db.execute("select id, title from documents")}
                titles.update({d["id"]: d.get("title") for d in upsert.get("documents", []) if d.get("id")})
                for kind in KINDS:
                    seen = set()
                    for row in upsert.get(kind, []):
                        _need(row, kind, "id")
                        if row["id"] in seen:
                            raise BadRow(f"{kind}: id {row['id']!r} appears twice in one batch")
                        seen.add(row["id"])
                        stats[kind][self._upsert(db, kind, row, titles)] += 1
                    gone = set(delete.get(kind, []))
                    if replace:
                        gone |= {r[0] for r in db.execute(f"select id from {kind}")} - seen
                    for rid in gone - seen:
                        stats[kind]["deleted"] += self._delete(db, kind, rid)
                # passages whose document is gone go with it
                for r in db.execute("select id from passages where doc_id not in (select id from documents)").fetchall():
                    stats["passages"]["deleted"] += self._delete(db, "passages", r[0])
                if state is not None:
                    self.set_meta("client_state", J(state, ensure_ascii=False), db)
                changed = any(s["inserted"] or s["updated"] or s["deleted"] for s in stats.values())
                now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                self.set_meta("last_ingest_at", now, db)
                if changed:
                    self.set_meta("indexed_at", now, db)
                db.execute("commit")
            except Exception:
                db.execute("rollback")
                raise
            if changed:
                self.generation += 1
        return stats

    def _upsert(self, db, kind, row, titles) -> str:
        h = row.get("hash") or row_hash(row)
        key = "id"
        old = db.execute(f"select {'rowid' if kind in ('sources', 'documents') else 'rid'} as rid, hash "
                         f"from {kind} where {key}=?", (row["id"],)).fetchone()
        if old and old["hash"] == h:
            return "unchanged"
        if kind == "nodes":
            _need(row, kind, "name")
            cols = dict(name=row["name"], type=row.get("type"), domain=row.get("domain"), summary=row.get("summary"),
                        path=row.get("path"), url=row.get("url"), aliases=_j(row.get("aliases")),
                        attrs=_j(row.get("attrs")))
        elif kind == "edges":
            _need(row, kind, "src", "predicate", "dst")
            cols = dict(src=row["src"], predicate=row["predicate"], dst=row["dst"], inverse=row.get("inverse"),
                        text=row.get("text"), marker=row.get("marker"), date=row.get("date"), since=row.get("since"),
                        authors=_j(row.get("authors")), sources=_j(row.get("sources")),
                        superseded=row.get("superseded") or None)
        elif kind == "claims":
            _need(row, kind, "subject", "text")
            cols = dict(subject=row["subject"], text=row["text"], section=row.get("section"), marker=row.get("marker"),
                        date=row.get("date"), since=row.get("since"), authors=_j(row.get("authors")),
                        sources=_j(row.get("sources")), mentions=_j(row.get("mentions")),
                        superseded=row.get("superseded") or None)
        elif kind == "sources":
            cols = dict(path=row.get("path"), url=row.get("url"), domain=row.get("domain"), date=row.get("date"),
                        summary=row.get("summary"), origins=_j(row.get("origins")), entities=_j(row.get("entities")))
        elif kind == "documents":
            cols = dict(path=row.get("path") or row["id"], kind=row.get("kind") or "doc", title=row.get("title"),
                        note_date=row.get("note_date"), domain=row.get("domain"), url=row.get("url"))
        else:
            _need(row, kind, "doc_id", "text")
            cols = dict(doc_id=row["doc_id"], ord=row.get("ord") or 0, heading=row.get("heading"), text=row["text"],
                        tokens=row.get("tokens") or approx_tokens(row["text"]))
        cols["hash"] = h
        if kind in EMBEDDED:
            text = embed_text(kind, dict(row, title=titles.get(row.get("doc_id"))) if kind == "passages" else row)
            cols["embed_want"] = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        if old:
            db.execute(f"update {kind} set {', '.join(k + '=?' for k in cols)} where id=?", (*cols.values(), row["id"]))
            rid = old["rid"]
        else:
            cur = db.execute(f"insert into {kind}(id, {', '.join(cols)}) values (?{', ?' * len(cols)})",
                             (row["id"], *cols.values()))
            rid = cur.lastrowid
        if kind in EMBEDDED:
            db.execute(f"delete from fts_{kind} where rowid=?", (rid,))
            if kind == "nodes":
                db.execute("insert into fts_nodes(rowid, name, aliases, summary) values (?,?,?,?)",
                           (rid, fts_text(row["name"]), fts_text(" , ".join(row.get("aliases") or [])),
                            fts_text(row.get("summary") or "")))
                db.execute("delete from aliases where node_rid=?", (rid,))
                names = {norm(a) for a in [row["name"], *(row.get("aliases") or [])] if a and a.strip()}
                db.executemany("insert into aliases(node_rid, node_id, alias_norm) values (?,?,?)",
                               [(rid, row["id"], a) for a in names])
            else:
                db.execute(f"insert into fts_{kind}(rowid, body) values (?,?)", (rid, fts_text(text)))
        if kind == "passages":
            db.execute("delete from passage_nodes where passage_rid=?", (rid,))
            links = {(n[0], n[1]) if isinstance(n, (list, tuple)) else (n["id"], n.get("via", "link"))
                     for n in row.get("nodes") or []}
            db.executemany("insert into passage_nodes(passage_rid, node_id, via) values (?,?,?)",
                           [(rid, nid, via) for nid, via in sorted(links)])
        return "updated" if old else "inserted"

    def _delete(self, db, kind, rid_text) -> int:
        if kind in ("sources", "documents"):
            return db.execute(f"delete from {kind} where id=?", (rid_text,)).rowcount
        row = db.execute(f"select rid from {kind} where id=?", (rid_text,)).fetchone()
        if not row:
            return 0
        rid = row[0]
        db.execute(f"delete from {kind} where rid=?", (rid,))
        db.execute(f"delete from fts_{kind} where rowid=?", (rid,))
        db.execute(f"delete from vec_{kind} where rowid=?", (rid,))
        if kind == "nodes":
            db.execute("delete from aliases where node_rid=?", (rid,))
        if kind == "passages":
            db.execute("delete from passage_nodes where passage_rid=?", (rid,))
        return 1

    # ---- embedding queue
    def next_to_embed(self, limit: int):
        """(kind, [(rid, want, text)]) for the next batch, short kinds first; None when idle."""
        db = self.db
        for kind in ("nodes", "edges", "claims", "passages"):
            if kind == "passages":
                rows = db.execute(
                    "select p.rid, p.embed_want, p.heading, p.text, d.title from passages p "
                    "left join documents d on d.id = p.doc_id where p.embed_have is not p.embed_want "
                    "order by p.tokens limit ?", (limit,)).fetchall()
                items = [(r["rid"], r["embed_want"], embed_text(kind, dict(r))) for r in rows]
            else:
                rows = db.execute(f"select * from {kind} where embed_have is not embed_want limit ?", (limit,)).fetchall()
                items = []
                for r in rows:
                    d = dict(r)
                    if kind == "nodes":
                        d["aliases"] = unj(d["aliases"])
                    items.append((d["rid"], d["embed_want"], embed_text(kind, d)))
            if items:
                return kind, items
        return None

    def save_embeddings(self, kind, items, vectors):
        with self.write_lock, self.db as db:
            for (rid, want, _), vec in zip(items, vectors):
                # The row may have changed or gone while the model was running.
                cur = db.execute(f"update {kind} set embed_have=? where rid=? and embed_want=?", (want, rid, want))
                if cur.rowcount:
                    db.execute(f"delete from vec_{kind} where rowid=?", (rid,))
                    db.execute(f"insert into vec_{kind}(rowid, embedding) values (?,?)", (rid, vec.tobytes()))

    def reset_embeddings(self):
        with self.write_lock, self.db as db:
            for kind in EMBEDDED:
                db.execute(f"delete from vec_{kind}")
                db.execute(f"update {kind} set embed_have = null")

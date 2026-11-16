"""kb graph push - send the wiki to a GraphRAG service.

The service knows nothing about Obsidian. Everything vault-shaped happens here: reading
pages, resolving wikilinks, cutting pages and distilled source notes into passages,
cleaning HTML, building obsidian:// links. What goes over the wire is generic rows with
stable ids: nodes, edges, claims, sources, documents, passages.

Standard library only, like kb.py.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import kb

DEFAULT_URL = "http://127.0.0.1:8787"
CLIENT_CONFIG = os.path.join(os.path.expanduser("~"), ".kb", "graphrag.json")
PASSAGE_TOKENS = 450          # target size
PASSAGE_HARD = 750            # a single block longer than this is split
MAX_RAW_CHARS = 300_000       # a cleaned source note longer than this is truncated and reported
SKIP_WIKI_SECTIONS = {"Sources", "Relationships"}   # one is a link list, the other is the edges

RE_CJK = re.compile("[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]")
RE_LINK = re.compile(r"!?\[\[([^\]\|#]+)(#[^\]\|]*)?(?:\|([^\]]*))?\]\]")
RE_NOTE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
RE_DATA_URI = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+")


def approx_tokens(s: str) -> int:
    cjk = len(RE_CJK.findall(s))
    return int(len(re.findall(r"\w+", RE_CJK.sub(" ", s))) * 1.3 + cjk) + 1


def plain(s: str) -> str:
    """Wikilink markup -> the words a reader sees."""
    def sub(m):
        if m.group(0).startswith("!"):
            return ""
        return (m.group(3) or m.group(1).split("/")[-1]).strip()
    return RE_LINK.sub(sub, s or "")


def row_hash(row: dict) -> str:
    return hashlib.sha1(json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def note_url(vault: str, rel: str) -> str:
    path = rel[:-3] if rel.endswith(".md") else rel
    return ("obsidian://open?vault=" + urllib.parse.quote(os.path.basename(vault.rstrip("/\\")), safe="")
            + "&file=" + urllib.parse.quote(path, safe=""))


# --------------------------------------------------------------------------- HTML -> Markdown

class _HtmlText(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "nav", "head", "iframe", "object"}
    BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "ul", "ol", "table", "tr", "pre",
             "blockquote", "figure", "figcaption", "details", "summary", "dl", "dt", "dd", "g", "text", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self.href = None

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self.title:
            self._in_title = True
            return
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "br":
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "a":
            self.href = dict(attrs).get("href")
        elif tag in self.BLOCK:
            self.out.append("\n\n" if tag not in ("tr", "g", "text") else "\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a":
            if self.href and self.href.startswith("http"):
                self.out.append(f" ({self.href})")
            self.href = None
        elif re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))


def html_to_markdown(html: str) -> str:
    """Readable text with headings, lists, table cells and link targets kept; scripts, styles,
    navigation and base64 payloads dropped. SVG is kept for its text: diagrams carry labels."""
    p = _HtmlText()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    text = RE_DATA_URI.sub("", "".join(p.out))
    text = "\n".join(ln.strip() for ln in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    title = re.sub(r"\s+", " ", p.title).strip()
    if title and not re.search(r"^# ", text, re.M):
        text = f"# {title}\n\n{text}"
    return text


# --------------------------------------------------------------------------- chunking

def _split_long(block: str) -> list[str]:
    if approx_tokens(block) <= PASSAGE_HARD:
        return [block]
    parts, cur = [], []
    units = block.split("\n") if "\n" in block else re.split(r"(?<=[.!?。！？])\s*", block)
    for u in units:
        if cur and approx_tokens("\n".join(cur + [u])) > PASSAGE_TOKENS:
            parts.append("\n".join(cur))
            cur = []
        while approx_tokens(u) > PASSAGE_HARD:      # one enormous line: cut by characters
            cut = max(200, int(len(u) * PASSAGE_TOKENS / approx_tokens(u)))
            parts.append(u[:cut])
            u = u[cut:]
        cur.append(u)
    if cur:
        parts.append("\n".join(cur))
    return [p for p in parts if p.strip()]


def chunk_markdown(text: str, skip_sections=(), title: str = "", strip=plain,
                   merge_under: int = 0) -> list[tuple[str, str]]:
    """[(heading path, text)] of about PASSAGE_TOKENS each, never crossing a heading.

    `strip` removes wikilink markup; pass an identity function to keep it and strip later.
    A heading that only repeats the document title is left out of the path. With
    `merge_under`, a section shorter than that many tokens is joined to the ones after it
    (their headings kept as lines), so a note made of many two-line sections does not
    turn into many passages too small to mean anything."""
    fm, body, _ = kb.split_frontmatter(text.replace("\r\n", "\n"))
    out: list[tuple[str, str]] = []
    path: list[tuple[int, str]] = []
    blocks: list[str] = []
    cur: list[str] = []
    in_code = False

    def end_block():
        if cur:
            block = "\n".join(cur).strip()
            if block:
                blocks.extend(_split_long(block))
            cur.clear()

    def end_section():
        end_block()
        heading = " > ".join(h for _, h in path if h != title)
        if not any(h in skip_sections for _, h in path):
            acc: list[str] = []
            for b in blocks:
                if acc and approx_tokens("\n\n".join(acc + [b])) > PASSAGE_TOKENS:
                    out.append((heading, "\n\n".join(acc)))
                    acc = []
                acc.append(b)
            if acc:
                out.append((heading, "\n\n".join(acc)))
        blocks.clear()

    for ln in body.split("\n"):
        if ln.lstrip().startswith("```"):
            in_code = not in_code
            cur.append(ln)
            continue
        if in_code:
            cur.append(ln)
            continue
        if kb.RE_BACKLINK.match(ln):
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            end_section()
            level = len(m.group(1))
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, plain(m.group(2)).strip()))
            continue
        if not ln.strip():
            end_block()
            continue
        # a new top-level list item starts a new block, so bullets are grouped whole
        if re.match(r"^[-*+] |^\d+\. ", ln) and cur:
            end_block()
        cur.append(strip(ln.rstrip()))
    end_section()
    if merge_under:
        merged: list[tuple[str, str]] = []
        for h, t in out:
            small = merged and approx_tokens(merged[-1][1]) < merge_under
            if small and approx_tokens(merged[-1][1]) + approx_tokens(t) <= PASSAGE_TOKENS:
                ph, pt = merged[-1]
                tail = h.split(" > ")[-1] if h and h != ph else ""
                merged[-1] = (ph, pt + "\n\n" + (tail + "\n" if tail else "") + t)
            else:
                merged.append((h, t))
        out = merged
    return [(h, t) for h, t in out if len(t.strip()) >= 20]


# --------------------------------------------------------------------------- rows

def build_rows(vault: str, include_raw: bool = True):
    """Returns (rows by kind, parse errors, notes for the state report)."""
    wiki = kb.Wiki(vault)
    nodes, edges, claims, sources, errors = kb.build_graph(wiki)
    if errors:
        return None, errors, {}
    entity = {n["id"] for n in nodes}
    rows = {k: [] for k in ("nodes", "edges", "claims", "sources", "documents", "passages")}

    for n in nodes:
        attrs = {k: n[k] for k in ("role", "org", "status", "team", "parent", "updated") if n.get(k)}
        rows["nodes"].append({"id": n["id"], "name": n["name"], "type": n["type"], "domain": n["domain"],
                              "aliases": n["aliases"], "summary": plain(n["summary"]), "path": n["path"],
                              "url": note_url(vault, n["path"]), "attrs": attrs})
    for e in edges:
        inv = kb.PREDICATES[e["predicate"]][0] or e["predicate"]
        rows["edges"].append({"id": e["id"], "src": e["subject"], "predicate": e["predicate"], "inverse": inv,
                              "dst": e["object"], "text": plain(e["text"]), "marker": e["marker"], "date": e["date"],
                              "since": e["since"], "authors": e["authors"], "sources": e["sources"],
                              "superseded": e["superseded"]})
    seen = set()
    for c in claims:
        if c["id"] in seen:          # the same sentence twice on one page
            continue
        seen.add(c["id"])
        rows["claims"].append({"id": c["id"], "subject": c["subject"], "text": plain(c["text"]),
                               "section": c["section"], "marker": c["marker"], "date": c["date"], "since": c["since"],
                               "authors": c["authors"], "sources": c["sources"],
                               "mentions": [m for m in c["mentions"] if m in entity], "superseded": c["superseded"]})
    for s in sources:
        rows["sources"].append({"id": s["source_page"], "path": s["path"], "url": note_url(vault, s["path"]),
                                "domain": s["domain"], "date": s["date"], "summary": plain(s["summary"]),
                                "origins": s["origins"], "entities": s["entities"]})

    def add_doc(rel, kind, title, domain, chunks, note_date=None):
        """chunks: [(heading, text, [[node id, via], ...])]"""
        if not chunks:
            return
        rows["documents"].append({"id": rel, "path": rel, "kind": kind, "title": title, "domain": domain,
                                  "note_date": note_date, "url": note_url(vault, rel)})
        for i, (heading, text, links) in enumerate(chunks):
            rows["passages"].append({"id": f"{rel}#{i}", "doc_id": rel, "ord": i, "heading": heading, "text": text,
                                     "tokens": approx_tokens(text), "nodes": links})

    # wiki pages: the synthesis
    for name, page in sorted(wiki.pages.items()):
        listed = [n for n in (wiki.resolve(t) for t in kb.link_targets(page.fm.get("entities"))) if n in entity]
        chunks = []
        # Chunk with the links still in, so each passage is tied to the entities it mentions.
        for heading, text in chunk_markdown(page.text, SKIP_WIKI_SECTIONS if page.is_entity else (), title=name,
                                            strip=lambda s: s):
            links = {n: "source" for n in listed}
            for m in RE_LINK.finditer(text):
                n = wiki.resolve(m.group(1))
                if n in entity:
                    links[n] = "wikilink"
            if page.is_entity:
                links[name] = "page"
            chunks.append((heading, plain(text), sorted([n, via] for n, via in links.items())))
        add_doc(page.rel, "wiki", name, page.fm.get("domain"), chunks)

    # distilled source notes: the original wording. The ledger is the scope rule.
    notes = {"truncated": [], "raw_notes": 0}
    if include_raw:
        by_source = {s["source_page"]: s for s in sources}
        for rel, row in sorted(kb.load_ledger(vault).items()):
            sp = wiki.resolve(row.get("source_page") or "") if row.get("status") == "distilled" else None
            full = os.path.join(vault, rel)
            if not sp or sp not in by_source or not os.path.isfile(full) or kb.RE_SYNC_COPY.search(rel):
                continue
            text = kb.read_text(full)
            if rel.lower().endswith(".html"):
                text = html_to_markdown(text)
            text = RE_DATA_URI.sub("", text)
            if len(text) > MAX_RAW_CHARS:
                text = text[:MAX_RAW_CHARS]
                notes["truncated"].append(rel)
            ents = [[n, "source"] for n in sorted(set(by_source[sp]["entities"]) & entity)]
            m = RE_NOTE_DATE.search(os.path.basename(rel))
            title = os.path.splitext(os.path.basename(rel))[0]
            add_doc(rel, "raw", title, by_source[sp]["domain"],
                    [(h, t, ents) for h, t in chunk_markdown(text, title=title, merge_under=200)],
                    note_date=m.group(1) if m else None)
            notes["raw_notes"] += 1

    for kind in rows:
        for r in rows[kind]:
            r["hash"] = row_hash(r)
    return rows, [], notes


# --------------------------------------------------------------------------- HTTP

def client_config(args) -> tuple[str, str]:
    cfg = {}
    if os.path.isfile(CLIENT_CONFIG):
        with open(CLIENT_CONFIG, encoding="utf-8") as fh:
            cfg = json.load(fh)
    url = args.url or os.environ.get("KB_GRAPHRAG_URL") or cfg.get("url") or DEFAULT_URL
    token = os.environ.get("KB_GRAPHRAG_TOKEN") or cfg.get("token") or ""
    return url.rstrip("/"), token


def call(url: str, token: str, method: str, path: str, body=None, timeout=600):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never through a proxy
    with opener.open(req, timeout=timeout) as resp:
        return json.load(resp)


def git_revision(vault: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", vault, "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True,
                             timeout=20)
        return out.stdout.strip() or None
    except Exception:
        return None


def cmd_push(args, vault) -> int:
    """Exit codes: 0 pushed (or nothing to do), 1 the wiki does not parse, 3 another writer
    holds the lock, 4 the service could not be reached or refused the push."""
    # Never export while another writer is part-way through a run.
    held, took_lock = kb.read_lock(vault), False
    if held and held.get("by") != args.by:
        print(f"kb: wiki is being written by {held['by']}; push skipped, the next push will catch up",
              file=sys.stderr)
        return 3
    if not held and not args.dry_run:
        lock = kb.lock_file(vault)
        try:
            os.remove(lock)
        except OSError:
            pass
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"by": "kb-graph-push", "at": time.time(), "pid": os.getpid()}, fh)
            took_lock = True
        except FileExistsError:
            print("kb: wiki lock was just taken by another writer; push skipped", file=sys.stderr)
            return 3
    try:
        return _push(args, vault)
    finally:
        if took_lock:
            try:
                os.remove(kb.lock_file(vault))
            except OSError:
                pass


def _push(args, vault) -> int:
    url, token = client_config(args)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rows, errors, notes = build_rows(vault, include_raw=not args.no_raw)
    try:
        if args.dry_run and errors:
            remote = {"manifest": {}, "client_state": {}}
        else:
            remote = call(url, token, "GET", "/ingest/manifest")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if not args.dry_run:
            print(f"kb: GraphRAG service at {url} is not reachable ({exc}); nothing pushed", file=sys.stderr)
            return 4
        remote = {"manifest": {}, "client_state": {}}

    if errors:
        for e in errors[:20]:
            print(e, file=sys.stderr)
        print(f"kb: {len(errors)} line(s) could not be parsed; nothing pushed, the service keeps its last good index",
              file=sys.stderr)
        if not args.dry_run:
            state = dict(remote.get("client_state") or {}, export_ok=False, export_error=errors[:5], failed_at=now)
            try:
                call(url, token, "POST", "/ingest", {"state": state})
            except (urllib.error.URLError, OSError) as exc:
                print(f"kb: could not report the failure to the service ({exc})", file=sys.stderr)
        return 1

    manifest = remote["manifest"]
    upsert, delete = {}, {}
    for kind, items in rows.items():
        have = manifest.get(kind, {})
        ids = {r["id"] for r in items}
        changed = items if args.full else [r for r in items if have.get(r["id"]) != r["hash"]]
        gone = sorted(set(have) - ids)
        if changed:
            upsert[kind] = changed
        if gone:
            delete[kind] = gone

    pending = sum(1 for i in kb.sources_status(vault) if i["state"] != "current")
    state = {"client": "kb graph push", "revision": git_revision(vault), "exported_at": now, "export_ok": True,
             "backlog_notes": pending, "raw_notes": notes["raw_notes"], "truncated": notes["truncated"],
             "by": args.by or "manual"}
    summary = "  ".join(f"{k} {len(rows[k])} (+{len(upsert.get(k, []))} -{len(delete.get(k, []))})" for k in rows)
    if args.dry_run:
        print("dry run: " + summary)
        return 0
    try:
        body = {"upsert": upsert, "delete": delete, "replace": bool(args.full), "state": state}
        result = call(url, token, "POST", "/ingest", body)
    except urllib.error.HTTPError as exc:
        print(f"kb: the service refused the push: {exc.code} {exc.read().decode('utf-8', 'replace')[:500]}",
              file=sys.stderr)
        return 4
    except (urllib.error.URLError, OSError) as exc:
        print(f"kb: push failed ({exc})", file=sys.stderr)
        return 4
    print(("full push: " if args.full else "pushed: ") + summary)
    print(f"revision {state['revision']}  pending embeddings {result['pending_embeddings']}"
          + (f"  truncated {len(notes['truncated'])}" if notes["truncated"] else ""))
    if args.wait:
        deadline = time.time() + args.wait
        left = result["pending_embeddings"]
        while left and time.time() < deadline:
            time.sleep(5)
            try:
                left = call(url, token, "GET", "/status")["counts"]["pending_embeddings"]
            except (urllib.error.URLError, OSError):
                break
        print(f"embeddings pending after wait: {left}")
    return 0

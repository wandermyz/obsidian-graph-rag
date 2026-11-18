#!/usr/bin/env python3
"""kb - deterministic tooling for the AIWiki.

Standard library only, so it runs anywhere Python does. Reading the text of a PDF source
needs one of PyMuPDF, pypdf or poppler's `pdftotext`; everything else, PDFs included, works
without them. `graph push` lives in kb_push.py.

    kb sources status|seed|mark     which source notes are new, changed, deleted
    kb sources text PATH...         a source note's text, PDFs included
    kb lint [--fix] [paths...]      mechanical checks; the grammar the graph relies on
    kb graph export --out DIR       nodes / edges / claims / sources as JSONL
    kb graph push                   send the graph and its passages to the GraphRAG service
    kb stats                        counts used by the lint and ingest skills
    kb lock acquire|release|status  one wiki writer at a time on this machine

The wiki's conventions live in AIWiki/_schema.md. This file is the executable form of
the parts of that schema a script can check. If the two disagree, fix one of them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict

# --------------------------------------------------------------------------- constants

WIKI = "AIWiki"
LEDGER = "_ledger.jsonl"
SOURCE_FOLDERS = ["Clippings", "AIGen", "Daily", "Notes", "Imported"]
SOURCE_EXTS = (".md", ".html", ".pdf")
BINARY_EXTS = (".pdf",)              # hashed on bytes, read through pdf_text()
ATTACHMENT_EXTS = (".pdf",)          # source types that also count inside attachments/ folders
MIN_SOURCE_BYTES = 200

FOLDER_TYPE = {
    "people": "person",
    "projects": "project",
    "concepts": "concept",
    "sources": "source",
    "syntheses": "synthesis",
}
ENTITY_TYPES = ("person", "project", "concept")
EVIDENCE_TYPES = ("source", "synthesis")
MARKERS = ("fact", "decision", "inferred", "claim", "open")
EPISTEMICS = ("grounded", "inferential", "mixed")
PROJECT_STATUS = ("active", "retired", "renamed")
INDEX_SECTIONS = {
    "person": "People",
    "project": "Projects",
    "concept": "Concepts",
    "source": "Sources",
    "synthesis": "Syntheses",
}

P, J, C, S = "person", "project", "concept", "source"
ANY = ENTITY_TYPES
# predicate -> (inverse name or None when symmetric, allowed subject types, allowed object types)
PREDICATES = {
    "owns": ("owned_by", (P, J), (J, C)),
    "leads": ("led_by", (P,), (J,)),
    "works_on": ("has_contributor", (P,), (J, C)),
    "member_of": ("has_member", (P,), (J,)),
    "authored": ("authored_by", (P,), (J, C, S, "synthesis")),
    "reviews": ("reviewed_by", (P,), (J, C, S, "synthesis")),
    "proposed": ("proposed_by", (P,), (J, C)),
    "reports_to": ("manages", (P,), (P,)),
    "works_with": (None, (P,), (P,)),
    "part_of": ("has_part", (J, C), (J, C)),
    "depends_on": ("dependency_of", (J, C), (J, C)),
    "built_on": ("basis_of", (J, C), (J, C)),
    "integrates_with": (None, (J,), (J,)),
    "succeeded_by": ("succeeds", (P, J, C), (P, J, C)),
    "deployed_on": ("hosts", (J,), (J,)),
    "evaluates": ("evaluated_by", (J, C), (J, C)),
    "implements": ("implemented_by", (J,), (C,)),
    "uses": ("used_by", (J, C), (J, C)),
    "is_a": ("has_kind", (J, C), (C,)),
    "enables": ("enabled_by", (J, C), (J, C)),
    "blocks": ("blocked_by", (J, C), (J, C)),
    "contrasts_with": (None, (J, C), (J, C)),
    "related_to": (None, ANY + EVIDENCE_TYPES, ANY + EVIDENCE_TYPES),
}
INVERSE = {inv: name for name, (inv, _, _) in PREDICATES.items() if inv}

DATE = r"\d{4}(?:-\d{2}(?:-\d{2})?)?"
RE_WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
RE_MARKER = re.compile(r"`\[([A-Za-z]+)\]`")
RE_CITE_TAIL = re.compile(r"\s*\(((?:\[\[[^\]]+\]\](?:\s*,\s*|\s+and\s+)?)+)\)\s*\.?\s*$")
RE_UNSOURCED_TAIL = re.compile(r"\s*\(source unknown\)\s*\.?\s*$")
RE_REL = re.compile(r"^\*\*([a-z_]+)\*\*\s+\[\[([^\]\|#]+)(?:\|[^\]]*)?\]\](?:\s+[—–-]\s*(.*))?$", re.S)
RE_BACKLINK = re.compile(r"^—\s*(?:Distilled|Extracted) to \[\[.*\]\]\s*$")
RE_SYNC_COPY = re.compile(r"\(conflict |\.(md|html|pdf) \d+\.\1$| \(\d+\)\.(?:md|html|pdf)$", re.I)


# --------------------------------------------------------------------------- small helpers

def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).casefold()


def read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read().replace("\r\n", "\n")


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def content_hash(text: str) -> str:
    """Hash of a source note that is stable across machines and across our own backlink.

    Line endings are normalised and the '— Distilled to [[...]]' line a distill appends
    is ignored, so neither autocrlf nor the wiki's own bookkeeping looks like an edit.
    """
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if not RE_BACKLINK.match(ln)]
    body = "\n".join(lines).strip()
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def source_hash(path: str) -> str:
    """Ledger hash of a source file: content_hash for text, the raw bytes for a PDF."""
    if path.lower().endswith(BINARY_EXTS):
        h = hashlib.sha1()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    return content_hash(read_text(path))


class PdfTextUnavailable(RuntimeError):
    """No PDF text extractor is installed, or the file would not open."""


def pdf_text(path: str) -> str:
    """Text of a PDF as Markdown, one '## Page N' section per page.

    Tries PyMuPDF, then pypdf, then poppler's pdftotext, so it works with whichever is
    installed. Raises PdfTextUnavailable when none is, or none can read the file. A scanned
    PDF with no text layer comes back as page headings only - check for that before
    distilling from it.
    """
    pages, errors = None, []
    try:
        import fitz  # PyMuPDF
        with fitz.open(path) as doc:
            pages = [pg.get_text() for pg in doc]
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - any parser failure falls through to the next backend
        errors.append(f"pymupdf: {exc}")
    if pages is None:
        try:
            import pypdf
            pages = [pg.extract_text() or "" for pg in pypdf.PdfReader(path).pages]
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pypdf: {exc}")
    if pages is None:
        import shutil
        import subprocess
        exe = shutil.which("pdftotext")
        if exe:
            try:
                out = subprocess.run([exe, "-layout", "-enc", "UTF-8", path, "-"], capture_output=True,
                                     timeout=120, check=True).stdout.decode("utf-8", errors="replace")
                pages = out.split("\f")
                if pages and not pages[-1].strip():
                    pages.pop()
            except (subprocess.SubprocessError, OSError) as exc:
                errors.append(f"pdftotext: {exc}")
    if pages is None:
        raise PdfTextUnavailable("; ".join(errors) or
                                 "no PDF text extractor found - install pymupdf or pypdf, or poppler's pdftotext")
    parts = []
    for i, text in enumerate(pages, 1):
        text = "\n".join(ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")).strip()
        parts.append(f"## Page {i}\n\n{text}" if text else f"## Page {i}")
    return "\n\n".join(parts) + "\n"


def source_text(path: str) -> str:
    """Readable text of a source file: the note itself, or a PDF's extracted text."""
    if path.lower().endswith(".pdf"):
        return pdf_text(path)
    return read_text(path)


def find_vault(arg: str | None) -> str:
    candidates = [arg, os.environ.get("OBSIDIAN_VAULT")]
    here = os.getcwd()
    while True:
        candidates.append(here)
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    for cand in candidates:
        if cand and os.path.isfile(os.path.join(cand, WIKI, "_schema.md")):
            return os.path.abspath(cand)
    sys.exit("kb: cannot find the vault. Pass --vault or set OBSIDIAN_VAULT.")


# --------------------------------------------------------------------------- frontmatter

def split_frontmatter(text: str):
    """Return (frontmatter_lines, body, body_start_line). Lines exclude the --- fences."""
    if not text.startswith("---\n"):
        return [], text, 1
    end = text.find("\n---", 4)
    if end < 0:
        return [], text, 1
    fm = text[4:end].split("\n")
    rest = text[end + 4:]
    if rest.startswith("\n"):
        rest = rest[1:]
    return fm, rest, len(fm) + 3


def unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_frontmatter(lines):
    """The YAML subset the wiki uses: scalars and one-level lists of scalars."""
    data, key = {}, None
    for ln in lines:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", ln)
        if m:
            key, val = m.group(1), re.sub(r"\s+#.*$", "", m.group(2)).strip()
            if val == "":
                data[key] = []
            elif val.startswith("[") and val.endswith("]"):
                data[key] = [unquote(x) for x in val[1:-1].split(",") if x.strip()]
            else:
                data[key] = unquote(val)
            continue
        m = re.match(r"^\s+-\s+(.*)$", ln)
        if m and key is not None:
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(unquote(m.group(1)))
    return data


def as_list(v):
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def link_targets(values):
    out = []
    for v in as_list(values):
        m = RE_WIKILINK.search(v)
        out.append(m.group(1).strip() if m else v.strip())
    return out


def fm_set_scalar(text: str, key: str, value: str) -> str:
    fm, body, _ = split_frontmatter(text)
    for i, ln in enumerate(fm):
        if re.match(rf"^{re.escape(key)}:", ln):
            fm[i] = f"{key}: {value}"
            break
    else:
        fm.append(f"{key}: {value}")
    return "---\n" + "\n".join(fm) + "\n---\n" + body


def fm_add_list_item(text: str, key: str, item: str) -> str:
    fm, body, _ = split_frontmatter(text)
    line = f'  - "{item}"' if "[[" in item else f"  - {item}"
    for i, ln in enumerate(fm):
        if re.match(rf"^{re.escape(key)}:", ln):
            j = i + 1
            while j < len(fm) and re.match(r"^\s+-\s", fm[j]):
                j += 1
            fm.insert(j, line)
            break
    else:
        fm += [f"{key}:", line]
    return "---\n" + "\n".join(fm) + "\n---\n" + body


# --------------------------------------------------------------------------- page model

class Bullet:
    __slots__ = ("line", "raw", "text", "cites", "marker", "date", "since", "authors",
                 "superseded", "errors", "section", "unsourced")

    def __init__(self, line, raw, section):
        self.line, self.raw, self.section = line, raw, section
        self.text, self.cites, self.marker = raw, [], None
        self.date = self.since = self.superseded = None
        self.authors, self.errors = [], []
        self.unsourced = False  # the page says outright that no source is known


def parse_meta(meta: str, b: Bullet) -> None:
    for part in [p.strip() for p in meta.split(";") if p.strip()]:
        m = re.fullmatch(rf"since ({DATE})", part)
        if m:
            b.since = m.group(1)
            continue
        m = re.fullmatch(rf"superseded ({DATE})", part)
        if m:
            b.superseded = m.group(1)
            continue
        if re.fullmatch(DATE, part):
            if b.date:
                b.errors.append(("marker-meta", f"two dates in marker parenthetical: ({meta})"))
            b.date = part
            continue
        if re.search(r"\d", RE_WIKILINK.sub("", part)):
            b.errors.append(("marker-meta",
                             f"'{part}' is not an ISO date (YYYY-MM-DD), 'since <date>', "
                             f"'superseded <date>' or an author"))
            continue
        for a in [x.strip() for x in part.split(",") if x.strip()]:
            m = RE_WIKILINK.fullmatch(a)
            b.authors.append(m.group(1).strip() if m else a)


def parse_bullet(line: int, raw: str, section: str) -> Bullet:
    """Split '<text> ([[Source]]) `[marker]` (date; author)' into its parts."""
    b = Bullet(line, raw, section)
    text = raw
    markers = list(RE_MARKER.finditer(raw))
    if markers:
        known = [m for m in markers if m.group(1) in MARKERS]
        for m in markers:
            if m.group(1) not in MARKERS and m.group(1).lower() in MARKERS:
                b.errors.append(("marker-unknown", f"marker `[{m.group(1)}]` must be lower case"))
        if len(known) > 1:
            b.errors.append(("marker-multiple", "more than one epistemic marker on one bullet; split it"))
        if known:
            m = known[-1]
            b.marker = m.group(1)
            text = raw[:m.start()].rstrip()
            tail = raw[m.end():].strip()
            if tail:
                mm = re.fullmatch(r"\(([^()]*(?:\[\[[^\]]*\]\][^()]*)*)\)", tail)
                if mm:
                    parse_meta(mm.group(1), b)
                else:
                    b.errors.append(("marker-tail",
                                     "text after the marker must be one parenthetical "
                                     "'(YYYY-MM-DD; [[Person]])'; move prose before the marker"))
    m = RE_UNSOURCED_TAIL.search(text)
    if m:
        b.unsourced = True
        text = text[:m.start()].rstrip()
    else:
        m = RE_CITE_TAIL.search(text)
        if m:
            b.cites = [x.strip() for x in RE_WIKILINK.findall(m.group(1))]
            text = text[:m.start()].rstrip()
    b.text = text
    return b


class Page:
    def __init__(self, vault: str, rel: str):
        self.rel = rel.replace("\\", "/")
        self.path = os.path.join(vault, rel)
        self.name = os.path.splitext(os.path.basename(rel))[0]
        parts = self.rel.split("/")
        self.folder = parts[1] if len(parts) > 2 else ""
        self.type = FOLDER_TYPE.get(self.folder, "meta")
        self.text = read_text(self.path)
        self.fm_lines, self.body, self.body_line = split_frontmatter(self.text)
        self.fm = parse_frontmatter(self.fm_lines)
        self.summary = ""
        self.bullets: list[Bullet] = []
        self._parse_body()

    def _parse_body(self):
        section, in_code, para, cur = "", False, [], None
        seen_h1 = False

        def flush():
            nonlocal cur
            if cur:
                raw = " ".join(s.strip() for s in cur[1]).strip()
                self.bullets.append(parse_bullet(cur[0], raw, section))
                cur = None

        for i, ln in enumerate(self.body.split("\n")):
            lineno = self.body_line + i
            if ln.lstrip().startswith("```"):
                flush()
                in_code = not in_code
                continue
            if in_code:
                continue
            if ln.startswith("# "):
                seen_h1 = True
                continue
            if ln.startswith("#"):
                flush()
                if seen_h1 and not self.summary and para:
                    self.summary = " ".join(para)
                section = ln.lstrip("#").strip()
                continue
            if ln.startswith("- "):
                flush()
                if seen_h1 and not section and not self.summary and para:
                    self.summary = " ".join(para)
                cur = (lineno, [ln[2:]])
                continue
            if cur and ln.startswith((" ", "\t")) and ln.strip():
                cur[1].append(ln)
                continue
            flush()
            if not ln.strip():
                if seen_h1 and not section and para and not self.summary:
                    self.summary = " ".join(para)
                continue
            if seen_h1 and not section and not self.summary and not ln.startswith(">"):
                para.append(ln.strip())
        flush()
        if not self.summary and para:
            self.summary = " ".join(para)

    @property
    def is_entity(self):
        return self.type in ENTITY_TYPES


class Wiki:
    def __init__(self, vault: str):
        self.vault = vault
        self.root = os.path.join(vault, WIKI)
        self.pages: dict[str, Page] = {}
        self.meta: dict[str, Page] = {}
        self.dupes: list[tuple[str, str]] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "_graph"]
            for fn in sorted(filenames):
                if not fn.endswith(".md") or RE_SYNC_COPY.search(fn):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), vault)
                page = Page(vault, rel)
                if page.type == "meta":
                    self.meta[page.name] = page
                elif page.name in self.pages:
                    self.dupes.append((self.pages[page.name].rel, page.rel))
                else:
                    self.pages[page.name] = page
        self.by_norm: dict[str, str] = {}
        self.alias_owner: dict[str, list[str]] = defaultdict(list)
        for name, page in self.pages.items():
            self.by_norm.setdefault(norm_name(name), name)
        for name, page in self.pages.items():
            for alias in as_list(page.fm.get("aliases")):
                self.alias_owner[norm_name(alias)].append(name)
        self._vault_names = None

    def resolve(self, link: str):
        """Wikilink text -> wiki page name, or None."""
        base = link.strip().replace("\\", "/").split("/")[-1]
        if base.endswith(".md"):
            base = base[:-3]
        if base in self.pages:
            return base
        n = norm_name(base)
        if n in self.by_norm:
            return self.by_norm[n]
        owners = self.alias_owner.get(n)
        if owners and len(set(owners)) == 1:
            return owners[0]
        return None

    @property
    def vault_names(self):
        """Every linkable basename in the vault, for broken-link checks."""
        if self._vault_names is None:
            names = set()
            for dirpath, dirnames, filenames in os.walk(self.vault):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    names.add(norm_name(fn))
                    names.add(norm_name(os.path.splitext(fn)[0]))
            self._vault_names = names
        return self._vault_names

    def link_exists(self, link: str) -> bool:
        if self.resolve(link):
            return True
        base = link.strip().replace("\\", "/").split("/")[-1]
        return norm_name(base) in self.vault_names or norm_name(base) in {norm_name(m) for m in self.meta}


# --------------------------------------------------------------------------- relationships

def parse_relationship(wiki: Wiki, page: Page, b: Bullet):
    """Return (subject, predicate, object, prose) in canonical direction, or None with errors."""
    m = RE_REL.match(b.text)
    if not m:
        b.errors.append(("rel-format",
                         "relationship must read '**predicate** [[Target]] — prose', one target per line"))
        return None
    pred, target, prose = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
    tname = wiki.resolve(target)
    if not tname:
        b.errors.append(("rel-target", f"relationship target [[{target}]] is not a wiki page"))
        return None
    canonical = INVERSE.get(pred, pred)
    if canonical not in PREDICATES:
        b.errors.append(("rel-predicate", f"'{pred}' is not in the predicate vocabulary (see _schema.md)"))
        return None
    subj, obj = (tname, page.name) if pred in INVERSE else (page.name, tname)
    _, s_types, o_types = PREDICATES[canonical]
    st, ot = wiki.pages[subj].type, wiki.pages[obj].type
    if st not in s_types or ot not in o_types:
        b.errors.append(("rel-types",
                         f"'{pred}' cannot link {page.type} [[{page.name}]] to "
                         f"{wiki.pages[tname].type} [[{tname}]]; pick another predicate or related_to"))
        return None
    return subj, canonical, obj, prose


# --------------------------------------------------------------------------- lint

def prose_markers(page: Page):
    """(line, marker) for epistemic markers that sit in prose paragraphs, not bullets."""
    out, in_code, in_bullet = [], False, False
    for i, ln in enumerate(page.body.split("\n")):
        if ln.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if ln.startswith("- "):
            in_bullet = True
        elif not ln.strip() or not ln.startswith((" ", "\t")):
            in_bullet = False
        if not in_bullet and not ln.startswith(">"):
            for m in RE_MARKER.finditer(ln):
                if m.group(1) in MARKERS:
                    out.append((page.body_line + i, m.group(1)))
    return out


def count_open(page: Page) -> int:
    """Every live `[open]` on the page: bullets that are not superseded, plus any in prose."""
    return (sum(1 for b in page.bullets if b.marker == "open" and not b.superseded)
            + sum(1 for _, m in prose_markers(page) if m == "open"))


class Finding:
    def __init__(self, level, code, rel, line, msg, fix=None):
        self.level, self.code, self.rel, self.line, self.msg, self.fix = level, code, rel, line, msg, fix

    def as_dict(self):
        return {"level": self.level, "code": self.code, "path": self.rel, "line": self.line, "msg": self.msg,
                "fixable": bool(self.fix)}


def page_source_names(wiki: Wiki, page: Page):
    return [n for n in (wiki.resolve(t) for t in link_targets(page.fm.get("sources"))) if n]


def lint_page(wiki: Wiki, page: Page, out: list):
    def add(level, code, msg, line=0, fix=None):
        out.append(Finding(level, code, page.rel, line, msg, fix))

    fm = page.fm
    # -- frontmatter
    for key in ("title", "type", "domain", "date", "updated", "tags"):
        if not fm.get(key):
            add("error", "fm-missing", f"frontmatter is missing '{key}'")
    if fm.get("title") and fm["title"] != page.name:
        add("error", "fm-title", f"title '{fm['title']}' does not match the filename")
    if fm.get("type") and fm["type"] != page.type:
        add("error", "fm-type", f"type '{fm['type']}' does not match folder '{page.folder}/' ({page.type})")
    if fm.get("domain") and fm["domain"] not in ("public", "enterprise"):
        add("error", "fm-domain", f"domain '{fm['domain']}' must be public or enterprise")
    for key in ("date", "updated"):
        if fm.get(key) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(fm[key])):
            add("error", "fm-date", f"{key} '{fm[key]}' must be YYYY-MM-DD")
    tags = as_list(fm.get("tags"))
    want = ["aiwiki", page.type] + (["public"] if fm.get("domain") == "public" else [])
    for tag in want:
        if fm.get("tags") and tag not in tags:
            add("error", "fm-tags", f"tags must include '{tag}'",
                fix=lambda t, tag=tag: fm_add_list_item(t, "tags", tag))
    if "public" in tags and fm.get("domain") != "public":
        add("error", "fm-tags", "tag 'public' on a page whose domain is not public")
    if fm.get("epistemics") and fm["epistemics"] not in EPISTEMICS:
        add("error", "fm-epistemics", f"epistemics '{fm['epistemics']}' must be one of {', '.join(EPISTEMICS)}")
    opens = count_open(page)
    if "open_questions" in fm and str(fm["open_questions"]) != str(opens):
        add("error", "fm-open-count", f"open_questions is {fm['open_questions']} but the page has {opens} `[open]` bullets",
            fix=lambda t, n=opens: fm_set_scalar(t, "open_questions", str(n)))
    if page.type == "project" and fm.get("status") and fm["status"] not in PROJECT_STATUS:
        add("error", "fm-status", f"status '{fm['status']}' must be one of {', '.join(PROJECT_STATUS)}")
    for key in ("team", "parent"):
        for t in link_targets(fm.get(key)):
            if not wiki.resolve(t):
                add("error", "fm-link", f"{key}: [[{t}]] is not a wiki page")

    # -- frontmatter links
    for key in ("related", "sources", "entities"):
        for t in link_targets(fm.get(key)):
            name = wiki.resolve(t)
            if not name:
                add("error", "fm-link", f"{key}: [[{t}]] is not a wiki page")
            elif key == "sources" and wiki.pages[name].type not in EVIDENCE_TYPES:
                add("error", "fm-sources-type", f"sources: [[{t}]] is a {wiki.pages[name].type} page, not a source")
            elif key == "entities" and not wiki.pages[name].is_entity:
                add("error", "fm-entities-type", f"entities: [[{t}]] is not an entity page")
    if page.type == "source":
        if not fm.get("origin"):
            add("error", "fm-origin", "source page has no origin")
        for t in link_targets(fm.get("origin")):
            if not fm.get("origin_removed") and not resolve_source_path(wiki.vault, t):
                add("error", "fm-origin", f"origin [[{t}]] does not resolve to a note in a source folder "
                                          f"(if the note was deleted, set origin_removed: <date>)")
        for t in link_targets(fm.get("entities")):
            name = wiki.resolve(t)
            if name and wiki.pages[name].is_entity and page.name not in page_source_names(wiki, wiki.pages[name]):
                out.append(Finding("error", "link-reciprocal", wiki.pages[name].rel, 0,
                                   f"[[{page.name}]] lists this page in entities, but it is missing from sources",
                                   fix=lambda t, s=page.name: fm_add_list_item(t, "sources", f"[[{s}]]")))
    if page.is_entity:
        for s in page_source_names(wiki, page):
            sp = wiki.pages[s]
            if sp.type == "source" and page.name not in [wiki.resolve(t) for t in link_targets(sp.fm.get("entities"))]:
                out.append(Finding("error", "link-reciprocal", sp.rel, 0,
                                   f"[[{page.name}]] cites this source, but it is missing from entities",
                                   fix=lambda t, e=page.name: fm_add_list_item(t, "entities", f"[[{e}]]")))
        if not page_source_names(wiki, page):
            add("error", "fm-sources-empty", "entity page has no sources")

    # -- body links
    in_code = False
    for i, ln in enumerate(page.body.split("\n")):
        if ln.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for t in RE_WIKILINK.findall(re.sub(r"`[^`]*`", "", ln)):
            if not wiki.link_exists(t):
                add("error", "link-broken", f"[[{t.strip()}]] does not resolve to any note", page.body_line + i)

    if page.is_entity:
        for line, marker in prose_markers(page):
            add("warn", "marker-in-prose", f"`[{marker}]` sits in a paragraph; only bullets reach the graph", line)

    # -- bullets: markers, citations, relationships
    sources = page_source_names(wiki, page)
    for b in page.bullets:
        for m in RE_MARKER.finditer(b.raw):
            if m.group(1).lower() not in MARKERS and m.group(1) in ("Fact", "Decision", "Inferred", "Claim", "Open"):
                b.errors.append(("marker-unknown", f"unknown marker `[{m.group(1)}]`"))
        if page.is_entity and b.section == "Relationships":
            parse_relationship(wiki, page, b)
        if page.is_entity and b.section != "Sources":
            for c in b.cites:
                name = wiki.resolve(c)
                if not name or wiki.pages[name].type not in EVIDENCE_TYPES:
                    b.errors.append(("cite-type", f"citation [[{c}]] must be a source or synthesis page; "
                                                  f"put authors in the marker parenthetical"))
            if not b.cites and not b.unsourced and len(sources) != 1:
                level = "error" if b.marker else "warn"
                out.append(Finding(level, "cite-missing", page.rel, b.line,
                                   "bullet needs a '([[Source Page]])' citation before the marker "
                                   "(the page has more than one source); write '(source unknown)' "
                                   "only after looking and finding none"))
        if page.is_entity:
            for code, msg in b.errors:
                add("error", code, msg, b.line)


def lint(wiki: Wiki, only: set | None = None):
    out: list[Finding] = []
    for a, b in wiki.dupes:
        out.append(Finding("error", "name-duplicate", b, 0, f"same page name as {a}"))
    for name, page in sorted(wiki.pages.items()):
        if only and page.rel not in only:
            continue
        lint_page(wiki, page, out)
    if only:
        out = [f for f in out if f.rel in only]
        return out

    # -- names and aliases
    for alias, owners in sorted(wiki.alias_owner.items()):
        owners = sorted(set(owners))
        if len(owners) > 1:
            out.append(Finding("error", "alias-shared", wiki.pages[owners[0]].rel, 0,
                               f"alias '{alias}' is claimed by {', '.join(owners)}"))
        if alias in wiki.by_norm and wiki.by_norm[alias] not in owners:
            out.append(Finding("error", "alias-shadows-page", wiki.pages[owners[0]].rel, 0,
                               f"alias '{alias}' is also the name of page [[{wiki.by_norm[alias]}]]"))

    # -- index
    index = wiki.meta.get("_index")
    if index:
        listed = set()
        for i, ln in enumerate(index.body.split("\n")):
            m = re.match(r"^- \[\[([^\]\|#]+)", ln)
            if not m:
                continue
            name = wiki.resolve(m.group(1))
            if name:
                listed.add(name)
            else:
                out.append(Finding("error", "index-orphan", index.rel, index.body_line + i,
                                   f"index lists [[{m.group(1)}]] but there is no such page"))
        for name, page in sorted(wiki.pages.items()):
            if name not in listed:
                out.append(Finding("error", "index-missing", index.rel, 0, f"[[{name}]] is not in the index",
                                   fix=lambda t, p=page: index_add(t, p)))

    # -- ledger
    ledger = load_ledger(wiki.vault)
    for path, row in sorted(ledger.items()):
        if row.get("status") == "distilled":
            sp = row.get("source_page")
            if not sp or not wiki.resolve(sp) or wiki.pages[wiki.resolve(sp)].type != "source":
                out.append(Finding("error", "ledger-source-page", f"{WIKI}/{LEDGER}", 0,
                                   f"{path}: distilled into '{sp}', which is not a source page"))
    for name, page in sorted(wiki.pages.items()):
        if page.type != "source":
            continue
        for t in link_targets(page.fm.get("origin")):
            path = resolve_source_path(wiki.vault, t)
            if path and path not in ledger:
                out.append(Finding("error", "ledger-missing", page.rel, 0,
                                   f"origin {path} is not in the ledger (run: kb sources seed)"))
    return out


def index_add(text: str, page: Page) -> str:
    heading = "## " + INDEX_SECTIONS[page.type]
    summary = re.split(r"(?<=[.!?])\s", page.summary.strip(), maxsplit=1)[0] if page.summary else "(no summary)"
    summary = RE_WIKILINK.sub(lambda m: m.group(1).split("/")[-1], summary)
    entry = f"- [[{page.name}]] — {summary}" + (" `#public`" if page.fm.get("domain") == "public" else "")
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln.strip() == heading:
            j = i + 1
            while j < len(lines) and not lines[j].startswith("## "):
                j += 1
            while j > i + 1 and not lines[j - 1].strip():
                j -= 1
            lines.insert(j, entry)
            return "\n".join(lines)
    return text.rstrip("\n") + f"\n\n{heading}\n\n{entry}\n"


def apply_fixes(vault: str, findings):
    by_path = defaultdict(list)
    for f in findings:
        if f.fix:
            by_path[f.rel].append(f)
    for rel, fs in by_path.items():
        path = os.path.join(vault, rel)
        text = read_text(path)
        for f in fs:
            text = f.fix(text)
        write_text(path, text)
    return sum(len(v) for v in by_path.values())


# --------------------------------------------------------------------------- sources ledger

def ledger_path(vault):
    return os.path.join(vault, WIKI, LEDGER)


def load_ledger(vault) -> dict:
    rows = {}
    path = ledger_path(vault)
    if os.path.isfile(path):
        for ln in read_text(path).split("\n"):
            if ln.strip():
                row = json.loads(ln)
                rows[row["path"]] = row
    return rows


def save_ledger(vault, rows: dict) -> None:
    lines = [json.dumps(rows[k], ensure_ascii=False, sort_keys=True) for k in sorted(rows)]
    write_text(ledger_path(vault), "\n".join(lines) + ("\n" if lines else ""))


def iter_source_files(vault):
    for folder in SOURCE_FOLDERS:
        base = os.path.join(vault, folder)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            # attachments/ holds embedded media; of that, only documents are sources
            exts = ATTACHMENT_EXTS if "attachments" in os.path.relpath(dirpath, base).split(os.sep) else SOURCE_EXTS
            for fn in sorted(filenames):
                if fn.lower().endswith(exts):
                    yield os.path.relpath(os.path.join(dirpath, fn), vault).replace("\\", "/")


def ignore_reason(vault, rel):
    if RE_SYNC_COPY.search(rel):
        return "sync copy"
    if os.path.getsize(os.path.join(vault, rel)) < MIN_SOURCE_BYTES:
        return "under %d bytes" % MIN_SOURCE_BYTES
    return None


_source_index_cache = {}


def source_index(vault):
    if vault not in _source_index_cache:
        idx = defaultdict(list)
        for rel in iter_source_files(vault):
            base = os.path.basename(rel)
            idx[norm_name(base)].append(rel)
            idx[norm_name(os.path.splitext(base)[0])].append(rel)
            idx[norm_name(rel)].append(rel)
            idx[norm_name(os.path.splitext(rel)[0])].append(rel)
        _source_index_cache[vault] = idx
    return _source_index_cache[vault]


def resolve_source_path(vault, link):
    """An origin wikilink -> vault-relative path of the source note, or None."""
    hits = source_index(vault).get(norm_name(link.replace("\\", "/")))
    if not hits:
        return None
    hits = sorted(set(hits), key=lambda r: (bool(RE_SYNC_COPY.search(r)), len(r)))
    return hits[0]


def iso_week(rel):
    m = re.match(r"Daily/(\d{4})-(\d{2})-(\d{2})", rel)
    if not m:
        return None
    try:
        y, w, _ = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isocalendar()
    except ValueError:
        return None
    return f"{y}-W{w:02d}"


def sources_status(vault):
    ledger = load_ledger(vault)
    present, items = {}, []
    for rel in iter_source_files(vault):
        why = ignore_reason(vault, rel)
        if why and rel not in ledger:
            continue
        present[rel] = source_hash(os.path.join(vault, rel))
    missing = {p: r for p, r in ledger.items() if p not in present}
    by_hash = defaultdict(list)
    for p, r in missing.items():
        by_hash[r.get("hash")].append(p)
    for rel, h in sorted(present.items()):
        row = ledger.get(rel)
        item = {"path": rel, "folder": rel.split("/")[0], "hash": h}
        if iso_week(rel):
            item["week"] = iso_week(rel)
        if row is None:
            if by_hash.get(h):
                item.update(state="renamed", was=by_hash[h].pop(0))
                missing.pop(item["was"], None)
            else:
                item["state"] = "new"
        elif row.get("hash") != h:
            item.update(state="changed", status=row.get("status"), source_page=row.get("source_page"))
        else:
            item.update(state="current", status=row.get("status"))
        items.append(item)
    for p, r in sorted(missing.items()):
        items.append({"path": p, "folder": p.split("/")[0], "state": "deleted",
                      "status": r.get("status"), "source_page": r.get("source_page")})
    return items


def cmd_sources(args, vault):
    if args.action == "status":
        items = sources_status(vault)
        pending = [i for i in items if i["state"] != "current"]
        if args.folder:
            pending = [i for i in pending if i["folder"] in args.folder]
        if args.state:
            pending = [i for i in pending if i["state"] in args.state]
        pending.sort(key=lambda i: (i["folder"], i["path"]), reverse=args.newest_first)
        if args.json:
            print(json.dumps(pending[: args.limit] if args.limit else pending, ensure_ascii=False, indent=1))
            return 0
        counts = Counter((i["folder"], i["state"]) for i in items)
        folders = SOURCE_FOLDERS
        states = ["current", "new", "changed", "renamed", "deleted"]
        print(f"{'folder':<10}" + "".join(f"{s:>9}" for s in states))
        for f in folders:
            print(f"{f:<10}" + "".join(f"{counts[(f, s)]:>9}" for s in states))
        shown = pending[: args.limit] if args.limit else pending
        if shown:
            print()
        for i in shown:
            extra = f"  <- {i['was']}" if i["state"] == "renamed" else ""
            extra += f"  [{i['week']}]" if i.get("week") else ""
            extra += f"  ({i['source_page']})" if i.get("source_page") else ""
            print(f"{i['state']:<8} {i['path']}{extra}")
        if args.limit and len(pending) > args.limit:
            print(f"... {len(pending) - args.limit} more")
        return 0

    if args.action == "text":
        if not args.paths:
            sys.exit("kb: sources text needs a path")
        for p in args.paths:
            rel = p.replace("\\", "/")
            full = os.path.join(vault, rel)
            if not os.path.isfile(full):
                sys.exit(f"kb: no such note: {rel}")
            try:
                text = source_text(full)
            except PdfTextUnavailable as exc:
                sys.exit(f"kb: cannot read {rel}: {exc}")
            if len(args.paths) > 1:
                print(f"# {rel}\n")
            print(text[: args.max_chars] if args.max_chars else text)
            if args.max_chars and len(text) > args.max_chars:
                print(f"\n[... truncated at {args.max_chars} of {len(text)} characters]")
        return 0

    ledger = load_ledger(vault)
    today = dt.date.today().isoformat()

    if args.action == "seed":
        wiki = Wiki(vault)
        added = 0
        for name, page in sorted(wiki.pages.items()):
            if page.type != "source":
                continue
            for t in link_targets(page.fm.get("origin")):
                path = resolve_source_path(vault, t)
                if path and path not in ledger:
                    ledger[path] = {"path": path, "hash": source_hash(os.path.join(vault, path)),
                                    "status": "distilled", "source_page": name,
                                    "at": str(page.fm.get("updated") or page.fm.get("date") or today),
                                    "by": "seed"}
                    added += 1
        save_ledger(vault, ledger)
        print(f"seeded {added} ledger rows from source pages ({len(ledger)} total)")
        return 0

    if args.action == "mark":
        if args.moved:
            old, new = args.moved
            if old not in ledger:
                sys.exit(f"kb: {old} is not in the ledger")
            row = ledger.pop(old)
            row["path"] = new
            ledger[new] = row
            save_ledger(vault, ledger)
            print(f"moved {old} -> {new}")
            return 0
        if args.forget:
            for p in args.paths:
                ledger.pop(p.replace("\\", "/"), None)
            save_ledger(vault, ledger)
            print(f"forgot {len(args.paths)} path(s)")
            return 0
        if not (args.distilled or args.skipped):
            sys.exit("kb: mark needs --distilled, --skipped, --moved or --forget")
        if args.distilled and not args.source_page:
            sys.exit("kb: --distilled needs --source-page")
        if args.skipped and not args.reason:
            sys.exit("kb: --skipped needs --reason")
        if args.distilled:
            wiki = Wiki(vault)
            sp = wiki.resolve(args.source_page)
            if not sp or wiki.pages[sp].type != "source":
                sys.exit(f"kb: '{args.source_page}' is not a source page")
            args.source_page = sp
        for p in args.paths:
            rel = p.replace("\\", "/")
            full = os.path.join(vault, rel)
            if not os.path.isfile(full):
                sys.exit(f"kb: no such note: {rel}")
            row = {"path": rel, "hash": source_hash(full), "at": today, "by": args.by or "manual"}
            if args.distilled:
                row.update(status="distilled", source_page=args.source_page)
            else:
                row.update(status="skipped", reason=args.reason)
            ledger[rel] = row
        save_ledger(vault, ledger)
        print(f"marked {len(args.paths)} note(s) {'distilled' if args.distilled else 'skipped'}")
        return 0
    return 2


# --------------------------------------------------------------------------- graph export

def build_graph(wiki: Wiki):
    """Parse the wiki into rows. Returns (nodes, edges, claims, sources, errors)."""
    nodes, claims, sources, errors = [], [], [], []
    edges: dict[tuple, dict] = {}
    for name, page in sorted(wiki.pages.items()):
        fm = page.fm
        if page.type == "source":
            sources.append({
                "source_page": name, "path": page.rel, "domain": fm.get("domain"),
                "date": str(fm.get("date") or ""), "updated": str(fm.get("updated") or ""),
                "summary": page.summary,
                "origins": [p for p in (resolve_source_path(wiki.vault, t) for t in link_targets(fm.get("origin"))) if p],
                "entities": [n for n in (wiki.resolve(t) for t in link_targets(fm.get("entities"))) if n],
            })
            continue
        if not page.is_entity:
            continue
        node = {"id": name, "name": name, "type": page.type, "domain": fm.get("domain"),
                "aliases": as_list(fm.get("aliases")), "summary": page.summary, "path": page.rel,
                "date": str(fm.get("date") or ""), "updated": str(fm.get("updated") or ""),
                "sources": page_source_names(wiki, page),
                "related": [n for n in (wiki.resolve(t) for t in link_targets(fm.get("related"))) if n]}
        for key in ("role", "org", "status"):
            if fm.get(key):
                node[key] = fm[key]
        for key in ("team", "parent"):
            t = link_targets(fm.get(key))
            if t and wiki.resolve(t[0]):
                node[key] = wiki.resolve(t[0])
        nodes.append(node)
        fallback = node["sources"] if len(node["sources"]) == 1 else []
        for b in page.bullets:
            if b.section == "Sources":
                continue
            cites = [n for n in (wiki.resolve(c) for c in b.cites) if n] or ([] if b.unsourced else fallback)
            common = {"marker": b.marker or "fact", "date": b.date, "since": b.since, "authors": b.authors,
                      "superseded": b.superseded, "sources": cites, "page": page.rel, "line": b.line}
            if b.section == "Relationships":
                rel = parse_relationship(wiki, page, b)
                if rel:
                    subj, pred, obj, prose = rel
                    key = (subj, pred, obj)
                    if PREDICATES[pred][0] is None and (obj, pred, subj) in edges:
                        key = (obj, pred, subj)
                    row = edges.get(key)
                    if row is None:
                        edges[key] = dict(common, id=hashlib.sha1("|".join(key).encode()).hexdigest()[:16],
                                          subject=key[0], predicate=pred, object=key[2], text=prose)
                    else:  # the same relationship written on both pages: keep one, merge evidence
                        row["sources"] = sorted(set(row["sources"]) | set(cites))
                        if not row["text"]:
                            row["text"] = prose
                        if row["marker"] == "fact" and b.marker:
                            row.update(marker=b.marker, date=b.date, since=b.since, authors=b.authors)
            else:
                cid = hashlib.sha1((name + "|" + norm_name(b.text)).encode()).hexdigest()[:16]
                claims.append(dict(common, id=cid, subject=name, section=b.section, text=b.text,
                                   mentions=sorted({n for n in (wiki.resolve(t) for t in RE_WIKILINK.findall(b.text))
                                                    if n and n != name})))
            for code, msg in b.errors:
                errors.append(f"{page.rel}:{b.line}: [{code}] {msg}")
    return nodes, list(edges.values()), claims, sources, errors


def cmd_graph(args, vault):
    wiki = Wiki(vault)
    nodes, edges, claims, sources, errors = build_graph(wiki)
    unsourced = [e for e in edges if not e["sources"]] + [c for c in claims if not c["sources"]]
    if errors:
        for e in errors[:50]:
            print(e, file=sys.stderr)
        print(f"kb: {len(errors)} line(s) could not be parsed; nothing exported", file=sys.stderr)
        return 1
    if args.strict and unsourced:
        for r in unsourced[:50]:
            print(f"{r['page']}:{r['line']}: no source", file=sys.stderr)
        print(f"kb: {len(unsourced)} edge(s)/claim(s) have no source page", file=sys.stderr)
        return 1
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        for fname, rows in (("nodes", nodes), ("edges", edges), ("claims", claims), ("sources", sources)):
            with open(os.path.join(args.out, fname + ".jsonl"), "w", encoding="utf-8", newline="\n") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    related = sum(1 for e in edges if e["predicate"] == "related_to")
    print(f"nodes {len(nodes)}  edges {len(edges)} ({related} related_to)  claims {len(claims)}  "
          f"sources {len(sources)}  unsourced {len(unsourced)}" + (f"  -> {args.out}" if args.out else "  (check only)"))
    return 0


# --------------------------------------------------------------------------- stats

def cmd_stats(args, vault):
    wiki = Wiki(vault)
    nodes, edges, claims, sources, errors = build_graph(wiki)
    today = dt.date.today()
    opens = []
    for c in claims:
        if c["marker"] == "open" and not c["superseded"]:
            since = c["since"] or c["date"]
            age = None
            if since:
                try:
                    parts = [int(x) for x in since.split("-")] + [1, 1]
                    age = (today - dt.date(parts[0], parts[1], parts[2])).days
                except ValueError:
                    pass
            opens.append({"page": c["subject"], "since": since, "age_days": age, "text": c["text"][:160],
                          "owners": c["authors"]})
    thin = sorted(n["name"] for n in nodes if len(n["sources"]) <= 1)
    inbound = Counter()
    for p in wiki.pages.values():
        for t in set(RE_WIKILINK.findall(p.text)):
            n = wiki.resolve(t)
            if n and n != p.name:
                inbound[n] += 1
    mentions = Counter()
    for p in wiki.pages.values():
        for t in set(RE_WIKILINK.findall(p.body)):
            if not wiki.link_exists(t):
                mentions[t.strip()] += 1
    data = {
        "pages": dict(Counter(p.type for p in wiki.pages.values())),
        "edges": len(edges), "edges_related_to": sum(1 for e in edges if e["predicate"] == "related_to"),
        "claims": len(claims), "claims_by_marker": dict(Counter(c["marker"] for c in claims)),
        "unsourced": sum(1 for r in edges + claims if not r["sources"]),
        "parse_errors": len(errors),
        "open": sorted(opens, key=lambda o: -(o["age_days"] or -1)),
        "single_source_pages": thin,
        "orphans": sorted(n["name"] for n in nodes if inbound[n["name"]] == 0),
        "contradictions": sum(p.text.count("[!warning] Contradiction") for p in wiki.pages.values()),
        "stale_pages": sorted(n["name"] for n in nodes if n["updated"] and n["updated"] < (today - dt.timedelta(days=args.stale_days)).isoformat()),
        "unresolved_links": mentions.most_common(30),
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    print("pages:", data["pages"])
    print(f"edges: {data['edges']} ({data['edges_related_to']} related_to)   claims: {data['claims']} {data['claims_by_marker']}")
    print(f"unsourced edges/claims: {data['unsourced']}   parse errors: {data['parse_errors']}")
    print(f"open questions: {len(opens)} ({sum(1 for o in opens if (o['age_days'] or 0) > 30)} older than 30 days)")
    print(f"single-source entity pages: {len(thin)}   orphans: {len(data['orphans'])}   "
          f"contradiction callouts: {data['contradictions']}   not updated in {args.stale_days}d: {len(data['stale_pages'])}")
    return 0


# --------------------------------------------------------------------------- writer lock

LOCK_STALE_SECONDS = 2 * 60 * 60


def lock_file(vault):
    """Per-machine, outside the vault: a lock must never sync or be committed."""
    key = hashlib.sha1(os.path.normcase(os.path.abspath(vault)).encode("utf-8")).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"kb-wiki-{key}.lock")


def read_lock(vault):
    """The current holder as a dict, or None when the lock is free or stale."""
    try:
        with open(lock_file(vault), encoding="utf-8") as fh:
            held = json.load(fh)
    except (OSError, ValueError):
        return None
    if time.time() - held.get("at", 0) > LOCK_STALE_SECONDS:
        return None
    return held


def cmd_lock(args, vault):
    path, held = lock_file(vault), read_lock(vault)
    if args.action == "status":
        if held:
            print(f"held by {held['by']} for {int((time.time() - held['at']) / 60)} min")
            return 1
        print("free")
        return 0
    if args.action == "release":
        if held and args.by and held["by"] != args.by:
            print(f"kb: lock is held by {held['by']}, not {args.by}; left in place", file=sys.stderr)
            return 1
        try:
            os.remove(path)
        except OSError:
            pass
        print("released")
        return 0
    # acquire
    if not args.by:
        sys.exit("kb: lock acquire needs --by <skill>")
    if held:
        print(f"kb: wiki is being written by {held['by']} "
              f"(for {int((time.time() - held['at']) / 60)} min); try again later", file=sys.stderr)
        return 1
    try:
        os.remove(path)  # a stale or unreadable lock
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:  # another writer got in between the check and the create
        print("kb: wiki lock was just taken by another writer; try again later", file=sys.stderr)
        return 1
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"by": args.by, "at": time.time(), "pid": os.getpid()}, fh)
    print(f"acquired by {args.by}")
    return 0


# --------------------------------------------------------------------------- cli

def cmd_lint(args, vault):
    wiki = Wiki(vault)
    only = None
    if args.paths:
        only = set()
        for p in args.paths:
            rel = os.path.relpath(os.path.abspath(p), vault).replace("\\", "/") if os.path.exists(p) else p.replace("\\", "/")
            if os.path.isdir(os.path.join(vault, rel)):
                only |= {pg.rel for pg in wiki.pages.values() if pg.rel.startswith(rel.rstrip("/") + "/")}
            else:
                only.add(rel)
    findings = lint(wiki, only)
    if args.fix:
        n = apply_fixes(vault, findings)
        if n:
            print(f"fixed {n} finding(s); re-checking")
            findings = lint(Wiki(vault), only)
    if args.code:
        findings = [f for f in findings if any(f.code.startswith(c) for c in args.code)]
    if not args.warnings:
        shown = [f for f in findings if f.level == "error"]
    else:
        shown = findings
    errors = sum(1 for f in findings if f.level == "error")
    warns = len(findings) - errors
    if args.json:
        print(json.dumps([f.as_dict() for f in shown], ensure_ascii=False, indent=1))
    else:
        if args.summary:
            for (code, level), n in sorted(Counter((f.code, f.level) for f in findings).items()):
                print(f"{n:>5}  {level:<5} {code}")
        else:
            for f in sorted(shown, key=lambda f: (f.rel, f.line)):
                loc = f"{f.rel}:{f.line}" if f.line else f.rel
                print(f"{loc}: {f.level} [{f.code}] {f.msg}")
        print(f"{errors} error(s), {warns} warning(s)" + ("" if args.warnings or not warns else " (use --warnings to list)"))
    return 1 if errors else 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="kb", description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", help="vault root (default: $OBSIDIAN_VAULT, then the enclosing vault)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sources", help="source ledger")
    sp.add_argument("action", choices=["status", "seed", "mark", "text"])
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--folder", action="append", choices=SOURCE_FOLDERS)
    sp.add_argument("--state", action="append", choices=["new", "changed", "renamed", "deleted"])
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--newest-first", action="store_true")
    sp.add_argument("--distilled", action="store_true")
    sp.add_argument("--skipped", action="store_true")
    sp.add_argument("--source-page")
    sp.add_argument("--reason")
    sp.add_argument("--by")
    sp.add_argument("--moved", nargs=2, metavar=("OLD", "NEW"))
    sp.add_argument("--forget", action="store_true")
    sp.add_argument("--max-chars", type=int, default=0, help="text: stop after this many characters")

    lp = sub.add_parser("lint", help="mechanical checks")
    lp.add_argument("paths", nargs="*", help="limit to these pages or folders")
    lp.add_argument("--fix", action="store_true", help="repair what is safe to repair")
    lp.add_argument("--json", action="store_true")
    lp.add_argument("--summary", action="store_true", help="counts per check instead of every finding")
    lp.add_argument("--warnings", action="store_true", help="list warnings as well as errors")
    lp.add_argument("--code", action="append", help="only findings whose code starts with this")

    gp = sub.add_parser("graph", help="graph export")
    gp.add_argument("action", choices=["export", "check", "push"])
    gp.add_argument("--out", help="directory for nodes/edges/claims/sources .jsonl")
    gp.add_argument("--strict", action="store_true", help="also fail when an edge or claim has no source page")
    gp.add_argument("--url", help="push: service URL (default: $KB_GRAPHRAG_URL, ~/.kb/graphrag.json, localhost:2335)")
    gp.add_argument("--full", action="store_true", help="push: resend every row and drop anything else")
    gp.add_argument("--dry-run", action="store_true", help="push: show what would change, send nothing")
    gp.add_argument("--no-raw", action="store_true", help="push: wiki pages only, no distilled source notes")
    gp.add_argument("--by", help="push: the skill holding the wiki lock, when run inside its lock")
    gp.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="push: wait for embeddings to finish")

    kp = sub.add_parser("lock", help="one wiki writer at a time on this machine")
    kp.add_argument("action", choices=["acquire", "release", "status"])
    kp.add_argument("--by", help="name of the skill taking or releasing the lock")

    st = sub.add_parser("stats", help="wiki health numbers")
    st.add_argument("--json", action="store_true")
    st.add_argument("--stale-days", type=int, default=60)

    args = ap.parse_args(argv)
    vault = find_vault(args.vault)
    if args.cmd == "sources":
        return cmd_sources(args, vault)
    if args.cmd == "lint":
        return cmd_lint(args, vault)
    if args.cmd == "graph":
        if args.action == "push":
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import kb_push
            return kb_push.cmd_push(args, vault)
        if args.action == "export" and not args.out:
            sys.exit("kb: graph export needs --out DIR (or use 'graph check')")
        return cmd_graph(args, vault)
    if args.cmd == "stats":
        return cmd_stats(args, vault)
    if args.cmd == "lock":
        return cmd_lock(args, vault)
    return 2


if __name__ == "__main__":
    sys.exit(main())

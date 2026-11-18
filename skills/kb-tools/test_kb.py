"""Tests for kb.py. Run: python -m unittest discover -s skills/kb-tools -p "test_*.py"."""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb  # noqa: E402


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def tiny_pdf(*pages):
    """A valid one-font PDF with one line of text per page, built by hand (no dependencies)."""
    n = len(pages)
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(f"{4 + 2 * i} 0 R" for i in range(n)), n),
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5 + 2 * i} 0 R >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def write_bytes(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def pdf_backend():
    try:
        kb.pdf_text(os.devnull)
    except kb.PdfTextUnavailable as exc:
        return "no PDF text extractor" not in str(exc)
    except Exception:  # noqa: BLE001
        return True
    return True


def page(title, typ, body, **fm):
    lines = ["---", f"title: {title}", f"type: {typ}", "domain: enterprise", "date: 2026-08-25",
             "updated: 2026-08-25", "tags:", "  - aiwiki", f"  - {typ}"]
    for key, val in fm.items():
        if isinstance(val, list):
            lines.append(f"{key}:")
            lines += [f'  - "{v}"' for v in val]
        else:
            lines.append(f"{key}: {val}")
    return "\n".join(lines + ["---", "", f"# {title}", "", body, ""])


INDEX = """---
title: AIWiki Index
type: meta
---

# AIWiki Index

## People

- [[Ana Diaz]] — engineer.

## Projects

- [[Otter]] — backend.
- [[Pilot]] — product.

## Concepts

- [[Agent Identity]] — pattern.

## Sources

- [[Daily 2026-W35]] — a week.
- [[Design Doc]] — a doc.
"""


def make_vault():
    root = tempfile.mkdtemp(prefix="kbtest")
    write(root, "AIWiki/_schema.md", "# schema\n")
    write(root, "AIWiki/_index.md", INDEX)
    write(root, "Daily/2026-08-25.md", "standup notes " * 30)
    write(root, "AIGen/2026-08-20-Design Doc.md", "design " * 60)
    write(root, "Notes/scratch.md", "nothing durable here, " * 20)
    write(root, "AIWiki/sources/Daily 2026-W35.md", page(
        "Daily 2026-W35", "source", "The week.", origin=["[[2026-08-25.md]]"],
        entities=["[[Otter]]", "[[Ana Diaz]]", "[[Pilot]]", "[[Agent Identity]]"]))
    write(root, "AIWiki/sources/Design Doc.md", page(
        "Design Doc", "source", "The doc.", origin=["[[2026-08-20-Design Doc.md]]"], entities=["[[Otter]]"]))
    write(root, "AIWiki/projects/Otter.md", page("Otter", "project", """The backend of [[Pilot]]. Written in Rust.

## Key points

- Ships behind a per-user flag. ([[Daily 2026-W35]]) `[decision]` (2026-08-27; [[Ana Diaz]])
- Was a monolith until the split. ([[Design Doc]]) `[fact]` (2026-07; superseded 2026-09-12)
- Speaks only RPC. ([[Design Doc]])

## Still open

- Does it need the audit by 9/30? ([[Daily 2026-W35]]) `[open]` (since 2026-08-20; [[Ana Diaz]])

## Relationships

- **part_of** [[Pilot]] — the product this is the backend of. ([[Design Doc]])
- **owned_by** [[Ana Diaz]] — owner. ([[Daily 2026-W35]]) `[decision]` (2026-08-27)
- **implements** [[Agent Identity]] ([[Design Doc]])

## Sources

- [[Daily 2026-W35]] — ownership.
- [[Design Doc]] — architecture.
""", sources=["[[Daily 2026-W35]]", "[[Design Doc]]"], aliases=["Ott"], open_questions=1))
    write(root, "AIWiki/projects/Pilot.md", page("Pilot", "project", """The product.

## Relationships

- **has_part** [[Otter]] — its backend.
""", sources=["[[Daily 2026-W35]]"]))
    write(root, "AIWiki/people/Ana Diaz.md", page("Ana Diaz", "person", """An engineer.

## Relationships

- **owns** [[Otter]] — owner.
""", sources=["[[Daily 2026-W35]]"]))
    write(root, "AIWiki/concepts/Agent Identity.md", page("Agent Identity", "concept", """Acting as the agent.

## Key points

- Tokens are minted without a stored secret.
""", sources=["[[Daily 2026-W35]]"]))
    return root


def run(root, *argv):
    out, err = io.StringIO(), io.StringIO()
    kb._source_index_cache.clear()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = kb.main(["--vault", root, *argv])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            err.write(str(exc.code))
    return code, out.getvalue(), err.getvalue()


class KbTest(unittest.TestCase):
    def setUp(self):
        self.root = make_vault()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        run(self.root, "sources", "seed")

    def lint_codes(self):
        _, out, _ = run(self.root, "lint", "--json", "--warnings")
        return sorted(f["code"] for f in json.loads(out[: out.rindex("]") + 1]))

    def edit(self, rel, old, new):
        path = os.path.join(self.root, rel)
        text = read(path)
        self.assertIn(old, text)
        write(self.root, rel, text.replace(old, new, 1))

    # ---- bullet grammar
    def test_bullet_parts(self):
        b = kb.parse_bullet(1, "It ships. ([[A]], [[B]]) `[decision]` (2026-08-27; [[Ana Diaz]], Bo; superseded 2026-09)", "Key points")
        self.assertEqual((b.text, b.cites, b.marker, b.date, b.authors, b.superseded, b.errors),
                         ("It ships.", ["A", "B"], "decision", "2026-08-27", ["Ana Diaz", "Bo"], "2026-09", []))
        b = kb.parse_bullet(1, "Asked daily. `[open]` (since 2026-08-20)", "Still open")
        self.assertEqual((b.since, b.cites, b.errors), ("2026-08-20", [], []))

    def test_bullet_errors(self):
        codes = lambda raw: [c for c, _ in kb.parse_bullet(1, raw, "Key points").errors]
        self.assertEqual(codes("X. `[fact]` (27 Aug 2026, Yang)"), ["marker-meta"])
        self.assertEqual(codes("X. `[fact]` and then more prose"), ["marker-tail"])
        self.assertEqual(codes("X. `[fact]` Y. `[claim]`"), ["marker-multiple"])
        self.assertEqual(codes("Plain text (with a parenthetical)."), [])

    # ---- lint
    def test_clean_vault_passes(self):
        self.assertEqual(self.lint_codes(), [])
        self.assertEqual(run(self.root, "lint")[0], 0)

    def test_relationship_rules(self):
        self.edit("AIWiki/projects/Otter.md", "**part_of** [[Pilot]]", "**leads** [[Pilot]]")
        self.assertEqual(self.lint_codes(), ["rel-types"])
        self.edit("AIWiki/projects/Otter.md", "**leads** [[Pilot]]", "**frobnicates** [[Pilot]]")
        self.assertEqual(self.lint_codes(), ["rel-predicate"])
        self.edit("AIWiki/projects/Otter.md", "**frobnicates** [[Pilot]]", "[[Pilot]]")
        self.assertEqual(self.lint_codes(), ["rel-format"])

    def test_citation_rules(self):
        self.edit("AIWiki/projects/Otter.md", "per-user flag. ([[Daily 2026-W35]])", "per-user flag.")
        self.edit("AIWiki/projects/Otter.md", "Speaks only RPC. ([[Design Doc]])", "Speaks only RPC.")
        _, out, _ = run(self.root, "lint", "--json", "--warnings")
        got = sorted((f["code"], f["level"]) for f in json.loads(out[: out.rindex("]") + 1]))
        self.assertEqual(got, [("cite-missing", "error"), ("cite-missing", "warn")])
        self.edit("AIWiki/projects/Otter.md", "per-user flag.", "per-user flag. ([[Ana Diaz]])")
        self.assertIn("cite-type", self.lint_codes())

    def test_fix_repairs_counts_index_and_backrefs(self):
        self.edit("AIWiki/projects/Otter.md", "open_questions: 1", "open_questions: 7")
        self.edit("AIWiki/_index.md", "- [[Pilot]] — product.\n", "")
        self.edit("AIWiki/sources/Design Doc.md", '  - "[[Otter]]"\n', "")
        self.assertEqual(self.lint_codes(), ["fm-open-count", "index-missing", "link-reciprocal"])
        self.assertEqual(run(self.root, "lint", "--fix")[0], 0)
        index = read(os.path.join(self.root, "AIWiki/_index.md"))
        self.assertIn("- [[Pilot]] — The product.", index)

    def test_broken_link_and_alias_clash(self):
        self.edit("AIWiki/projects/Pilot.md", "The product.", "The product, see [[Nowhere]].")
        self.edit("AIWiki/people/Ana Diaz.md", "sources:", "aliases:\n  - Ott\nsources:")
        self.assertEqual(self.lint_codes(), ["alias-shared", "link-broken"])

    # ---- ledger
    def test_ledger_lifecycle(self):
        items = {i["path"]: i for i in kb.sources_status(self.root)}
        self.assertEqual(items["Daily/2026-08-25.md"]["state"], "current")
        self.assertEqual(items["Notes/scratch.md"]["state"], "new")

        self.assertEqual(run(self.root, "sources", "mark", "Notes/scratch.md", "--skipped", "--reason", "scratch")[0], 0)
        self.assertEqual({i["path"]: i for i in kb.sources_status(self.root)}["Notes/scratch.md"]["state"], "current")

        note = os.path.join(self.root, "Daily/2026-08-25.md")
        text = read(note)
        write(self.root, "Daily/2026-08-25.md", text.replace("\n", "\r\n") + "\n— Distilled to [[Daily 2026-W35]]\n")
        self.assertEqual({i["path"]: i for i in kb.sources_status(self.root)}["Daily/2026-08-25.md"]["state"], "current",
                         "line endings and our own backlink are not an edit")
        write(self.root, "Daily/2026-08-25.md", text + "\nA new decision.\n")
        self.assertEqual({i["path"]: i for i in kb.sources_status(self.root)}["Daily/2026-08-25.md"]["state"], "changed")

        os.rename(os.path.join(self.root, "AIGen/2026-08-20-Design Doc.md"), os.path.join(self.root, "AIGen/Design Doc v2.md"))
        kb._source_index_cache.clear()
        items = {i["path"]: i for i in kb.sources_status(self.root)}
        self.assertEqual((items["AIGen/Design Doc v2.md"]["state"], items["AIGen/Design Doc v2.md"]["was"]),
                         ("renamed", "AIGen/2026-08-20-Design Doc.md"))

        os.remove(os.path.join(self.root, "Notes/scratch.md"))
        self.assertEqual({i["path"]: i for i in kb.sources_status(self.root)}["Notes/scratch.md"]["state"], "deleted")

    def test_sync_copies_and_stubs_are_ignored(self):
        write(self.root, "Daily/2026-08-25 (conflict 2026-08-26 01.13 mobile).md", "x" * 500)
        write(self.root, "Notes/stub.md", "tiny")
        paths = {i["path"] for i in kb.sources_status(self.root)}
        self.assertFalse(any("conflict" in p or "stub" in p for p in paths))

    def test_mark_distilled_needs_a_real_source_page(self):
        code, _, err = run(self.root, "sources", "mark", "Notes/scratch.md", "--distilled", "--source-page", "Otter")
        self.assertNotEqual(code, 0)
        self.assertIn("not a source page", err)

    # ---- writer lock
    def test_lock_admits_one_writer(self):
        self.addCleanup(lambda: os.path.exists(kb.lock_file(self.root)) and os.remove(kb.lock_file(self.root)))
        self.assertEqual(run(self.root, "lock", "acquire", "--by", "kb-ingest-pending")[0], 0)
        code, _, err = run(self.root, "lock", "acquire", "--by", "kb-lint")
        self.assertEqual(code, 1)
        self.assertIn("kb-ingest-pending", err)
        self.assertEqual(run(self.root, "lock", "release", "--by", "kb-lint")[0], 1, "only the holder releases")
        self.assertEqual(run(self.root, "lock", "release", "--by", "kb-ingest-pending")[0], 0)
        self.assertEqual(run(self.root, "lock", "acquire", "--by", "kb-lint")[0], 0)

    def test_stale_lock_is_taken_over(self):
        self.addCleanup(lambda: os.path.exists(kb.lock_file(self.root)) and os.remove(kb.lock_file(self.root)))
        with open(kb.lock_file(self.root), "w", encoding="utf-8") as fh:
            json.dump({"by": "crashed-run", "at": time.time() - kb.LOCK_STALE_SECONDS - 60}, fh)
        self.assertEqual(run(self.root, "lock", "status")[0], 0)
        self.assertEqual(run(self.root, "lock", "acquire", "--by", "kb-lint")[0], 0)

    # ---- graph
    def test_graph_export(self):
        out = os.path.join(self.root, "out")
        code, summary, _ = run(self.root, "graph", "export", "--out", out, "--strict")
        self.assertEqual(code, 0, summary)
        load = lambda name: [json.loads(ln) for ln in read(os.path.join(out, name + ".jsonl")).splitlines()]
        nodes, edges, claims, sources = load("nodes"), load("edges"), load("claims"), load("sources")
        self.assertEqual(sorted(n["id"] for n in nodes), ["Agent Identity", "Ana Diaz", "Otter", "Pilot"])
        self.assertEqual(next(n for n in nodes if n["id"] == "Otter")["summary"], "The backend of [[Pilot]]. Written in Rust.")

        triples = sorted((e["subject"], e["predicate"], e["object"]) for e in edges)
        self.assertEqual(triples, [("Ana Diaz", "owns", "Otter"), ("Otter", "implements", "Agent Identity"),
                                   ("Otter", "part_of", "Pilot")],
                         "inverse predicates collapse onto one canonical edge")
        owns = next(e for e in edges if e["predicate"] == "owns")
        self.assertEqual((owns["marker"], owns["date"], owns["sources"]), ("decision", "2026-08-27", ["Daily 2026-W35"]))

        by_text = {c["text"]: c for c in claims}
        self.assertEqual(by_text["Was a monolith until the split."]["superseded"], "2026-09-12")
        self.assertEqual(by_text["Does it need the audit by 9/30?"]["marker"], "open")
        self.assertEqual(by_text["Tokens are minted without a stored secret."]["sources"], ["Daily 2026-W35"],
                         "a single-source page lends its source to uncited bullets")
        self.assertEqual(next(s for s in sources if s["source_page"] == "Design Doc")["origins"], ["AIGen/2026-08-20-Design Doc.md"])

    def test_graph_refuses_unparseable_wiki(self):
        self.edit("AIWiki/projects/Otter.md", "**part_of** [[Pilot]]", "[[Pilot]]")
        code, _, err = run(self.root, "graph", "check")
        self.assertEqual(code, 1)
        self.assertIn("rel-format", err)


    # ---- PDF sources
    def test_pdfs_are_sources_even_in_attachments(self):
        write_bytes(self.root, "Notes/attachments/Playbook.pdf", tiny_pdf("Escalate to the DRI first."))
        write_bytes(self.root, "Imported/Primer.pdf", tiny_pdf("Prompt caching primer."))
        write_bytes(self.root, "Imported/Primer (2).pdf", tiny_pdf("Prompt caching primer."))
        write_bytes(self.root, "Notes/attachments/diagram.png", b"\x89PNG" + b"x" * 400)
        items = {i["path"]: i for i in kb.sources_status(self.root)}
        self.assertEqual(items["Notes/attachments/Playbook.pdf"]["state"], "new")
        self.assertEqual(items["Imported/Primer.pdf"]["state"], "new")
        self.assertNotIn("Imported/Primer (2).pdf", items, "sync copies of PDFs are ignored")
        self.assertNotIn("Notes/attachments/diagram.png", items, "other attachments are not sources")

    def test_pdf_ledger_hashes_bytes(self):
        write_bytes(self.root, "Imported/Primer.pdf", tiny_pdf("Prompt caching primer."))
        self.assertEqual(run(self.root, "sources", "mark", "Imported/Primer.pdf", "--distilled",
                             "--source-page", "Design Doc")[0], 0)
        self.assertEqual({i["path"]: i for i in kb.sources_status(self.root)}["Imported/Primer.pdf"]["state"], "current")
        write_bytes(self.root, "Imported/Primer.pdf", tiny_pdf("Prompt caching primer, revised."))
        self.assertEqual({i["path"]: i for i in kb.sources_status(self.root)}["Imported/Primer.pdf"]["state"], "changed")

    def test_pdf_origin_resolves(self):
        write_bytes(self.root, "Notes/attachments/Playbook.pdf", tiny_pdf("Escalate to the DRI first."))
        kb._source_index_cache.clear()
        self.assertEqual(kb.resolve_source_path(self.root, "Playbook.pdf"), "Notes/attachments/Playbook.pdf")

    @unittest.skipUnless(pdf_backend(), "no PDF text extractor installed")
    def test_pdf_text(self):
        write_bytes(self.root, "Imported/Primer.pdf", tiny_pdf("Cache the system prompt.", "Hit rate was high."))
        code, out, err = run(self.root, "sources", "text", "Imported/Primer.pdf")
        self.assertEqual(code, 0, err)
        self.assertIn("## Page 1", out)
        self.assertIn("Cache the system prompt.", out)
        self.assertIn("## Page 2", out)
        self.assertIn("Hit rate was high.", out)

if __name__ == "__main__":
    unittest.main()

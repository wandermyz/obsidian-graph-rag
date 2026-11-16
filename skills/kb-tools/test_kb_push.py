"""Tests for kb graph push. Run with test_kb.py: python -m unittest discover -s <this folder> -p "test_*.py"."""
import json
import os
import shutil
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb  # noqa: E402
import kb_push  # noqa: E402
from test_kb import make_vault, read, run, write  # noqa: E402


class FakeService:
    """Just enough of the ingest API: a manifest, and a batch endpoint that applies to it."""

    def __init__(self):
        self.manifest = {k: {} for k in ("nodes", "edges", "claims", "sources", "documents", "passages")}
        self.rows = {k: {} for k in self.manifest}
        self.state, self.batches, self.auth = {}, [], []
        svc = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                svc.auth.append(self.headers.get("Authorization"))
                if self.path == "/ingest/manifest":
                    self._send({"manifest": svc.manifest, "client_state": svc.state})
                else:
                    self._send({"counts": {"pending_embeddings": 0}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                svc.batches.append(body)
                for kind, rows in body.get("upsert", {}).items():
                    if body.get("replace"):
                        svc.manifest[kind], svc.rows[kind] = {}, {}
                    for r in rows:
                        svc.manifest[kind][r["id"]] = r["hash"]
                        svc.rows[kind][r["id"]] = r
                for kind, ids in body.get("delete", {}).items():
                    for i in ids:
                        svc.manifest[kind].pop(i, None)
                        svc.rows[kind].pop(i, None)
                if body.get("state") is not None:
                    svc.state = body["state"]
                self._send({"stats": {}, "pending_embeddings": 0})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class PushTest(unittest.TestCase):
    def setUp(self):
        self.root = make_vault()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        run(self.root, "sources", "seed")
        self.svc = FakeService()
        self.addCleanup(self.svc.close)
        self.addCleanup(lambda: os.path.exists(kb.lock_file(self.root)) and os.remove(kb.lock_file(self.root)))
        os.environ["KB_GRAPHRAG_TOKEN"] = "secret"
        self.addCleanup(os.environ.pop, "KB_GRAPHRAG_TOKEN", None)

    def push(self, *extra):
        return run(self.root, "graph", "push", "--url", self.svc.url, *extra)

    def edit(self, rel, old, new):
        text = read(os.path.join(self.root, rel))
        self.assertIn(old, text)
        write(self.root, rel, text.replace(old, new, 1))

    # ---- building rows
    def test_plain_strips_wikilink_markup(self):
        self.assertEqual(kb_push.plain("See [[Pilot]], [[projects/Otter|the backend]] and ![[img.png]]."),
                         "See Pilot, the backend and .")

    def test_chunks_follow_headings_and_keep_bullets_whole(self):
        text = "# Title\n\nLead paragraph about the thing.\n\n## Alpha\n\n" + "\n".join(
            f"- bullet {i} " + "word " * 60 for i in range(12)) + "\n\n## Beta\n\nShort tail section here.\n"
        chunks = kb_push.chunk_markdown(text, title="Title")
        self.assertEqual(chunks[0], ("", "Lead paragraph about the thing."))
        alpha = [t for h, t in chunks if h == "Alpha"]
        self.assertGreater(len(alpha), 1, "a long section is split")
        self.assertTrue(all(kb_push.approx_tokens(t) <= kb_push.PASSAGE_TOKENS + 10 for t in alpha))
        self.assertTrue(all(t.startswith("- bullet") for t in alpha), "never mid-bullet")
        self.assertEqual(chunks[-1], ("Beta", "Short tail section here."))
        merged = kb_push.chunk_markdown("## A\n\nTiny section one.\n\n## B\n\nTiny section two.\n", merge_under=200)
        self.assertEqual(merged, [("A", "Tiny section one.\n\nB\nTiny section two.")])

    def test_html_is_cleaned(self):
        html = ("<html><head><title>Report</title><style>p{color:red}</style></head><body><nav>Home | Up</nav>"
                "<h2>Findings</h2><p>Latency <b>halved</b>.</p><ul><li>One</li><li>Two</li></ul>"
                "<script>alert(1)</script><img src='data:image/png;base64,AAAA'>"
                "<a href='https://example.com/x'>link</a><svg><text>diagram label</text></svg></body></html>")
        md = kb_push.html_to_markdown(html)
        self.assertTrue(md.startswith("# Report"))
        self.assertIn("## Findings", md)
        self.assertIn("Latency halved.", md)
        self.assertIn("- One", md)
        self.assertIn("link (https://example.com/x)", md)
        self.assertIn("diagram label", md)
        for junk in ("alert", "color:red", "Home | Up", "base64"):
            self.assertNotIn(junk, md)

    def test_rows(self):
        rows, errors, notes = kb_push.build_rows(self.root)
        self.assertEqual(errors, [])
        lob = next(n for n in rows["nodes"] if n["id"] == "Otter")
        self.assertEqual((lob["summary"], lob["aliases"]), ("The backend of Pilot. Written in Rust.", ["Ott"]))
        self.assertTrue(lob["url"].startswith("obsidian://open?vault=") and lob["url"].endswith("AIWiki%2Fprojects%2FOtter"))
        owns = next(e for e in rows["edges"] if e["predicate"] == "owns")
        self.assertEqual((owns["src"], owns["dst"], owns["inverse"], owns["marker"]), ("Ana Diaz", "Otter", "owned_by", "decision"))
        docs = {d["id"]: d for d in rows["documents"]}
        self.assertEqual(docs["AIWiki/projects/Otter.md"]["kind"], "wiki")
        self.assertEqual((docs["Daily/2026-08-25.md"]["kind"], docs["Daily/2026-08-25.md"]["note_date"]), ("raw", "2026-08-25"))
        self.assertNotIn("Notes/scratch.md", docs, "a note the ledger has not distilled stays out")
        lead = next(p for p in rows["passages"] if p["id"] == "AIWiki/projects/Otter.md#0")
        self.assertEqual(lead["text"], "The backend of Pilot. Written in Rust.")
        self.assertEqual(lead["nodes"], [["Otter", "page"], ["Pilot", "wikilink"]])
        heads = {p["heading"] for p in rows["passages"] if p["doc_id"] == "AIWiki/projects/Otter.md"}
        self.assertEqual(heads, {"", "Key points", "Still open"}, "Relationships and Sources are not passages")
        raw = next(p for p in rows["passages"] if p["doc_id"] == "Daily/2026-08-25.md")
        self.assertIn(["Otter", "source"], raw["nodes"], "a raw note is tied to what its source page yielded")
        self.assertEqual(notes["raw_notes"], 2)
        wiki_only, _, _ = kb_push.build_rows(self.root, include_raw=False)
        self.assertTrue(all(d["kind"] == "wiki" for d in wiki_only["documents"]))

    # ---- pushing
    def test_full_then_incremental(self):
        code, out, err = self.push("--full")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.svc.auth[0], "Bearer secret")
        self.assertTrue(self.svc.batches[0]["replace"])
        self.assertEqual(sorted(self.svc.rows["nodes"]), ["Agent Identity", "Ana Diaz", "Otter", "Pilot"])
        self.assertTrue(self.svc.state["export_ok"])

        code, out, _ = self.push()
        self.assertEqual(code, 0)
        self.assertEqual((self.svc.batches[-1]["upsert"], self.svc.batches[-1]["delete"]), ({}, {}), "nothing changed")

        # edit: one claim's text changes -> its old id goes, a new one arrives, the passage is updated
        self.edit("AIWiki/projects/Otter.md", "Speaks only RPC.", "Speaks RPC and gRPC.")
        self.push()
        b = self.svc.batches[-1]
        self.assertEqual([c["text"] for c in b["upsert"]["claims"]], ["Speaks RPC and gRPC."])
        self.assertEqual(len(b["delete"]["claims"]), 1)
        self.assertEqual([p["id"] for p in b["upsert"]["passages"]], ["AIWiki/projects/Otter.md#1"])
        self.assertNotIn("nodes", b["upsert"])

    def test_add_rename_delete_a_page(self):
        self.push("--full")
        # add
        from test_kb import page
        write(self.root, "AIWiki/concepts/Token Minting.md", page("Token Minting", "concept", """Issuing tokens.

## Key points

- Minted per request.
""", sources=["[[Daily 2026-W35]]"]))
        self.edit("AIWiki/sources/Daily 2026-W35.md", '  - "[[Otter]]"', '  - "[[Otter]]"\n  - "[[Token Minting]]"')
        self.push()
        self.assertIn("Token Minting", self.svc.rows["nodes"])
        self.assertIn("AIWiki/concepts/Token Minting.md", self.svc.rows["documents"])
        # rename
        text = read(os.path.join(self.root, "AIWiki/concepts/Token Minting.md"))
        os.remove(os.path.join(self.root, "AIWiki/concepts/Token Minting.md"))
        write(self.root, "AIWiki/concepts/Token Issuance.md",
              text.replace("Token Minting", "Token Issuance").replace("tags:", "aliases:\n  - Token Minting\ntags:"))
        self.edit("AIWiki/sources/Daily 2026-W35.md", "[[Token Minting]]", "[[Token Issuance]]")
        self.push()
        self.assertNotIn("Token Minting", self.svc.rows["nodes"])
        self.assertEqual(self.svc.rows["nodes"]["Token Issuance"]["aliases"], ["Token Minting"])
        self.assertNotIn("AIWiki/concepts/Token Minting.md", self.svc.rows["documents"])
        self.assertEqual({c["subject"] for c in self.svc.rows["claims"].values() if "Minted" in c["text"]}, {"Token Issuance"})
        # delete
        os.remove(os.path.join(self.root, "AIWiki/concepts/Token Issuance.md"))
        self.edit("AIWiki/sources/Daily 2026-W35.md", '\n  - "[[Token Issuance]]"', "")
        self.push()
        self.assertNotIn("Token Issuance", self.svc.rows["nodes"])
        self.assertFalse([p for p in self.svc.rows["passages"] if "Token" in p])
        self.assertFalse([c for c in self.svc.rows["claims"].values() if c["subject"] == "Token Issuance"])

    def test_dry_run_sends_nothing(self):
        code, out, _ = self.push("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("nodes 4 (+4 -0)", out)
        self.assertEqual(self.svc.batches, [])

    def test_unparseable_wiki_pushes_no_rows_and_reports_it(self):
        self.push("--full")
        nodes = dict(self.svc.rows["nodes"])
        self.edit("AIWiki/projects/Otter.md", "**part_of** [[Pilot]]", "[[Pilot]]")
        code, _, err = self.push()
        self.assertEqual(code, 1)
        self.assertIn("rel-format", err)
        self.assertEqual(self.svc.rows["nodes"], nodes, "the last good index is untouched")
        self.assertEqual(self.svc.batches[-1].get("upsert", {}), {})
        self.assertFalse(self.svc.state["export_ok"])
        self.assertIn("rel-format", self.svc.state["export_error"][0])

    def test_lock_rules(self):
        run(self.root, "lock", "acquire", "--by", "kb-ingest-pending")
        code, _, err = self.push()
        self.assertEqual(code, 3, "never during another writer's run")
        self.assertIn("kb-ingest-pending", err)
        self.assertEqual(self.svc.batches, [])
        self.assertEqual(self.push("--by", "kb-ingest-pending")[0], 0, "inside the writer's own lock")
        self.assertEqual(kb.read_lock(self.root)["by"], "kb-ingest-pending", "the writer's lock is left alone")
        run(self.root, "lock", "release", "--by", "kb-ingest-pending")
        self.assertEqual(self.push()[0], 0)
        self.assertIsNone(kb.read_lock(self.root), "a lock the push took itself is released")

    def test_service_down_is_reported_not_fatal_to_the_wiki(self):
        self.svc.close()
        time.sleep(0.1)
        code, _, err = self.push()
        self.assertEqual(code, 4)
        self.assertIn("not reachable", err)
        self.assertIsNone(kb.read_lock(self.root))


if __name__ == "__main__":
    unittest.main()

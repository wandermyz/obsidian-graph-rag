"""End to end: a wiki in a temp vault, `kb graph push` over real HTTP, then /search.

Covers the lifecycle the push model has to get right: add, edit, rename and delete a page,
each visible in search after the next push and not before.
"""
import dataclasses
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from graphrag_service import config
from graphrag_service.app import create_app

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "kb-tools"))
import kb  # noqa: E402
from test_kb import make_vault, page, read, run, write  # noqa: E402


@pytest.fixture
def live(tmp_path):
    settings = dataclasses.replace(config.load(), data_dir=tmp_path, model="hash", dim=64)
    app = create_app(settings)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        try:
            opener.open(url + "/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.1)

    root = make_vault()
    run(root, "sources", "seed")

    class Live:
        vault = root

        def __init__(self):
            self.url = url

        def push(self, *extra):
            code, out, err = run(root, "graph", "push", "--url", url, "--wait", "30", *extra)
            assert code == 0, err
            return out

        def search(self, query, **kw):
            req = urllib.request.Request(url + "/search", data=json.dumps({"query": query, **kw}).encode(),
                                         headers={"Content-Type": "application/json"})
            return json.load(opener.open(req, timeout=30))

        def edit(self, rel, old, new):
            text = read(os.path.join(root, rel))
            assert old in text
            write(root, rel, text.replace(old, new, 1))

    yield Live()
    server.should_exit = True
    thread.join(timeout=10)
    shutil.rmtree(root, ignore_errors=True)
    if os.path.exists(kb.lock_file(root)):
        os.remove(kb.lock_file(root))


def subjects(out):
    return {c["subject"] for c in out["claims"]}


def test_add_edit_rename_delete_reach_search(live):
    assert "nodes 4 (+4 -0)" in live.push("--full")
    out = live.search("who owns the backend?")
    assert any((e["subject"], e["predicate"], e["object"]) == ("Ana Diaz", "owns", "Otter") for e in out["edges"])
    assert out["index"]["export_ok"] and out["index"]["pending_embeddings"] == 0
    assert any(p["kind"] == "raw" and p["path"] == "Daily/2026-08-25.md" for p in live.search("standup notes")["passages"])

    # add a page
    q = "how are zebra credentials rotated"
    assert "Credential Rotation" not in subjects(live.search(q))
    write(live.vault, "AIWiki/concepts/Credential Rotation.md", page("Credential Rotation", "concept", """Replacing secrets on a schedule.

## Key points

- Zebra credentials are rotated every ninety days. `[decision]` (2026-09-01; [[Ana Diaz]])
""", sources=["[[Daily 2026-W35]]"]))
    live.edit("AIWiki/sources/Daily 2026-W35.md", '  - "[[Otter]]"', '  - "[[Otter]]"\n  - "[[Credential Rotation]]"')
    assert "Credential Rotation" not in subjects(live.search(q)), "not before the push"
    assert "claims 6 (+1 -0)" in live.push()
    out = live.search(q)
    claim = next(c for c in out["claims"] if c["subject"] == "Credential Rotation")
    assert (claim["marker"], claim["date"], claim["authors"], claim["sources"]) == \
        ("decision", "2026-09-01", ["Ana Diaz"], ["Daily 2026-W35"])
    assert any(p["path"] == "AIWiki/concepts/Credential Rotation.md" for p in out["passages"])

    # edit it
    live.edit("AIWiki/concepts/Credential Rotation.md", "every ninety days", "every thirty days")
    assert "claims 6 (+1 -1)" in live.push()
    texts = [c["text"] for c in live.search(q)["claims"] if c["subject"] == "Credential Rotation"]
    assert texts == ["Zebra credentials are rotated every thirty days."]

    # rename it
    text = read(os.path.join(live.vault, "AIWiki/concepts/Credential Rotation.md"))
    os.remove(os.path.join(live.vault, "AIWiki/concepts/Credential Rotation.md"))
    write(live.vault, "AIWiki/concepts/Secret Rotation.md",
          text.replace("Credential Rotation", "Secret Rotation").replace("tags:", "aliases:\n  - Credential Rotation\ntags:"))
    live.edit("AIWiki/sources/Daily 2026-W35.md", "[[Credential Rotation]]", "[[Secret Rotation]]")
    live.push()
    out = live.search(q)
    assert "Secret Rotation" in subjects(out) and "Credential Rotation" not in subjects(out)
    assert all(p["path"] != "AIWiki/concepts/Credential Rotation.md" for p in out["passages"])
    assert live.search("Credential Rotation")["entities"][0]["name"] == "Secret Rotation", "old name is an alias"

    # delete it
    os.remove(os.path.join(live.vault, "AIWiki/concepts/Secret Rotation.md"))
    live.edit("AIWiki/sources/Daily 2026-W35.md", '\n  - "[[Secret Rotation]]"', "")
    assert "nodes 4 (+0 -1)" in live.push()
    out = live.search(q)
    assert "Secret Rotation" not in subjects(out)
    assert "Secret Rotation" not in {e["name"] for e in out["entities"]}
    assert all("Rotation" not in p["path"] for p in out["passages"])


def test_a_wiki_that_does_not_parse_leaves_the_index_alone(live):
    live.push("--full")
    live.edit("AIWiki/projects/Otter.md", "**part_of** [[Pilot]]", "[[Pilot]]")
    code, _, err = run(live.vault, "graph", "push", "--url", live.url)
    assert code == 1 and "rel-format" in err
    out = live.search("Otter")
    assert any(e["predicate"] == "part_of" for e in out["edges"]), "the last good index is still served"
    assert out["index"]["export_ok"] is False, "and the caller can tell it is stale"
    live.edit("AIWiki/projects/Otter.md", "- [[Pilot]]", "- **part_of** [[Pilot]]")
    live.push()
    assert live.search("Otter")["index"]["export_ok"] is True

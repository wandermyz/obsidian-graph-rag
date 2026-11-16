"""Service tests. Synthetic data and the hash embedder: no model download, no private content."""
import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from graphrag_service import config
from graphrag_service.app import create_app
from graphrag_service.text import approx_tokens, fts_query, fts_text


def sample():
    nodes = [
        {"id": "Ana Diaz", "name": "Ana Diaz", "type": "person", "domain": "enterprise",
         "summary": "Engineer on the backend squad.", "aliases": ["Ana"], "path": "people/Ana Diaz.md"},
        {"id": "Bo Chen", "name": "Bo Chen", "type": "person", "domain": "enterprise",
         "summary": "Program manager.", "path": "people/Bo Chen.md"},
        {"id": "Otter", "name": "Otter", "type": "project", "domain": "enterprise",
         "summary": "The Rust backend of Beacon.", "aliases": ["Ott"], "path": "projects/Otter.md",
         "attrs": {"status": "active"}},
        {"id": "Beacon", "name": "Beacon", "type": "project", "domain": "enterprise", "summary": "The product.",
         "path": "projects/Beacon.md"},
        {"id": "Harbor", "name": "Harbor", "type": "project", "domain": "enterprise",
         "summary": "Deployment tooling for containers.", "path": "projects/Harbor.md"},
        {"id": "Agent Identity", "name": "Agent Identity", "type": "concept", "domain": "public",
         "summary": "Acting as the agent rather than as the user.", "aliases": ["代理身份"],
         "path": "concepts/Agent Identity.md"},
    ]
    edges = [
        {"id": "e1", "src": "Ana Diaz", "predicate": "owns", "inverse": "owned_by", "dst": "Otter", "text": "Owner.",
         "marker": "decision", "date": "2026-08-27", "sources": ["Week 35"]},
        {"id": "e2", "src": "Otter", "predicate": "part_of", "inverse": "has_part", "dst": "Beacon",
         "text": "the product this is the backend of", "marker": "fact", "sources": ["Design Doc"]},
        {"id": "e3", "src": "Bo Chen", "predicate": "owns", "inverse": "owned_by", "dst": "Otter",
         "marker": "decision", "date": "2026-05-01", "superseded": "2026-08-27", "sources": ["Week 35"]},
        {"id": "e4", "src": "Ana Diaz", "predicate": "leads", "inverse": "led_by", "dst": "Harbor",
         "marker": "fact", "sources": ["Week 35"]},
        {"id": "e5", "src": "Otter", "predicate": "implements", "dst": "Agent Identity", "marker": "fact",
         "sources": ["Design Doc"]},
    ]
    claims = [
        {"id": "c1", "subject": "Otter", "text": "Ships behind a per-user flag.", "marker": "decision",
         "date": "2026-08-27", "authors": ["Ana Diaz"], "sources": ["Week 35"], "section": "Key points"},
        {"id": "c2", "subject": "Otter", "text": "Was a monolith until the split.", "marker": "fact",
         "date": "2026-07", "superseded": "2026-09-12", "sources": ["Design Doc"]},
        {"id": "c3", "subject": "Otter", "text": "Does it need the certification by the end of September?",
         "marker": "open", "since": "2026-08-20", "authors": ["Ana Diaz"], "sources": ["Week 35"]},
        {"id": "c4", "subject": "Agent Identity", "text": "令牌在没有存储密钥的情况下签发。", "marker": "inferred",
         "sources": ["Design Doc"], "mentions": ["Otter"]},
        {"id": "c5", "subject": "Harbor", "text": "Rolls containers out region by region.", "marker": "claim",
         "sources": ["Week 35"]},
    ]
    sources = [
        {"id": "Week 35", "path": "sources/Week 35.md", "domain": "enterprise", "origins": ["daily/2026-08-27.md"],
         "entities": ["Otter", "Ana Diaz"]},
        {"id": "Design Doc", "path": "sources/Design Doc.md", "domain": "enterprise", "origins": ["gen/design.md"],
         "entities": ["Otter"]},
    ]
    documents = [
        {"id": "projects/Otter.md", "kind": "wiki", "title": "Otter", "domain": "enterprise",
         "url": "app://open?file=projects/Otter"},
        {"id": "projects/Harbor.md", "kind": "wiki", "title": "Harbor", "domain": "enterprise"},
        {"id": "concepts/Agent Identity.md", "kind": "wiki", "title": "Agent Identity", "domain": "public"},
        {"id": "daily/2026-08-27.md", "kind": "raw", "title": "2026-08-27", "note_date": "2026-08-27",
         "domain": "enterprise"},
    ]
    passages = [
        {"id": "projects/Otter.md#0", "doc_id": "projects/Otter.md", "ord": 0, "heading": "",
         "text": "The Rust backend of Beacon. It speaks only the gateway protocol.", "nodes": [["Otter", "page"]]},
        {"id": "projects/Harbor.md#0", "doc_id": "projects/Harbor.md", "ord": 0, "heading": "Key points",
         "text": "Harbor rolls containers out region by region and pauses on a failed canary.",
         "nodes": [["Harbor", "page"]]},
        {"id": "concepts/Agent Identity.md#0", "doc_id": "concepts/Agent Identity.md", "ord": 0, "heading": "",
         "text": "代理身份：令牌在没有存储密钥的情况下签发，并在网关处校验。", "nodes": [["Agent Identity", "page"]]},
        {"id": "daily/2026-08-27.md#0", "doc_id": "daily/2026-08-27.md", "ord": 0, "heading": "Standup",
         "text": "Ana takes over as owner of the backend from Bo. Flag rollout stays per-user.",
         "nodes": [["Otter", "source"], ["Ana Diaz", "source"]]},
    ]
    return {"nodes": nodes, "edges": edges, "claims": claims, "sources": sources, "documents": documents,
            "passages": passages}


@pytest.fixture
def svc(tmp_path: Path):
    settings = dataclasses.replace(config.load(), data_dir=tmp_path, model="hash", dim=64)
    app = create_app(settings)
    with TestClient(app) as client:
        client.app_state = app.state

        def call(method, url, **kw):
            return client.request(method, url, **kw)

        def push(**body):
            r = call("POST", "/ingest", json=body)
            app.state.worker.drain()
            return r

        def search(query, **kw):
            r = call("POST", "/search", json={"query": query, **kw})
            assert r.status_code == 200, r.text
            return r.json()

        client.call, client.push, client.search = call, push, search
        assert push(upsert=sample(), replace=True, state={"revision": "abc123", "export_ok": True}).status_code == 200
        yield client


def names(items, key="subject"):
    return [i[key] for i in items]


# ---- text
def test_chinese_is_indexed_as_bigrams():
    assert fts_text("谁负责 Beacon 的认证") == " 谁负 负责  Beacon  的认 认证 "
    assert fts_query("Who owns the 认证 flow?") == '"owns" OR "认证" OR "flow"'
    assert fts_query("the of and") is None
    assert approx_tokens("four short english words") == 6 and approx_tokens("四个汉字") == 5


# ---- ingest
def test_push_is_idempotent_and_reports_changes(svc):
    stats = svc.push(upsert=sample()).json()["stats"]
    assert all(s["inserted"] == s["updated"] == s["deleted"] == 0 for s in stats.values())
    changed = sample()
    changed["claims"][0]["text"] = "Ships behind a per-tenant flag."
    stats = svc.push(upsert={"claims": changed["claims"]}).json()["stats"]
    assert (stats["claims"]["updated"], stats["claims"]["unchanged"]) == (1, 4)


def test_manifest_lets_a_client_diff(svc):
    m = svc.call("GET", "/ingest/manifest").json()
    assert set(m["manifest"]) == {"nodes", "edges", "claims", "sources", "documents", "passages"}
    assert len(m["manifest"]["claims"]) == 5 and m["client_state"]["revision"] == "abc123"


def test_bad_batch_changes_nothing(svc):
    r = svc.push(upsert={"claims": [{"id": "c9", "subject": "Otter", "text": "Fine."}, {"id": "c10"}]})
    assert r.status_code == 422
    assert "c9" not in svc.call("GET", "/ingest/manifest").json()["manifest"]["claims"]
    assert svc.push(upsert={"widgets": []}).status_code == 422


def test_delete_and_rename_show_up_in_search(svc):
    assert "Harbor" in names(svc.search("rolling containers out by region")["claims"])
    # rename: the client deletes the old ids and inserts the new ones
    data = sample()
    harbor = next(n for n in data["nodes"] if n["id"] == "Harbor")
    harbor.update(id="Quay", name="Quay", aliases=["Harbor"])
    svc.push(upsert={"nodes": [harbor],
                     "claims": [{"id": "c5b", "subject": "Quay", "text": "Rolls containers out region by region.",
                                 "marker": "claim", "sources": ["Week 35"]}],
                     "edges": [{"id": "e4b", "src": "Ana Diaz", "predicate": "leads", "dst": "Quay"}]},
             delete={"nodes": ["Harbor"], "claims": ["c5"], "edges": ["e4"]})
    out = svc.search("rolling containers out by region")
    assert "Quay" in names(out["claims"]) and "Harbor" not in names(out["claims"])
    assert svc.call("GET", "/entities/Harbor").json()["name"] == "Quay", "the old name still resolves as an alias"
    # delete the page outright
    svc.push(delete={"nodes": ["Quay"], "claims": ["c5b"], "edges": ["e4b"], "documents": ["projects/Harbor.md"]})
    out = svc.search("rolling containers out by region")
    assert "Quay" not in names(out["claims"]) and "Quay" not in names(out["entities"], "name")
    assert all(p["path"] != "projects/Harbor.md" for p in out["passages"]), "passages go with their document"
    assert svc.call("GET", "/entities/Quay").status_code == 404


def test_replace_drops_what_is_not_pushed(svc):
    data = sample()
    data["claims"] = data["claims"][:2]
    stats = svc.push(upsert=data, replace=True).json()["stats"]
    assert stats["claims"]["deleted"] == 3


# ---- search
@pytest.mark.parametrize("mode", ["hybrid", "graph"])
def test_search_returns_marked_sourced_evidence(svc, mode):
    out = svc.search("is Otter behind a per-user flag?", mode=mode)
    top = out["claims"][0]
    assert (top["subject"], top["marker"], top["date"], top["sources"]) == ("Otter", "decision", "2026-08-27", ["Week 35"])
    assert out["sources"]["Week 35"]["origins"] == ["daily/2026-08-27.md"]
    assert out["entities"][0]["name"] == "Otter"
    assert out["passages"] and out["index"]["revision"] == "abc123" and out["tokens"] > 0
    assert any(e["predicate"] == "owns" for e in out["edges"])


def test_vector_mode_returns_passages_only(svc):
    out = svc.search("gateway protocol backend", mode="vector")
    assert out["passages"][0]["path"] == "projects/Otter.md" and not out["claims"] and not out["edges"]
    assert out["passages"][0]["url"] == "app://open?file=projects/Otter"


def test_superseded_is_hidden_unless_asked_for(svc):
    out = svc.search("who owns Otter")
    owners = [e["subject"] for e in out["edges"] if e["predicate"] == "owns"]
    assert owners == ["Ana Diaz"]
    assert "Was a monolith until the split." not in names(out["claims"], "text")
    out = svc.search("who owns Otter", include_superseded=True)
    assert {e["subject"] for e in out["edges"] if e["predicate"] == "owns"} == {"Ana Diaz", "Bo Chen"}
    assert next(e for e in out["edges"] if e["subject"] == "Bo Chen")["superseded"] == "2026-08-27"


def test_graph_mode_reaches_two_hops(svc):
    out = svc.search("which other project does the owner of Ott lead?", mode="graph")
    assert "Harbor" in names(out["entities"], "name"), "Ott -> Ana Diaz -> Harbor"
    assert ["Otter", "owned_by", "Ana Diaz", "leads", "Harbor"] in out["paths"]


def test_chinese_query_matches_chinese_text(svc):
    out = svc.search("令牌是怎么签发的？", mode="hybrid")
    assert out["claims"][0]["subject"] == "Agent Identity" and out["claims"][0]["marker"] == "inferred"
    assert svc.search("代理身份 是什么")["entities"][0]["name"] == "Agent Identity", "alias in Chinese"


def test_filters(svc):
    out = svc.search("Otter flag", filters={"markers": ["open"]})
    assert names(out["claims"], "marker") == ["open"]
    out = svc.search("Otter owner standup", filters={"include_raw": False})
    assert all(p["kind"] == "wiki" for p in out["passages"])
    out = svc.search("Otter owner standup")
    assert any(p["kind"] == "raw" for p in out["passages"])
    out = svc.search("Otter", filters={"types": ["person"]})
    assert set(names(out["entities"], "type")) == {"person"}
    out = svc.search("flag", filters={"after": "2026-09-01"})
    assert "Ships behind a per-user flag." not in names(out["claims"], "text")


def test_max_tokens_bounds_the_response(svc):
    assert svc.search("Otter", max_tokens=100)["tokens"] <= 100


# ---- read endpoints
def test_entity_neighbors_open_documents(svc):
    e = svc.call("GET", "/entities/ott").json()
    assert e["name"] == "Otter" and e["status"] == "active" and len(e["claims"]) == 2
    n = svc.call("GET", "/entities/Otter/neighbors", params={"predicate": "owned_by"}).json()["neighbors"]
    assert [x["entity"]["name"] for x in n] == ["Ana Diaz"]
    o = svc.call("GET", "/open", params={"owner": "ana diaz"}).json()
    assert o["count"] == 1 and o["open"][0]["since"] == "2026-08-20" and o["open"][0]["age_days"] > 0
    d = svc.call("GET", "/documents", params={"path": "daily/2026-08-27.md"}).json()
    assert d["kind"] == "raw" and len(d["passages"]) == 1
    s = svc.call("GET", "/status").json()
    assert s["counts"]["nodes"] == 6 and s["counts"]["pending_embeddings"] == 0 and s["model"]["id"] == "hash"


# ---- access
def test_no_token_is_needed(svc):
    assert svc.get("/healthz").status_code == 200
    assert svc.post("/search", json={"query": "x"}).status_code == 200
    assert svc.get("/status").status_code == 200
    assert svc.get("/ingest/manifest").status_code == 200


def test_domain_filter_sees_only_that_domain(svc):
    out = svc.search("Otter flag owner backend 令牌", filters={"domain": "public"})
    assert set(names(out["claims"])) <= {"Agent Identity"}
    assert not out["edges"], "an edge needs both ends in the domain"
    assert {p["path"] for p in out["passages"]} <= {"concepts/Agent Identity.md"}
    assert set(names(out["entities"], "name")) <= {"Agent Identity"}


def test_a_small_budget_still_returns_a_passage(svc):
    out = svc.search("Otter gateway protocol flag", max_tokens=260)
    assert out["tokens"] <= 260 and out["claims"] and len(out["passages"]) >= 1

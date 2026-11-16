# HTTP API

The service listens on `127.0.0.1:8787` by default. Everything is JSON over HTTP.

- [Access](#access)
- [Data model](#data-model)
- [Ingest](#ingest): `GET /ingest/manifest`, `POST /ingest`
- [Search](#search): `POST /search`
- [Read endpoints](#read-endpoints): `/entities`, `/open`, `/documents`, `/status`, `/healthz`
- [Errors](#errors)
- [Writing a client](#writing-a-client)

## Access

The service has no authentication of its own: every endpoint is open to whatever can reach
the port. It binds to `127.0.0.1`; to reach it from elsewhere, put an authenticating tunnel
in front, such as a dev tunnel (`devtunnel host -p 8787`, without `--allow-anonymous`).

## Data model

Six kinds of row. Each has a string `id` chosen by the client, unique within its kind and
stable across pushes: the same thing must get the same id every time, or it will be deleted
and re-inserted.

Only the fields marked **required** must be present. Unknown fields are ignored.

### `nodes` — entities

| Field | Type | |
|---|---|---|
| `id` | string | **required** |
| `name` | string | **required**. Display name; also matched in queries |
| `type` | string | Free vocabulary, e.g. `person`, `project`, `concept`. Used by the `types` filter |
| `domain` | string | Free vocabulary, e.g. `public`, `enterprise`. Used by the `domain` filter |
| `aliases` | string[] | Other names. A query that contains a name or alias treats that entity as a top hit |
| `summary` | string | One paragraph |
| `path` | string | Where the entity's page lives in the client's world |
| `url` | string | A link the caller can open |
| `attrs` | object | Anything else; returned by `GET /entities/{name}` |

### `edges` — typed relationships between two entities

| Field | Type | |
|---|---|---|
| `id` | string | **required** |
| `src`, `dst` | string | **required**. Node ids |
| `predicate` | string | **required**. e.g. `owns`, `part_of`. The predicate named `related_to` is treated as a weak link in graph mode |
| `inverse` | string | Name of the predicate read from `dst` to `src`, e.g. `owned_by`. Used in `paths` and by the `predicate` filter on `/neighbors` |
| `text` | string | A sentence about the relationship |
| `marker` | string | How well grounded it is; see below |
| `date`, `since` | string | ISO date or prefix (`2026`, `2026-08`, `2026-08-27`) |
| `authors` | string[] | Who said or decided it |
| `sources` | string[] | Ids of `sources` rows that support it |
| `superseded` | string | Date it stopped being true. Empty or absent means current |

### `claims` — statements about one entity

| Field | Type | |
|---|---|---|
| `id` | string | **required** |
| `subject` | string | **required**. Node id |
| `text` | string | **required** |
| `section` | string | Where on the page it came from |
| `marker`, `date`, `since`, `authors`, `sources`, `superseded` | | As for edges |
| `mentions` | string[] | Node ids named in the text. Each adds a weak link in graph mode |

**Markers.** The service stores and returns `marker` as given and treats it as opaque, with
two exceptions: a missing marker is reported as `fact`, and `GET /open` lists claims whose
marker is `open`. A typical vocabulary is `fact`, `decision`, `inferred`, `claim`, `open`.

### `sources` — where claims and edges came from

| Field | Type | |
|---|---|---|
| `id` | string | **required**. The value used in `sources` lists on claims and edges |
| `path`, `url`, `domain`, `date`, `summary` | string | |
| `origins` | string[] | Paths of the original documents this source summarises |
| `entities` | string[] | Node ids this source yielded |

### `documents` and `passages` — text returned as evidence

A document is a page or note; its passages are the chunks that get embedded and searched.

| `documents` field | Type | |
|---|---|---|
| `id` | string | **required** |
| `path` | string | Defaults to `id` |
| `kind` | string | `wiki` for the synthesis, `raw` for original notes. `raw` can be excluded per search and ranks slightly below `wiki` in graph mode |
| `title` | string | Prefixed to each passage when embedding |
| `note_date` | string | ISO date; used by the `after` filter |
| `domain`, `url` | string | |

| `passages` field | Type | |
|---|---|---|
| `id` | string | **required** |
| `doc_id` | string | **required**. Document id. Deleting a document deletes its passages |
| `text` | string | **required**. Plain text; 150–700 tokens works well |
| `ord` | integer | Position in the document |
| `heading` | string | Heading path, e.g. `Design > Auth` |
| `nodes` | `[[node id, via], ...]` | Entities this passage is about. `via` weights the link in graph mode: `page` 1.0 (the entity's own page), `wikilink` or `link` 0.5 (mentioned), `source` 0.3 (the passage's document fed that entity) |

### Hashes

Any row may carry `hash`, an opaque string. The service stores it and returns it from the
manifest. If a row arrives with the hash already stored for that id, it is skipped. If `hash`
is absent the service hashes the row itself.

## Ingest

### `GET /ingest/manifest`

What the service holds, so a client can work out what changed.

```json
{"manifest": {"nodes": {"<id>": "<hash>"}, "edges": {}, "claims": {}, "sources": {},
              "documents": {}, "passages": {}},
 "client_state": {"revision": "19cd6850", "export_ok": true}}
```

### `POST /ingest`

```json
{
  "upsert": {"nodes": [{"id": "Otter", "name": "Otter", "type": "project", "hash": "ab12"}],
             "claims": [{"id": "c1", "subject": "Otter", "text": "Ships behind a flag.",
                         "marker": "decision", "date": "2026-08-27", "sources": ["Week 35"]}]},
  "delete": {"claims": ["c0"], "documents": ["old/page.md"]},
  "replace": false,
  "state": {"revision": "19cd6850", "export_ok": true, "backlog_notes": 12}
}
```

| Field | |
|---|---|
| `upsert` | Rows to insert or update, by kind |
| `delete` | Ids to remove, by kind |
| `replace` | `true`: every stored row whose id is not in `upsert` is deleted. Use for a full resync |
| `state` | The client's own note. Stored as is and returned from `/status`, the manifest, and (as `revision` and `export_ok`) in every search response. Send `{"export_ok": false, ...}` without rows to tell callers the index is stale |

- **One request is one transaction.** A row with a missing required field, a duplicate id
  within a kind, or an unknown kind fails the whole request with `422` and changes nothing.
  Send a consistent change in one request; the body may be several megabytes.
- **References are not checked at ingest.** A claim whose subject does not exist is stored
  and simply not returned until the node arrives, so order within a batch does not matter.
- **Embedding is asynchronous.** The response comes back as soon as rows are written. Rows
  are keyword-searchable at once and vector-searchable when `pending_embeddings` reaches 0.
  A row whose embedded text did not change is not re-embedded.

Response:

```json
{"stats": {"claims": {"inserted": 1, "updated": 0, "unchanged": 0, "deleted": 1}, "...": {}},
 "pending_embeddings": 1}
```

## Search

### `POST /search`

```json
{
  "query": "who owns the trigger channel now?",
  "mode": "hybrid",
  "top_k": 8,
  "max_tokens": 3000,
  "filters": {"types": ["person", "project"], "after": "2026-08-01", "domain": "enterprise",
              "markers": ["fact", "decision"], "include_raw": true},
  "include_superseded": false
}
```

| Field | Default | |
|---|---|---|
| `query` | required | Plain text, any language; embedded on the server |
| `mode` | `hybrid` | `vector`: passages only, by embedding. `hybrid`: all kinds, vector and keyword lists fused by rank. `graph`: hybrid re-ranked by Personalized PageRank over the graph; also returns `paths` |
| `top_k` | 8 | Up to `2 * top_k` claims, `2 * top_k` edges, `top_k` entities, `top_k` passages |
| `max_tokens` | 3000 | Upper bound on the response, by an estimate of 1.3 tokens per word and 1 per CJK character. Claims and edges are filled first, up to 60% |
| `filters.types` | all | Entities of these types; claims whose subject is one; edges with at least one such end |
| `filters.after` | none | Drops claims, edges and documents dated before it. Undated rows are kept |
| `filters.domain` | all | Entities, claims and passages of this domain; edges need both ends in it |
| `filters.markers` | all | Claims and edges with one of these markers |
| `filters.include_raw` | `true` | `false` drops passages of `raw` documents |
| `include_superseded` | `false` | `true` also returns claims and edges with `superseded` set |

Response:

```json
{
  "claims":   [{"subject": "Trigger Channel", "text": "...", "marker": "decision",
                "date": "2026-08-27", "authors": ["A. Person"], "section": "Key points",
                "sources": ["Week 35"], "superseded": false}],
  "edges":    [{"subject": "A. Person", "predicate": "owns", "object": "Trigger Channel",
                "text": "...", "marker": "decision", "date": "2026-08-27",
                "sources": ["Week 35"], "superseded": false}],
  "entities": [{"name": "Trigger Channel", "type": "concept", "summary": "...",
                "path": "concepts/Trigger Channel.md", "aliases": ["..."], "url": "..."}],
  "passages": [{"text": "...", "path": "daily/2026-08-27.md", "kind": "raw", "title": "2026-08-27",
                "heading": "Standup", "note_date": "2026-08-27", "score": 0.031, "url": "..."}],
  "paths":    [["A. Person", "owns", "Trigger Channel", "part_of", "Otter"]],
  "sources":  {"Week 35": {"path": "sources/Week 35.md", "url": "...", "date": "2026-08-31",
                           "origins": ["daily/2026-08-27.md"]}},
  "tokens": 2841,
  "mode": "hybrid",
  "index": {"revision": "19cd6850", "indexed_at": "2026-11-16T04:42:47+00:00",
            "export_ok": true, "pending_embeddings": 0}
}
```

- Lists are ranked best first. `score` on a passage is only comparable within one response.
- `sources` resolves every source id cited by the returned claims and edges, so a caller can
  go from a claim to the original document.
- `paths` (graph mode) are chains of two or three edges, written
  `[entity, predicate, entity, predicate, entity]`, using `inverse` when an edge is read
  backwards (or `^predicate` when no inverse was given).
- A passage cut to fit the budget carries `"truncated": true`.
- `index` says how fresh the answer is. Treat `export_ok: false` or a non-zero
  `pending_embeddings` as "may be incomplete".

## Read endpoints

| Endpoint | Returns |
|---|---|
| `GET /entities/{name}` | The entity matched by id, name or alias: its fields and `attrs`, plus `claims` and `edges` in the shapes above. `?include_superseded=true` for history. `404` if unknown |
| `GET /entities/{name}/neighbors` | `{"entity": ..., "neighbors": [{"entity": {...}, "edge": {...}}]}`. `?predicate=owns` or its inverse filters |
| `GET /open` | `{"count": n, "open": [claim + "age_days"]}`: current claims with marker `open`, oldest first. `?entity=`, `?owner=` (matches `authors`), `?older_than_days=` |
| `GET /documents?path=` | `{"path", "kind", "title", "note_date", "url", "passages": [{"heading", "text"}]}` in order |
| `GET /status` | `index`, row `counts` with `pending_embeddings`, `client_state`, `last_ingest_at`, `model` (`id`, `dim`, `loaded`, `error`), `embed_error`, `schema`, `uptime_s` |
| `GET /healthz` | `{"ok": true}` |

## Errors

| Status | Meaning |
|---|---|
| `404` | No such entity or document |
| `422` | Malformed request, or a bad ingest batch; the body says which row. Nothing was changed |
| `503` | The embedding model failed to load; see `model.error` in `/status` |

Error bodies are `{"detail": "..."}`.

## Writing a client

The loop every client runs:

1. Build the full set of rows from your source of truth, with stable ids and a hash per row.
2. `GET /ingest/manifest`.
3. Upsert rows whose hash differs or whose id is new; delete ids the manifest has and you do
   not. Send it as one `POST /ingest` with a `state` that names the revision you exported.
4. If your export fails, send nothing but `state: {"export_ok": false, ...}`. The service
   keeps serving the last good index and callers can see it is stale.

A minimal push, standard library only:

```python
import hashlib, json, urllib.request

URL = "http://127.0.0.1:8787"

def call(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.load(resp)

def push(rows: dict, revision: str):
    """rows: {"nodes": [...], "edges": [...], "claims": [...], "sources": [...],
              "documents": [...], "passages": [...]}"""
    for items in rows.values():
        for r in items:
            r["hash"] = hashlib.sha1(json.dumps(r, sort_keys=True).encode()).hexdigest()[:16]
    have = call("GET", "/ingest/manifest")["manifest"]
    upsert = {k: [r for r in v if have.get(k, {}).get(r["id"]) != r["hash"]] for k, v in rows.items()}
    delete = {k: sorted(set(have.get(k, {})) - {r["id"] for r in v}) for k, v in rows.items()}
    return call("POST", "/ingest", {"upsert": upsert, "delete": delete,
                                    "state": {"revision": revision, "export_ok": True}})
```

`skills/kb-tools/kb_push.py` is a complete client for one particular source of truth, an LLM
wiki in an Obsidian vault: it resolves wikilinks, cuts pages into passages by heading, cleans
HTML notes to text, builds `obsidian://` links and respects a writer lock. Read it for ideas;
your source will differ.

Choosing ids:

- An entity: its canonical name or page path. A rename is then a delete plus an insert, which
  is what you want; add the old name to `aliases` so it still resolves.
- An edge: a hash of `src`, `predicate`, `dst`.
- A claim: a hash of its subject and its normalised text, so an edited claim is a new claim.
- A passage: document id plus position. Position shifts cost nothing, because a row whose
  text is unchanged is not re-embedded.

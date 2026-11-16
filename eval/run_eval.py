"""Golden-set evaluation of a running GraphRAG service.

    uv run python eval/run_eval.py --golden <path to golden.jsonl> [--url ...]

The golden set is private data and lives with the content it was written from, never in
this repo. One JSON object per line:

    {"id": "fact-01", "kind": "fact|multihop|temporal|broad", "lang": "en|zh",
     "question": "...", "expect_pages": ["Entity Name", ...], "answer_terms": ["term", ...]}

Per mode and per kind it reports:
  recall@k   share of expected entities found in the response (claims' subjects, edges'
             ends, entities, passages' documents); `vector` mode has passages only
  p-recall   the same, counting only the top-k passages' documents
  all-found  share of questions with every expected entity found
  terms      share of questions whose answer terms all occur in the returned text
  tokens     mean size of the response
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import defaultdict

MODES = ("vector", "hybrid", "graph")


def post(url, body):
    req = urllib.request.Request(url + "/search", data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=120) as resp:
        return json.load(resp)


def score(row, out):
    want = set(row["expect_pages"])
    in_passages = {p["title"] for p in out["passages"]}
    found = set(in_passages)
    found |= {c["subject"] for c in out["claims"]} | {e["name"] for e in out["entities"]}
    for e in out["edges"]:
        found |= {e["subject"], e["object"]}
    text = " ".join([c["subject"] + " " + c["text"] for c in out["claims"]]
                    + [f"{e['subject']} {e['predicate']} {e['object']} {e.get('text', '')}" for e in out["edges"]]
                    + [e["name"] + " " + e["summary"] for e in out["entities"]]
                    + [p["title"] + " " + p["text"] for p in out["passages"]]).casefold()
    terms = row.get("answer_terms") or []
    return {
        "recall": len(want & found) / len(want),
        "p_recall": len(want & in_passages) / len(want),
        "all": float(want <= found),
        "terms": float(all(t.casefold() in text for t in terms)) if terms else None,
        "tokens": out["tokens"],
        "missing": sorted(want - found),
    }


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True)
    ap.add_argument("--url", default=os.environ.get("GRAPHRAG_URL", "http://127.0.0.1:2335"))
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--max-tokens", type=int, default=3000)
    ap.add_argument("--modes", nargs="+", default=list(MODES), choices=MODES)
    ap.add_argument("--no-raw", action="store_true", help="search the synthesis only")
    ap.add_argument("--misses", action="store_true", help="list the questions each mode did not fully answer")
    ap.add_argument("--json", help="write per-question results here (private: contains the questions)")
    args = ap.parse_args()

    with open(args.golden, encoding="utf-8") as fh:
        rows = [json.loads(ln) for ln in fh if ln.strip()]
    results = defaultdict(list)
    latency = defaultdict(list)
    for row in rows:
        for mode in args.modes:
            t0 = time.time()
            out = post(args.url, {"query": row["question"], "mode": mode, "top_k": args.top_k,
                                  "max_tokens": args.max_tokens, "filters": {"include_raw": not args.no_raw}})
            latency[mode].append((time.time() - t0) * 1000)
            results[mode].append(dict(score(row, out), id=row["id"], kind=row["kind"], lang=row.get("lang", "en")))

    def mean(items, key):
        vals = [i[key] for i in items if i[key] is not None]
        return statistics.mean(vals) if vals else float("nan")

    groups = ["fact", "multihop", "temporal", "broad"]
    print(f"{len(rows)} questions, top_k={args.top_k}, max_tokens={args.max_tokens}"
          + (", wiki only" if args.no_raw else ""))
    print(f"\n{'mode':<7} {'group':<9} {'n':>3} {'recall@k':>9} {'p-recall':>9} {'all-found':>10} {'terms':>6} {'tokens':>7}")
    for mode in args.modes:
        res = results[mode]
        slices = [(g, [r for r in res if r["kind"] == g]) for g in groups]
        slices += [("zh", [r for r in res if r["lang"] == "zh"]), ("en", [r for r in res if r["lang"] != "zh"]),
                   ("ALL", res)]
        for name, items in slices:
            if items:
                print(f"{mode:<7} {name:<9} {len(items):>3} {mean(items, 'recall'):>9.3f} {mean(items, 'p_recall'):>9.3f} "
                      f"{mean(items, 'all'):>10.3f} {mean(items, 'terms'):>6.2f} {mean(items, 'tokens'):>7.0f}")
        print(f"{mode:<7} latency ms: median {statistics.median(latency[mode]):.0f}  max {max(latency[mode]):.0f}\n")
    if args.misses:
        for mode in args.modes:
            for r in results[mode]:
                if r["missing"] or r["terms"] == 0.0:
                    print(f"{mode:<7} {r['id']:<12} missing={r['missing']} terms={'ok' if r['terms'] else 'MISS'}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()

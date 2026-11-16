"""Phase-0 spike: sqlite-vec loading, embedding throughput and query latency on this CPU.

    uv run python scripts/spike_embed.py Qwen/Qwen3-Embedding-0.6B [--docs 64]

Uses synthetic text only, so it can run anywhere.
"""
import argparse
import sqlite3
import statistics
import time


def check_sqlite_vec():
    import sqlite_vec
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    (ver,) = db.execute("select vec_version()").fetchone()
    db.execute("create virtual table v using vec0(e float[4] distance_metric=cosine)")
    db.execute("insert into v(rowid, e) values (1, ?)", (sqlite_vec.serialize_float32([1, 0, 0, 0]),))
    row = db.execute("select rowid, distance from v where e match ? and k = 1",
                     (sqlite_vec.serialize_float32([1, 0, 0, 0]),)).fetchone()
    fts = db.execute("select sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0]
    print(f"sqlite {sqlite3.sqlite_version}  sqlite-vec {ver}  knn ok={row[0] == 1}  fts5={bool(fts)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--docs", type=int, default=64)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--dtype", default="float32", choices=["float32", "bfloat16"])
    ap.add_argument("--threads", type=int, default=0)
    args = ap.parse_args()
    check_sqlite_vec()

    import torch
    from sentence_transformers import SentenceTransformer
    t0 = time.time()
    if args.threads:
        torch.set_num_threads(args.threads)
    model = SentenceTransformer(args.model, device="cpu", model_kwargs={"torch_dtype": getattr(torch, args.dtype)})
    print(f"model {args.model}  load {time.time() - t0:.1f}s  dim {model.get_embedding_dimension()}  "
          f"threads {torch.get_num_threads()}  dtype {args.dtype}")

    sentence = "The backend service mints agent tokens without a stored secret and validates them at the gateway. "
    short = [f"Claim {i}: {sentence}" for i in range(args.docs)]            # ~25 tokens
    long = [f"Section {i}. " + sentence * 22 for i in range(args.docs // 4)]  # ~450 tokens
    zh = [f"第{i}条：后端服务在没有存储密钥的情况下签发代理令牌，并在网关处校验。" for i in range(args.docs)]
    for name, docs in (("short en (~25 tok)", short), ("short zh", zh), ("passage (~450 tok)", long)):
        t0 = time.time()
        model.encode(docs, batch_size=args.batch, normalize_embeddings=True)
        dt = time.time() - t0
        print(f"  {name:<20} {len(docs):>4} docs  {dt:6.1f}s  {len(docs) / dt:6.1f} docs/s")

    lat = []
    for q in ["who owns the trigger channel", "谁负责 connector auth？", "what is still open on the launch milestone",
              "how are agent tokens minted", "which project depends on the gateway"] * 3:
        t0 = time.time()
        model.encode([q], prompt="Instruct: Given a question, retrieve passages that answer it\nQuery:",
                     normalize_embeddings=True)
        lat.append((time.time() - t0) * 1000)
    print(f"  query latency ms: median {statistics.median(lat):.0f}  max {max(lat):.0f}")


if __name__ == "__main__":
    main()

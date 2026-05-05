from __future__ import annotations

import argparse
from pathlib import Path

from .cluster import build_clusters
from .db import connect, init_db
from .failures import iter_failures
from .query import query_image, write_query_html
from .review import generate_review
from .scan import scan_roots
from .web import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imgdupe")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Index image files into SQLite.")
    scan_parser.add_argument("roots", nargs="+", type=Path)
    scan_parser.add_argument("--db", type=Path, required=True)

    query_parser = subparsers.add_parser("query", help="Find visually similar images.")
    query_parser.add_argument("image", type=Path)
    query_parser.add_argument("--db", type=Path, required=True)
    query_parser.add_argument("--html", type=Path)
    query_parser.add_argument("--limit", type=int, default=50)
    query_parser.add_argument("--min-score", type=float, default=0.0)
    query_parser.add_argument("--hide-exact", action="store_true")

    cluster_parser = subparsers.add_parser("cluster", help="Build similar-image groups.")
    cluster_parser.add_argument("--db", type=Path, required=True)
    cluster_parser.add_argument("--min-score", type=float, default=70.0)

    review_parser = subparsers.add_parser("review", help="Generate static cluster review pages.")
    review_parser.add_argument("--db", type=Path, required=True)
    review_parser.add_argument("--out", type=Path, required=True)
    review_parser.add_argument("--thumbnail-size", type=int, default=256)

    serve_parser = subparsers.add_parser("serve", help="Run a local web UI for image search.")
    serve_parser.add_argument("--db", type=Path, required=True)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--limit", type=int, default=100)
    serve_parser.add_argument("--min-score", type=float, default=55.0)
    serve_parser.add_argument("--thumbnail-size", type=int, default=256)

    failures_parser = subparsers.add_parser("failures", help="Show image indexing failures.")
    failures_parser.add_argument("--db", type=Path, required=True)
    failures_parser.add_argument("--limit", type=int, default=100)

    args = parser.parse_args(argv)

    conn = connect(args.db)
    init_db(conn)

    if args.command == "scan":
        stats = scan_roots(conn, args.roots)
        print(
            "scan complete: "
            f"seen={stats.seen} indexed={stats.indexed} "
            f"skipped={stats.skipped} failed={stats.failed}"
        )
        return 0

    if args.command == "query":
        query_path = args.image.resolve()
        results = query_image(
            conn,
            query_path,
            limit=args.limit,
            min_score=args.min_score,
            include_exact=not args.hide_exact,
        )
        for image_row, candidate, score in results:
            print(
                f"{score.score:6.2f} {score.decision:18} "
                f"bands={candidate.total_hits:3d} "
                f"phash={score.phash_dist} whash={score.whash_dist} "
                f"dhash={score.dhash_dist} grid={score.grid_match_count} "
                f"{image_row['path']}"
            )
        if args.html:
            write_query_html(args.html, query_path, results)
            print(f"wrote {args.html}")
        return 0

    if args.command == "cluster":
        stats = build_clusters(conn, min_score=args.min_score)
        print(
            "cluster complete: "
            f"images={stats.images} candidate_pairs={stats.candidate_pairs} "
            f"scored_pairs={stats.scored_pairs} stored_matches={stats.stored_matches} "
            f"clusters={stats.clusters} clustered_images={stats.clustered_images}"
        )
        return 0

    if args.command == "review":
        stats = generate_review(
            conn,
            args.out,
            thumbnail_size=args.thumbnail_size,
        )
        print(
            "review complete: "
            f"clusters={stats.clusters} images={stats.images} "
            f"thumbnails={stats.thumbnails} out={stats.out_dir}"
        )
        return 0

    if args.command == "serve":
        serve(
            conn,
            args.db,
            host=args.host,
            port=args.port,
            limit=args.limit,
            min_score=args.min_score,
            thumbnail_size=args.thumbnail_size,
        )
        return 0

    if args.command == "failures":
        rows = iter_failures(conn, limit=args.limit)
        for row in rows:
            print(f"{row['path']}\t{row['decode_error']}")
        print(f"failures: {len(rows)}")
        return 0

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

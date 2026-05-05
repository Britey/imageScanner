from __future__ import annotations

import argparse
from pathlib import Path

from .cluster import build_clusters
from .db import connect, init_db
from .query import query_image, write_query_html
from .scan import scan_roots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imgdupe")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Index image files into SQLite.")
    scan_parser.add_argument("roots", nargs="+", type=Path)
    scan_parser.add_argument("--db", type=Path, required=True)

    query_parser = subparsers.add_parser("query", help="Find likely duplicates of one image.")
    query_parser.add_argument("image", type=Path)
    query_parser.add_argument("--db", type=Path, required=True)
    query_parser.add_argument("--html", type=Path)
    query_parser.add_argument("--limit", type=int, default=50)

    cluster_parser = subparsers.add_parser("cluster", help="Build duplicate clusters.")
    cluster_parser.add_argument("--db", type=Path, required=True)
    cluster_parser.add_argument("--min-score", type=float, default=70.0)

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
        results = query_image(conn, query_path, limit=args.limit)
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

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

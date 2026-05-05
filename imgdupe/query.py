from __future__ import annotations

import html
import sqlite3
from pathlib import Path

from .bands import Candidate, find_candidates
from .config import ScanConfig
from .db import fetch_hash_row, hash_row_to_dict
from .hashing import compute_image_hashes
from .match import PairScore, score_hashes
from .utils import human_size


def query_image(
    conn: sqlite3.Connection,
    image_path: Path,
    *,
    config: ScanConfig | None = None,
    limit: int = 50,
    min_score: float = 0.0,
) -> list[tuple[sqlite3.Row, Candidate, PairScore]]:
    config = config or ScanConfig()
    hashes, _ = compute_image_hashes(
        image_path,
        min_width=config.min_width,
        min_height=config.min_height,
    )
    indexed_row = conn.execute(
        "SELECT id FROM images WHERE path = ?",
        (str(image_path.resolve()),),
    ).fetchone()
    candidates = find_candidates(
        conn,
        hashes,
        exclude_image_id=int(indexed_row["id"]) if indexed_row is not None else None,
        whole_band_size=config.whole_band_size,
        grid_band_size=config.grid_band_size,
    )

    results: list[tuple[sqlite3.Row, Candidate, PairScore]] = []
    for candidate in candidates:
        hash_row = fetch_hash_row(conn, candidate.image_id)
        if hash_row is None:
            continue
        image_row = conn.execute(
            "SELECT * FROM images WHERE id = ?",
            (candidate.image_id,),
        ).fetchone()
        if image_row is None:
            continue
        candidate_hashes = hash_row_to_dict(hash_row)
        pair_score = score_hashes(
            hashes,
            candidate_hashes,
            sha_equal=hashes.get("sha256") == image_row["sha256"],
        )
        if pair_score.decision != "reject" and pair_score.score >= min_score:
            results.append((image_row, candidate, pair_score))

    results.sort(key=lambda item: item[2].score, reverse=True)
    return results[:limit]


def write_query_html(
    out_path: Path,
    query_path: Path,
    results: list[tuple[sqlite3.Row, Candidate, PairScore]],
) -> None:
    rows = []
    for image_row, candidate, score in results:
        path = str(image_row["path"])
        rows.append(
            f"""
            <tr>
              <td><img src="{html.escape(Path(path).as_uri())}" loading="lazy"></td>
              <td class="path">{html.escape(path)}</td>
              <td>{score.score:.2f}</td>
              <td>{html.escape(score.decision)}</td>
              <td>{score.phash_dist}</td>
              <td>{score.whash_dist}</td>
              <td>{score.dhash_dist}</td>
              <td>{score.grid_match_count}</td>
              <td>{candidate.total_hits}</td>
              <td>{image_row["width"]}x{image_row["height"]}</td>
              <td>{human_size(image_row["size_bytes"])}</td>
            </tr>
            """
        )

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>imgdupe query</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; color: #1f2933; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #d8dee9; padding: 8px; vertical-align: top; }}
    th {{ text-align: left; background: #f3f6f8; position: sticky; top: 0; }}
    img {{ width: 160px; height: 160px; object-fit: contain; background: #f7f7f7; }}
    .path {{ word-break: break-all; max-width: 520px; }}
  </style>
</head>
<body>
  <h1>Query Results</h1>
  <p>{html.escape(str(query_path))}</p>
  <table>
    <thead>
      <tr>
        <th>Preview</th><th>Path</th><th>Score</th><th>Decision</th>
        <th>pHash</th><th>wHash</th><th>dHash</th><th>Grid</th>
        <th>Bands</th><th>Dimensions</th><th>Size</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body>
</html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(document, encoding="utf-8")

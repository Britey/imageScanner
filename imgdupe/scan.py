from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from .bands import insert_bands
from .config import ScanConfig
from .db import clear_hashes, get_existing_image, replace_hashes, upsert_image
from .hashing import compute_image_hashes, sha256_file
from .utils import iter_image_paths, utc_now_sql


@dataclass
class ScanStats:
    seen: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0


def scan_roots(
    conn: sqlite3.Connection,
    roots: list[Path],
    *,
    config: ScanConfig | None = None,
) -> ScanStats:
    config = config or ScanConfig()
    stats = ScanStats()
    paths = list(iter_image_paths(roots))

    for path in tqdm(paths, desc="Scanning images", unit="image"):
        stats.seen += 1
        try:
            stat = path.stat()
        except OSError:
            stats.failed += 1
            continue

        existing = get_existing_image(conn, path)
        if (
            existing is not None
            and int(existing["size_bytes"]) == stat.st_size
            and int(existing["mtime_ns"]) == stat.st_mtime_ns
            and existing["decode_error"] is None
            and existing["missing_at"] is None
        ):
            stats.skipped += 1
            continue

        indexed_at = utc_now_sql()
        try:
            hashes, metadata = compute_image_hashes(
                path,
                min_width=config.min_width,
                min_height=config.min_height,
            )
            image_id = upsert_image(
                conn,
                path=path,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                indexed_at=indexed_at,
                width=int(metadata["width"]),
                height=int(metadata["height"]),
                image_format=str(metadata["format"]),
                sha256=hashes["sha256"],
                decode_error=None,
            )
            replace_hashes(conn, image_id, hashes)
            insert_bands(
                conn,
                image_id,
                hashes,
                whole_band_size=config.whole_band_size,
                grid_band_size=config.grid_band_size,
            )
            stats.indexed += 1
        except Exception as exc:
            image_id = upsert_image(
                conn,
                path=path,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                indexed_at=indexed_at,
                sha256=_sha256_or_none(path),
                decode_error=f"{type(exc).__name__}: {exc}",
            )
            clear_hashes(conn, image_id)
            stats.failed += 1

        if (stats.indexed + stats.failed) % config.batch_size == 0:
            conn.commit()

    conn.commit()
    return stats


def _sha256_or_none(path: Path) -> bytes | None:
    try:
        return sha256_file(path)
    except OSError:
        return None

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from .bands import insert_bands
from .config import ScanConfig
from .db import clear_hashes, get_existing_image, replace_crop_hashes, replace_hashes, upsert_image
from .hashing import compute_image_hashes, sha256_file
from .utils import iter_image_paths, utc_now_sql


@dataclass
class ScanStats:
    seen: int = 0
    queued: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True)
class ScanTask:
    path: Path
    size_bytes: int
    mtime_ns: int
    had_existing_hashes: bool


@dataclass(frozen=True)
class ScanResult:
    path: Path
    size_bytes: int
    mtime_ns: int
    width: int | None
    height: int | None
    image_format: str | None
    sha256: bytes | None
    hashes: dict[str, bytes]
    decode_error: str | None
    had_existing_hashes: bool


def scan_roots(
    conn: sqlite3.Connection,
    roots: list[Path],
    *,
    config: ScanConfig | None = None,
) -> ScanStats:
    config = config or ScanConfig()
    workers = max(1, config.workers)
    stats = ScanStats()
    paths = list(iter_image_paths(roots))
    tasks: list[ScanTask] = []

    for path in tqdm(paths, desc="Checking images", unit="image"):
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
            and (not config.crop_index or _has_crop_hashes(conn, int(existing["id"])))
        ):
            stats.skipped += 1
            continue

        tasks.append(
            ScanTask(
                path=path,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                had_existing_hashes=existing is not None,
            )
        )
        stats.queued += 1

    if not tasks:
        conn.commit()
        return stats

    if workers <= 1:
        results = (
            _hash_task(task, config.min_width, config.min_height, config.crop_index)
            for task in tasks
        )
        iterator = tqdm(results, total=len(tasks), desc="Hashing images", unit="image")
        for result in iterator:
            _store_result(conn, result, config, stats)
    else:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    _hash_task,
                    task,
                    config.min_width,
                    config.min_height,
                    config.crop_index,
                )
                for task in tasks
            ]
            for future in tqdm(as_completed(futures), total=len(futures), desc="Hashing images", unit="image"):
                _store_result(conn, future.result(), config, stats)

    conn.commit()
    return stats


def _store_result(
    conn: sqlite3.Connection,
    result: ScanResult,
    config: ScanConfig,
    stats: ScanStats,
) -> None:
    indexed_at = utc_now_sql()
    if result.decode_error is None:
        image_id = upsert_image(
            conn,
            path=result.path,
            size_bytes=result.size_bytes,
            mtime_ns=result.mtime_ns,
            indexed_at=indexed_at,
            width=result.width,
            height=result.height,
            image_format=result.image_format,
            sha256=result.sha256,
            decode_error=None,
        )
        replace_hashes(
            conn,
            image_id,
            result.hashes,
            clear_existing=result.had_existing_hashes,
        )
        replace_crop_hashes(
            conn,
            image_id,
            result.hashes,
            clear_existing=result.had_existing_hashes,
        )
        insert_bands(
            conn,
            image_id,
            result.hashes,
            whole_band_size=config.whole_band_size,
            grid_band_size=config.grid_band_size,
        )
        stats.indexed += 1
    else:
        image_id = upsert_image(
            conn,
            path=result.path,
            size_bytes=result.size_bytes,
            mtime_ns=result.mtime_ns,
            indexed_at=indexed_at,
            sha256=result.sha256,
            decode_error=result.decode_error,
        )
        if result.had_existing_hashes:
            clear_hashes(conn, image_id)
        stats.failed += 1

    if config.batch_size > 0 and (stats.indexed + stats.failed) % config.batch_size == 0:
        conn.commit()


def _hash_task(
    task: ScanTask,
    min_width: int,
    min_height: int,
    crop_index: bool,
) -> ScanResult:
    try:
        hashes, metadata = compute_image_hashes(
            task.path,
            min_width=min_width,
            min_height=min_height,
            include_crop_regions=crop_index,
        )
        return ScanResult(
            path=task.path,
            size_bytes=task.size_bytes,
            mtime_ns=task.mtime_ns,
            width=int(metadata["width"]),
            height=int(metadata["height"]),
            image_format=str(metadata["format"]),
            sha256=hashes["sha256"],
            hashes=hashes,
            decode_error=None,
            had_existing_hashes=task.had_existing_hashes,
        )
    except Exception as exc:
        return ScanResult(
            path=task.path,
            size_bytes=task.size_bytes,
            mtime_ns=task.mtime_ns,
            width=None,
            height=None,
            image_format=None,
            sha256=_sha256_or_none(task.path),
            hashes={},
            decode_error=f"{type(exc).__name__}: {exc}",
            had_existing_hashes=task.had_existing_hashes,
        )


def _sha256_or_none(path: Path) -> bytes | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


def _has_crop_hashes(conn: sqlite3.Connection, image_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM crop_hashes WHERE image_id = ? AND region = 'center_50' LIMIT 1",
        (image_id,),
    ).fetchone()
    return row is not None

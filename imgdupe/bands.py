from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass


WHOLE_HASH_TYPES = ("dhash256", "phash256", "whash256")
GRID_HASH_TYPES = tuple(f"grid{i}" for i in range(9))


@dataclass(frozen=True)
class Candidate:
    image_id: int
    total_hits: int
    hits_by_type: dict[str, int]


def split_bands(hash_bytes: bytes, band_size_bytes: int = 2):
    for index in range(0, len(hash_bytes), band_size_bytes):
        band = hash_bytes[index : index + band_size_bytes]
        if len(band) == band_size_bytes:
            yield index // band_size_bytes, band


def insert_bands(
    conn: sqlite3.Connection,
    image_id: int,
    hashes: dict[str, bytes],
    *,
    whole_band_size: int = 2,
    grid_band_size: int = 2,
) -> None:
    rows = []
    for hash_type in WHOLE_HASH_TYPES:
        hash_bytes = hashes.get(hash_type)
        if not hash_bytes:
            continue
        for band_index, band_value in split_bands(hash_bytes, whole_band_size):
            rows.append((hash_type, band_index, band_value, image_id))

    for hash_type in GRID_HASH_TYPES:
        hash_bytes = hashes.get(hash_type)
        if not hash_bytes:
            continue
        for band_index, band_value in split_bands(hash_bytes, grid_band_size):
            rows.append((hash_type, band_index, band_value, image_id))

    conn.executemany(
        """
        INSERT OR IGNORE INTO hash_bands
            (hash_type, band_index, band_value, image_id)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )


def find_candidates(
    conn: sqlite3.Connection,
    hashes: dict[str, bytes],
    *,
    exclude_image_id: int | None = None,
    whole_band_size: int = 2,
    grid_band_size: int = 2,
) -> list[Candidate]:
    hit_counts: Counter[int] = Counter()
    type_counts: dict[int, Counter[str]] = defaultdict(Counter)

    for hash_type in WHOLE_HASH_TYPES + GRID_HASH_TYPES:
        hash_bytes = hashes.get(hash_type)
        if not hash_bytes:
            continue
        band_size = grid_band_size if hash_type.startswith("grid") else whole_band_size
        for band_index, band_value in split_bands(hash_bytes, band_size):
            rows = conn.execute(
                """
                SELECT image_id
                FROM hash_bands
                WHERE hash_type = ?
                  AND band_index = ?
                  AND band_value = ?
                """,
                (hash_type, band_index, band_value),
            )
            for row in rows:
                image_id = int(row["image_id"])
                if exclude_image_id is not None and image_id == exclude_image_id:
                    continue
                hit_counts[image_id] += 1
                type_counts[image_id][hash_type] += 1

    candidates: list[Candidate] = []
    for image_id, total_hits in hit_counts.items():
        by_type = dict(type_counts[image_id])
        whole_hit = any(by_type.get(name, 0) >= 2 for name in WHOLE_HASH_TYPES)
        grid_hit = any(name.startswith("grid") for name in by_type)
        if total_hits >= 2 or whole_hit or grid_hit:
            candidates.append(Candidate(image_id, total_hits, by_type))
    candidates.sort(key=lambda item: item.total_hits, reverse=True)
    return candidates

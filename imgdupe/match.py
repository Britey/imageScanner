from __future__ import annotations

from dataclasses import dataclass

from .config import Thresholds
from .hashing import hamming_bytes


@dataclass(frozen=True)
class PairScore:
    dhash_dist: int | None
    phash_dist: int | None
    whash_dist: int | None
    grid_match_count: int
    grid_min_dist: int | None
    crop_min_dist: int | None
    score: float
    decision: str


def grid_score(
    hashes_a: dict[str, bytes],
    hashes_b: dict[str, bytes],
    *,
    cell_threshold: int,
) -> tuple[int, int | None]:
    distances = []
    for index in range(9):
        a = hashes_a.get(f"grid{index}")
        b = hashes_b.get(f"grid{index}")
        if a is not None and b is not None:
            distances.append(hamming_bytes(a, b))
    if not distances:
        return 0, None
    return sum(distance <= cell_threshold for distance in distances), min(distances)


def score_hashes(
    hashes_a: dict[str, bytes],
    hashes_b: dict[str, bytes],
    *,
    sha_equal: bool = False,
    crop_hashes_b: dict[str, bytes] | None = None,
    query_crop_hashes: dict[str, bytes] | None = None,
    thresholds: Thresholds | None = None,
) -> PairScore:
    thresholds = thresholds or Thresholds()
    if sha_equal:
        return PairScore(None, None, None, 9, 0, 0, 100.0, "exact_duplicate")

    dhash = _distance_or_none(hashes_a, hashes_b, "dhash256")
    phash = _distance_or_none(hashes_a, hashes_b, "phash256")
    whash = _distance_or_none(hashes_a, hashes_b, "whash256")
    grid_matches, grid_min = grid_score(
        hashes_a,
        hashes_b,
        cell_threshold=thresholds.grid_cell,
    )
    crop_min = crop_score(hashes_a, hashes_b, crop_hashes_b or {}, query_crop_hashes or {})

    score = 0.0
    if phash is not None:
        score += max(0.0, 35.0 * (1.0 - phash / 48.0))
    if whash is not None:
        score += max(0.0, 25.0 * (1.0 - whash / 48.0))
    if dhash is not None:
        score += max(0.0, 20.0 * (1.0 - dhash / 48.0))
    score += 20.0 * (grid_matches / 9.0)
    if crop_min is not None:
        score = max(score, max(0.0, 90.0 * (1.0 - crop_min / 48.0)))
    decision = classify(
        score=score,
        dhash=dhash,
        phash=phash,
        whash=whash,
        grid_matches=grid_matches,
        crop_min=crop_min,
        thresholds=thresholds,
    )
    return PairScore(dhash, phash, whash, grid_matches, grid_min, crop_min, round(score, 2), decision)


def crop_score(
    query_hashes: dict[str, bytes],
    candidate_hashes: dict[str, bytes],
    candidate_crop_hashes: dict[str, bytes],
    query_crop_hashes: dict[str, bytes],
) -> int | None:
    query_phash = query_hashes.get("phash256")
    candidate_phash = candidate_hashes.get("phash256")
    distances = []
    if query_phash is not None:
        distances.extend(
            hamming_bytes(query_phash, crop_hash)
            for crop_hash in candidate_crop_hashes.values()
            if len(crop_hash) == len(query_phash)
        )
    if candidate_phash is not None:
        distances.extend(
            hamming_bytes(crop_hash, candidate_phash)
            for crop_hash in query_crop_hashes.values()
            if len(crop_hash) == len(candidate_phash)
        )
    if query_crop_hashes and candidate_crop_hashes:
        distances.extend(
            hamming_bytes(query_crop, candidate_crop)
            for query_crop in query_crop_hashes.values()
            for candidate_crop in candidate_crop_hashes.values()
            if len(query_crop) == len(candidate_crop)
        )
    return min(distances) if distances else None


def classify(
    *,
    score: float,
    dhash: int | None,
    phash: int | None,
    whash: int | None,
    grid_matches: int,
    crop_min: int | None,
    thresholds: Thresholds,
) -> str:
    if (
        _lte(phash, thresholds.phash_strong)
        or (_lte(phash, 24) and _lte(whash, 24))
        or grid_matches >= thresholds.grid_strong
        or _lte(crop_min, 16)
        or score >= thresholds.score_strong
    ):
        return "strong_duplicate"
    if (
        (_lte(phash, thresholds.phash_probable) and _lte(whash, thresholds.whash_probable))
        or grid_matches >= thresholds.grid_probable
        or _lte(crop_min, 32)
        or score >= thresholds.score_probable
    ):
        return "probable_duplicate"
    if (
        _lte(phash, thresholds.phash_review)
        or _lte(whash, thresholds.whash_review)
        or grid_matches >= thresholds.grid_review
        or _lte(crop_min, 40)
        or score >= thresholds.score_review
    ):
        return "review"
    return "reject"


def _distance_or_none(
    hashes_a: dict[str, bytes],
    hashes_b: dict[str, bytes],
    key: str,
) -> int | None:
    a = hashes_a.get(key)
    b = hashes_b.get(key)
    if a is None or b is None:
        return None
    return hamming_bytes(a, b)


def _lte(value: int | None, threshold: int) -> bool:
    return value is not None and value <= threshold

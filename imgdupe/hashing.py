from __future__ import annotations

import hashlib
from pathlib import Path

import imagehash
from PIL import Image, ImageOps


class ImageTooSmallError(ValueError):
    pass


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.digest()


def load_normalized(path: Path, *, min_width: int = 32, min_height: int = 32) -> Image.Image:
    with Image.open(path) as opened:
        image_format = opened.format
        img = ImageOps.exif_transpose(opened)
        if img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            rgba = img.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, rgba).convert("RGB")
        else:
            img = img.convert("RGB")

    if img.width < min_width or img.height < min_height:
        raise ImageTooSmallError(f"image is too small: {img.width}x{img.height}")
    img.info["source_format"] = image_format
    return img


def imagehash_to_bytes(hash_value: imagehash.ImageHash) -> bytes:
    bits = hash_value.hash.flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    byte_len = (len(bits) + 7) // 8
    return value.to_bytes(byte_len, "big")


def grid_hashes(img: Image.Image) -> list[bytes]:
    width, height = img.size
    results: list[bytes] = []
    for gy in range(3):
        for gx in range(3):
            left = width * gx // 3
            upper = height * gy // 3
            right = width * (gx + 1) // 3
            lower = height * (gy + 1) // 3
            crop = img.crop((left, upper, right, lower))
            results.append(imagehash_to_bytes(imagehash.phash(crop, hash_size=8)))
    return results


def compute_image_hashes(
    path: Path,
    *,
    min_width: int = 32,
    min_height: int = 32,
) -> tuple[dict[str, bytes], dict[str, int | str]]:
    img = load_normalized(path, min_width=min_width, min_height=min_height)
    hashes = {
        "sha256": sha256_file(path),
        "dhash256": imagehash_to_bytes(imagehash.dhash(img, hash_size=16)),
        "phash256": imagehash_to_bytes(imagehash.phash(img, hash_size=16)),
        "whash256": imagehash_to_bytes(imagehash.whash(img, hash_size=16)),
    }
    for index, value in enumerate(grid_hashes(img)):
        hashes[f"grid{index}"] = value
    metadata = {
        "width": img.width,
        "height": img.height,
        "format": str(img.info.get("source_format") or ""),
    }
    return hashes, metadata


def hamming_bytes(a: bytes, b: bytes) -> int:
    if len(a) != len(b):
        raise ValueError(f"cannot compare hashes with lengths {len(a)} and {len(b)}")
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).bit_count()

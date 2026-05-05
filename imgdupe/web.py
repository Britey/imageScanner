from __future__ import annotations

import html
import mimetypes
import shutil
import sqlite3
import tempfile
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image, ImageOps

from .query import query_image
from .utils import human_size


def serve(
    conn: sqlite3.Connection,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    limit: int = 100,
    min_score: float = 55.0,
    thumbnail_size: int = 256,
) -> str:
    state = WebState(
        conn=conn,
        limit=limit,
        min_score=min_score,
        thumbnail_size=thumbnail_size,
        work_dir=Path(tempfile.mkdtemp(prefix="imgdupe-web-")),
    )
    handler = _make_handler(state)
    server = HTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_port}"
    try:
        print(f"Serving imgdupe web UI at {url}")
        server.serve_forever()
    finally:
        shutil.rmtree(state.work_dir, ignore_errors=True)
    return url


class WebState:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        limit: int,
        min_score: float,
        thumbnail_size: int,
        work_dir: Path,
    ) -> None:
        self.conn = conn
        self.limit = limit
        self.min_score = min_score
        self.thumbnail_size = thumbnail_size
        self.work_dir = work_dir
        self.uploads_dir = work_dir / "uploads"
        self.thumbs_dir = work_dir / "thumbs"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)


def _make_handler(state: WebState):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._send_html(_index_page())
                return
            if parsed.path.startswith("/thumb/"):
                self._send_thumbnail(parsed.path.removeprefix("/thumb/"))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/search":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                upload_path = self._save_upload()
                results = query_image(
                    state.conn,
                    upload_path,
                    limit=state.limit,
                    min_score=state.min_score,
                )
                self._send_html(_results_page(results, state.min_score))
            except Exception as exc:
                self._send_html(_error_page(exc), status=HTTPStatus.BAD_REQUEST)

        def _save_upload(self) -> Path:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                raise ValueError("expected multipart/form-data upload")
            boundary = _boundary_from_content_type(content_type)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            file_bytes, filename = _extract_file(body, boundary)
            suffix = Path(filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
                suffix = ".upload"
            upload_path = state.uploads_dir / f"query-{len(list(state.uploads_dir.iterdir())) + 1}{suffix}"
            upload_path.write_bytes(file_bytes)
            return upload_path

        def _send_thumbnail(self, raw_image_id: str) -> None:
            try:
                image_id = int(raw_image_id.removesuffix(".jpg"))
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            thumb_path = state.thumbs_dir / f"{image_id}.jpg"
            if not thumb_path.exists():
                row = state.conn.execute(
                    "SELECT path FROM images WHERE id = ?",
                    (image_id,),
                ).fetchone()
                if row is None or not _write_thumbnail(
                    Path(row["path"]),
                    thumb_path,
                    state.thumbnail_size,
                ):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
            self._send_file(thumb_path, "image/jpeg")

        def _send_html(self, document: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = document.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, path: Path, content_type: str | None = None) -> None:
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args) -> None:
            print(f"{self.address_string()} - {format % args}")

    return Handler


def _index_page() -> str:
    body = """
    <section class="search">
      <form method="post" action="/search" enctype="multipart/form-data">
        <label class="dropzone" for="image-input">
          <input id="image-input" type="file" name="image" accept="image/*" required>
          <span>Image</span>
        </label>
        <button type="submit">Search</button>
      </form>
    </section>
    <script>
      const input = document.querySelector("#image-input");
      window.addEventListener("paste", event => {
        const item = [...event.clipboardData.items].find(i => i.type.startsWith("image/"));
        if (!item) return;
        const file = item.getAsFile();
        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
      });
    </script>
    """
    return _page("Image Search", body)


def _results_page(results, min_score: float) -> str:
    cards = []
    for image_row, candidate, score in results:
        cards.append(
            f"""
            <article class="result">
              <a href="{html.escape(Path(image_row['path']).as_uri())}">
                <img src="/thumb/{int(image_row['id'])}.jpg" alt="">
              </a>
              <div class="score">{score.score:.2f}</div>
              <div>{html.escape(_label(score.decision))}</div>
              <div>{image_row['width']}x{image_row['height']} · {human_size(image_row['size_bytes'])}</div>
              <div class="details">pHash {score.phash_dist} · wHash {score.whash_dist} · dHash {score.dhash_dist} · grid {score.grid_match_count} · bands {candidate.total_hits}</div>
              <p class="path">{html.escape(image_row['path'])}</p>
            </article>
            """
        )
    empty = "<p>No results met the current score threshold.</p>" if not cards else ""
    body = f"""
    <nav><a href="/">New search</a></nav>
    <header>
      <h1>Results</h1>
      <p>Showing matches with score at least {min_score:.1f}</p>
    </header>
    <section class="grid">{''.join(cards)}</section>
    {empty}
    """
    return _page("Results", body)


def _error_page(exc: Exception) -> str:
    return _page(
        "Search Error",
        f"""
        <nav><a href="/">New search</a></nav>
        <h1>Search Error</h1>
        <p>{html.escape(type(exc).__name__)}: {html.escape(str(exc))}</p>
        """,
    )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; color: #20262d; background: #f7f9fb; }}
    main {{ max-width: 1200px; margin: 0 auto; }}
    a {{ color: #245b9f; }}
    button {{ border: 0; border-radius: 6px; background: #245b9f; color: #fff; padding: 10px 16px; font-weight: 700; }}
    .search {{ display: grid; place-items: center; min-height: 70vh; }}
    form {{ display: grid; gap: 16px; width: min(520px, 100%); }}
    .dropzone {{ display: grid; place-items: center; min-height: 220px; border: 2px dashed #9fb3c8; border-radius: 8px; background: #fff; cursor: pointer; }}
    .dropzone input {{ max-width: 90%; }}
    .dropzone span {{ color: #52606d; font-weight: 700; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; }}
    .result {{ border: 1px solid #d9e1e8; border-radius: 6px; background: #fff; padding: 12px; }}
    img {{ width: 100%; aspect-ratio: 1; object-fit: contain; background: #eef2f6; }}
    .score {{ font-size: 24px; font-weight: 800; margin-top: 8px; }}
    .details {{ color: #52606d; font-size: 13px; margin-top: 4px; }}
    .path {{ word-break: break-all; color: #3e4c59; font-size: 13px; }}
  </style>
</head>
<body>
  <main>{body}</main>
</body>
</html>
"""


def _write_thumbnail(source: Path, target: Path, thumbnail_size: int) -> bool:
    try:
        with Image.open(source) as opened:
            img = ImageOps.exif_transpose(opened).convert("RGB")
            img.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (thumbnail_size, thumbnail_size), (245, 247, 250))
            left = (thumbnail_size - img.width) // 2
            top = (thumbnail_size - img.height) // 2
            canvas.paste(img, (left, top))
            canvas.save(target, "JPEG", quality=82, optimize=True)
        return True
    except Exception:
        return False


def _boundary_from_content_type(content_type: str) -> bytes:
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            return ("--" + part.removeprefix("boundary=").strip('"')).encode()
    raise ValueError("missing multipart boundary")


def _extract_file(body: bytes, boundary: bytes) -> tuple[bytes, str]:
    for part in body.split(boundary):
        if b'name="image"' not in part:
            continue
        header, _, content = part.partition(b"\r\n\r\n")
        if not content:
            continue
        filename = _filename_from_header(header.decode("utf-8", errors="replace"))
        if content.endswith(b"\r\n"):
            content = content[:-2]
        return content, filename
    raise ValueError("no image file was uploaded")


def _filename_from_header(header: str) -> str:
    marker = "filename="
    if marker not in header:
        return "query.upload"
    value = header.split(marker, 1)[1].split(";", 1)[0].strip().strip('"')
    return value or "query.upload"


def _label(decision: str) -> str:
    labels = {
        "exact_duplicate": "Exact visual match",
        "strong_duplicate": "Strong visual match",
        "probable_duplicate": "Probable visual match",
        "review": "Possible visual match",
    }
    return labels.get(decision, decision.replace("_", " "))

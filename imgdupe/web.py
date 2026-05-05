from __future__ import annotations

import html
import mimetypes
import shutil
import sqlite3
import tempfile
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image, ImageOps

from .query import query_image
from .utils import human_size


@dataclass(frozen=True)
class SearchOptions:
    limit: int
    min_score: float
    include_exact: bool
    quality: str


def serve(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    limit: int = 100,
    min_score: float = 55.0,
    thumbnail_size: int = 256,
) -> str:
    state = WebState(
        conn=conn,
        db_path=db_path,
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
        db_path: Path,
        limit: int,
        min_score: float,
        thumbnail_size: int,
        work_dir: Path,
    ) -> None:
        self.conn = conn
        self.db_path = db_path
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
                self._send_html(_index_page(state))
                return
            if parsed.path == "/failures":
                self._send_html(_failures_page(state))
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
                upload_path, options = self._save_upload()
                results = query_image(
                    state.conn,
                    upload_path,
                    limit=options.limit,
                    min_score=options.min_score,
                    include_exact=options.include_exact,
                )
                self._send_html(_results_page(results, options))
            except Exception as exc:
                self._send_html(_error_page(exc), status=HTTPStatus.BAD_REQUEST)

        def _save_upload(self) -> tuple[Path, SearchOptions]:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                raise ValueError("expected multipart/form-data upload")
            boundary = _boundary_from_content_type(content_type)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            fields, file_bytes, filename = _extract_multipart(body, boundary)
            suffix = Path(filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
                suffix = ".upload"
            upload_path = state.uploads_dir / f"query-{len(list(state.uploads_dir.iterdir())) + 1}{suffix}"
            upload_path.write_bytes(file_bytes)
            return upload_path, _options_from_fields(fields, state)

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


def _index_page(state: WebState) -> str:
    stats = _stats(state.conn)
    body = f"""
    <header class="topbar">
      <div>
        <h1>Image Search</h1>
        <p class="muted">{html.escape(str(state.db_path))}</p>
      </div>
      <a href="/failures">Failures</a>
    </header>
    <section class="stats">
      <div><strong>{stats['indexed']}</strong><span>Indexed</span></div>
      <div><strong>{stats['hashed']}</strong><span>Searchable</span></div>
      <div><strong>{stats['failed']}</strong><span>Failures</span></div>
      <div><strong>{stats['groups']}</strong><span>Groups</span></div>
      <div><strong>{stats['matches']}</strong><span>Matches</span></div>
    </section>
    <section class="search">
      <form method="post" action="/search" enctype="multipart/form-data">
        <label class="dropzone" for="image-input">
          <input id="image-input" type="file" name="image" accept="image/*" required>
          <span>Image</span>
        </label>
        <div class="controls">
          <label>
            Quality
            <select name="quality">
              <option value="balanced" selected>Balanced</option>
              <option value="strict">Strict</option>
              <option value="loose">Loose</option>
            </select>
          </label>
          <label>
            Minimum score
            <input type="number" name="min_score" min="0" max="100" step="1" value="{state.min_score:.0f}">
          </label>
          <label>
            Max results
            <input type="number" name="limit" min="1" max="500" step="1" value="{state.limit}">
          </label>
          <label class="checkbox">
            <input type="checkbox" name="include_exact" value="1" checked>
            Include exact file matches
          </label>
        </div>
        <button type="submit">Search</button>
      </form>
    </section>
    <script>
      const input = document.querySelector("#image-input");
      const quality = document.querySelector("select[name='quality']");
      const minScore = document.querySelector("input[name='min_score']");
      const scores = {{ strict: "75", balanced: "{state.min_score:.0f}", loose: "40" }};
      quality.addEventListener("change", () => {{
        minScore.value = scores[quality.value] || scores.balanced;
      }});
      window.addEventListener("paste", event => {{
        const item = [...event.clipboardData.items].find(i => i.type.startsWith("image/"));
        if (!item) return;
        const file = item.getAsFile();
        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
      }});
    </script>
    """
    return _page("Image Search", body)


def _results_page(results, options: SearchOptions) -> str:
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
              <div>{image_row['width']}x{image_row['height']} &middot; {human_size(image_row['size_bytes'])}</div>
              <div class="details">pHash {score.phash_dist} &middot; wHash {score.whash_dist} &middot; dHash {score.dhash_dist} &middot; grid {score.grid_match_count} &middot; bands {candidate.total_hits}</div>
              <p class="path">{html.escape(image_row['path'])}</p>
            </article>
            """
        )
    empty = "<p>No results met the current score threshold.</p>" if not cards else ""
    body = f"""
    <nav><a href="/">New search</a></nav>
    <header>
      <h1>Results</h1>
      <p>Quality {html.escape(options.quality)} &middot; score at least {options.min_score:.1f} &middot; max {options.limit} results &middot; exact matches {html.escape('shown' if options.include_exact else 'hidden')}</p>
    </header>
    <section class="grid">{''.join(cards)}</section>
    {empty}
    """
    return _page("Results", body)


def _failures_page(state: WebState) -> str:
    rows = state.conn.execute(
        """
        SELECT path, decode_error
        FROM images
        WHERE decode_error IS NOT NULL
        ORDER BY indexed_at DESC, path
        LIMIT 500
        """
    ).fetchall()
    items = []
    for row in rows:
        items.append(
            f"""
            <tr>
              <td class="path">{html.escape(row['path'])}</td>
              <td>{html.escape(row['decode_error'] or '')}</td>
            </tr>
            """
        )
    empty = "<p>No indexing failures recorded.</p>" if not items else ""
    body = f"""
    <nav><a href="/">Search</a></nav>
    <h1>Indexing Failures</h1>
    {empty}
    <table>
      <thead><tr><th>Path</th><th>Error</th></tr></thead>
      <tbody>{''.join(items)}</tbody>
    </table>
    """
    return _page("Failures", body)


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
    .topbar {{ display: flex; justify-content: space-between; gap: 16px; align-items: start; margin-bottom: 16px; }}
    .muted {{ color: #52606d; word-break: break-all; }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px; margin-bottom: 24px; }}
    .stats div {{ background: #fff; border: 1px solid #d9e1e8; border-radius: 6px; padding: 12px; }}
    .stats strong {{ display: block; font-size: 24px; }}
    .stats span {{ color: #52606d; }}
    .search {{ display: grid; place-items: center; min-height: 50vh; }}
    form {{ display: grid; gap: 16px; width: min(520px, 100%); }}
    .dropzone {{ display: grid; place-items: center; min-height: 220px; border: 2px dashed #9fb3c8; border-radius: 8px; background: #fff; cursor: pointer; }}
    .dropzone input {{ max-width: 90%; }}
    .dropzone span {{ color: #52606d; font-weight: 700; }}
    .controls {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .controls label {{ display: grid; gap: 6px; font-size: 13px; color: #52606d; }}
    .controls input, .controls select {{ min-width: 0; padding: 8px; border: 1px solid #bcccdc; border-radius: 6px; background: #fff; }}
    .controls .checkbox {{ display: flex; align-items: center; gap: 8px; }}
    .controls .checkbox input {{ width: auto; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; }}
    .result {{ border: 1px solid #d9e1e8; border-radius: 6px; background: #fff; padding: 12px; }}
    img {{ width: 100%; aspect-ratio: 1; object-fit: contain; background: #eef2f6; }}
    .score {{ font-size: 24px; font-weight: 800; margin-top: 8px; }}
    .details {{ color: #52606d; font-size: 13px; margin-top: 4px; }}
    .path {{ word-break: break-all; color: #3e4c59; font-size: 13px; }}
    table {{ border-collapse: collapse; width: 100%; background: #fff; }}
    th, td {{ border-bottom: 1px solid #d9e1e8; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f3f6f8; }}
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


def _extract_multipart(body: bytes, boundary: bytes) -> tuple[dict[str, str], bytes, str]:
    fields: dict[str, str] = {}
    file_bytes: bytes | None = None
    filename = "query.upload"
    for part in body.split(boundary):
        header, _, content = part.partition(b"\r\n\r\n")
        if not header or not content:
            continue
        header_text = header.decode("utf-8", errors="replace")
        if content.endswith(b"\r\n"):
            content = content[:-2]
        name = _field_name_from_header(header_text)
        if not name:
            continue
        if name == "image":
            filename = _filename_from_header(header_text)
            file_bytes = content
        else:
            fields[name] = content.decode("utf-8", errors="replace")
    if file_bytes is not None:
        return fields, file_bytes, filename
    raise ValueError("no image file was uploaded")


def _field_name_from_header(header: str) -> str | None:
    marker = 'name="'
    if marker not in header:
        return None
    return header.split(marker, 1)[1].split('"', 1)[0]


def _filename_from_header(header: str) -> str:
    marker = "filename="
    if marker not in header:
        return "query.upload"
    value = header.split(marker, 1)[1].split(";", 1)[0].strip().strip('"')
    return value or "query.upload"


def _options_from_fields(fields: dict[str, str], state: WebState) -> SearchOptions:
    quality = fields.get("quality", "balanced")
    quality_scores = {
        "strict": 75.0,
        "balanced": state.min_score,
        "loose": 40.0,
    }
    min_score = _float_field(fields, "min_score", quality_scores.get(quality, state.min_score))
    limit = _int_field(fields, "limit", state.limit)
    return SearchOptions(
        limit=max(1, min(limit, 500)),
        min_score=max(0.0, min(min_score, 100.0)),
        include_exact=fields.get("include_exact") == "1",
        quality=quality if quality in quality_scores else "balanced",
    )


def _int_field(fields: dict[str, str], key: str, default: int) -> int:
    try:
        return int(fields.get(key, ""))
    except ValueError:
        return default


def _float_field(fields: dict[str, str], key: str, default: float) -> float:
    try:
        return float(fields.get(key, ""))
    except ValueError:
        return default


def _stats(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS indexed,
            SUM(CASE WHEN decode_error IS NULL AND missing_at IS NULL THEN 1 ELSE 0 END) AS ok,
            SUM(CASE WHEN decode_error IS NOT NULL THEN 1 ELSE 0 END) AS failed
        FROM images
        """
    ).fetchone()
    hashed = conn.execute("SELECT COUNT(*) AS count FROM hashes").fetchone()["count"]
    groups = conn.execute("SELECT COUNT(DISTINCT cluster_id) AS count FROM clusters").fetchone()["count"]
    matches = conn.execute("SELECT COUNT(*) AS count FROM matches").fetchone()["count"]
    return {
        "indexed": int(row["indexed"] or 0),
        "ok": int(row["ok"] or 0),
        "failed": int(row["failed"] or 0),
        "hashed": int(hashed or 0),
        "groups": int(groups or 0),
        "matches": int(matches or 0),
    }


def _label(decision: str) -> str:
    labels = {
        "exact_duplicate": "Exact visual match",
        "strong_duplicate": "Strong visual match",
        "probable_duplicate": "Probable visual match",
        "review": "Possible visual match",
    }
    return labels.get(decision, decision.replace("_", " "))

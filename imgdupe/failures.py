from __future__ import annotations

import sqlite3


def iter_failures(conn: sqlite3.Connection, *, limit: int = 100):
    return conn.execute(
        """
        SELECT path, decode_error
        FROM images
        WHERE decode_error IS NOT NULL
        ORDER BY indexed_at DESC, path
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

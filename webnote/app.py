from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
DOCS_DIR = UPLOAD_DIR / "docs"
IMAGES_DIR = UPLOAD_DIR / "images"
DB_PATH = DATA_DIR / "notes.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
DOCS_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tabs (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content_html TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS uploads (
            id TEXT PRIMARY KEY,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            size INTEGER NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    existing = conn.execute("SELECT COUNT(*) AS c FROM tabs").fetchone()["c"]
    if existing == 0:
        ts = now_iso()
        defaults = ["Quick Notes", "Work", "Ideas"]
        for idx, title in enumerate(defaults):
            conn.execute(
                "INSERT INTO tabs (id, title, content_html, sort_order, created_at, updated_at) VALUES (?, ?, '', ?, ?, ?)",
                (str(uuid.uuid4()), title, idx, ts, ts),
            )
    conn.commit()
    conn.close()


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/api/bootstrap")
def bootstrap() -> Any:
    conn = get_conn()
    tabs = [dict(row) for row in conn.execute("SELECT * FROM tabs ORDER BY sort_order, created_at").fetchall()]
    docs = [dict(row) for row in conn.execute("SELECT * FROM uploads ORDER BY created_at DESC").fetchall()]
    conn.close()
    return jsonify({"tabs": tabs, "uploads": docs})


@app.post("/api/tabs")
def create_tab() -> Any:
    payload = request.get_json(force=True, silent=True) or {}
    title = (payload.get("title") or "New Page").strip()[:80] or "New Page"
    tab_id = str(uuid.uuid4())
    ts = now_iso()
    conn = get_conn()
    sort_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM tabs").fetchone()["next_order"]
    conn.execute(
        "INSERT INTO tabs (id, title, content_html, sort_order, created_at, updated_at) VALUES (?, ?, '', ?, ?, ?)",
        (tab_id, title, sort_order, ts, ts),
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM tabs WHERE id = ?", (tab_id,)).fetchone())
    conn.close()
    return jsonify(row), 201


@app.put("/api/tabs/<tab_id>")
def update_tab(tab_id: str) -> Any:
    payload = request.get_json(force=True, silent=True) or {}
    title = payload.get("title")
    content_html = payload.get("content_html")
    ts = now_iso()
    conn = get_conn()
    row = conn.execute("SELECT * FROM tabs WHERE id = ?", (tab_id,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "tab not found"}), 404

    next_title = (str(title).strip()[:80] or row["title"]) if title is not None else row["title"]
    next_content = str(content_html) if content_html is not None else row["content_html"]
    conn.execute(
        "UPDATE tabs SET title = ?, content_html = ?, updated_at = ? WHERE id = ?",
        (next_title, next_content, ts, tab_id),
    )
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM tabs WHERE id = ?", (tab_id,)).fetchone())
    conn.close()
    return jsonify(updated)


@app.post("/api/tabs/reorder")
def reorder_tabs() -> Any:
    payload = request.get_json(force=True, silent=True) or {}
    ids = payload.get("ids") or []
    conn = get_conn()
    for idx, tab_id in enumerate(ids):
        conn.execute("UPDATE tabs SET sort_order = ?, updated_at = ? WHERE id = ?", (idx, now_iso(), tab_id))
    conn.commit()
    rows = [dict(row) for row in conn.execute("SELECT * FROM tabs ORDER BY sort_order, created_at").fetchall()]
    conn.close()
    return jsonify({"tabs": rows})


@app.delete("/api/tabs/<tab_id>")
def delete_tab(tab_id: str) -> Any:
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) AS c FROM tabs").fetchone()["c"]
    if count <= 1:
        conn.close()
        return jsonify({"error": "at least one tab must remain"}), 400
    conn.execute("DELETE FROM tabs WHERE id = ?", (tab_id,))
    conn.commit()
    rows = [dict(row) for row in conn.execute("SELECT * FROM tabs ORDER BY sort_order, created_at").fetchall()]
    conn.close()
    return jsonify({"tabs": rows})


@app.post("/api/upload/document")
def upload_document() -> Any:
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "no file uploaded"}), 400
    original_name = secure_filename(file.filename) or "file"
    ext = Path(original_name).suffix
    stored_name = f"{uuid.uuid4().hex}{ext}"
    target = DOCS_DIR / stored_name
    file.save(target)
    upload_id = str(uuid.uuid4())
    ts = now_iso()
    conn = get_conn()
    conn.execute(
        "INSERT INTO uploads (id, original_name, stored_name, relative_path, size, kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (upload_id, original_name, stored_name, f"docs/{stored_name}", target.stat().st_size, "document", ts),
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone())
    conn.close()
    return jsonify(row), 201


@app.post("/api/upload/image")
def upload_image() -> Any:
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "no image uploaded"}), 400
    original_name = secure_filename(file.filename) or "image.png"
    ext = Path(original_name).suffix or ".png"
    stored_name = f"{uuid.uuid4().hex}{ext}"
    target = IMAGES_DIR / stored_name
    file.save(target)
    upload_id = str(uuid.uuid4())
    ts = now_iso()
    conn = get_conn()
    conn.execute(
        "INSERT INTO uploads (id, original_name, stored_name, relative_path, size, kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (upload_id, original_name, stored_name, f"images/{stored_name}", target.stat().st_size, "image", ts),
    )
    conn.commit()
    conn.close()
    return jsonify({
        "id": upload_id,
        "url": f"/uploads/images/{stored_name}",
        "name": original_name,
        "size": target.stat().st_size,
    }), 201


@app.delete("/api/uploads/<upload_id>")
def delete_upload(upload_id: str) -> Any:
    conn = get_conn()
    row = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "file not found"}), 404
    data = dict(row)
    rel = Path(data["relative_path"])
    target = UPLOAD_DIR / rel
    if target.exists() and target.is_file():
        target.unlink()
    conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
    conn.commit()
    remaining = [dict(item) for item in conn.execute("SELECT * FROM uploads ORDER BY created_at DESC").fetchall()]
    conn.close()
    return jsonify({"uploads": remaining})


@app.get("/uploads/<path:subpath>")
def uploaded_file(subpath: str):
    safe_path = Path(subpath)
    if safe_path.parts and safe_path.parts[0] == "docs":
        return send_from_directory(DOCS_DIR, "/".join(safe_path.parts[1:]))
    if safe_path.parts and safe_path.parts[0] == "images":
        return send_from_directory(IMAGES_DIR, "/".join(safe_path.parts[1:]))
    return jsonify({"error": "file not found"}), 404


if __name__ == "__main__":
    init_db()
    host = os.environ.get("WEBNOTE_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBNOTE_PORT", "8320"))
    app.run(host=host, port=port, debug=False)

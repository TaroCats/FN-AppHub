"""SQLite 存储层：访问列表持久化。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

DATA_DIR = Path(os.environ.get("APPHUB_DATA", "/app/data"))
DB_PATH = DATA_DIR / "apphub.db"
ICON_DIR = DATA_DIR / "icons"

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    url         TEXT    NOT NULL,
    icon        TEXT    NOT NULL DEFAULT '',
    group_name  TEXT    NOT NULL DEFAULT '未分组',
    note        TEXT    NOT NULL DEFAULT '',
    open_mode   TEXT    NOT NULL DEFAULT 'inline',
    mode        TEXT    NOT NULL DEFAULT 'proxy',
    color       TEXT    NOT NULL DEFAULT '',
    container   TEXT    NOT NULL DEFAULT '',
    url_mode    TEXT    NOT NULL DEFAULT 'auto',
    port        TEXT    NOT NULL DEFAULT '',
    home_path   TEXT    NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
    name       TEXT PRIMARY KEY,
    sort_order INTEGER NOT NULL DEFAULT 0
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, _conn() as conn:
        conn.executescript(SCHEMA)
        # 旧库迁移：open_mode（打开方式：inline 内嵌打开 / window 新窗口打开）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(apps)").fetchall()}
        if "open_mode" not in cols:
            conn.execute(
                "ALTER TABLE apps ADD COLUMN open_mode TEXT NOT NULL DEFAULT 'inline'"
            )
        # 旧库迁移：地址方式（auto 统一配置只填端口 / custom 自定义完整地址）、端口、主页地址
        needs_addr_backfill = False
        for col, ddl in (
            ("url_mode", "ALTER TABLE apps ADD COLUMN url_mode TEXT NOT NULL DEFAULT 'auto'"),
            ("port", "ALTER TABLE apps ADD COLUMN port TEXT NOT NULL DEFAULT ''"),
            ("home_path", "ALTER TABLE apps ADD COLUMN home_path TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in cols:
                conn.execute(ddl)
                needs_addr_backfill = True
        if needs_addr_backfill:
            # 带路径的历史地址归为自定义；纯主机+端口的历史地址归为统一配置并补端口
            for row in conn.execute("SELECT id, url, url_mode, port FROM apps").fetchall():
                raw = row["url"] or ""
                try:
                    parsed = urlparse(raw if "://" in raw else "http://" + raw)
                except ValueError:
                    continue
                if parsed.path not in ("", "/"):
                    conn.execute("UPDATE apps SET url_mode = 'custom' WHERE id = ?", (row["id"],))
                elif not row["port"]:
                    fallback = 443 if parsed.scheme == "https" else 80
                    conn.execute(
                        "UPDATE apps SET port = ? WHERE id = ?",
                        (str(parsed.port or fallback), row["id"]),
                    )
        # 分组表与现有应用分组对齐（含历史数据）
        rows = conn.execute(
            "SELECT group_name, MIN(sort_order) AS o FROM apps GROUP BY group_name"
        ).fetchall()
        for r in rows:
            conn.execute(
                "INSERT OR IGNORE INTO groups (name, sort_order) VALUES (?, ?)",
                (r["group_name"], r["o"] or 0),
            )
        conn.execute(
            "INSERT OR IGNORE INTO groups (name, sort_order) VALUES ('未分组', 0)"
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["icon_url"] = f"/api/apps/{d['id']}/icon" if d.get("icon") else ""
    return d


def list_apps() -> list[dict[str, Any]]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM apps ORDER BY sort_order ASC, id ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_app(app_id: int) -> Optional[dict[str, Any]]:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    return _row_to_dict(row) if row else None


def find_by_url(url: str) -> Optional[dict[str, Any]]:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT * FROM apps WHERE url = ?", (url,)).fetchone()
    return _row_to_dict(row) if row else None


def create_app(data: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    with _lock, _conn() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM apps"
        ).fetchone()["m"]
        cur = conn.execute(
            """INSERT INTO apps
               (name, url, icon, group_name, note, open_mode, mode, color, container,
                url_mode, port, home_path, sort_order, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["name"],
                data["url"],
                data.get("icon", ""),
                data.get("group_name") or "未分组",
                data.get("note", ""),
                data.get("open_mode") or "inline",
                data.get("mode") or "proxy",
                data.get("color", ""),
                data.get("container", ""),
                data.get("url_mode") or "auto",
                data.get("port", ""),
                data.get("home_path", ""),
                max_order + 10,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO groups (name, sort_order) VALUES (?, (SELECT COALESCE(MAX(sort_order), 0) + 10 FROM groups))",
            (data.get("group_name") or "未分组",),
        )
        new_id = cur.lastrowid
    app = get_app(int(new_id))
    assert app is not None
    return app


def update_app(app_id: int, data: dict[str, Any]) -> Optional[dict[str, Any]]:
    current = get_app(app_id)
    if not current:
        return None
    fields = []
    params: list[Any] = []
    for key in ("name", "url", "icon", "group_name", "note", "open_mode", "mode", "color", "container",
                "url_mode", "port", "home_path"):
        if key in data and data[key] is not None:
            fields.append(f"{key} = ?")
            params.append(data[key])
    if not fields:
        return current
    fields.append("updated_at = ?")
    params.append(time.time())
    params.append(app_id)
    with _lock, _conn() as conn:
        conn.execute(f"UPDATE apps SET {', '.join(fields)} WHERE id = ?", params)
        if "group_name" in data and data.get("group_name"):
            conn.execute(
                "INSERT OR IGNORE INTO groups (name, sort_order) VALUES (?, (SELECT COALESCE(MAX(sort_order), 0) + 10 FROM groups))",
                (data["group_name"],),
            )
    return get_app(app_id)


def delete_app(app_id: int) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))
    return cur.rowcount > 0


def reorder(ids: list[int]) -> None:
    with _lock, _conn() as conn:
        for index, app_id in enumerate(ids):
            conn.execute(
                "UPDATE apps SET sort_order = ? WHERE id = ?", ((index + 1) * 10, app_id)
            )


def get_setting(key: str, default: str = "") -> str:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def distinct_groups() -> list[str]:
    """有应用的分组名，按分组表顺序。"""
    with _lock, _conn() as conn:
        rows = conn.execute(
            """SELECT a.group_name AS name
                 FROM apps a
                 LEFT JOIN groups g ON g.name = a.group_name
                GROUP BY a.group_name
                ORDER BY COALESCE(MIN(g.sort_order), 999999), MIN(a.sort_order)"""
        ).fetchall()
    return [r["name"] for r in rows]


def list_groups() -> list[dict[str, Any]]:
    """全部分组（含空分组），带应用数量。"""
    with _lock, _conn() as conn:
        rows = conn.execute(
            """SELECT g.name, g.sort_order,
                      (SELECT COUNT(*) FROM apps a WHERE a.group_name = g.name) AS count
                 FROM groups g
                ORDER BY g.sort_order ASC, g.name ASC"""
        ).fetchall()
    return [{"name": r["name"], "sort_order": r["sort_order"], "count": r["count"]} for r in rows]


def create_group(name: str) -> list[dict[str, Any]]:
    name = (name or "").strip()
    if not name:
        return list_groups()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO groups (name, sort_order) "
            "VALUES (?, (SELECT COALESCE(MAX(sort_order), 0) + 10 FROM groups))",
            (name,),
        )
    return list_groups()


def rename_group(old: str, new: str) -> int:
    """重命名分组；目标分组已存在则合并。返回受影响应用数。"""
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or not new or old == new:
        return 0
    with _lock, _conn() as conn:
        cur = conn.execute(
            "UPDATE apps SET group_name = ?, updated_at = ? WHERE group_name = ?",
            (new, time.time(), old),
        )
        affected = cur.rowcount
        conn.execute(
            "INSERT OR IGNORE INTO groups (name, sort_order) VALUES (?, (SELECT COALESCE(MAX(sort_order), 0) + 10 FROM groups))",
            (new,),
        )
        conn.execute("DELETE FROM groups WHERE name = ?", (old,))
    return affected


def delete_group(name: str) -> int:
    """删除分组，组内应用移到「未分组」。返回被移出的应用数。"""
    name = (name or "").strip()
    if not name or name == "未分组":
        return 0
    with _lock, _conn() as conn:
        cur = conn.execute(
            "UPDATE apps SET group_name = '未分组', updated_at = ? WHERE group_name = ?",
            (time.time(), name),
        )
        affected = cur.rowcount
        conn.execute("DELETE FROM groups WHERE name = ?", (name,))
        conn.execute(
            "INSERT OR IGNORE INTO groups (name, sort_order) VALUES ('未分组', 0)"
        )
    return affected


def reorder_groups(names: list[str]) -> None:
    with _lock, _conn() as conn:
        for index, name in enumerate(names):
            conn.execute(
                "UPDATE groups SET sort_order = ? WHERE name = ?",
                ((index + 1) * 10, name),
            )

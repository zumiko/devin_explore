import json
import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "internal_tools.db"))


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def audit(conn, actor_id, action, entity_type, entity_id, before=None, after=None, reason=None, ts=None):
    conn.execute(
        "INSERT INTO audit_log (ts, actor_id, action, entity_type, entity_id, before_json, after_json, reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts or now_iso(),
            actor_id,
            action,
            entity_type,
            entity_id,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
            reason,
        ),
    )


def get_setting(conn, key):
    return conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()["value"]

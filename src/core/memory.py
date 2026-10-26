"""SQLite 存储：消息、判定、摘要、心情、人物档案。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class Memory:
    def __init__(self, db_path: str | Path = "data/bot.db") -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    room_name TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    content TEXT NOT NULL,
                    is_group INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_room
                    ON messages(room_id, created_at);

                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    content TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    reason TEXT,
                    accepted INTEGER NOT NULL,
                    probability REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS summaries (
                    room_id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS moods (
                    room_id TEXT PRIMARY KEY,
                    value INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS room_modes (
                    room_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed (
                    room_id TEXT NOT NULL,
                    message_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (room_id, message_key)
                );

                CREATE TABLE IF NOT EXISTS pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS judge_cache (
                    room_id TEXT NOT NULL,
                    content_key TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    probability REAL NOT NULL,
                    accepted INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (room_id, content_key)
                );

                CREATE TABLE IF NOT EXISTS notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contacts (
                    user_id TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL DEFAULT '',
                    relation TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    is_owner INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS group_memory (
                    room_id TEXT PRIMARY KEY,
                    text TEXT NOT NULL DEFAULT '',
                    region TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_stats (
                    room_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT,
                    messages INTEGER NOT NULL DEFAULT 0,
                    last_at REAL NOT NULL,
                    last_text TEXT,
                    PRIMARY KEY (room_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS profiles (
                    room_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT,
                    notes TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (room_id, user_id)
                );
                """
            )
            try:
                self._conn.execute(
                    "ALTER TABLE decisions ADD COLUMN probability REAL NOT NULL DEFAULT 0"
                )
            except Exception:  # noqa: BLE001
                pass
            self._conn.commit()

    # ---- 消息 ----
    def purge(self, message_days: int = 30, decision_days: int = 7) -> None:
        import time as _t

        now = _t.time()
        with self._lock:
            self._conn.execute(
                "DELETE FROM messages WHERE created_at < ?",
                (now - message_days * 86400,),
            )
            self._conn.execute(
                "DELETE FROM decisions WHERE created_at < ?",
                (now - decision_days * 86400,),
            )
            self._conn.commit()

    def add_message(
        self,
        room_id: str,
        room_name: str,
        sender: str,
        content: str,
        is_group: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (room_id, room_name, sender, content, is_group, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (room_id, room_name, sender, content, 1 if is_group else 0, time.time()),
            )
            self._conn.commit()

    def recent_messages(self, room_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sender, content, created_at FROM messages"
                " WHERE room_id = ? ORDER BY id DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_messages_since(self, room_id: str, minutes: int, limit: int = 30) -> list[dict]:
        since = time.time() - max(1, minutes) * 60
        with self._lock:
            rows = self._conn.execute(
                "SELECT sender, content, created_at FROM messages"
                " WHERE room_id = ? AND created_at >= ?"
                " ORDER BY id DESC LIMIT ?",
                (room_id, since, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def messages_since(self, room_id: str, since_ts: float, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, sender, content, is_group, created_at FROM messages"
                " WHERE room_id = ? AND created_at >= ? ORDER BY id ASC LIMIT ?",
                (room_id, since_ts, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 判定记录 ----
    def add_decision(
        self,
        room_id: str,
        sender: str,
        content: str,
        score: int,
        reason: str,
        accepted: bool,
        probability: float = 0.0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO decisions (room_id, sender, content, score, reason, accepted, probability, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    room_id, sender, content, score, reason,
                    1 if accepted else 0, probability, time.time(),
                ),
            )
            self._conn.commit()

    def recent_all(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, room_id, room_name, sender, content, is_group, created_at"
                " FROM messages ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_decisions(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def verdict_stats_today(self) -> dict:
        import datetime

        start = datetime.datetime.combine(
            datetime.date.today(), datetime.time.min
        ).timestamp()
        with self._lock:
            rows = self._conn.execute(
                "SELECT reason, accepted, COUNT(*) AS n FROM decisions"
                " WHERE created_at >= ? GROUP BY reason, accepted ORDER BY n DESC",
                (start,),
            ).fetchall()
        total = sum(row["n"] for row in rows)
        return {
            "total": total,
            "accepted": sum(row["n"] for row in rows if row["accepted"]),
            "items": [
                {
                    "reason": row["reason"],
                    "accepted": bool(row["accepted"]),
                    "count": row["n"],
                }
                for row in rows
            ],
        }

    # ---- 摘要 ----
    def get_summary(self, room_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT text FROM summaries WHERE room_id = ?", (room_id,)
            ).fetchone()
        return str(row["text"]) if row else ""

    def set_summary(self, room_id: str, text: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO summaries (room_id, text, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(room_id) DO UPDATE SET text = excluded.text,"
                " updated_at = excluded.updated_at",
                (room_id, text, time.time()),
            )
            self._conn.commit()

    # ---- 心情 ----
    def get_mood(self, room_id: str, default: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM moods WHERE room_id = ?", (room_id,)
            ).fetchone()
        return int(row["value"]) if row else default

    def get_mood_state(self, room_id: str, default: int) -> tuple[int, float]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value, updated_at FROM moods WHERE room_id = ?", (room_id,)
            ).fetchone()
        if not row:
            return default, time.time()
        return int(row["value"]), float(row["updated_at"])

    def set_mood(self, room_id: str, value: int) -> None:
        value = max(0, min(100, int(value)))
        with self._lock:
            self._conn.execute(
                "INSERT INTO moods (room_id, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(room_id) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (room_id, value, time.time()),
            )
            self._conn.commit()

    # ---- 人物档案 ----
    def remember_user(self, room_id: str, user_id: str, nickname: str, note: str = "") -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT notes FROM profiles WHERE room_id = ? AND user_id = ?",
                (room_id, user_id),
            ).fetchone()
            existing = str(row["notes"] or "") if row else ""
            merged = f"{existing}\n{note}".strip() if note else existing
            self._conn.execute(
                "INSERT INTO profiles (room_id, user_id, nickname, notes, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(room_id, user_id) DO UPDATE SET nickname = excluded.nickname,"
                " notes = excluded.notes, updated_at = excluded.updated_at",
                (room_id, user_id, nickname, merged, time.time()),
            )
            self._conn.commit()

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage (prompt_tokens, completion_tokens, created_at) VALUES (?, ?, ?)",
                (prompt_tokens, completion_tokens, time.time()),
            )
            self._conn.commit()

    def usage_stats(self) -> dict:
        today = time.time() - 86400
        with self._lock:
            total = self._conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0) AS p, COALESCE(SUM(completion_tokens),0) AS c, COUNT(*) AS n FROM usage"
            ).fetchone()
            recent = self._conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0) AS p, COALESCE(SUM(completion_tokens),0) AS c, COUNT(*) AS n"
                " FROM usage WHERE created_at >= ?",
                (today,),
            ).fetchone()
        return {
            "total": {"prompt": total["p"], "completion": total["c"], "calls": total["n"]},
            "day": {"prompt": recent["p"], "completion": recent["c"], "calls": recent["n"]},
        }

    def set_contact(self, user_id: str, nickname: str, relation: str, notes: str, is_owner: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO contacts (user_id, nickname, relation, notes, is_owner, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET nickname = excluded.nickname,"
                " relation = excluded.relation, notes = excluded.notes,"
                " is_owner = excluded.is_owner, updated_at = excluded.updated_at",
                (user_id, nickname, relation, notes, 1 if is_owner else 0, time.time()),
            )
            self._conn.commit()

    def get_contact(self, user_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM contacts WHERE user_id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else {}

    def list_contacts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM contacts ORDER BY is_owner DESC, updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_contact(self, user_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM contacts WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def has_outbox(self, room_id: str, text: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM outbox WHERE room_id = ? AND text = ? LIMIT 1",
                (room_id, text),
            ).fetchone()
        return row is not None

    def add_outbox(self, room_id: str, text: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO outbox (room_id, text, attempts, next_at, created_at)"
                " VALUES (?, ?, 0, ?, ?)",
                (room_id, text, time.time() + 30, time.time()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def all_outbox(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, room_id, text, attempts, created_at FROM outbox ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_outbox(self, outbox_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM outbox WHERE id = ?", (outbox_id,))
            self._conn.commit()

    def outbox_rooms(self) -> set[str]:
        """哪些会话已经有没发出去的回复（重启时先别急着重试这些会话）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT room_id FROM outbox"
            ).fetchall()
        return {str(row["room_id"]) for row in rows}

    def due_outbox(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, room_id, text, attempts, created_at FROM outbox"
                " WHERE next_at <= ? ORDER BY id LIMIT 3",
                (time.time(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def bump_outbox(self, outbox_id: int, attempts: int) -> None:
        with self._lock:
            if attempts >= 3:
                self._conn.execute("DELETE FROM outbox WHERE id = ?", (outbox_id,))
            else:
                self._conn.execute(
                    "UPDATE outbox SET attempts = ?, next_at = ? WHERE id = ?",
                    (attempts, time.time() + 60 * attempts, outbox_id),
                )
            self._conn.commit()

    def add_notice(self, room_id: str, text: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO notices (room_id, text, sent, created_at) VALUES (?, ?, 0, ?)",
                (room_id, text, time.time()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def take_notice(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, room_id, text FROM notices WHERE sent = 0 ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self._conn.execute("UPDATE notices SET sent = 1 WHERE id = ?", (row["id"],))
            self._conn.commit()
        return {"id": row["id"], "room_id": row["room_id"], "text": row["text"]}

    def add_pending(self, room_id: str, payload: dict) -> int:
        import json

        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO pending (room_id, payload, created_at) VALUES (?, ?, ?)",
                (room_id, json.dumps(payload, ensure_ascii=False, default=str), time.time()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_pending(self) -> list[dict]:
        import json

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, room_id, payload FROM pending ORDER BY id"
            ).fetchall()
        result = []
        for row in rows:
            try:
                message = json.loads(row["payload"])
            except Exception:  # noqa: BLE001
                continue
            message["_pending_id"] = row["id"]
            result.append(message)
        return result

    def drop_pending(self, pending_id: int) -> None:
        if not pending_id:
            return
        with self._lock:
            self._conn.execute("DELETE FROM pending WHERE id = ?", (pending_id,))
            self._conn.commit()

    def get_judge_cache(self, room_id: str, content_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT score, reason, probability, accepted, created_at FROM judge_cache"
                " WHERE room_id = ? AND content_key = ?",
                (room_id, content_key),
            ).fetchone()
        if not row:
            return None
        if time.time() - float(row["created_at"]) > 600:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM judge_cache WHERE room_id = ? AND content_key = ?",
                    (room_id, content_key),
                )
                self._conn.commit()
            return None
        return {
            "score": int(row["score"]),
            "reason": str(row["reason"]),
            "probability": float(row["probability"]),
            "accepted": bool(row["accepted"]),
        }

    def set_judge_cache(
        self, room_id: str, content_key: str, score: int, reason: str,
        probability: float, accepted: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO judge_cache (room_id, content_key, score, reason, probability, accepted, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(room_id, content_key) DO UPDATE SET"
                " score = excluded.score, reason = excluded.reason,"
                " probability = excluded.probability, accepted = excluded.accepted,"
                " created_at = excluded.created_at",
                (
                    room_id, content_key, score, reason, probability,
                    1 if accepted else 0, time.time(),
                ),
            )
            self._conn.commit()

    def is_processed(self, room_id: str, message_key: str) -> bool:
        if not message_key:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM processed WHERE room_id = ? AND message_key = ?",
                (room_id, message_key),
            ).fetchone()
        return row is not None

    def mark_processed(self, room_id: str, message_key: str) -> None:
        if not message_key:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO processed (room_id, message_key, created_at)"
                " VALUES (?, ?, ?)",
                (room_id, message_key, time.time()),
            )
            # 只留最近 3 天
            self._conn.execute(
                "DELETE FROM processed WHERE created_at < ?",
                (time.time() - 3 * 86400,),
            )
            self._conn.commit()

    def set_mode(self, room_id: str, mode: str, ttl_minutes: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO room_modes (room_id, mode, expires_at) VALUES (?, ?, ?)"
                " ON CONFLICT(room_id) DO UPDATE SET mode = excluded.mode,"
                " expires_at = excluded.expires_at",
                (room_id, mode, time.time() + max(1, ttl_minutes) * 60),
            )
            self._conn.commit()

    def get_mode(self, room_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT mode, expires_at FROM room_modes WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            if not row:
                return ""
            if float(row["expires_at"]) < time.time():
                self._conn.execute("DELETE FROM room_modes WHERE room_id = ?", (room_id,))
                self._conn.commit()
                return ""
        return str(row["mode"])

    def get_group_memory(self, room_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT text, region, updated_at FROM group_memory WHERE room_id = ?",
                (room_id,),
            ).fetchone()
        if not row:
            return {"text": "", "region": "", "updated_at": 0.0}
        return {"text": str(row["text"]), "region": str(row["region"]), "updated_at": float(row["updated_at"])}

    def set_group_memory(self, room_id: str, text: str, region: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO group_memory (room_id, text, region, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(room_id) DO UPDATE SET text = excluded.text,"
                " region = excluded.region, updated_at = excluded.updated_at",
                (room_id, text, region, time.time()),
            )
            self._conn.commit()

    def delete_group_memory(self, room_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM group_memory WHERE room_id = ?", (room_id,))
            self._conn.commit()

    def all_group_memory(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT room_id, text, region, updated_at FROM group_memory"
            ).fetchall()
        return [dict(row) for row in rows]

    def bump_user(self, room_id: str, user_id: str, nickname: str, text: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO user_stats (room_id, user_id, nickname, messages, last_at, last_text)"
                " VALUES (?, ?, ?, 1, ?, ?)"
                " ON CONFLICT(room_id, user_id) DO UPDATE SET"
                " messages = messages + 1, last_at = excluded.last_at,"
                " nickname = excluded.nickname, last_text = excluded.last_text",
                (room_id, user_id, nickname, time.time(), text[:60]),
            )
            self._conn.commit()

    def top_users(self, room_id: str, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, nickname, messages, last_at, last_text"
                " FROM user_stats WHERE room_id = ?"
                " ORDER BY messages DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def user_notes(self, room_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, nickname, notes FROM profiles"
                " WHERE room_id = ? AND notes != '' LIMIT ?",
                (room_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 导出（回放用） ----
    def export_room(self, room_id: str, day: str | None = None) -> list[dict]:
        sql = "SELECT * FROM messages WHERE room_id = ?"
        args: list = [room_id]
        if day:
            start = time.mktime(time.strptime(day, "%Y-%m-%d"))
            sql += " AND created_at >= ? AND created_at < ?"
            args.extend([start, start + 86400])
        sql += " ORDER BY id ASC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict:
        with self._lock:
            messages = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            decisions = self._conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
            accepted = self._conn.execute(
                "SELECT COUNT(*) AS n FROM decisions WHERE accepted = 1"
            ).fetchone()["n"]
        return {
            "messages": messages,
            "decisions": decisions,
            "accepted": accepted,
            "blocked": decisions - accepted,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def dumps(self, payload: object) -> str:
        return json.dumps(payload, ensure_ascii=False)

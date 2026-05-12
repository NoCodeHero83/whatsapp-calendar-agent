"""
Per-user conversation state for multi-turn appointment booking.
Uses SQLite so state survives server restarts.
Also tracks processed WhatsApp message IDs for idempotency.
"""

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "agent.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_state (
                phone       TEXT PRIMARY KEY,
                stage       TEXT NOT NULL DEFAULT 'collecting_info',
                data        TEXT NOT NULL DEFAULT '{}',
                updated_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id   TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            )
        """)
        conn.commit()
    logger.info(f"SQLite state DB ready at {_DB_PATH}")


_init_db()


@dataclass
class UserState:
    data: dict = field(default_factory=dict)
    stage: str = "collecting_info"


# ── Conversation state ────────────────────────────────────────────────────────

def get_state(phone: str) -> UserState:
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT stage, data FROM conversation_state WHERE phone = ?",
                (phone,),
            ).fetchone()
    if row:
        return UserState(stage=row["stage"], data=json.loads(row["data"]))
    return UserState()


def save_state(phone: str, state: UserState) -> None:
    with _lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_state (phone, stage, data, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    stage      = excluded.stage,
                    data       = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (phone, state.stage, json.dumps(state.data), datetime.utcnow().isoformat()),
            )
            conn.commit()
    logger.debug(f"State saved — phone={phone} stage={state.stage} fields={list(state.data.keys())}")


def clear_state(phone: str) -> None:
    with _lock:
        with _connect() as conn:
            conn.execute("DELETE FROM conversation_state WHERE phone = ?", (phone,))
            conn.commit()
    logger.info(f"Conversation state cleared for {phone}")


# ── Idempotency ───────────────────────────────────────────────────────────────

def is_message_processed(message_id: str) -> bool:
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
    return row is not None


def mark_message_processed(message_id: str) -> None:
    with _lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
                (message_id, datetime.utcnow().isoformat()),
            )
            conn.commit()
    logger.debug(f"Message {message_id} marked as processed")


# ── Helpers ───────────────────────────────────────────────────────────────────

def merge_data(existing: dict, new_data: dict) -> dict:
    """Merge extracted fields into accumulated data. Non-None values win."""
    merged = dict(existing)
    for key, value in new_data.items():
        if value is not None:
            merged[key] = value
    return merged

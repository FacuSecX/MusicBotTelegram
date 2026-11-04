# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Durable, owner-scoped copies of a playlist for stable playback navigation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import secrets
import sqlite3

from .storage import Store

SESSION_ID = re.compile(r"^[0-9a-f]{1,16}$")
MAX_SESSIONS = 100
MAX_TRACKS = 1000


class PlaybackSessions:
    def __init__(self, store: Store):
        self.store = store
        self.db = store.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS playback_sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                scope TEXT NOT NULL,
                name TEXT NOT NULL,
                tracks_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS playback_sessions_owner
                ON playback_sessions(user_id);
            CREATE TABLE IF NOT EXISTS playback_deliveries (
                session_id TEXT PRIMARY KEY REFERENCES playback_sessions(id) ON DELETE CASCADE,
                chat_id INTEGER NOT NULL,
                complete INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS playback_alternatives (
                session_id TEXT NOT NULL REFERENCES playback_sessions(id) ON DELETE CASCADE,
                track_index INTEGER NOT NULL,
                track_json TEXT NOT NULL,
                PRIMARY KEY(session_id, track_index)
            );
            CREATE TABLE IF NOT EXISTS playback_messages (
                session_id TEXT NOT NULL REFERENCES playback_deliveries(session_id) ON DELETE CASCADE,
                track_index INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY(session_id, track_index)
            );
        """)

    @staticmethod
    def _decode(row) -> dict:
        return dict(id=row["id"], user_id=row["user_id"], scope=row["scope"],
                    name=row["name"], tracks=json.loads(row["tracks_json"]),
                    created_at=row["created_at"])

    def create(self, user_id: int, scope: str) -> dict:
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise ValueError("El usuario no es válido.")
        # A savepoint starts a read transaction before looking up name/members,
        # so the stored name and the ordered tracks belong to the same snapshot.
        self.db.execute("SAVEPOINT playback_create")
        try:
            name = self.store.scope_name(user_id, scope)
            tracks = self.store.snapshot(user_id, scope)
            if name is None or not tracks:
                raise ValueError("La playlist no existe, está vacía o no te pertenece.")
            if len(tracks) > MAX_TRACKS:
                raise ValueError(f"La playlist supera el límite de {MAX_TRACKS} canciones por reproducción.")
            payload = json.dumps(tracks, ensure_ascii=False, separators=(",", ":"))
            created_at = datetime.now(timezone.utc).isoformat()
            for attempt in range(3):
                session_id = secrets.token_hex(6)
                try:
                    self.db.execute("""INSERT INTO playback_sessions
                        (id,user_id,scope,name,tracks_json,created_at) VALUES (?,?,?,?,?,?)""",
                        (session_id, user_id, scope, name, payload, created_at))
                    break
                except sqlite3.IntegrityError:
                    if attempt == 2:
                        raise
            self.db.execute("""DELETE FROM playback_sessions WHERE user_id=?
                AND id NOT IN (SELECT session_id FROM playback_deliveries)
                AND id NOT IN
                (SELECT id FROM playback_sessions WHERE user_id=? ORDER BY rowid DESC LIMIT ?)""",
                (user_id, user_id, MAX_SESSIONS))
            self.db.execute("RELEASE SAVEPOINT playback_create")
        except BaseException:
            self.db.execute("ROLLBACK TO SAVEPOINT playback_create")
            self.db.execute("RELEASE SAVEPOINT playback_create")
            raise
        # Decode the serialized payload, avoiding shared references to callers.
        return dict(id=session_id, user_id=user_id, scope=scope, name=name,
                    tracks=json.loads(payload), created_at=created_at)

    def get(self, user_id: int, session_id: str) -> dict | None:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            return None
        row = self.db.execute("SELECT * FROM playback_sessions WHERE id=? AND user_id=?",
                              (session_id, user_id)).fetchone()
        return self._decode(row) if row else None

    def get_track(self, user_id: int, session_id: str, index: int) -> dict | None:
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            return None
        session = self.get(user_id, session_id)
        if session is None or index >= len(session["tracks"]):
            return None
        return session["tracks"][index]

    @staticmethod
    def _same_playlist(session: dict, name: str, tracks: list[dict]) -> bool:
        # Cached file identifiers and metadata can change after an upload.
        # Only library membership/order and the displayed list name define reuse.
        return session["name"] == name and [t["id"] for t in session["tracks"]] == [t["id"] for t in tracks]

    def _delivery(self, session: dict, row) -> dict:
        ids = [None] * len(session["tracks"])
        for item in self.db.execute(
                "SELECT track_index,message_id FROM playback_messages WHERE session_id=?",
                (session["id"],)):
            if 0 <= item["track_index"] < len(ids):
                ids[item["track_index"]] = item["message_id"]
        return dict(session, chat_id=row["chat_id"], complete=bool(row["complete"]), message_ids=ids)

    def find_delivery(self, user_id: int, chat_id: int, scope: str) -> dict | None:
        name = self.store.scope_name(user_id, scope)
        tracks = self.store.snapshot(user_id, scope)
        if name is None or not tracks:
            raise ValueError("La playlist no existe, está vacía o no te pertenece.")
        rows = self.db.execute("""SELECT s.*,d.chat_id,d.complete FROM playback_sessions s
            JOIN playback_deliveries d ON d.session_id=s.id
            WHERE s.user_id=? AND s.scope=? AND d.chat_id=?
            ORDER BY s.rowid DESC""", (user_id, scope, chat_id))
        for row in rows:
            session = self._decode(row)
            if self._same_playlist(session, name, tracks):
                return self._delivery(session, row)
        return None

    def start_delivery(self, user_id: int, session_id: str, chat_id: int) -> dict:
        session = self.get(user_id, session_id)
        if session is None:
            raise ValueError("La reproducción no existe o no te pertenece.")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or not chat_id:
            raise ValueError("El chat no es válido.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO playback_deliveries(session_id,chat_id) VALUES (?,?)",
                            (session_id, chat_id))
        row = self.db.execute("SELECT * FROM playback_deliveries WHERE session_id=?", (session_id,)).fetchone()
        if row["chat_id"] != chat_id:
            raise ValueError("La reproducción pertenece a otro chat.")
        return self._delivery(session, row)

    def record_message(self, user_id: int, session_id: str, chat_id: int,
                       index: int, message_id: int) -> None:
        session = self.get(user_id, session_id)
        row = self.db.execute("SELECT * FROM playback_deliveries WHERE session_id=?", (session_id,)).fetchone()
        if session is None or row is None or row["chat_id"] != chat_id:
            raise ValueError("La reproducción no existe o no pertenece a este chat.")
        if (not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(session["tracks"])
                or not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0):
            raise ValueError("El mensaje de audio no es válido.")
        with self.db:
            self.db.execute("INSERT INTO playback_messages(session_id,track_index,message_id) VALUES (?,?,?)",
                            (session_id, index, message_id))

    def complete_delivery(self, user_id: int, session_id: str, chat_id: int) -> dict:
        session = self.get(user_id, session_id)
        row = self.db.execute("SELECT * FROM playback_deliveries WHERE session_id=?", (session_id,)).fetchone()
        if session is None or row is None or row["chat_id"] != chat_id:
            raise ValueError("La reproducción no existe o no pertenece a este chat.")
        result = self._delivery(session, row)
        ids = result["message_ids"]
        if any(mid is None for mid in ids) or any(a <= b for a,b in zip(ids,ids[1:])):
            raise ValueError("La playlist todavía no está completa o sus audios no están en orden.")
        with self.db:
            self.db.execute("UPDATE playback_deliveries SET complete=1 WHERE session_id=?", (session_id,))
        result["complete"] = True
        return result

    def invalidate_messages(self, user_id: int, chat_id: int, message_ids) -> list[str]:
        ids = sorted({mid for mid in message_ids if isinstance(mid,int) and not isinstance(mid,bool) and mid > 0})
        affected = set()
        for start in range(0,len(ids),100):
            batch=ids[start:start+100]
            placeholders=','.join('?' for _ in batch)
            rows=self.db.execute(f"""SELECT DISTINCT d.session_id FROM playback_deliveries d
                JOIN playback_sessions s ON s.id=d.session_id
                JOIN playback_messages m ON m.session_id=d.session_id
                WHERE s.user_id=? AND d.chat_id=? AND m.message_id IN ({placeholders})""",(user_id,chat_id,*batch))
            affected.update(row[0] for row in rows)
        with self.db:
            # Only message mappings are retired. Snapshots, alternatives, tracks,
            # Telegram file references and the user's library are retained.
            self.db.executemany('DELETE FROM playback_deliveries WHERE session_id=?',[(sid,) for sid in affected])
        return sorted(affected)

    def protected_message_ids(self, user_id: int, chat_id: int) -> set[int]:
        """All known audio messages, including partially sent playlists."""
        rows = self.db.execute("""SELECT m.message_id FROM playback_messages m
            JOIN playback_deliveries d ON d.session_id=m.session_id
            JOIN playback_sessions s ON s.id=m.session_id
            WHERE s.user_id=? AND d.chat_id=?""", (user_id, chat_id))
        return {row[0] for row in rows}

    def set_alternative(self, user_id: int, session_id: str, index: int, track: dict) -> None:
        """Apply a user's explicit version choice only to one unsent slot.

        The library and the original snapshot remain unchanged: reuse matching
        still identifies the original playlist after a version is chosen.
        """
        original = self.get_track(user_id, session_id, index)
        if original is None:
            raise ValueError("La canción no existe o no te pertenece.")
        if self.db.execute("SELECT 1 FROM playback_messages WHERE session_id=? AND track_index=?",
                           (session_id, index)).fetchone():
            raise ValueError("Esta canción ya fue enviada en la playlist.")
        if (not isinstance(track, dict) or not all(track.get(key) for key in ("id", "title", "url"))
                or self.store.get_track(track["id"]) is None):
            raise ValueError("La versión alternativa no está disponible.")
        payload = {key: track.get(key) for key in ("id", "title", "artist", "duration", "url")}
        payload["artist"] = payload["artist"] or ""
        with self.db:
            self.db.execute("""INSERT INTO playback_alternatives(session_id,track_index,track_json)
                VALUES (?,?,?) ON CONFLICT(session_id,track_index) DO UPDATE SET track_json=excluded.track_json""",
                (session_id, index, json.dumps(payload, ensure_ascii=False)))

    def get_alternative(self, user_id: int, session_id: str, index: int) -> dict | None:
        if self.get_track(user_id, session_id, index) is None:
            return None
        row = self.db.execute("SELECT track_json FROM playback_alternatives WHERE session_id=? AND track_index=?",
                              (session_id, index)).fetchone()
        return json.loads(row[0]) if row else None

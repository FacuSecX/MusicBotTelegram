# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Persistent music library, scoped by Telegram user id."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                duration INTEGER NOT NULL DEFAULT 0,
                url TEXT NOT NULL,
                file_id TEXT
            );
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                track_id TEXT NOT NULL REFERENCES tracks(id),
                UNIQUE(user_id, track_id)
            );
            CREATE INDEX IF NOT EXISTS favorites_user_order ON favorites(user_id, id);
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                UNIQUE(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                track_id TEXT NOT NULL REFERENCES tracks(id),
                UNIQUE(playlist_id, track_id)
            );
            CREATE INDEX IF NOT EXISTS playlist_tracks_order ON playlist_tracks(playlist_id, id);
        """)

    def upsert_track(self, track: dict) -> None:
        if not track.get("id") or not track.get("title") or not track.get("url"):
            raise ValueError("La canción debe tener id, título y URL.")
        with self.db:
            self.db.execute("""
                INSERT INTO tracks(id,title,artist,duration,url) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, artist=excluded.artist,
                    duration=excluded.duration, url=excluded.url
            """, (track["id"], track["title"], track.get("artist") or "",
                  int(track.get("duration") or 0), track["url"]))

    def get_track(self, track_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        return dict(row) if row else None

    def set_file_id(self, track_id: str, file_id: str) -> None:
        with self.db:
            result = self.db.execute("UPDATE tracks SET file_id=? WHERE id=?", (file_id, track_id))
            if not result.rowcount:
                raise ValueError("La canción no existe.")

    def get_file_id(self, track_id: str) -> str | None:
        row = self.db.execute("SELECT file_id FROM tracks WHERE id=?", (track_id,)).fetchone()
        return row[0] if row else None

    def is_favorite(self, user_id: int, track_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM favorites WHERE user_id=? AND track_id=?",
                               (user_id, track_id)).fetchone() is not None

    def toggle_favorite(self, user_id: int, track_id: str) -> bool:
        with self.db:
            removed = self.db.execute("DELETE FROM favorites WHERE user_id=? AND track_id=?",
                                      (user_id, track_id)).rowcount
            if removed:
                return False
            self.db.execute("INSERT INTO favorites(user_id,track_id) VALUES (?,?)", (user_id, track_id))
            return True

    @staticmethod
    def _page(offset: int, limit: int) -> tuple[int, int]:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("La página debe tener un límite de 1 a 100 y un inicio positivo.")
        return offset, limit

    def favorites(self, user_id: int, offset: int = 0, limit: int = 6) -> tuple[list[dict], int]:
        offset, limit = self._page(offset, limit)
        total = self.db.execute("SELECT COUNT(*) FROM favorites WHERE user_id=?", (user_id,)).fetchone()[0]
        rows = self.db.execute("""SELECT t.* FROM favorites f JOIN tracks t ON t.id=f.track_id
            WHERE f.user_id=? ORDER BY f.id DESC LIMIT ? OFFSET ?""", (user_id, limit, offset))
        return [dict(row) for row in rows], total

    def create_playlist(self, user_id: int, name: str) -> int:
        name = name.strip()
        if not 1 <= len(name) <= 60:
            raise ValueError("El nombre debe tener entre 1 y 60 caracteres.")
        try:
            with self.db:
                result = self.db.execute("INSERT INTO playlists(user_id,name) VALUES (?,?)", (user_id, name))
                return result.lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError("Ya tenés una playlist con ese nombre.") from exc

    def playlists(self, user_id: int) -> list[dict]:
        rows = self.db.execute("""SELECT p.id,p.name,COUNT(pt.id) AS count FROM playlists p
            LEFT JOIN playlist_tracks pt ON pt.playlist_id=p.id WHERE p.user_id=?
            GROUP BY p.id ORDER BY p.id""", (user_id,))
        return [dict(row) for row in rows]

    def delete_playlist(self, user_id: int, playlist_id: int) -> bool:
        with self.db:
            return bool(self.db.execute("DELETE FROM playlists WHERE id=? AND user_id=?",
                                        (playlist_id, user_id)).rowcount)

    def _owns(self, user_id: int, playlist_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM playlists WHERE id=? AND user_id=?",
                               (playlist_id, user_id)).fetchone() is not None

    def add_to_playlist(self, user_id: int, playlist_id: int, track_id: str) -> bool:
        with self.db:
            result = self.db.execute("""INSERT OR IGNORE INTO playlist_tracks(playlist_id,track_id)
                SELECT id,? FROM playlists WHERE id=? AND user_id=?""", (track_id, playlist_id, user_id))
            return bool(result.rowcount)

    def remove_from_playlist(self, user_id: int, playlist_id: int, track_id: str) -> bool:
        with self.db:
            result = self.db.execute("""DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?
                AND EXISTS(SELECT 1 FROM playlists WHERE id=? AND user_id=?)""",
                (playlist_id, track_id, playlist_id, user_id))
            return bool(result.rowcount)

    def playlist_tracks(self, user_id: int, playlist_id: int, offset: int = 0,
                        limit: int = 6) -> tuple[list[dict], int]:
        offset, limit = self._page(offset, limit)
        if not self._owns(user_id, playlist_id):
            return [], 0
        total = self.db.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id=?",
                                (playlist_id,)).fetchone()[0]
        rows = self.db.execute("""SELECT t.* FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id
            WHERE pt.playlist_id=? ORDER BY pt.id LIMIT ? OFFSET ?""", (playlist_id, limit, offset))
        return [dict(row) for row in rows], total

    @staticmethod
    def _scope_playlist_id(scope: str) -> int | None:
        if scope == "f":
            return None
        if not isinstance(scope, str) or not re.fullmatch(r"p[1-9][0-9]*", scope):
            raise ValueError("La lista indicada no es válida.")
        playlist_id = int(scope[1:])
        if playlist_id > 9223372036854775807:
            raise ValueError("La lista indicada no es válida.")
        return playlist_id

    def snapshot(self, user_id: int, scope: str) -> list[dict]:
        """Copy the complete ordered list using one ownership-scoped SELECT.

        A playback session can retain this copy while the stored list changes.
        No pagination or second membership query can change its boundaries.
        """
        playlist_id = self._scope_playlist_id(scope)
        if playlist_id is None:
            rows = self.db.execute("""SELECT t.* FROM favorites f
                JOIN tracks t ON t.id=f.track_id
                WHERE f.user_id=? ORDER BY f.id DESC""", (user_id,))
        else:
            rows = self.db.execute("""SELECT t.* FROM playlist_tracks pt
                JOIN playlists p ON p.id=pt.playlist_id
                JOIN tracks t ON t.id=pt.track_id
                WHERE p.user_id=? AND p.id=? ORDER BY pt.id""",
                (user_id, playlist_id))
        return [dict(row) for row in rows]

    def scope_name(self, user_id: int, scope: str) -> str | None:
        playlist_id = self._scope_playlist_id(scope)
        if playlist_id is None:
            return "Favoritos"
        row = self.db.execute("SELECT name FROM playlists WHERE user_id=? AND id=?",
                              (user_id, playlist_id)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.db.close()

# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Portable playlist data only: no environment, credentials, or chat history."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3


FORMAT = 'musicbot-playlists'
VERSION = 1
MAX_BACKUP_BYTES = 10 * 1024 * 1024
MAX_TRACKS = 20_000
MAX_PLAYLISTS = 50
MAX_PLAYLIST_TRACKS = 1_000
_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
_FILE_ID = re.compile(r'^[A-Za-z0-9_-]{1,2048}$')
_REQUIRED = frozenset({'format', 'version', 'created_at', 'tracks', 'favorites', 'playlists'})


class BackupError(Exception):
    def __init__(self, kind: str, detail: str):
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


def _invalid():
    return BackupError('invalid_backup', 'El archivo no es un backup válido de playlists de Music Bot.')


def _positive_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2**63 - 1


def _text(value, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or ('\x00' in value):
        raise _invalid()
    if not empty and not value.strip():
        raise _invalid()
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise _invalid() from None
    return value


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required).issubset(value) or set(value) - set(required) - set(optional):
        raise _invalid()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _constant(value):
    raise _invalid()


def _track_ids(value, known, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise _invalid()
    seen = set()
    for ident in value:
        if not isinstance(ident, str) or ident not in known or ident in seen:
            raise _invalid()
        seen.add(ident)
    return list(value)


def validate_backup(data: bytes) -> dict:
    """Validate the complete file before any receiving library is changed."""
    if not isinstance(data, bytes) or not data:
        raise _invalid()
    if len(data) > MAX_BACKUP_BYTES:
        raise BackupError('too_large', 'El backup supera el límite de 10 MB.')
    try:
        payload = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    except BackupError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _invalid() from None
    _object(payload, _REQUIRED, {'bot_id'})
    if payload['format'] != FORMAT or type(payload['version']) is not int or payload['version'] != VERSION:
        raise _invalid()
    created = _text(payload['created_at'], 40)
    try:
        stamp = datetime.fromisoformat(created.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        normalized_stamp = stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise _invalid() from None
    bot_id = payload.get('bot_id')
    if 'bot_id' in payload and not _positive_id(bot_id):
        raise _invalid()
    raw_tracks = payload['tracks']
    if not isinstance(raw_tracks, list) or len(raw_tracks) > MAX_TRACKS:
        raise _invalid()
    tracks, known = [], set()
    for raw in raw_tracks:
        _object(raw, {'id', 'title', 'artist', 'duration', 'url'}, {'file_id'})
        ident = raw['id']
        if not isinstance(ident, str) or not _ID.fullmatch(ident) or ident in known:
            raise _invalid()
        url = f'https://www.youtube.com/watch?v={ident}'
        if raw['url'] != url:
            raise _invalid()
        if type(raw['duration']) is not int or not 0 <= raw['duration'] <= 864_000:
            raise _invalid()
        item = dict(id=ident, title=_text(raw['title'], 500), artist=_text(raw['artist'], 300, empty=True),
                    duration=raw['duration'], url=url)
        if 'file_id' in raw:
            if bot_id is None or not isinstance(raw['file_id'], str) or not _FILE_ID.fullmatch(raw['file_id']):
                raise _invalid()
            item['file_id'] = raw['file_id']
        known.add(ident)
        tracks.append(item)
    favorites = _track_ids(payload['favorites'], known, MAX_TRACKS)
    raw_playlists = payload['playlists']
    if not isinstance(raw_playlists, list) or len(raw_playlists) > MAX_PLAYLISTS:
        raise _invalid()
    playlists, names, referenced = [], set(), set(favorites)
    for raw in raw_playlists:
        _object(raw, {'name', 'tracks'})
        name = _text(raw['name'], 60).strip()
        if name in names:
            raise _invalid()
        names.add(name)
        members = _track_ids(raw['tracks'], known, MAX_PLAYLIST_TRACKS)
        referenced.update(members)
        playlists.append({'name': name, 'tracks': members})
    # A library backup cannot be used to inject an unrelated global catalogue.
    if known != referenced:
        raise _invalid()
    normalized = dict(format=FORMAT, version=VERSION, created_at=normalized_stamp,
                      tracks=tracks, favorites=favorites, playlists=playlists)
    if bot_id is not None:
        normalized['bot_id'] = bot_id
    return normalized


def export_library(store, user_id: int, *, bot_id: int | None = None) -> bytes:
    """Take one owner-scoped snapshot; export only explicitly allowed fields."""
    if not _positive_id(user_id) or (bot_id is not None and not _positive_id(bot_id)):
        raise BackupError('configuration', 'No se pudo identificar la biblioteca para el backup.')
    db = store.db
    db.execute('SAVEPOINT musicbot_library_export')
    try:
        favorites = [row['track_id'] for row in db.execute(
            'SELECT track_id FROM favorites WHERE user_id=? ORDER BY id DESC LIMIT ?', (user_id, MAX_TRACKS + 1))]
        if len(favorites) > MAX_TRACKS:
            raise _invalid()
        playlists = []
        for row in db.execute('SELECT id,name FROM playlists WHERE user_id=? ORDER BY id LIMIT ?', (user_id, MAX_PLAYLISTS + 1)):
            members = [item['track_id'] for item in db.execute(
                'SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY id LIMIT ?', (row['id'], MAX_PLAYLIST_TRACKS + 1))]
            playlists.append({'name': row['name'], 'tracks': members})
        tracks = []
        # One scoped join avoids scanning or exporting another user's library.
        for row in db.execute('''SELECT t.* FROM tracks t WHERE t.id IN
            (SELECT track_id FROM favorites WHERE user_id=? UNION
             SELECT pt.track_id FROM playlist_tracks pt JOIN playlists p ON p.id=pt.playlist_id WHERE p.user_id=?)
            ORDER BY t.id LIMIT ?''', (user_id, user_id, MAX_TRACKS + 1)):
            item = dict(id=row['id'], title=row['title'], artist=row['artist'] or '', duration=row['duration'] or 0,
                        url=f"https://www.youtube.com/watch?v={row['id']}")
            if bot_id is not None and row['file_id']:
                item['file_id'] = row['file_id']
            tracks.append(item)
        db.execute('RELEASE SAVEPOINT musicbot_library_export')
    except sqlite3.Error:
        db.execute('ROLLBACK TO SAVEPOINT musicbot_library_export')
        db.execute('RELEASE SAVEPOINT musicbot_library_export')
        raise BackupError('export_failed', 'No se pudo preparar el backup de tu biblioteca.') from None
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT musicbot_library_export')
        db.execute('RELEASE SAVEPOINT musicbot_library_export')
        raise
    payload = dict(format=FORMAT, version=VERSION, created_at=datetime.now(timezone.utc).isoformat(),
                   tracks=tracks, favorites=favorites, playlists=playlists)
    if bot_id is not None:
        payload['bot_id'] = bot_id
    try:
        data = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        raise _invalid() from None
    validate_backup(data)
    return data


def import_library(store, user_id: int, data: bytes, *, bot_id: int | None = None) -> dict:
    """Atomically replace one user's lists, while keeping other users intact."""
    payload = validate_backup(data)
    if not _positive_id(user_id) or (bot_id is not None and not _positive_id(bot_id)):
        raise BackupError('configuration', 'No se pudo identificar la biblioteca de destino.')
    same_bot = bot_id is not None and payload.get('bot_id') == bot_id
    db = store.db
    restored = 0
    db.execute('SAVEPOINT musicbot_library_import')
    try:
        db.execute('DELETE FROM favorites WHERE user_id=?', (user_id,))
        db.execute('DELETE FROM playlists WHERE user_id=?', (user_id,))
        for item in payload['tracks']:
            # Existing metadata/file IDs belong to the receiving bot and may
            # also be in use by other users. Never overwrite them on import.
            db.execute('''INSERT OR IGNORE INTO tracks(id,title,artist,duration,url)
                VALUES(?,?,?,?,?)''', (item['id'], item['title'], item['artist'], item['duration'], item['url']))
            if same_bot and item.get('file_id'):
                result = db.execute("UPDATE tracks SET file_id=? WHERE id=? AND COALESCE(file_id,'')=''",
                                    (item['file_id'], item['id']))
                restored += result.rowcount
        # The UI displays favorites by descending insertion ID.
        db.executemany('INSERT INTO favorites(user_id,track_id) VALUES(?,?)',
                       [(user_id, ident) for ident in reversed(payload['favorites'])])
        for playlist in payload['playlists']:
            cursor = db.execute('INSERT INTO playlists(user_id,name) VALUES(?,?)', (user_id, playlist['name']))
            db.executemany('INSERT INTO playlist_tracks(playlist_id,track_id) VALUES(?,?)',
                           [(cursor.lastrowid, ident) for ident in playlist['tracks']])
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='playback_sessions'").fetchone():
            db.execute('DELETE FROM playback_sessions WHERE user_id=?', (user_id,))
        db.execute('RELEASE SAVEPOINT musicbot_library_import')
    except sqlite3.Error:
        db.execute('ROLLBACK TO SAVEPOINT musicbot_library_import')
        db.execute('RELEASE SAVEPOINT musicbot_library_import')
        raise BackupError('import_failed', 'No se pudo importar el backup. Tu biblioteca anterior se conservó.') from None
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT musicbot_library_import')
        db.execute('RELEASE SAVEPOINT musicbot_library_import')
        raise
    return dict(tracks=len(payload['tracks']), favorites=len(payload['favorites']), playlists=len(payload['playlists']),
                playlist_tracks=sum(len(item['tracks']) for item in payload['playlists']), file_ids_restored=restored)

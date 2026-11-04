# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Owner-scoped message cleanup with explicit opt-in deletion of audio cards."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import math
import time

from telegram.error import BadRequest, RetryAfter, TelegramError

CLEANABLE_KINDS = frozenset({"text", "menu", "search", "status", "command", "temporary_panel"})
PROTECTED_KINDS = frozenset({"audio", "protected", "panel"})
# Leave a minute for requests in flight before Telegram's strict 48-hour cutoff.
DELETE_AGE_SECONDS = 48 * 60 * 60 - 60


def _timestamp(value=None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("La fecha del mensaje no es válida.")
    return float(value)


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class ChatHistory:
    """Track known messages only. Never guess IDs from Telegram chat history."""

    def __init__(self, store):
        self.db = store.db
        self._cleanup_lock = asyncio.Lock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chat_history (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(chat_id,message_id)
            );
            CREATE INDEX IF NOT EXISTS chat_history_owner
                ON chat_history(user_id,chat_id,created_at);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(chat_history)")}
        if "due_at" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE chat_history ADD COLUMN due_at REAL")
        self.db.execute("CREATE INDEX IF NOT EXISTS chat_history_expiration ON chat_history(due_at)")
        # Migrate known legacy texts into the same temporary-message policy.
        # Untracked IDs are never inferred, and all audio/media remain protected.
        self.db.execute("""UPDATE chat_history SET due_at=created_at +
            CASE WHEN kind='command' THEN 30 ELSE 90 END
            WHERE due_at IS NULL AND kind IN ('text','menu','search','status','command')""")
        self.db.commit()

    def record(self, user_id, chat_id, message_id, kind="text", created_at=None) -> bool:
        if (not _positive_int(user_id) or not _positive_int(message_id)
                or not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0):
            raise ValueError("El mensaje o su propietario no son válidos.")
        if kind not in CLEANABLE_KINDS | PROTECTED_KINDS:
            raise ValueError("El tipo de mensaje no es válido.")
        stamp = _timestamp(created_at)
        with self.db:
            result = self.db.execute("""INSERT INTO chat_history
                (user_id,chat_id,message_id,kind,created_at) VALUES (?,?,?,?,?)
                ON CONFLICT(chat_id,message_id) DO UPDATE SET
                    kind=CASE WHEN chat_history.kind IN ('audio','protected','panel')
                        THEN chat_history.kind ELSE excluded.kind END,
                    created_at=MIN(chat_history.created_at,excluded.created_at)
                WHERE chat_history.user_id=excluded.user_id""",
                (user_id, chat_id, message_id, kind, stamp))
        return bool(result.rowcount)

    def record_message(self, user_id, message, kind=None) -> bool:
        # Any media is protected by default, even if its caption resembles text.
        media = any(getattr(message, field, None) for field in
                    ("audio", "voice", "video", "document", "photo", "animation",
                     "video_note", "sticker", "paid_media"))
        if getattr(message, "audio", None):
            kind = "audio"
        elif media:
            kind = "protected"
        elif kind is None:
            kind = "text"
        return self.record(user_id, message.chat_id, message.message_id,
                           kind, getattr(message, "date", None))

    async def _call(self, operation):
        try:
            return await operation()
        except RetryAfter as exc:
            delay = exc.retry_after
            seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            if not math.isfinite(seconds) or seconds < 0 or seconds + 0.2 > 30:
                raise
            await asyncio.sleep(seconds + 0.2)
            return await operation()

    def _forget(self, user_id, chat_id, ids):
        if not ids:
            return
        with self.db:
            self.db.executemany("""DELETE FROM chat_history
                WHERE user_id=? AND chat_id=? AND message_id=?
                    AND kind IN ('text','menu','search','status','command','temporary_panel')""",
                [(user_id, chat_id, mid) for mid in ids])

    def forget_disposable(self, user_id, chat_id, ids):
        self._forget(user_id, chat_id, ids)

    @staticmethod
    def _delay(seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, (int,float)) or not math.isfinite(seconds) or not 1 <= seconds <= DELETE_AGE_SECONDS:
            raise ValueError("El tiempo del mensaje no es válido.")
        return float(seconds)

    def schedule(self, user_id, chat_id, message_id, seconds, *, now=None):
        due = _timestamp(now) + self._delay(seconds)
        with self.db:
            result = self.db.execute("""UPDATE chat_history SET due_at=?
                WHERE user_id=? AND chat_id=? AND message_id=?
                AND kind IN ('text','menu','search','status','command','temporary_panel')""",
                (due,user_id,chat_id,message_id))
        return bool(result.rowcount)

    def protect_panel(self, user_id, chat_id, message_id):
        self.record(user_id,chat_id,message_id,kind="panel")
        with self.db:
            self.db.execute("""UPDATE chat_history SET kind='panel',due_at=NULL
                WHERE user_id=? AND chat_id=? AND message_id=?
                AND kind IN ('text','menu','search','status','command','panel','temporary_panel')""",
                (user_id,chat_id,message_id))

    def schedule_panel(self, user_id, chat_id, message_id, seconds, *, now=None):
        due = _timestamp(now) + self._delay(seconds)
        self.record(user_id,chat_id,message_id,kind="temporary_panel")
        with self.db:
            result = self.db.execute("""UPDATE chat_history SET kind='temporary_panel',due_at=?
                WHERE user_id=? AND chat_id=? AND message_id=?
                AND kind IN ('text','menu','search','status','command','panel','temporary_panel')""",
                (due,user_id,chat_id,message_id))
        return bool(result.rowcount)

    async def expire_due(self, bot, *, now=None, max_messages=100):
        stamp = _timestamp(now)
        if not _positive_int(max_messages) or max_messages > 1000:
            raise ValueError("El límite de mensajes no es válido.")
        result = dict(deleted=0,failed=0,expired_locally=0)
        async with self._cleanup_lock:
            rows = self.db.execute("""SELECT user_id,chat_id,message_id,created_at FROM chat_history
                WHERE due_at IS NOT NULL AND due_at<=?
                AND kind IN ('text','menu','search','status','command','temporary_panel')
                ORDER BY due_at LIMIT ?""",(stamp,max_messages)).fetchall()
            groups = {}
            for row in rows:
                key = (row['user_id'],row['chat_id'])
                if row['created_at'] <= stamp - DELETE_AGE_SECONDS:
                    self._forget(*key,[row['message_id']]);result['expired_locally'] += 1
                else:
                    groups.setdefault(key,[]).append(row['message_id'])
            for (uid,chat),ids in groups.items():
                for start in range(0,len(ids),100):
                    batch=ids[start:start+100]
                    try:
                        await self._call(lambda: bot.delete_messages(chat_id=chat,message_ids=batch))
                    except BadRequest:
                        for mid in batch:
                            try:
                                await self._call(lambda mid=mid:bot.delete_message(chat_id=chat,message_id=mid))
                            except BadRequest as exc:
                                if 'message to delete not found' in str(exc).lower():
                                    self._forget(uid,chat,[mid]);result['deleted']+=1
                                else:
                                    self.schedule(uid,chat,mid,30,now=stamp);result['failed']+=1
                            except TelegramError:
                                self.schedule(uid,chat,mid,30,now=stamp);result['failed']+=1
                            else:
                                self._forget(uid,chat,[mid]);result['deleted']+=1
                    except TelegramError:
                        for mid in batch:self.schedule(uid,chat,mid,30,now=stamp)
                        result['failed']+=len(batch)
                    else:
                        self._forget(uid,chat,batch);result['deleted']+=len(batch)
        return result

    def _forget_any(self, user_id, chat_id, ids):
        """Forget only confirmed removals; saved audio/library tables are untouched."""
        if ids:
            with self.db:
                self.db.executemany("DELETE FROM chat_history WHERE user_id=? AND chat_id=? AND message_id=?",
                                    [(user_id, chat_id, mid) for mid in ids])

    def _registered_messages(self, user_id, chat_id):
        rows = {row['message_id']: dict(row) for row in self.db.execute(
            "SELECT message_id,kind,created_at FROM chat_history WHERE user_id=? AND chat_id=?",
            (user_id, chat_id))}
        # Old native playlists retain exact owner-scoped audio message IDs even
        # when no chat-history row remains. The session creation time precedes
        # delivery, so it is a conservative age bound, never a guessed ID.
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'playback_messages', 'playback_deliveries', 'playback_sessions'} <= tables:
            messages = self.db.execute("""SELECT m.message_id,s.created_at FROM playback_messages m
                JOIN playback_deliveries d ON d.session_id=m.session_id
                JOIN playback_sessions s ON s.id=m.session_id
                WHERE s.user_id=? AND d.chat_id=?""", (user_id, chat_id))
            for item in messages:
                mid = item['message_id']
                if mid in rows or not _positive_int(mid):
                    continue
                try:
                    created = _timestamp(datetime.fromisoformat(item['created_at']))
                except (TypeError, ValueError, OverflowError):
                    continue
                rows[mid] = dict(message_id=mid, kind='audio', created_at=created)
        return rows.values()

    async def _delete_registered(self, bot, user_id, chat_id, keep_ids, max_messages, now, *, audio_only):
        if not _positive_int(user_id) or not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
            raise ValueError("El chat o su propietario no son válidos.")
        if not _positive_int(max_messages) or max_messages > 1000:
            raise ValueError("El límite de mensajes debe ser de 1 a 1000.")
        if isinstance(keep_ids, (str, bytes)):
            raise ValueError("Los mensajes a conservar no son válidos.")
        try:
            keep = set(keep_ids)
        except TypeError:
            raise ValueError("Los mensajes a conservar no son válidos.") from None
        if any(not _positive_int(mid) for mid in keep):
            raise ValueError("Los mensajes a conservar no son válidos.")
        stamp = _timestamp(now)
        cutoff = stamp - DELETE_AGE_SECONDS
        result = dict(deleted=0, already_gone=0, failed=0, too_old=0,
                      kept=0, remaining=0, deleted_ids=[])
        async with self._cleanup_lock:
            eligible = []
            for row in self._registered_messages(user_id, chat_id):
                if audio_only and row['kind'] != 'audio':
                    continue
                mid = row['message_id']
                if mid in keep:
                    result['kept'] += 1
                elif row['created_at'] <= cutoff:
                    result['too_old'] += 1
                elif row['created_at'] > stamp:
                    result['remaining'] += 1
                else:
                    eligible.append(mid)
            eligible.sort()
            ids = eligible[:max_messages]
            result['remaining'] += max(0, len(eligible) - len(ids))

            def forget(mid, missing=False):
                self._forget_any(user_id, chat_id, [mid])
                result['deleted_ids'].append(mid)
                result['already_gone' if missing else 'deleted'] += 1

            for offset in range(0, len(ids), 100):
                batch = ids[offset:offset+100]
                try:
                    response = await self._call(lambda: bot.delete_messages(chat_id=chat_id, message_ids=batch))
                    if response is False:
                        raise TelegramError('Telegram no confirmó el borrado de mensajes.')
                except BadRequest:
                    for mid in batch:
                        try:
                            response = await self._call(lambda mid=mid: bot.delete_message(chat_id=chat_id, message_id=mid))
                            if response is False:
                                raise TelegramError('Telegram no confirmó el borrado del mensaje.')
                        except BadRequest as exc:
                            if 'message to delete not found' in str(exc).lower():
                                forget(mid, missing=True)
                            else:
                                result['failed'] += 1
                        except TelegramError:
                            result['failed'] += 1
                        else:
                            forget(mid)
                except TelegramError:
                    result['failed'] += len(batch)
                else:
                    # Telegram skips absent IDs in a successful batch. These IDs
                    # are now absent/removed and can invalidate a native queue.
                    self._forget_any(user_id, chat_id, batch)
                    result['deleted_ids'].extend(batch)
                    result['deleted'] += len(batch)
            result['remaining'] += result['failed']
        return result

    async def clear_chat(self, bot, user_id, chat_id, keep_ids=(), *, max_messages=1000, now=None):
        """Explicitly remove known chat cards, including audio, keeping the library.

        Automatic expiry and cleanup() continue preserving audio. Callers must
        invalidate native delivery metadata for returned deleted_ids; this method
        never changes track file IDs, local MP3 files, favorites or playlists.
        """
        return await self._delete_registered(bot, user_id, chat_id, keep_ids,
                                             max_messages, now, audio_only=False)

    async def delete_audio_messages(self, bot, user_id, chat_id, keep_ids=(), *, max_messages=1000, now=None):
        """Remove older known audio cards after a replacement was confirmed sent."""
        return await self._delete_registered(bot, user_id, chat_id, keep_ids,
                                             max_messages, now, audio_only=True)

    async def cleanup(self, bot, user_id, chat_id, before_message_id=None,
                      *, max_messages=500, now=None) -> dict:
        if not _positive_int(user_id) or not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
            raise ValueError("El chat o su propietario no son válidos.")
        if before_message_id is not None and not _positive_int(before_message_id):
            raise ValueError("El límite de limpieza no es válido.")
        if not _positive_int(max_messages) or max_messages > 1000:
            raise ValueError("El límite de limpieza debe ser de 1 a 1000 mensajes.")
        stamp = _timestamp(now)
        cutoff = stamp - DELETE_AGE_SECONDS
        async with self._cleanup_lock:
            where = "user_id=? AND chat_id=?"
            params = [user_id, chat_id]
            if before_message_id is not None:
                where += " AND message_id<?"
                params.append(before_message_id)
            rows = self.db.execute(f"SELECT message_id,kind,created_at FROM chat_history WHERE {where} ORDER BY message_id",
                                   params).fetchall()
            protected = sum(row["kind"] not in CLEANABLE_KINDS for row in rows)
            too_old = sum(row["kind"] in CLEANABLE_KINDS and row["created_at"] <= cutoff for row in rows)
            eligible = [row["message_id"] for row in rows
                        if row["kind"] in CLEANABLE_KINDS and cutoff < row["created_at"] <= stamp]
            ids = eligible[:max_messages]
            result = dict(deleted=0, already_gone=0, failed=0, protected=protected,
                          too_old=too_old, remaining=max(0, len(eligible) - len(ids)))
            for offset in range(0, len(ids), 100):
                batch = ids[offset:offset + 100]
                try:
                    await self._call(lambda: bot.delete_messages(chat_id=chat_id, message_ids=batch))
                except BadRequest:
                    # A single undeletable message must not block the whole batch.
                    for mid in batch:
                        try:
                            await self._call(lambda mid=mid: bot.delete_message(chat_id=chat_id, message_id=mid))
                        except BadRequest as exc:
                            if "message to delete not found" in str(exc).lower():
                                self._forget(user_id, chat_id, [mid])
                                result["already_gone"] += 1
                            else:
                                result["failed"] += 1
                        except TelegramError:
                            result["failed"] += 1
                        else:
                            self._forget(user_id, chat_id, [mid])
                            result["deleted"] += 1
                except TelegramError:
                    result["failed"] += len(batch)
                else:
                    # Telegram skips missing IDs within deleteMessages; this count
                    # means successfully cleaned or already absent, not a history read.
                    self._forget(user_id, chat_id, batch)
                    result["deleted"] += len(batch)
            result["remaining"] += result["failed"]
            return result

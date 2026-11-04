# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Deliver complete playback snapshots with a unique searchable music token."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import math
from pathlib import Path
from urllib.parse import quote

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import BadRequest, RetryAfter

from .provider import ProviderError, Track


def _invalid_file_id(error: BadRequest) -> bool:
    return any(value in str(error).lower() for value in
               ("file identifier", "file_id", "wrong remote file"))


class PlaylistDelivery:
    def __init__(self, store, provider, sessions, spacing: float = 1.1):
        if not isinstance(spacing, (int, float)) or not math.isfinite(spacing) or spacing < 0:
            raise ValueError("El intervalo de envío no es válido.")
        self.store = store
        self.provider = provider
        self.sessions = sessions
        self.spacing = float(spacing)
        self._locks = {}

    async def _call(self, operation, stream=None):
        """Retry one Telegram flood wait, preserving an upload's open stream."""
        try:
            return await operation()
        except RetryAfter as exc:
            delay = exc.retry_after
            seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            if not math.isfinite(seconds) or seconds < 0 or seconds + 0.2 > 60:
                raise
            await asyncio.sleep(seconds + 0.2)
            if stream is not None:
                stream.seek(0)
            return await operation()

    def _buttons(self, uid, tid):
        star = "⭐ Quitar favorito" if self.store.is_favorite(uid, tid) else "☆ Favorito"
        return Markup([[Button(star, callback_data=f"fav:{tid}"),
                        Button("➕ A una lista", callback_data=f"add:{tid}")]])

    async def _send_track(self, bot, chat, uid, track, caption):
        tid = track["id"]
        kwargs = dict(chat_id=chat, title=track["title"][:64],
                      performer=(track.get("artist") or "")[:64], caption=caption,
                      reply_markup=self._buttons(uid, tid), read_timeout=120, write_timeout=120)
        fid = self.store.get_file_id(tid)
        if fid:
            try:
                message = await self._call(lambda: bot.send_audio(audio=fid, **kwargs))
                self.store.set_file_id(tid, message.audio.file_id)
                return message
            except BadRequest as exc:
                if not _invalid_file_id(exc):
                    raise
                self.store.set_file_id(tid, "")
        async with self.provider.audio(Track.from_dict(track)) as audio:
            with Path(audio).open("rb") as stream:
                message = await self._call(
                    lambda: bot.send_audio(audio=stream, filename=f"{tid}{Path(audio).suffix}", **kwargs),
                    stream=stream)
            self.store.set_file_id(tid, message.audio.file_id)
            return message

    async def send(self, bot, chat, uid, scope, start_index=0, *, announce=True, force_new=False):
        if not isinstance(start_index, int) or isinstance(start_index, bool) or start_index < 0:
            raise ValueError("La canción inicial no es válida.")
        # Concurrent callbacks cannot create duplicate copies of one scope.
        lock = self._locks.setdefault((uid, chat, scope), asyncio.Lock())
        async with lock:
            return await self._send(bot, chat, uid, scope, start_index, announce, force_new)

    async def _send(self, bot, chat, uid, scope, start_index, announce, force_new):
        # No network await occurs before reading/copying the complete library list.
        session = None if force_new else self.sessions.find_delivery(uid, chat, scope)
        if session is None:
            session = self.sessions.create(uid, scope)
        tracks = session["tracks"]
        if start_index >= len(tracks):
            raise ValueError("La canción inicial ya no está disponible.")
        if "message_ids" not in session:
            session = self.sessions.start_delivery(uid, session["id"], chat)
        reused = session["complete"]
        token = "PL" + session["id"]
        search_url = "tg://search?query=" + quote(token, safe="")
        message_ids = list(session["message_ids"])
        pending = [i for i in range(len(tracks) - 1, -1, -1) if message_ids[i] is None]
        # Resume known partial deliveries under the same token. Already sent
        # messages stay in place; the remaining N..1 order is preserved.
        for sent, index in enumerate(pending):
            track = self.sessions.get_alternative(uid, session["id"], index) or tracks[index]
            caption = f"{session['name'][:100]} · {index + 1}/{len(tracks)}\n{token}"
            try:
                message = await self._send_track(bot, chat, uid, track, caption)
            except ProviderError as exc:
                if exc.kind in {"geo_restricted", "captcha", "auth_required", "unavailable"}:
                    exc.original_track = dict(track)
                    exc.playback_session_id = session["id"]
                    exc.playback_index = index
                raise
            self.sessions.record_message(uid, session["id"], chat, index, message.message_id)
            message_ids[index] = message.message_id
            if sent < len(pending) - 1 and self.spacing:
                await asyncio.sleep(self.spacing)
        if not reused:
            session = self.sessions.complete_delivery(uid, session["id"], chat)
        summary = (f"🎵 {session['name']} · {len(tracks)} canciones\n\n"
                   f"Tocá Abrir playlist, elegí Música y reproducí la canción {start_index + 1}. "
                   "Desactivá aleatorio y orden inverso. "
                   "Abrir audios directamente en el chat puede mezclar historial.")
        if reused:
            summary += "\nLa playlist ya está preparada; reutilicé sus audios."
        markup = Markup([[Button("🎧 Abrir playlist", url=search_url)],
                         [Button("Volver a la lista", callback_data=f"list:{scope}:0")]])
        summary_message_id = None
        if announce:
            message = await self._call(lambda: bot.send_message(chat_id=chat, text=summary, reply_markup=markup))
            summary_message_id = message.message_id
        return dict(session, message_ids=message_ids, token=token, search_url=search_url,
                    start_index=start_index, reused=reused, summary_text=summary,
                    reply_markup=markup, summary_message_id=summary_message_id)

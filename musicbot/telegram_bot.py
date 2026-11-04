# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Telegram transport that records only known private-chat messages for cleanup."""
import os
from telegram.ext import ExtBot


class HistoryBot(ExtBot):
    __slots__ = ('_chat_history', '_allowed_users')

    def __init__(self, token, history, allowed, **kwargs):
        super().__init__(token, **kwargs)
        object.__setattr__(self, '_chat_history', history)
        object.__setattr__(self, '_allowed_users', frozenset(allowed))

    def _record(self, message, kind=None):
        chat = getattr(message, 'chat', None)
        if chat and chat.type == 'private' and chat.id in self._allowed_users:
            self._chat_history.record_message(chat.id, message, kind=kind)
            if kind == 'text':
                try:
                    ttl = max(10,min(3600,int(os.getenv('MESSAGE_TTL_SECONDS','90'))))
                except ValueError:
                    ttl = 90
                self._chat_history.schedule(chat.id,chat.id,message.message_id,ttl)
        return message

    async def send_message(self, *args, **kwargs):
        return self._record(await super().send_message(*args, **kwargs), kind='text')

    async def send_audio(self, *args, **kwargs):
        return self._record(await super().send_audio(*args, **kwargs), kind='audio')

    async def send_media_group(self, *args, **kwargs):
        messages = await super().send_media_group(*args, **kwargs)
        for message in messages:
            self._record(message, kind='protected')
        return messages

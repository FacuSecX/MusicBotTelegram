# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""One durable navigation panel per private user/chat."""
from __future__ import annotations
import asyncio
import json
import time
from .chat_history import DELETE_AGE_SECONDS
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError


def _message_id(message):
    value = getattr(message, 'message_id', None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _text_message(message):
    return (bool(getattr(message, 'text', None))
            and not any(getattr(message, name, None) for name in
                        ('audio', 'voice', 'video', 'document', 'photo', 'animation', 'media_group_id')))


def _audio_caption(text):
    """Telegram counts caption length in UTF-16 code units."""
    text = str(text or '')
    if len(text.encode('utf-16-le')) <= 2048:
        return text
    # Leave room for the ellipsis, without splitting a supplementary character.
    used, characters = 0, []
    for character in text:
        units = 2 if ord(character) > 0xffff else 1
        if used + units > 1023:
            break
        characters.append(character)
        used += units
    return ''.join(characters) + '…'


class MenuPanels:
    def __init__(self, store, history):
        self.db, self.history = store.db, history
        self._locks = {}
        self._handles = {}
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS menu_panels (
                user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                message_id INTEGER, persistent INTEGER NOT NULL DEFAULT 0,
                data_json TEXT NOT NULL DEFAULT '{}',
                last_input_id INTEGER,
                PRIMARY KEY(user_id,chat_id)
            );
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(menu_panels)')}
        if 'last_input_id' not in columns:
            with self.db:
                self.db.execute('ALTER TABLE menu_panels ADD COLUMN last_input_id INTEGER')
        for name in ('last_text','last_markup'):
            if name not in columns:
                with self.db:
                    self.db.execute('ALTER TABLE menu_panels ADD COLUMN '+name+' TEXT')
        if 'media_type' not in columns:
            with self.db:
                self.db.execute("ALTER TABLE menu_panels ADD COLUMN media_type TEXT NOT NULL DEFAULT 'text'")

    def get(self, uid, chat):
        row = self.db.execute('SELECT * FROM menu_panels WHERE user_id=? AND chat_id=?', (uid, chat)).fetchone()
        return dict(row) if row else None

    def data(self, uid, chat):
        row = self.get(uid, chat)
        return json.loads(row['data_json']) if row else {}

    def set_data(self, uid, chat, **values):
        data = self.data(uid, chat)
        data.update(values)
        with self.db:
            self.db.execute('''INSERT INTO menu_panels(user_id,chat_id,data_json) VALUES (?,?,?)
                ON CONFLICT(user_id,chat_id) DO UPDATE SET data_json=excluded.data_json''',
                (uid, chat, json.dumps(data, ensure_ascii=False)))

    def revision(self, uid, chat):
        return self.data(uid, chat).get('_revision', 0)

    def _save(self, uid, chat, mid, persistent, input_id=None, *, media_type='text'):
        with self.db:
            self.db.execute('''INSERT INTO menu_panels(user_id,chat_id,message_id,persistent,last_input_id,media_type) VALUES (?,?,?,?,?,?)
                ON CONFLICT(user_id,chat_id) DO UPDATE SET message_id=excluded.message_id,persistent=excluded.persistent,
                    last_input_id=COALESCE(excluded.last_input_id,menu_panels.last_input_id),media_type=excluded.media_type''',
                (uid, chat, mid, int(persistent), input_id, media_type))

    def _remember(self, uid, chat, text, markup):
        payload = markup.to_dict() if markup is not None else None
        with self.db:
            self.db.execute("""UPDATE menu_panels SET last_text=?,last_markup=?
                WHERE user_id=? AND chat_id=? AND message_id IS NOT NULL""",
                (text,json.dumps(payload,ensure_ascii=False),uid,chat))

    def release(self, uid, chat):
        row = self.get(uid, chat)
        if row and row['message_id']:
            self._protect(uid, chat, row['message_id'], bool(row['persistent']))

    def _protect(self, uid, chat, mid, persistent):
        known = self.db.execute('SELECT kind FROM chat_history WHERE user_id=? AND chat_id=? AND message_id=?',
                                (uid, chat, mid)).fetchone()
        if known and known['kind'] in {'audio', 'protected'}:
            return
        if persistent:
            self.history.protect_panel(uid, chat, mid)
        else:
            self.history.schedule_panel(uid, chat, mid, 180)

    async def _retire(self, bot, uid, chat, mid):
        row = self.db.execute('SELECT kind,created_at FROM chat_history WHERE user_id=? AND chat_id=? AND message_id=?',
                              (uid, chat, mid)).fetchone()
        # The panel registry alone cannot authorize deletion of an audio. This
        # second check also fails safely if a stale/corrupt pointer references it.
        if row is None or row['kind'] not in {'panel', 'temporary_panel'}:
            return
        try:
            await bot.delete_message(chat, mid)
        except TelegramError as error:
            if isinstance(error, BadRequest) and 'not found' in str(error).lower():
                pass
            else:
                try:
                    await bot.edit_message_reply_markup(chat_id=chat, message_id=mid, reply_markup=None)
                except TelegramError:
                    pass
                if time.time() - row['created_at'] < DELETE_AGE_SECONDS:
                    self.history.schedule_panel(uid, chat, mid, 1)
                return
        self.history.schedule_panel(uid, chat, mid, 1)
        self.history.forget_disposable(uid, chat, [mid])

    async def show(self, update, context, text, markup, *, persistent=None, force_bottom=False):
        uid, chat = update.effective_user.id, update.effective_chat.id
        async with self._locks.setdefault((uid, chat), asyncio.Lock()):
            return await self._show_locked(update,context,text,markup,
                persistent=persistent,force_bottom=force_bottom)

    async def attach_audio(self, update, context, message, *, preserve_view=False):
        """Adopt a delivered song as the one navigation surface, never a TTL panel."""
        mid = _message_id(message)
        if mid is None or not getattr(message, 'audio', None):
            return None
        uid, chat = update.effective_user.id, update.effective_chat.id
        target_chat = getattr(message, 'chat', None)
        if target_chat is not None and (getattr(target_chat, 'id', None) != chat
                                       or getattr(target_chat, 'type', None) != 'private'):
            return None
        async with self._locks.setdefault((uid, chat), asyncio.Lock()):
            row = self.get(uid, chat)
            if preserve_view and (not row or not row['message_id'] or row['last_text'] is None):
                # The delivery already carried controls when the user hid the
                # menu. Remove those controls without reopening navigation.
                try:
                    await context.bot.edit_message_reply_markup(chat_id=chat, message_id=mid, reply_markup=None)
                except BadRequest as error:
                    if not any(reason in str(error).lower() for reason in ('not found', 'not modified')):
                        raise
                    if 'not found' in str(error).lower():
                        return None
                self.history.record(uid, chat, mid, 'audio', getattr(message, 'date', None))
                self.set_data(uid, chat, _hidden_audio_id=mid)
                return None
            old_mid = row['message_id'] if row else None
            caption = _audio_caption(getattr(message, 'caption', None))
            markup = getattr(message, 'reply_markup', None)
            if preserve_view:
                caption = _audio_caption(row['last_text'])
                try:
                    payload = json.loads(row['last_markup']) if row['last_markup'] else None
                    markup = InlineKeyboardMarkup.de_json(payload, None) if payload is not None else None
                except (ValueError, TypeError, AttributeError):
                    return None
                try:
                    edited = await context.bot.edit_message_caption(chat_id=chat, message_id=mid,
                                                                    caption=caption, reply_markup=markup)
                except BadRequest as error:
                    if 'not modified' not in str(error).lower():
                        raise
                else:
                    if _message_id(edited) is not None:
                        message = edited
            self.history.record(uid, chat, mid, 'audio', getattr(message, 'date', None))
            self._save(uid, chat, mid, bool(row and row['persistent']), media_type='audio')
            self._remember(uid, chat, caption, markup)
            self.set_data(uid, chat, _revision=self.revision(uid, chat)+1, _hidden_audio_id=None)
            self._handles[(uid, chat)] = message
            if old_mid is not None and old_mid != mid:
                await self._retire(context.bot, uid, chat, old_mid)
            return message

    def detach_audio(self, uid, chat):
        """Forget a removed/cleared audio pointer without changing view revision."""
        row = self.get(uid, chat)
        if row and row['media_type'] == 'audio':
            self._save(uid, chat, None, False)
            self._handles.pop((uid, chat), None)
        if row:
            self.set_data(uid, chat, _hidden_audio_id=None)

    async def to_bottom(self, update, context):
        """Move the current view after new content, without reopening hidden UI."""
        uid, chat = update.effective_user.id, update.effective_chat.id
        async with self._locks.setdefault((uid, chat), asyncio.Lock()):
            row = self.get(uid,chat)
            if not row or not row['message_id'] or row['last_text'] is None:
                return None
            try:
                payload = json.loads(row['last_markup']) if row['last_markup'] else None
                markup = InlineKeyboardMarkup.de_json(payload,None) if payload is not None else None
            except (ValueError,TypeError,AttributeError):
                return None
            return await self._show_locked(update,context,row['last_text'],markup,
                persistent=bool(row['persistent']),force_bottom=True)

    async def _show_locked(self, update, context, text, markup, *, persistent=None, force_bottom=False):
        uid, chat = update.effective_user.id, update.effective_chat.id
        row = self.get(uid, chat)
        if row and row['message_id'] is None:
            hidden = self.data(uid, chat).get('_hidden_audio_id')
            if isinstance(hidden, int) and not isinstance(hidden, bool) and hidden > 0:
                owned = self.db.execute('SELECT kind FROM chat_history WHERE user_id=? AND chat_id=? AND message_id=?',
                                        (uid, chat, hidden)).fetchone()
                if owned and owned['kind'] == 'audio':
                    # This is an explicit navigation request. Unlike a late
                    # result/to_bottom, it may restore the same hidden song.
                    row = dict(row, message_id=hidden, media_type='audio')
                else:
                    self.set_data(uid, chat, _hidden_audio_id=None)
        previous_revision = self.revision(uid, chat)
        self.set_data(uid, chat, _revision=previous_revision+1)
        keep = bool(row and row['persistent']) if persistent is None else persistent
        operation_active = uid in context.application.bot_data['service'].busy
        protect = keep or operation_active
        mid = row['message_id'] if row else None
        query = update.callback_query
        source = query.message if query else None
        source_mid = _message_id(source)
        incoming = update.effective_message if query is None else None
        incoming_mid = _message_id(incoming)
        sender = getattr(incoming, 'from_user', None)
        real_input = (incoming_mid is not None and _text_message(incoming)
                      and sender is not None and getattr(sender, 'is_bot', True) is False
                      and getattr(sender, 'id', None) == uid)
        if mid is not None and row['media_type'] == 'audio':
            # All navigation edits the song's caption/keyboard. A force-bottom
            # request must not create a second text card beneath that song.
            caption = _audio_caption(text)
            try:
                message = await context.bot.edit_message_caption(chat_id=chat, message_id=mid,
                                                                caption=caption, reply_markup=markup)
            except BadRequest as error:
                detail = str(error).lower()
                if 'not modified' in detail:
                    self._save(uid, chat, mid, keep, incoming_mid if real_input else None, media_type='audio')
                    self._remember(uid, chat, caption, markup)
                    self.set_data(uid, chat, _hidden_audio_id=None)
                    return self._handles.get((uid, chat), source)
                if not any(reason in detail for reason in ('not found', "can't be edited", 'message_id_invalid')):
                    self.set_data(uid, chat, _revision=previous_revision)
                    raise
                # A manually deleted audio cannot remain a navigation anchor.
                # Keep its library entry intact and reopen an ordinary panel.
                self.detach_audio(uid, chat)
                row, mid = self.get(uid, chat), None
            except BaseException:
                self.set_data(uid, chat, _revision=previous_revision)
                raise
            else:
                self._save(uid, chat, mid, keep, incoming_mid if real_input else None, media_type='audio')
                self._remember(uid, chat, caption, markup)
                self.set_data(uid, chat, _hidden_audio_id=None)
                if _message_id(message) is not None:
                    self._handles[(uid, chat)] = message
                if source_mid and source_mid != mid:
                    await self._retire(context.bot, uid, chat, source_mid)
                return message
        latest_media = self.db.execute("""SELECT MAX(message_id) FROM chat_history
            WHERE user_id=? AND chat_id=? AND kind IN ('audio','protected')""",
            (uid, chat)).fetchone()[0]
        new_input = (real_input and incoming_mid > mid
                     and incoming_mid != row.get('last_input_id')) if mid is not None else False
        media_below = mid is not None and latest_media is not None and latest_media > mid
        relocate = mid is not None and (force_bottom or new_input or media_below)
        if relocate:
            # Audio sent after a panel must stay above its controls. Send
            # the replacement before retiring the known previous panel;
            # a failed send must leave the visible menu usable.
            try:
                message = await update.effective_message.reply_text(text, reply_markup=markup, do_quote=False)
            except BaseException:
                self.set_data(uid, chat, _revision=previous_revision)
                raise
            new_mid = _message_id(message)
            if new_mid is not None:
                self._handles[(uid, chat)] = message
                self._save(uid, chat, new_mid, keep, incoming_mid if real_input else None)
                self._protect(uid, chat, new_mid, protect)
                self._remember(uid, chat, text, markup)
                await self._retire(context.bot, uid, chat, mid)
            return message
        # Real Telegram messages always have IDs. This also permits simple
        # in-process adapters to render callbacks without a persistent ID.
        if query and source_mid is None and _text_message(source):
            return await query.edit_message_text(text, reply_markup=markup)
        if mid is None and source_mid and _text_message(source):
            sender = getattr(source, 'from_user', None)
            if sender is not None and getattr(sender, 'is_bot', False):
                mid = source_mid
        if mid is not None:
            try:
                if query and source_mid == mid:
                    message = await query.edit_message_text(text, reply_markup=markup)
                else:
                    message = await context.bot.edit_message_text(text, chat_id=chat, message_id=mid, reply_markup=markup)
            except BadRequest as error:
                detail = str(error).lower()
                if 'not modified' in detail:
                    self._save(uid, chat, mid, keep, incoming_mid if real_input else None)
                    self._protect(uid, chat, mid, protect)
                    self._remember(uid, chat, text, markup)
                    return source
                if not any(reason in detail for reason in ('not found', "can't be edited", 'message_id_invalid')):
                    raise
                self._save(uid, chat, None, keep)
            else:
                self._save(uid, chat, mid, keep, incoming_mid if real_input else None)
                self._protect(uid, chat, mid, protect)
                self._remember(uid, chat, text, markup)
                # A button on an old text menu must not leave a second menu.
                if source_mid and source_mid != mid and _text_message(source):
                    sender = getattr(source, 'from_user', None)
                    if sender is not None and getattr(sender, 'is_bot', False):
                        try:
                            await context.bot.delete_message(chat, source_mid)
                        except TelegramError:
                            pass
                return message
        handle = self._handles.get((uid, chat))
        if handle is not None and _message_id(handle) is None:
            await handle.edit_text(text, reply_markup=markup)
            return handle
        message = await update.effective_message.reply_text(text, reply_markup=markup, do_quote=False)
        self._handles[(uid, chat)] = message
        mid = _message_id(message)
        if mid:
            self._save(uid, chat, mid, keep, incoming_mid if real_input else None)
            self._protect(uid, chat, mid, protect)
            self._remember(uid, chat, text, markup)
        return message

    async def hide(self, bot, uid, chat):
        async with self._locks.setdefault((uid, chat), asyncio.Lock()):
            row = self.get(uid, chat)
            if row and row['message_id']:
                known = self.db.execute('SELECT kind FROM chat_history WHERE user_id=? AND chat_id=? AND message_id=?',
                                        (uid, chat, row['message_id'])).fetchone()
                if row['media_type'] == 'audio' or (known and known['kind'] in {'audio', 'protected'}):
                    hidden = row['message_id'] if known and known['kind'] == 'audio' else None
                    try:
                        await bot.edit_message_reply_markup(chat_id=chat, message_id=row['message_id'], reply_markup=None)
                    except BadRequest as error:
                        if not any(reason in str(error).lower() for reason in ('not found', 'not modified')):
                            raise
                        if 'not found' in str(error).lower():
                            hidden = None
                    self._save(uid, chat, None, False)
                    self.set_data(uid, chat, _revision=self.revision(uid, chat)+1, _hidden_audio_id=hidden)
                    self._handles.pop((uid, chat), None)
                    return
                try:
                    await bot.delete_message(chat, row['message_id'])
                except BadRequest as error:
                    if 'not found' not in str(error).lower():
                        # Older messages may no longer be deletable; remove the
                        # keyboard so the old panel cannot remain interactive.
                        await bot.edit_message_reply_markup(chat_id=chat, message_id=row['message_id'], reply_markup=None)
                self.history.schedule_panel(uid, chat, row['message_id'], 1)
                self.history.forget_disposable(uid, chat, [row['message_id']])
            self._save(uid, chat, None, False)
            self.set_data(uid, chat, _revision=self.revision(uid, chat)+1)
            self._handles.pop((uid, chat), None)

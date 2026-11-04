# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Telegram music interface. Run with python -m musicbot.bot."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from contextlib import AsyncExitStack
import logging
import os
from pathlib import Path
import time
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup, InputMediaAudio, Update
from telegram.error import BadRequest, TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from .provider import Track, YouTubeProvider, ProviderError
from .storage import Store
from .playback import PlaybackSessions
from .playlist_delivery import PlaylistDelivery
from .alternatives import find_available, find_cached
from .chat_history import ChatHistory
from .telegram_bot import HistoryBot
from .panels import MenuPanels
from .message_lifecycle import observe_message, start_lifecycle, stop_lifecycle
from urllib.parse import urlsplit

LOG = logging.getLogger('musicbot')
PAGE = 6
HELP = ('🎵 Buscá una canción escribiendo su nombre o /buscar artista canción.\n'
        'Tocá uno de los 6 resultados para descargar y recibir el audio directamente.\n\n'
        '/menu — mostrar menú · /ocultarmenu — ocultarlo\n/detener — cerrar playlist y limpiar el chat\n'
        '⭐ /favorites — tus favoritos\n'
        '📚 /playlists — tus listas\n'
        '🧹 /limpiarchat o /clearchat — limpiar el chat conservando canciones y listas\n'
        '➕ /crearplaylist Nombre — crear una lista\n'
        '/cancelar — cancelar la creación\n\n'
        'Usá ⭐ en cada audio para guardarlo y ➕ para añadirlo a una lista. '
        'Pausa, reproducción y cambio de canción se controlan desde el reproductor de Telegram. '
        'Mientras la playlist está activa, sus tarjetas permiten usar Next del reproductor. Tocá Play en la canción 1. Cerrar playlist y limpiar retira las tarjetas conservando las canciones.\n\n'
        'Descargá y compartí material que tengas permiso de usar.')
ERRORS = {
    'geo_restricted': 'YouTube no permite este video desde el país de salida del servidor. Podemos buscar otra versión disponible de la misma canción.',
    'http_403': 'YouTube rechazó el acceso (403). Probá más tarde.',
    'http_402': 'YouTube rechazó la solicitud (402). Probá más tarde.',
    'rate_limited': 'YouTube limitó las consultas. Esperá un minuto.',
    'auth_required': 'YouTube exige iniciar sesión para este contenido.',
    'captcha': 'YouTube rechazó la descarga desde el servidor y exige verificar una sesión. La búsqueda y tus listas siguen disponibles.',
    'unavailable': 'El video no está disponible o tiene restricciones.',
    'network': 'No pude conectar con YouTube. Probá más tarde.',
    'timeout': 'YouTube tardó demasiado en responder. Probá más tarde.',
    'too_large': 'Este audio supera el tamaño permitido.',
    'too_long': 'La canción supera el límite de duración.',
}

class Service:
    def __init__(self, store, provider, allowed):
        self.store, self.provider, self.allowed = store, provider, allowed
        self.cache = OrderedDict()
        self.search_lock = asyncio.Semaphore(1)
        self.audio_lock = asyncio.Semaphore(1)
        self.blocked_until = 0.0
        self.busy = set()
        self.play_tasks = {}
        self.last_search = {}
        self.alternative_choices = OrderedDict()
        self.history = ChatHistory(store)
        self.panels = MenuPanels(store, self.history)
        self.last_delivery_metrics = {}
        self.sessions = PlaybackSessions(store)
        self.playlist_delivery = PlaylistDelivery(store, provider, self.sessions)

    async def search(self, query):
        key = ' '.join(query.casefold().split())
        async with self.search_lock:
            hit = self.cache.get(key)
            if hit and time.monotonic() - hit[0] < 600:
                self.cache.move_to_end(key)
                return hit[1]
            if time.monotonic() < self.blocked_until:
                raise ProviderError('rate_limited', 'Cooldown after a restricted response')
            try:
                tracks = await self.provider.search(query)
            except ProviderError as exc:
                if exc.kind in {'http_403','http_402','rate_limited','captcha','auth_required'}:
                    self.blocked_until = time.monotonic() + 60
                raise
            for track in tracks:
                self.store.upsert_track(track.to_dict())
            self.cache[key] = (time.monotonic(), tracks)
            while len(self.cache) > 200:
                self.cache.popitem(last=False)
            return tracks

    def cached_tracks(self):
        return [dict(row) for row in self.store.db.execute(
            "SELECT * FROM tracks WHERE COALESCE(file_id,'') != ''")]

    def audio_buttons(self, uid, tid, scope=None, index=0):
        star = '⭐ Quitar favorito' if self.store.is_favorite(uid, tid) else '☆ Favorito'
        rows = [[Button(star, callback_data=f'fav:{tid}'), Button('➕ A una lista', callback_data=f'add:{tid}')]]
        if scope:
            rows.append([Button('⏮ Anterior', callback_data=f'next:{scope}:{max(0,index-1)}'),
                         Button('Siguiente ⏭', callback_data=f'next:{scope}:{index+1}')])
        else:
            rows.append([Button('‹ Atrás', callback_data='results'),
                         Button('🏠 Menú principal', callback_data='menu', style='primary')])
        return Markup(rows)

    async def retire_previous_audio(self, bot, chat, uid, message):
        mid=getattr(message,'message_id',None)
        if not isinstance(mid,int) or isinstance(mid,bool) or mid <= 0:
            return
        self.history.record(uid,chat,mid,'audio',getattr(message,'date',None))
        result=await self.history.delete_audio_messages(bot,uid,chat,keep_ids=[mid])
        self.sessions.invalidate_messages(uid,chat,result['deleted_ids'])
        self.panels.set_data(uid,chat,active_native_session=None)

    async def send_track(self, bot, chat, uid, tid, scope=None, index=0):
        started = time.monotonic()
        track = self.store.get_track(tid)
        if not track:
            await bot.send_message(chat, 'La canción ya no está disponible. Buscala otra vez.')
            return
        metrics = {'id': tid, 'cache_hit': False, 'prepare_seconds': 0.0, 'upload_seconds': 0.0}
        kwargs = dict(chat_id=chat, title=track['title'][:64], performer=track.get('artist','')[:64],
                      caption=track['title'][:900], reply_markup=self.audio_buttons(uid, tid, scope, index),
                      read_timeout=120, write_timeout=120)
        fid = self.store.get_file_id(tid)
        if fid:
            try:
                upload = time.monotonic()
                message = await bot.send_audio(audio=fid, **kwargs)
                metrics.update(cache_hit=True, upload_seconds=time.monotonic()-upload,
                               total_seconds=time.monotonic()-started)
                self.last_delivery_metrics = metrics
                if scope is None:
                    await self.retire_previous_audio(bot,chat,uid,message)
                LOG.info('audio_delivered cache_hit=true total_seconds=%.3f upload_seconds=%.3f', metrics['total_seconds'], metrics['upload_seconds'])
                return message
            except BadRequest as exc:
                if not any(s in str(exc).lower() for s in ('file identifier', 'file_id', 'wrong remote file')):
                    raise
                self.store.set_file_id(tid, '')
        preparing = time.monotonic()
        async with self.provider.audio(Track.from_dict(track)) as audio:
            metrics['prepare_seconds'] = time.monotonic()-preparing
            upload = time.monotonic()
            with Path(audio).open('rb') as stream:
                msg = await bot.send_audio(audio=stream, filename=f'{tid}{Path(audio).suffix}', **kwargs)
            metrics['upload_seconds'] = time.monotonic()-upload
            self.store.set_file_id(tid, msg.audio.file_id)
            metrics['total_seconds'] = time.monotonic()-started
            self.last_delivery_metrics = metrics
            if scope is None:
                await self.retire_previous_audio(bot,chat,uid,msg)
            LOG.info('audio_delivered cache_hit=false prepare_seconds=%.3f upload_seconds=%.3f total_seconds=%.3f',
                     metrics['prepare_seconds'], metrics['upload_seconds'], metrics['total_seconds'])
            return msg

    def tracks_for(self, uid, scope, offset, limit):
        if scope == 'f':
            return self.store.favorites(uid, offset=offset, limit=limit)
        if scope.startswith('p'):
            return self.store.playlist_tracks(uid, int(scope[1:]), offset=offset, limit=limit)
        raise ValueError('Invalid list')

    async def send_group(self, bot, chat, uid, scope, offset):
        tracks, total = self.tracks_for(uid, scope, offset, 10)
        if not tracks:
            await bot.send_message(chat, 'Esta lista está vacía.')
            return
        if len(tracks) == 1:
            await self.send_track(bot, chat, uid, tracks[0]['id'], scope, offset)
            return
        for attempt in range(2):
            try:
                async with AsyncExitStack() as stack:
                    media = []
                    for track in tracks:
                        fid = self.store.get_file_id(track['id'])
                        if fid:
                            audio = fid
                        else:
                            file = await stack.enter_async_context(self.provider.audio(Track.from_dict(track)))
                            audio = stack.enter_context(Path(file).open('rb'))
                        media.append(InputMediaAudio(media=audio, title=track['title'][:64], performer=track.get('artist','')[:64]))
                    messages = await bot.send_media_group(chat_id=chat, media=media, read_timeout=180, write_timeout=180)
                    for track, message in zip(tracks, messages):
                        self.store.set_file_id(track['id'], message.audio.file_id)
                break
            except BadRequest as exc:
                if attempt or not any(x in str(exc).lower() for x in ('file identifier', 'file_id', 'wrong remote file')):
                    raise
                # Telegram rejects the whole album; invalidate its cached IDs once.
                for track in tracks:
                    self.store.set_file_id(track['id'], '')
        rows = [[Button('⭐ '+t['title'][:40], callback_data='fav:'+t['id']), Button('➕ Lista', callback_data='add:'+t['id'])] for t in tracks]
        if offset + 10 < total:
            rows.append([Button('▶ Próximas 10', callback_data=f'group:{scope}:{offset+10}')])
        await bot.send_message(chat, 'Audios enviados. Abrí uno en el reproductor de Telegram. Favoritos y listas:', reply_markup=Markup(rows))


def svc(context):
    return context.application.bot_data['service']

async def allowed(update, context):
    user, chat = update.effective_user, update.effective_chat
    ok = bool(user and chat and chat.type == 'private' and user.id in svc(context).allowed)
    if not ok and update.callback_query:
        await update.callback_query.answer('Bot privado de pruebas.', show_alert=True)
    elif not ok and update.effective_message:
        await update.effective_message.reply_text('Este bot está en pruebas privadas.')
    if ok:
        message = update.effective_message
        if isinstance(getattr(message, 'message_id', None), int):
            # Both incoming text and previously untracked bot menus are known
            # only after Telegram supplies their actual message object.
            s = svc(context)
            if update.callback_query:
                s.history.record_message(user.id, message)
            elif getattr(message, 'text', None):
                s.history.record_message(user.id, message, kind='command')
    return ok

async def start(update, context):
    await main_menu(update, context)

async def help_command(update, context):
    if await allowed(update, context):
        await render(update, context, HELP)

async def create_playlist(update, context):
    if not await allowed(update, context): return
    name = ' '.join(context.args).strip()
    if not name:
        context.user_data['awaiting_playlist'] = True
        await render(update, context, '➕ Crear playlist\n\nEscribí el nombre de la nueva lista.', Markup([[Button('‹ Atrás', callback_data='menu')]]))
        return
    await save_playlist(update, context, name)

async def save_playlist(update, context, name):
    s = svc(context)
    if len(s.store.playlists(update.effective_user.id)) >= 50:
        await render(update, context, 'Ya tenés 50 listas. Eliminá una antes de crear otra.')
        return
    try:
        pid = s.store.create_playlist(update.effective_user.id, name)
    except (ValueError, __import__('sqlite3').IntegrityError):
        await render(update, context, 'Usá un nombre distinto, de 1 a 60 caracteres.')
        return
    context.user_data.pop('awaiting_playlist', None)
    await render(update, context, f'✅ Playlist creada: {name}', Markup([[Button('🔎 Añadir canciones', callback_data='searchprompt', style='primary')], [Button('Ver playlist', callback_data=f'list:p{pid}:0')], [Button('‹ Atrás', callback_data='menu')]]))

async def cancel(update, context):
    if not await allowed(update, context): return
    context.user_data.pop('awaiting_playlist', None)
    await render(update, context, 'Listo. Podés seguir buscando canciones.')

async def search(update, context):
    if not await allowed(update, context): return
    if context.user_data.get('awaiting_playlist') and not update.effective_message.text.startswith('/'):
        await save_playlist(update, context, update.effective_message.text.strip())
        return
    if update.effective_message.text.startswith('/'):
        context.user_data.pop('awaiting_playlist', None)
    query = ' '.join(context.args) if update.effective_message.text.startswith('/') else update.effective_message.text.strip()
    if not 2 <= len(query) <= 200:
        await render(update, context, 'Escribí entre 2 y 200 caracteres. Ejemplo: /buscar Alan Walker Faded')
        return
    s, uid = svc(context), update.effective_user.id
    now = time.monotonic()
    if uid in s.busy:
        await render(update, context, 'Todavía estoy procesando tu solicitud anterior.')
        return
    if now - s.last_search.get(uid, 0) < 5:
        await render(update, context, 'Esperá unos segundos entre búsquedas.')
        return
    s.last_search[uid] = now
    s.busy.add(uid)
    try:
        status = await render(update, context, '🔎 Buscando…')
        view = s.panels.revision(uid, update.effective_chat.id)
        tracks = await s.search(query)
        if s.panels.revision(uid, update.effective_chat.id) != view:
            return
        if not tracks:
            await render(update, context, 'No encontré coincidencias. Probá con otro título o artista.')
            return
        s.panels.set_data(uid, update.effective_chat.id, search_ids=[track.id for track in tracks], search_query=query)
        await search_results(update, context)
    except ProviderError as exc:
        LOG.warning('search_failed kind=%s', exc.kind)
        if s.panels.revision(uid, update.effective_chat.id) != view:
            return
        await render(update, context, ERRORS.get(exc.kind, 'No pude completar la búsqueda. Probá más tarde.'))
    finally:
        s.busy.discard(uid)
        s.panels.release(uid, update.effective_chat.id)

async def show_list(update, context, scope='f', offset=0):
    s, uid = svc(context), update.effective_user.id
    s.panels.set_data(uid, update.effective_chat.id, playback_back=f'list:{scope}:{offset}')
    tracks, total = s.tracks_for(uid, scope, offset, PAGE)
    if total and offset >= total:
        offset = max(0, ((total-1)//PAGE)*PAGE)
        tracks, total = s.tracks_for(uid, scope, offset, PAGE)
    name = 'Favoritos' if scope=='f' else next((p['name'] for p in s.store.playlists(uid) if p['id']==int(scope[1:])), 'Lista')
    rows = []
    for i, track in enumerate(tracks, offset):
        rows.append([Button('🎵 '+track['title'][:45], callback_data=f'next:{scope}:{i}'),
                     Button('✕', callback_data=f'remove:{scope}:{track["id"]}:{offset}')])
    nav = []
    if offset: nav.append(Button('⬅ Anterior', callback_data=f'list:{scope}:{max(0,offset-PAGE)}'))
    if offset+PAGE < total: nav.append(Button('Siguiente ➡', callback_data=f'list:{scope}:{offset+PAGE}'))
    if nav: rows.append(nav)
    if tracks: rows.append([Button('▶ Reproducir playlist', callback_data=f'group:{scope}:0')])
    if scope != 'f': rows.append([Button('🗑 Eliminar lista', callback_data=f'delask:{scope[1:]}')])
    rows.append([Button('‹ Atrás', callback_data='playmenus'), Button('🧹 Borrar chat', callback_data='clean', style='danger')])
    text = f'{name} · {total} canciones' + (f'\nMostrando {offset+1}–{offset+len(tracks)}' if tracks else '\nBuscá una canción y guardala con ⭐ o ➕.')
    await render(update, context, text, Markup(rows))

async def render(update, context, text, markup=None, **kwargs):
    markup = kwargs.get('reply_markup', markup)
    if markup is None:
        markup = Markup([[Button('‹ Atrás', callback_data='menu')]])
    return await svc(context).panels.show(update, context, text, markup, force_bottom=kwargs.get("force_bottom",False))


async def main_menu(update, context, *, force_bottom=False, already_allowed=False, notice=None):
    if not already_allowed and not await allowed(update, context): return
    context.user_data.pop('awaiting_playlist', None)
    rows = [[Button('🔎 Buscar canción', callback_data='searchprompt', style='primary')],
            [Button('➕ Crear playlists', callback_data='create', style='success')],
            [Button('🗑 Eliminar playlists', callback_data='deleteplaylists', style='danger')],
            [Button('▶ Reproducir playlists', callback_data='playmenus', style='primary')],
            [Button('⭐ Favoritos', callback_data='list:f:0')],
            [Button('🙈 Ocultar menú', callback_data='hide')],
            [Button('🧹 Borrar chat', callback_data='clean', style='danger')]]
    s=svc(context)
    if s.store.db.execute('''SELECT 1 FROM playback_deliveries d JOIN playback_sessions p ON p.id=d.session_id
        WHERE p.user_id=? AND d.chat_id=? LIMIT 1''',(update.effective_user.id,update.effective_chat.id)).fetchone():
        rows.insert(-2,[Button('Cerrar playlist y limpiar',callback_data='finish',style='danger')])
    return await svc(context).panels.show(update, context,
        '🎵 Music Bot\n\nElegí una opción. También podés escribir una canción o usar /buscar.' + ('\n\n'+notice if notice else ''),
        Markup(rows), persistent=True, force_bottom=force_bottom)


async def hide_menu(update, context):
    if not await allowed(update, context): return
    context.user_data.pop('awaiting_playlist', None)
    await svc(context).panels.hide(context.bot, update.effective_user.id, update.effective_chat.id)


async def search_prompt(update, context):
    context.user_data.pop('awaiting_playlist', None)
    await render(update, context, '🔎 Buscar canción\n\nEscribí el artista y el título en el chat. También funciona /buscar artista canción.',
        Markup([[Button('‹ Atrás', callback_data='menu')]]))


async def search_results(update, context):
    s, uid, chat = svc(context), update.effective_user.id, update.effective_chat.id
    data = s.panels.data(uid, chat)
    tracks = [s.store.get_track(tid) for tid in data.get('search_ids', [])]
    tracks = [Track.from_dict(track) for track in tracks if track]
    if not tracks:
        await search_prompt(update, context)
        return
    lines, rows = [], []
    for i, track in enumerate(tracks, 1):
        duration = f'{track.duration//60}:{track.duration%60:02}' if track.duration else 'duración no disponible'
        lines.append(f'{i}. {track.title[:140]}\n{track.artist[:90]} · {duration}')
        rows.append([Button(f'{i}. {track.title[:52]}', callback_data='pick:'+track.id)])
    rows.append([Button('‹ Atrás', callback_data='searchprompt'),
                 Button('🏠 Menú principal', callback_data='menu', style='primary')])
    await render(update, context, 'Elegí una canción:\n\n'+'\n\n'.join(lines), Markup(rows))


async def favorites(update, context):
    if await allowed(update, context):
        context.user_data.pop('awaiting_playlist', None)
        await show_list(update, context)

async def playlists(update, context, play=False):
    if not await allowed(update, context): return
    context.user_data.pop('awaiting_playlist', None)
    svc(context).panels.set_data(update.effective_user.id, update.effective_chat.id, playback_back='playmenus' if play else 'playlists')
    lists = svc(context).store.playlists(update.effective_user.id)
    rows = [[Button(f'▶ {p["name"]} ({p["count"]})' if play else f'📚 {p["name"]} ({p["count"]})', callback_data=f'group:p{p["id"]}:0' if play else f'list:p{p["id"]}:0', style='success' if play else None)] for p in lists]
    rows.extend([[Button('⭐ Favoritos', callback_data='group:f:0' if play else 'list:f:0')], [Button('➕ Crear playlist', callback_data='create', style='success')], [Button('‹ Atrás', callback_data='menu')]])
    await render(update, context, ('▶ Reproducir playlists\n\nElegí la playlist que querés escuchar.' if play else 'Tus playlists') if lists else 'Todavía no tenés playlists. Creá la primera:', Markup(rows))

async def delete_playlists(update, context):
    if not await allowed(update,context):return
    s,uid=svc(context),update.effective_user.id
    context.user_data.pop('awaiting_playlist',None)
    rows=[[Button('🗑 '+item['name'],callback_data=f"delask:{item['id']}:manage",style='danger')] for item in s.store.playlists(uid)]
    rows.append([Button('‹ Atrás',callback_data='menu')])
    await render(update,context,'🗑 Eliminar playlists\n\nElegí una playlist para eliminar.' if len(rows)>1 else 'No tenés playlists para eliminar.',Markup(rows))


async def callback(update, context):
    if not await allowed(update, context): return
    q, s, uid = update.callback_query, svc(context), update.effective_user.id
    await q.answer()
    parts = (q.data or '').split(':')
    # Existing result buttons also start delivery without a confirmation card.
    action = 'get' if parts[0] == 'pick' else parts[0]
    if action in {'menu','hide','searchprompt','results','playmenus','list','playlists','pick','get','group','next','add','put','clean','repairask','repair'}:
        context.user_data.pop('awaiting_playlist', None)
    try:
        if action == 'menu': await main_menu(update, context)
        elif action == 'hide': await hide_menu(update, context)
        elif action == 'deleteplaylists': await delete_playlists(update,context)
        elif action == 'finish': await clean_chat(update,context)
        elif action == 'searchprompt': await search_prompt(update, context)
        elif action == 'results': await search_results(update, context)
        elif action == 'playmenus': await playlists(update, context, play=True)
        elif action == 'fav':
            tid = parts[1]
            track = s.store.get_track(tid)
            if not track: return
            state = s.store.toggle_favorite(uid, tid)
            old = q.message.reply_markup.inline_keyboard if q.message.reply_markup else []
            rows = []
            for row in old:
                rows.append([Button(('⭐ ' if state else '☆ ')+track['title'][:40], callback_data='fav:'+tid)
                             if button.callback_data == 'fav:'+tid else button for button in row])
            if rows:
                if isinstance(getattr(q.message, 'message_id', None), int) and q.message.text:
                    await render(update, context, q.message.text, Markup(rows))
                else:
                    await q.edit_message_reply_markup(reply_markup=Markup(rows))
                    panel = s.panels.get(uid, update.effective_chat.id)
                    if (panel and panel.get('media_type') == 'audio'
                            and panel['message_id'] == getattr(q.message, 'message_id', None)):
                        caption = getattr(q.message, 'caption', None)
                        s.panels._remember(uid, update.effective_chat.id,
                                           caption if isinstance(caption, str) else panel.get('last_text') or '',
                                           Markup(rows))
            else:
                await render(update, context, '⭐ Guardada en favoritos.' if state else 'Quitada de favoritos.')
        elif action == 'clean':
            await clean_chat(update, context)
        elif action == 'repairask':
            scope = parts[1]
            await render(update, context, 'Reparar vuelve a enviar esta playlist. Usalo solo si borraste sus audios del chat.',
                Markup([[Button('Reparar playlist', callback_data=f'repair:{scope}:0')],
                        [Button('Volver', callback_data=f'list:{scope}:0')]]))
        elif action == 'list': await show_list(update, context, parts[1], int(parts[2]))
        elif action == 'playlists': await playlists(update, context)
        elif action == 'create':
            context.user_data['awaiting_playlist'] = True
            await render(update, context, '➕ Crear playlist\n\nEscribí el nombre de la nueva lista.', Markup([[Button('‹ Atrás', callback_data='menu')]]))
        elif action == 'add':
            tid = parts[1]
            rows = [[Button(p['name'], callback_data=f'put:{p["id"]}:{tid}')] for p in s.store.playlists(uid)]
            if not rows:
                await render(update, context, 'Todavía no tenés playlists. Creá una para guardar esta canción.', Markup([[Button('➕ Crear playlist', callback_data='create', style='success')], [Button('‹ Atrás', callback_data='results')]]))
            else:
                rows.append([Button('‹ Atrás', callback_data='results')])
                await render(update, context, '¿En qué playlist la guardamos?', Markup(rows))
        elif action == 'put':
            ok = s.store.add_to_playlist(uid, int(parts[1]), parts[2])
            await render(update, context, 'Guardada en la playlist.' if ok else 'Ya está guardada o la playlist no está disponible.', Markup([[Button('Seguir buscando', callback_data='results', style='primary')], [Button('Ver playlist', callback_data=f'list:p{parts[1]}:0')], [Button('‹ Atrás', callback_data='results')]]))
        elif action == 'remove':
            scope, tid, offset = parts[1], parts[2], int(parts[3])
            if scope == 'f':
                if s.store.is_favorite(uid, tid): s.store.toggle_favorite(uid, tid)
            else: s.store.remove_from_playlist(uid, int(scope[1:]), tid)
            await show_list(update, context, scope, offset)
        elif action == 'delask':
            pid = int(parts[1])
            owner=next((item for item in s.store.playlists(uid) if item['id']==pid),None)
            if owner is None:raise ValueError('La playlist no existe.')
            origin='manage' if len(parts)>2 and parts[2]=='manage' else 'list'
            back='deleteplaylists' if origin=='manage' else f'list:p{pid}:0'
            await render(update, context, f"¿Eliminar la playlist {owner['name']}? Las canciones guardadas seguirán disponibles.", Markup([[Button('Sí, eliminar', callback_data=f'delyes:{pid}:{origin}',style='danger')], [Button('‹ Atrás', callback_data=back)]]))
        elif action == 'delyes':
            s.store.delete_playlist(uid, int(parts[1]))
            if len(parts)>2 and parts[2]=='manage':await delete_playlists(update,context)
            else:await playlists(update, context)
        elif action in {'get','next','group','repair','altplay'}:
            if uid in s.busy:
                await render(update, context, 'Todavía estoy procesando tu solicitud anterior.')
                return
            scope, index = None, 0
            if action == 'next':
                scope, index = parts[1], int(parts[2])
                tracks, _ = s.tracks_for(uid, scope, index, 1)
                if not tracks:
                    await render(update, context, 'Llegaste al final de esta lista.')
                    return
                tid = tracks[0]['id']
            elif action == 'get':
                tid = parts[1]
                if not s.store.get_track(tid):
                    await render(update, context, 'Buscá de nuevo esta canción.')
                    return
            elif action == 'altplay':
                sid, slot, alternate = parts[1], int(parts[2]), parts[3]
                entry = s.alternative_choices.get((uid, sid, slot))
                session = s.sessions.get(uid, sid)
                if not entry or time.monotonic() - entry['at'] > 600 or alternate not in entry['ids'] or not session:
                    raise ValueError('La alternativa venció.')
                scope = session['scope']
                current = s.sessions.find_delivery(uid, update.effective_chat.id, scope)
                if not current or current['id'] != sid:
                    raise ValueError('La playlist cambió.')
                s.sessions.set_alternative(uid, sid, slot, s.store.get_track(alternate))
                index = entry['start_index']
            s.busy.add(uid)
            s.play_tasks[uid]=asyncio.current_task()
            status = None
            try:
                if action == 'get':
                    status = await render(update, context, '🎵 Preparando la canción…')
                else:
                    await render(update, context, '🎵 Preparando la playlist desde la canción 1. Al terminar, tocá Play en el primer audio.', Markup([[Button('Cerrar playlist y limpiar',callback_data='finish',style='danger')]]))
                view = s.panels.revision(uid, update.effective_chat.id)
                async with s.audio_lock:
                    if action in {'group', 'next', 'repair', 'altplay'}:
                        target_scope = scope if action in {'next', 'altplay'} else parts[1]
                        result = await s.playlist_delivery.send(context.bot, update.effective_chat.id, uid, target_scope,
                            start_index=index, announce=False, force_new=(action == 'repair'))
                        cleanup=await s.history.delete_audio_messages(context.bot,uid,update.effective_chat.id,keep_ids=result['message_ids'])
                        s.sessions.invalidate_messages(uid,update.effective_chat.id,cleanup['deleted_ids'])
                        s.panels.set_data(uid,update.effective_chat.id,active_native_session=result['id'])
                        if s.panels.revision(uid, update.effective_chat.id) == view:
                            await show_playback(update, context, result, target_scope)
                        else:
                            await s.panels.to_bottom(update,context)
                    else:
                        message = await s.send_track(context.bot, update.effective_chat.id, uid, tid)
                        if s.panels.revision(uid, update.effective_chat.id) != view:
                            await s.panels.attach_audio(update, context, message, preserve_view=True)
                            return
                        await s.panels.attach_audio(update, context, message)
            except ProviderError as exc:
                LOG.warning('audio_failed kind=%s', exc.kind)
                if s.panels.revision(uid, update.effective_chat.id) != view:
                    return
                if exc.kind in {'geo_restricted', 'captcha', 'auth_required', 'unavailable'}:
                    original = getattr(exc, 'original_track', None)
                    if original is None and action == 'get':
                        original = s.store.get_track(tid)
                    if original:
                        await offer_country_alternatives(update, context, original, exc, index)
                    else:
                        await render(update, context, ERRORS[exc.kind])
                else:
                    await render(update, context, ERRORS.get(exc.kind, 'No pude preparar este audio. Probá otra canción.'))
            finally:
                s.busy.discard(uid)
                if s.play_tasks.get(uid) is asyncio.current_task():s.play_tasks.pop(uid,None)
                s.panels.release(uid, update.effective_chat.id)
    except (ValueError, IndexError, KeyError):
        await render(update, context, 'Este botón ya no está disponible. Volvé a abrir /playlists o realizá otra búsqueda.')

async def offer_country_alternatives(update, context, original, error, start_index=0):
    s, uid = svc(context), update.effective_user.id
    cached = s.cached_tracks()
    if error.kind == 'captcha':
        choices = find_cached(Track.from_dict(original), cached)
        if not choices:
            await render(update, context, ERRORS['captcha'])
            return
    else:
        reason = 'Este video está bloqueado por país desde el servidor.' if error.kind == 'geo_restricted' else (
            'Este video requiere una cuenta o tiene una restricción de acceso.' if error.kind == 'auth_required' else 'Esta publicación no está disponible.')
        await render(update, context, reason + ' Buscando la misma canción en versiones accesibles…')
    view = s.panels.revision(uid, update.effective_chat.id)
    try:
        if error.kind != 'captcha':
            choices = await find_available(s.provider, Track.from_dict(original), cached_tracks=cached)
        if s.panels.revision(uid, update.effective_chat.id) != view:
            return
        for track in choices:
            s.store.upsert_track(track.to_dict())
        sid = getattr(error, 'playback_session_id', None)
        slot = getattr(error, 'playback_index', None)
        rows = []
        if sid is not None and slot is not None:
            s.alternative_choices[(uid, sid, slot)] = dict(at=time.monotonic(), ids={t.id for t in choices}, start_index=start_index)
            while len(s.alternative_choices) > 100:
                s.alternative_choices.popitem(last=False)
            rows = [[Button(track.title[:60], callback_data=f'altplay:{sid}:{slot}:{track.id}')] for track in choices]
        else:
            rows = [[Button(track.title[:60], callback_data='get:'+track.id)] for track in choices]
        if choices:
            text = 'Estas versiones de la misma canción están disponibles. Elegí cuál reproducir'
            text += ' para continuar la playlist:' if sid else ':'
            rows.append([Button('‹ Atrás', callback_data='results')])
            await render(update, context, text, Markup(rows))
        else:
            await render(update, context, 'No encontré otra versión accesible verificada de esta canción. Probá otra coincidencia de la búsqueda.')
    except ProviderError as lookup_error:
        await render(update, context, ERRORS.get(lookup_error.kind, 'No pude verificar otras versiones en este momento.'))


async def show_playback(update, context, result, scope):
    back = svc(context).panels.data(update.effective_user.id, update.effective_chat.id).get('playback_back', f'list:{scope}:0')
    state = 'Playlist lista' if result.get('reused') else 'Playlist preparada'
    text = (f"🎵 {result['name']} · {len(result['tracks'])} canciones\n{state}.\n\n"
            f"Tocá Play en el audio marcado como canción {result['start_index'] + 1}. La tarjeta 1 está encima de este menú. "
            "Desactivá aleatorio y orden inverso (en Unigram, las flechas verticales azules).\n"
            "La reproducción inicial requiere tocar Play. Después, Next del reproductor sigue la lista.\n"
            f"Primera canción: {result['tracks'][0].get('title','Canción 1')}")
    await render(update, context, text, Markup([
        [Button('🎧 Ver audios de esta playlist', url=result['search_url'])],
        [Button('‹ Atrás', callback_data=back)],
        [Button('Cerrar playlist y limpiar', callback_data='finish', style='danger')],
        [Button('Reparar si borré los audios', callback_data=f'repairask:{scope}')]]))


async def clean_chat(update, context):
    if not await allowed(update, context):return
    s,uid,chat=svc(context),update.effective_user.id,update.effective_chat.id
    pending=s.play_tasks.get(uid)
    if pending is not None and pending is not asyncio.current_task() and not pending.done():
        pending.cancel()
        await asyncio.gather(pending,return_exceptions=True)
    if uid in s.busy:
        await render(update,context,'Esperá a que termine la operación actual antes de limpiar.')
        return
    s.busy.add(uid)
    try:
        await render(update,context,'🧹 Retirando mensajes del chat…')
        panel=s.panels.get(uid,chat)
        audio_panel = bool(panel and panel.get('media_type') == 'audio')
        keep=[panel['message_id']] if panel and panel['message_id'] and not audio_panel else []
        result=await s.history.clear_chat(context.bot,uid,chat,keep_ids=keep)
        s.sessions.invalidate_messages(uid,chat,result['deleted_ids'])
        if audio_panel:
            if panel['message_id'] not in result['deleted_ids'] and (result['failed'] or result['too_old']):
                await s.panels.hide(context.bot, uid, chat)
            s.panels.detach_audio(uid, chat)
        s.panels.set_data(uid,chat,active_native_session=None)
        context.user_data.pop('awaiting_playlist',None)
        notice=None
        if result['failed'] or result['too_old']:
            notice='Algunos mensajes no pudieron retirarse. Los de más de 48 horas requieren borrado manual.'
        await main_menu(update,context,force_bottom=True,already_allowed=True,notice=notice)
    finally:
        s.busy.discard(uid)
        s.panels.release(uid,chat)


async def on_error(update, context):
    LOG.error('handler_failed type=%s', type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message:
        try: await render(update, context, 'No pude completar la operación. Probá de nuevo en unos segundos.')
        except TelegramError: pass

async def post_init(app):
    await app.bot.set_my_commands([BotCommand('menu','Mostrar menú'), BotCommand('detener','Cerrar playlist y limpiar el chat'), BotCommand('ocultarmenu','Ocultar menú'), BotCommand('limpiarchat','Borrar mensajes conservando música'), BotCommand('clearchat','Limpiar mensajes del chat'), BotCommand('limpiar','Limpiar chat conservando canciones'), BotCommand('buscar','Buscar canciones'), BotCommand('favorites','Mis favoritos'), BotCommand('playlists','Mis playlists'), BotCommand('crearplaylist','Crear una playlist'), BotCommand('cancelar','Cancelar creación de playlist'), BotCommand('help','Ayuda')])
    await start_lifecycle(app)
    LOG.info('Bot iniciado y comandos registrados')

async def post_shutdown(app):
    await stop_lifecycle(app)
    app.bot_data['service'].store.close()

def test_api_options():
    base, files = os.environ.get('BOT_API_BASE_URL'), os.environ.get('BOT_API_FILE_BASE_URL')
    if not base and not files:
        return {}
    if os.environ.get('MUSICBOT_INSTALL_TEST_MODE') != '1' or not base or not files:
        raise SystemExit('Los endpoints de prueba requieren modo de instalación aislada explícito.')
    for value, path in ((base, '/bot'), (files, '/file/bot')):
        try:
            parts = urlsplit(value)
            valid = (parts.scheme == 'http' and parts.hostname == '127.0.0.1'
                     and parts.port and 1 <= parts.port <= 65535 and parts.path == path
                     and not parts.username and not parts.password and not parts.query and not parts.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise SystemExit('El endpoint de prueba debe ser local y usar la ruta esperada.')
    if urlsplit(base).netloc != urlsplit(files).netloc:
        raise SystemExit('Los endpoints de prueba deben pertenecer al mismo servidor local.')
    return {'base_url': base, 'base_file_url': files}


def main():
    base = Path(__file__).resolve().parent.parent
    load_dotenv(base / '.env')
    token = os.environ.get('BOT_TOKEN','')
    ids = {int(x.strip()) for x in os.environ.get('ALLOWED_USER_IDS','').split(',') if x.strip()}
    if not token or not ids: raise SystemExit('Configurá BOT_TOKEN y ALLOWED_USER_IDS en .env')
    data = Path(os.environ.get('DATA_DIR','data'))
    if not data.is_absolute(): data = base / data
    data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    service = Service(Store(data/'music.sqlite3'), YouTubeProvider(data/'tmp', max_duration=int(os.environ.get('MAX_DURATION','1200'))), ids)
    transport = HistoryBot(token, service.history, ids, **test_api_options(),
        request=HTTPXRequest(connection_pool_size=32, connect_timeout=20, read_timeout=120, write_timeout=120),
        get_updates_request=HTTPXRequest(connect_timeout=20, read_timeout=40, write_timeout=120))
    app = (Application.builder().bot(transport).concurrent_updates(16)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.bot_data['service'] = service
    app.add_handler(MessageHandler(filters.ALL, observe_message), group=-100)
    for commands, handler in [(['start'],start), (['help'],help_command), (['menu'],main_menu), (['ocultarmenu'],hide_menu), (['buscar'],search), (['favorites','favoritos'],favorites), (['playlists'],playlists), (['crearplaylist'],create_playlist), (['cancelar'],cancel), (['limpiar','limpiarchat','clearchat','detener','cerrarplaylist','stop'],clean_chat)]:
        app.add_handler(CommandHandler(commands, handler))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=['message','callback_query'], drop_pending_updates=False)

if __name__ == '__main__': main()

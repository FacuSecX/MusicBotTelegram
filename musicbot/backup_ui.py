# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Private menu for portable library export/import, separate from bot secrets."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup

from .library_backup import BackupError, MAX_BACKUP_BYTES, export_library, import_library, validate_backup
from .mega_transfer import MegaError, MegaTransfer, validate_mega_file_link


_TRANSFER_ERRORS = {
    'configuration': 'La cuenta de MEGA no está configurada. Completá el asistente de MEGA del servidor.',
    'credentials': 'MEGA no aceptó la sesión configurada. Revisá la cuenta desde el servidor.',
    'mfa': 'MEGA necesita verificar la cuenta desde el asistente del servidor.',
    'not_installed': 'Falta instalar MEGAcmd en el servidor.',
    'network': 'No pude conectar con MEGA. Probá nuevamente más tarde.',
    'timeout': 'MEGA tardó demasiado. La operación se canceló.',
    'busy': 'Hay otra operación de MEGA en curso. Probá nuevamente en unos segundos.',
    'too_large': 'El backup supera los 10 MB permitidos.',
    'invalid_link': 'Pegá únicamente el enlace público de un archivo de MEGA, incluyendo su clave.',
    'invalid_file': 'El enlace no contiene un archivo de backup válido.',
}
_BACKUP_ERRORS = {
    'invalid_backup': 'El archivo no es un backup válido de playlists de Music Bot. Tu biblioteca se conservó.',
    'too_large': 'El backup supera los 10 MB permitidos. Tu biblioteca se conservó.',
    'configuration': 'No pude preparar el backup en el servidor.',
    'busy': 'Hay una actualización de la biblioteca en curso. Probá nuevamente.',
    'export_failed': 'No pude preparar el backup de tus playlists.',
    'import_failed': 'No pude importar el backup. Tu biblioteca anterior se conservó.',
}


async def _allowed(update, context):
    from .bot import allowed
    return await allowed(update, context)


async def _render(update, context, text, markup=None):
    from .bot import render
    return await render(update, context, text, markup)


class BackupController:
    def __init__(self, service):
        self.service = service
        self.transfer = None  # Lazily initialized; importing the bot creates no MEGA session.
        database = service.store.db.execute('PRAGMA database_list').fetchone()[2]
        self.base = Path(__file__).resolve().parents[1]
        self.data_dir = Path(database).parent if database else self.base / 'data'
        service.store.db.execute('''CREATE TABLE IF NOT EXISTS library_backup_exports (
            user_id INTEGER PRIMARY KEY, url TEXT NOT NULL, created_at TEXT NOT NULL)''')
        service.store.db.commit()

    def _mega(self):
        if self.transfer is None:
            home = Path(os.environ.get('MEGA_HOME') or self.data_dir / 'mega-home')
            if not home.is_absolute():
                home = self.base / home
            email, password = os.environ.get('MEGA_EMAIL'), os.environ.get('MEGA_PASSWORD')
            self.transfer = MegaTransfer(home, remote_dir=os.environ.get('MEGA_REMOTE_DIR') or None,
                                         max_bytes=MAX_BACKUP_BYTES, timeout=120,
                                         email=email or None, password=password or None)
        return self.transfer

    def last_url(self, uid):
        row = self.service.store.db.execute('SELECT url FROM library_backup_exports WHERE user_id=?', (uid,)).fetchone()
        if row:
            try:
                return validate_mega_file_link(row['url'])
            except MegaError:
                pass
        return None

    def _current(self, update, revision):
        return self.service.panels.revision(update.effective_user.id, update.effective_chat.id) == revision

    def _directory(self, name):
        path = self.data_dir / name
        if path.is_symlink():
            raise BackupError('configuration', _BACKUP_ERRORS['configuration'])
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise BackupError('configuration', _BACKUP_ERRORS['configuration'])
        if os.name == 'posix':
            path.chmod(0o700)
        return path

    async def show(self, update, context, notice=None):
        if not await _allowed(update, context):
            return
        uid = update.effective_user.id
        link = self.last_url(uid)
        rows = [[Button('📥 Importar Backup', callback_data='backup:import')],
                [Button('📤 Exportar Backup', callback_data='backup:export')],
                [Button('⬇️ Descargar Backup', url=link)] if link else
                [Button('⬇️ Descargar Backup', callback_data='backup:download')],
                [Button('‹ Atrás', callback_data='menu')]]
        text = ('☁️ Backups de playlists\n\nExportá o importá tus playlists y favoritos. '
                'El archivo no incluye audios, Tokens ni la configuración del servidor.')
        if notice:
            text += '\n\n' + notice
        return await _render(update, context, text, Markup(rows))

    async def handle_callback(self, update, context, action):
        if not await _allowed(update, context):
            return
        context.user_data.pop('awaiting_playlist', None)
        if action == 'import':
            context.user_data['awaiting_backup_import'] = True
            await _render(update, context,
                '📥 Importar Backup\n\nPegá el enlace público de MEGA del archivo de backup. '
                'Se reemplazarán tus playlists y favoritos actuales; la configuración del bot se conserva.',
                Markup([[Button('‹ Atrás', callback_data='backup:menu')]]))
        elif action == 'export':
            context.user_data.pop('awaiting_backup_import', None)
            await self._export(update, context)
        elif action in {'menu', 'download'}:
            context.user_data.pop('awaiting_backup_import', None)
            if action == 'download':
                link = self.last_url(update.effective_user.id)
                await self.show(update, context, 'Enlace de tu último backup:\n' + link if link else
                                'Todavía no exportaste un backup. Tocá Exportar Backup para crear el primero.')
            else:
                await self.show(update, context)
        else:
            await self.show(update, context, 'Esta opción no está disponible.')

    def _start(self, uid):
        if uid in self.service.busy:
            return False
        self.service.busy.add(uid)
        self.service.play_tasks[uid] = asyncio.current_task()
        return True

    def _finish(self, update):
        uid, chat = update.effective_user.id, update.effective_chat.id
        self.service.busy.discard(uid)
        if self.service.play_tasks.get(uid) is asyncio.current_task():
            self.service.play_tasks.pop(uid, None)
        self.service.panels.release(uid, chat)

    async def _export(self, update, context):
        uid = update.effective_user.id
        if not self._start(uid):
            await self.show(update, context, 'Todavía estoy procesando tu solicitud anterior.')
            return
        revision = None
        try:
            await _render(update, context, '📤 Preparando y subiendo el backup a MEGA…',
                          Markup([[Button('‹ Atrás', callback_data='backup:menu')]]))
            revision = self.service.panels.revision(uid, update.effective_chat.id)
            async with self.service.audio_lock:
                content = export_library(self.service.store, uid, bot_id=context.bot.id)
                summary = validate_backup(content)
                with tempfile.TemporaryDirectory(prefix='export-', dir=self._directory('backup-jobs')) as folder:
                    filename = 'playlists-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex + '.musicbot.json'
                    path = Path(folder) / filename
                    with path.open('xb') as output:
                        output.write(content)
                    if os.name == 'posix':
                        path.chmod(0o600)
                    link = validate_mega_file_link(await self._mega().upload(path))
                with self.service.store.db:
                    self.service.store.db.execute('''INSERT INTO library_backup_exports(user_id,url,created_at) VALUES(?,?,?)
                        ON CONFLICT(user_id) DO UPDATE SET url=excluded.url,created_at=excluded.created_at''',
                        (uid, link, datetime.now(timezone.utc).isoformat()))
            if self._current(update, revision):
                await self.show(update, context, f"✅ Backup exportado: {len(summary['playlists'])} playlists, "
                    f"{len(summary['favorites'])} favoritos y {len(summary['tracks'])} canciones.\n\nEnlace de MEGA:\n{link}")
        except (MegaError, BackupError) as error:
            if revision is None or self._current(update, revision):
                mapping = _TRANSFER_ERRORS if isinstance(error, MegaError) else _BACKUP_ERRORS
                await self.show(update, context, mapping.get(error.kind, 'No pude completar el backup. Probá nuevamente.'))
        except (OSError, sqlite3.Error):
            if revision is None or self._current(update, revision):
                await self.show(update, context, 'No pude preparar el backup. Revisá el espacio disponible del servidor.')
        finally:
            self._finish(update)

    def _snapshot(self):
        if self.service.store.db.in_transaction:
            raise BackupError('busy', _BACKUP_ERRORS['busy'])
        path = self._directory('library-backups') / ('pre-import-' + uuid.uuid4().hex + '.sqlite3')
        created = False
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            created = True
            destination = sqlite3.connect(str(path))
            try:
                self.service.store.db.backup(destination)
                if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise BackupError('configuration', _BACKUP_ERRORS['configuration'])
            finally:
                destination.close()
            return path
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _restart(context):
        app = context.application
        stop = getattr(app, 'stop_running', None)
        if getattr(app, 'running', False) is True and callable(stop):
            app.bot_data['restart_after_backup_import'] = True
            def later():
                if getattr(app, 'running', False) is True:
                    stop()
            asyncio.get_running_loop().call_later(1.5, later)

    async def import_text(self, update, context):
        if not await _allowed(update, context):
            return False
        if not context.user_data.get('awaiting_backup_import'):
            return False
        try:
            link = validate_mega_file_link((update.effective_message.text or '').strip())
        except MegaError:
            await _render(update, context, _TRANSFER_ERRORS['invalid_link'],
                          Markup([[Button('‹ Atrás', callback_data='backup:menu')]]))
            return True
        uid = update.effective_user.id
        if not self._start(uid):
            await _render(update, context, 'Todavía estoy procesando tu solicitud anterior.')
            return True
        committed, revision = False, None
        try:
            await _render(update, context, '📥 Descargando y verificando el backup de MEGA…',
                          Markup([[Button('‹ Atrás', callback_data='backup:menu')]]))
            revision = self.service.panels.revision(uid, update.effective_chat.id)
            async with self.service.audio_lock:
                with tempfile.TemporaryDirectory(prefix='import-', dir=self._directory('backup-jobs')) as folder:
                    path = Path(folder) / 'library.musicbot.json'
                    downloaded = Path(await self._mega().download(link, path))
                    if downloaded != path or path.is_symlink() or not path.is_file():
                        raise BackupError('invalid_backup', _BACKUP_ERRORS['invalid_backup'])
                    if not 0 < path.stat().st_size <= MAX_BACKUP_BYTES:
                        raise BackupError('too_large', _BACKUP_ERRORS['too_large'])
                    with path.open('rb') as stream:
                        content = stream.read(MAX_BACKUP_BYTES + 1)
                    validate_backup(content)
                    if not context.user_data.get('awaiting_backup_import') or not self._current(update, revision):
                        return True
                    self._snapshot()
                    summary = import_library(self.service.store, uid, content, bot_id=context.bot.id)
                    committed = True
                context.user_data.pop('awaiting_backup_import', None)
                self.service.cache.clear()
                self.service.alternative_choices.clear()
                self.service.panels.set_data(uid, update.effective_chat.id,
                    search_ids=[], active_native_session=None, playback_back='playmenus')
            if self._current(update, revision):
                await self.show(update, context, f"✅ Backup importado: {summary['playlists']} playlists, "
                    f"{summary['favorites']} favoritos y {summary['tracks']} canciones.\n\n"
                    'El servicio del bot se reiniciará para cargar la biblioteca restaurada.')
        except (MegaError, BackupError) as error:
            if revision is None or self._current(update, revision):
                mapping = _TRANSFER_ERRORS if isinstance(error, MegaError) else _BACKUP_ERRORS
                await _render(update, context, mapping.get(error.kind, 'No pude importar el backup. Tu biblioteca se conservó.'),
                              Markup([[Button('‹ Atrás', callback_data='backup:menu')]]))
        except (OSError, sqlite3.Error):
            if revision is None or self._current(update, revision):
                text = ('La biblioteca se importó. El servicio del bot se reiniciará.' if committed else
                        'No pude preparar la importación. Tu biblioteca actual se conservó.')
                await _render(update, context, text, Markup([[Button('‹ Atrás', callback_data='backup:menu')]]))
        finally:
            if committed:
                self._restart(context)
            self._finish(update)
        return True


async def backups_command(update, context):
    from .bot import svc
    if await _allowed(update, context):
        context.user_data.pop('awaiting_backup_import', None)
        context.user_data.pop('awaiting_playlist', None)
        await svc(context).backups.show(update, context)


async def backup_attachment(update, context):
    if not context.user_data.get('awaiting_backup_import'):
        return
    if await _allowed(update, context):
        await _render(update, context, 'Para importar un backup, pegá únicamente su enlace público de MEGA. '
                      'No se aceptan archivos adjuntos.', Markup([[Button('‹ Atrás', callback_data='backup:menu')]]))

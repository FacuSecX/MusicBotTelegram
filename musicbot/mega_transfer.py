# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Bounded MEGAcmd transfers using a private session and append-only backups."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

MAX_BACKUP_BYTES = 10 * 1024 * 1024
_REMOTE = re.compile(r'^/MusicBotBackups-[A-Za-z0-9_-]{8,100}$')
_HANDLE = r'[A-Za-z0-9_-]{8}'
_KEY = r'[A-Za-z0-9_-]{43}'
_LINK = re.compile(r'https://(?:www\.)?mega\.nz/[^\s<>"\']+')
_MESSAGES = {
    'configuration': 'La conexión de MEGA no está configurada correctamente.',
    'not_installed': 'Falta instalar el cliente MEGAcmd en el servidor.',
    'credentials': 'MEGA no aceptó los datos de acceso.',
    'mfa': 'MEGA necesita el código de verificación de tu cuenta.',
    'network': 'No se pudo completar la conexión con MEGA. Probá de nuevo más tarde.',
    'timeout': 'MEGA tardó demasiado en responder. La operación fue cancelada.',
    'invalid_link': 'Pegá un enlace público de un archivo de MEGA, incluyendo su clave.',
    'too_large': 'El backup supera los 10 MB permitidos.',
    'invalid_file': 'El enlace no contiene un archivo JSON de backup válido.',
    'busy': 'Hay otra operación de MEGA en curso. Probá de nuevo en unos segundos.',
}


class MegaError(Exception):
    def __init__(self, kind: str, detail: str | None = None):
        self.kind = kind
        self.detail = detail or _MESSAGES.get(kind, _MESSAGES['network'])
        super().__init__(self.detail)


def validate_mega_file_link(value: str) -> str:
    """Accept public file links only; folders, redirects and arbitrary hosts fail."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(c) < 33 for c in value):
        raise MegaError('invalid_link')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in {'mega.nz', 'www.mega.nz'}
                or parsed.username is not None or parsed.password is not None or parsed.port is not None
                or parsed.query):
            raise ValueError
        if re.fullmatch('/file/' + _HANDLE, parsed.path) and re.fullmatch(_KEY, parsed.fragment):
            return 'https://mega.nz' + parsed.path + '#' + parsed.fragment
        legacy = re.fullmatch('!(' + _HANDLE + ')!(' + _KEY + ')', parsed.fragment)
        if parsed.path == '/' and legacy:
            return f'https://mega.nz/file/{legacy[1]}#{legacy[2]}'
    except (ValueError, TypeError, AttributeError):
        pass
    raise MegaError('invalid_link')


def _private_home(home: Path) -> Path:
    home = Path(home).absolute()
    if home.is_symlink() or not home.is_absolute():
        raise MegaError('configuration')
    # The official POSIX client puts its Unix socket below HOME/.megaCmd.
    # Reject a too-long socket path instead of silently sharing /tmp fallback.
    if os.name == 'posix' and len(os.fsencode(str(home / '.megaCmd' / 'mb.sock'))) >= 102:
        raise MegaError('configuration', 'La ruta privada de MEGA es demasiado larga.')
    try:
        home.mkdir(parents=True, exist_ok=True)
        if not home.is_dir():
            raise OSError
        if os.name == 'posix':
            if home.stat().st_uid != os.geteuid():
                raise OSError
            home.chmod(0o700)
    except OSError:
        raise MegaError('configuration') from None
    return home


def mega_environment(home: Path) -> dict:
    """Never pass the bot Token or unrelated application secrets to MEGAcmd."""
    return {'HOME': str(home), 'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
            'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TERM': 'dumb',
            'MEGACMD_SOCKET_NAME': 'mb.sock'}


def _settings(home: Path, remote_dir: str | None) -> str:
    path = home / 'backup-settings.json'
    if path.is_symlink():
        raise MegaError('configuration')
    existing = None
    try:
        if path.exists():
            if path.stat().st_size > 2048:
                raise ValueError
            data = json.loads(path.read_text(encoding='utf-8'))
            existing = data['remote_dir']
            if not isinstance(existing, str) or not _REMOTE.fullmatch(existing):
                raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise MegaError('configuration') from None
    if remote_dir is not None and (not isinstance(remote_dir, str) or not _REMOTE.fullmatch(remote_dir)):
        raise MegaError('configuration')
    if existing is not None:
        if remote_dir is not None and remote_dir != existing:
            raise MegaError('configuration', 'La carpeta privada de MEGA ya tiene otro destino configurado.')
        return existing
    chosen = remote_dir or '/MusicBotBackups-' + uuid.uuid4().hex
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump({'version': 1, 'remote_dir': chosen}, stream)
        if os.name == 'posix':
            path.chmod(0o600)
    except FileExistsError:
        return _settings(home, remote_dir)
    except OSError:
        raise MegaError('configuration') from None
    return chosen


def _classify(output: str, default='network') -> str:
    text = output.lower()
    if any(x in text for x in ('two-factor', 'two factor', 'multi-factor', 'multifactor', 'mfa', 'authentication code', 'auth-code', '2fa')):
        return 'mfa'
    if any(x in text for x in ('not logged', 'notloggedin', 'invalid password', 'wrong password',
                               'incorrect password', 'login failed', 'invalid email', 'access denied')):
        return 'credentials'
    return default


def _session_matches(output: str, email: str) -> bool:
    addresses = re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", output)
    return email.casefold() in {address.casefold() for address in addresses}


async def _read_bounded(stream, maximum=256 * 1024):
    chunks, count = [], 0
    while True:
        block = await stream.read(65536)
        if not block:
            return b''.join(chunks)
        count += len(block)
        if count > maximum:
            raise MegaError('network')
        chunks.append(block)


async def _stop_process(process):
    if process.returncode is not None:
        return
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except asyncio.TimeoutError:
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, OSError):
            pass
        await process.wait()


def _folder_size(folder: Path) -> int:
    total = 0
    for path in folder.rglob('*'):
        if path.is_symlink():
            raise MegaError('invalid_file')
        if path.is_file():
            total += path.stat().st_size
    return total


class MegaTransfer:
    """A private MEGA client. Remote operations only create and export new files."""
    def __init__(self, home: Path, remote_dir: str | None = None,
                 max_bytes: int = MAX_BACKUP_BYTES, timeout: float = 120,
                 *, email: str | None = None, password: str | None = None):
        if (not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_BACKUP_BYTES
                or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 600):
            raise MegaError('configuration')
        self.home = _private_home(home)
        self.remote_dir = _settings(self.home, remote_dir)
        self.max_bytes, self.timeout = max_bytes, float(timeout)
        self.environment = mega_environment(self.home)
        self._email = self._password = None
        if email is not None or password is not None:
            self._email, self._password, _ = _credentials(email, password)
        self._lock = asyncio.Lock()
        self.work = self.home / 'transfers'
        if self.work.is_symlink():
            raise MegaError('configuration')
        self.work.mkdir(exist_ok=True)
        if os.name == 'posix':
            self.work.chmod(0o700)

    @asynccontextmanager
    async def _operation(self):
        async with self._lock:
            descriptor = None
            try:
                if os.name == 'posix':
                    import fcntl
                    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
                    descriptor = os.open(self.home / 'operation.lock', flags, 0o600)
                    deadline = time.monotonic() + self.timeout
                    while True:
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise MegaError('busy')
                            await asyncio.sleep(0.1)
                yield
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    async def _cancel_transfers(self):
        # The daemon continues after a CLI is killed. Cancel transfers on this
        # HOME's isolated socket as well; never delete or revoke remote files.
        for command, arguments in [('mega-transfers', ['-c', '-a']), ('mega-quit', [])]:
            executable = shutil.which(command)
            if executable is None:
                continue
            process = None
            try:
                process = await asyncio.create_subprocess_exec(executable, *arguments,
                    env=self.environment, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    **({'start_new_session': True, 'umask': 0o077} if os.name == 'posix' else {}))
                await asyncio.wait_for(process.wait(), 10)
                if process.returncode == 0:
                    return
            except (OSError, asyncio.TimeoutError):
                if process is not None:
                    await _stop_process(process)

    async def _run(self, command: str, *arguments: str, monitor: Path | None = None) -> str:
        if command not in {'whoami', 'mkdir', 'put', 'export', 'get'}:
            raise MegaError('configuration')
        executable = shutil.which('mega-' + command)
        if executable is None:
            raise MegaError('not_installed')
        try:
            process = await asyncio.create_subprocess_exec(executable, *map(str, arguments),
                env=self.environment, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                **({'start_new_session': True, 'umask': 0o077} if os.name == 'posix' else {}))
        except OSError:
            raise MegaError('network') from None
        async def collect():
            out, err = await asyncio.gather(_read_bounded(process.stdout), _read_bounded(process.stderr))
            await process.wait()
            return out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')
        pending = asyncio.create_task(collect())
        deadline = time.monotonic() + self.timeout
        transfer = command in {'get', 'put'}
        try:
            while True:
                if monitor is not None and _folder_size(monitor) > self.max_bytes:
                    raise MegaError('too_large')
                if pending.done():
                    out, err = await pending
                    break
                if time.monotonic() >= deadline:
                    raise MegaError('timeout')
                await asyncio.wait({pending}, timeout=0.1)
            if monitor is not None and _folder_size(monitor) > self.max_bytes:
                raise MegaError('too_large')
            if process.returncode:
                # Some MEGAcmd versions report an existing folder even with -p.
                # Only our exact validated backup folder may treat this as success.
                existing_backup_folder = (
                    command == 'mkdir' and arguments == ('-p', self.remote_dir)
                    and _REMOTE.fullmatch(self.remote_dir) is not None
                    and process.returncode == 54
                    and re.search(r'\balready exists\b', (err + '\n' + out).casefold()) is not None
                )
                if not existing_backup_folder:
                    raise MegaError(_classify(err + '\n' + out))
            return out
        except BaseException:
            await asyncio.shield(_stop_process(process))
            if transfer:
                await asyncio.shield(self._cancel_transfers())
            raise
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def check(self) -> bool:
        async with self._operation():
            await self._ensure_authenticated()
        return True

    async def _ensure_authenticated(self):
        try:
            current = await self._run('whoami')
        except MegaError as error:
            if error.kind != 'credentials' or self._email is None:
                raise
            await _login_async(self.home, self._email, self._password, None, self.timeout)
            await self._run('whoami')
        else:
            if self._email is not None and not _session_matches(current, self._email):
                raise MegaError('configuration', 'La sesión privada de MEGA corresponde a otra cuenta.')

    async def upload(self, path: Path) -> str:
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != '.json':
            raise MegaError('invalid_file')
        size = path.stat().st_size
        if not 0 < size <= self.max_bytes:
            raise MegaError('too_large' if size else 'invalid_file')
        async with self._operation():
            await self._ensure_authenticated()
            await self._run('mkdir', '-p', self.remote_dir)
            name = 'playlist-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex + '.musicbot.json'
            with tempfile.TemporaryDirectory(prefix='upload-', dir=self.work) as folder:
                prepared = Path(folder) / name
                shutil.copyfile(path, prepared)
                if os.name == 'posix':
                    prepared.chmod(0o600)
                await self._run('put', str(prepared), self.remote_dir + '/')
                output = await self._run('export', '-a', '-f', self.remote_dir + '/' + name)
                links = _LINK.findall(output)
                for link in links:
                    try:
                        return validate_mega_file_link(link.rstrip(').,;'))
                    except MegaError:
                        pass
                raise MegaError('network', 'MEGA subió el backup pero no devolvió un enlace válido.')

    async def download(self, url: str, destination: Path) -> Path:
        url = validate_mega_file_link(url)
        destination = Path(destination)
        if destination.suffix.lower() != '.json' or destination.exists() or destination.is_symlink():
            raise MegaError('invalid_file')
        if destination.parent.is_symlink() or not destination.parent.is_dir():
            raise MegaError('invalid_file')
        async with self._operation():
            with tempfile.TemporaryDirectory(prefix='download-', dir=self.work) as folder:
                temporary = Path(folder)
                await self._run('get', url, str(temporary), monitor=temporary)
                files = list(temporary.iterdir())
                if len(files) != 1 or files[0].is_symlink() or not files[0].is_file() or files[0].suffix.lower() != '.json':
                    raise MegaError('invalid_file')
                source = files[0]
                size = source.stat().st_size
                if not 0 < size <= self.max_bytes:
                    raise MegaError('too_large' if size else 'invalid_file')
                created = False
                try:
                    with source.open('rb') as stream, destination.open('xb') as output:
                        created = True
                        count = 0
                        while block := stream.read(65536):
                            count += len(block)
                            if count > self.max_bytes:
                                raise MegaError('too_large')
                            output.write(block)
                    if os.name == 'posix':
                        destination.chmod(0o600)
                except BaseException:
                    if created:
                        destination.unlink(missing_ok=True)
                    raise
        return destination


def _credentials(email, password, auth_code=None):
    if (not isinstance(email, str) or not 3 <= len(email) <= 254 or email.count('@') != 1
            or any(ord(c) < 33 or c in '\\"\'' for c in email)
            or not isinstance(password, str) or not 1 <= len(password) <= 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in password)):
        raise MegaError('credentials')
    if auth_code not in {None, ''} and (not isinstance(auth_code, str) or not re.fullmatch(r'[0-9]{6}', auth_code)):
        raise MegaError('mfa')
    return email, password, auth_code or None


def _login_pty(home: Path, email: str, password: str, auth_code: str | None, timeout: float,
               cancelled: threading.Event | None = None):
    """The password is sent only after the official shell asks for it, not in argv/history."""
    if os.name != 'posix':
        raise MegaError('configuration', 'La configuración automática de MEGA requiere Debian.')
    import pty
    import select
    import termios
    executable = shutil.which('mega-cmd')
    if executable is None:
        raise MegaError('not_installed')
    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    process = None
    try:
        process = subprocess.Popen([executable], stdin=slave, stdout=slave, stderr=slave,
                                   env=mega_environment(home), start_new_session=True, close_fds=True, umask=0o077)
        os.close(slave); slave = None
        deadline = time.monotonic() + timeout
        transcript = bytearray()
        def wait_for_password_or_prompt(password_stage=False):
            while time.monotonic() < deadline:
                if cancelled is not None and cancelled.is_set():
                    raise MegaError('network')
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(master, 8192)
                    except OSError:
                        chunk = b''
                    if not chunk:
                        break
                    transcript.extend(chunk)
                    if len(transcript) > 256 * 1024:
                        raise MegaError('network')
                    clean = re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', bytes(transcript)).lower()
                    if any(token in clean for token in (b'two-factor', b'multi-factor', b'authentication code', b'2fa', b'mfa')) and not auth_code:
                        raise MegaError('mfa')
                    if password_stage:
                        if b'password:' in clean:
                            transcript.clear(); return
                        if any(token in clean for token in (b'invalid email', b'already logged', b'login failed')):
                            raise MegaError('credentials')
                    elif b'mega cmd>' in clean or re.search(rb':[\x20-\x7e]*\$\s*$', clean):
                        return
                if process.poll() is not None:
                    break
            raise MegaError('timeout')
        wait_for_password_or_prompt()
        transcript.clear()
        command = 'login ' + (('--auth-code=' + auth_code + ' ') if auth_code else '') + '"' + email + '"\n'
        os.write(master, command.encode('utf-8'))
        wait_for_password_or_prompt(password_stage=True)
        # MEGAcmd's password reader consumes raw characters and accepts Enter
        # as CR (ASCII 13), even on Linux; LF would remain in its input buffer.
        os.write(master, password.encode('utf-8') + b'\r')
        transcript.clear()
        wait_for_password_or_prompt()
        combined = bytes(transcript).decode('utf-8', 'replace')
        if _classify(combined, ''):
            raise MegaError(_classify(combined))
        os.write(master, b'quit --only-shell\n')
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate(); process.wait(timeout=3)
    except (OSError, subprocess.SubprocessError):
        raise MegaError('network') from None
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()
        os.close(master)
        if slave is not None:
            os.close(slave)


async def _login_async(home, email, password, auth_code, timeout):
    cancelled = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(_login_pty, home, email, password, auth_code, timeout, cancelled))
    try:
        await asyncio.shield(worker)
    except BaseException:
        cancelled.set()
        await asyncio.gather(worker, return_exceptions=True)
        raise


async def setup_session(home: Path, email: str, password: str, *, auth_code: str | None = None,
                        remote_dir: str | None = None, timeout: float = 120) -> dict:
    email, password, auth_code = _credentials(email, password, auth_code)
    transfer = MegaTransfer(home, remote_dir, timeout=timeout)
    async with transfer._operation():
        try:
            current = await transfer._run('whoami')
        except MegaError as error:
            if error.kind not in {'credentials', 'network'}:
                raise
        else:
            if _session_matches(current, email):
                return {'ok': True, 'remote_dir': transfer.remote_dir}
            raise MegaError('configuration', 'La carpeta privada de MEGA ya está asociada a otra cuenta.')
        await _login_async(transfer.home, email, password, auth_code, timeout)
        await transfer._run('whoami')
    # SDK credentials stay only in the dedicated private HOME; the export
    # format never contains that directory, the password or the bot Token.
    if os.name == 'posix':
        for path in transfer.home.rglob('*'):
            if not path.is_symlink() and path.is_file():
                path.chmod(0o600)
    return {'ok': True, 'remote_dir': transfer.remote_dir}

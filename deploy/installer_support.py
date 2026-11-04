# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Private credential checks and configuration writing for instalarbotmusica.sh.

The token travels through stdin, never argv, logs, an error URL or source code.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

TOKEN = re.compile(r'^[0-9]{6,14}:[A-Za-z0-9_-]{20,80}$')


def api_root(value: str, allow_test: bool = False) -> str:
    value = value.rstrip('/')
    if value == 'https://api.telegram.org':
        return value
    parsed = urlsplit(value)
    if (allow_test and parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', '::1', 'localhost'}
            and parsed.port and parsed.path in {'', '/'} and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment):
        return value
    raise ValueError('El destino de validación no es el servidor oficial o un servidor local de prueba autorizado.')


def read_pair(stream=None) -> tuple[str, str]:
    raw = (stream or sys.stdin.buffer).read(1024)
    fields = raw.split(b'\0')
    if len(fields) != 3 or fields[-1] or len(raw) >= 1024:
        raise ValueError('La entrada privada no es válida.')
    return fields[0].decode('utf-8'), fields[1].decode('utf-8')


def check_credentials(token: str, chat_id: str, base='https://api.telegram.org', allow_test=False, opener=urlopen) -> tuple[int, str]:
    try:
        base = api_root(base, allow_test)
    except (ValueError, TypeError):
        return 13, 'El servidor de validación configurado no es válido.'
    if not TOKEN.fullmatch(token):
        return 10, 'El Token no tiene el formato de BotFather. Volvé a introducir Token y ChatID.'
    if not re.fullmatch(r'[1-9][0-9]{0,18}', chat_id) or int(chat_id) > 9223372036854775807:
        return 11, 'El ChatID debe ser el número positivo de tu chat privado.'

    def call(method, fields=None):
        request = Request(base + '/bot' + token + '/' + method,
                          data=json.dumps(fields or {}).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with opener(request, timeout=20) as response:
                payload = json.load(response)
        except HTTPError as exc:
            # HTTPError's representation contains the URL and therefore the token.
            if exc.code in {401, 404}:
                return None, 'token'
            if exc.code in {400, 403}:
                return None, 'chat'
            return None, 'network'
        except (URLError, TimeoutError, OSError, ValueError):
            return None, 'network'
        if not isinstance(payload, dict):
            return None, 'network'
        if not payload.get('ok'):
            code = payload.get('error_code')
            return None, 'token' if code in {401, 404} else ('chat' if code in {400, 403} else 'network')
        return payload.get('result'), None

    me, error = call('getMe')
    if error:
        return (10, 'Telegram rechazó el Token. Volvé a introducir Token y ChatID.') if error == 'token' else (12, 'No se pudo contactar con Telegram. Los archivos de instalación se conservan; reintentá.')
    if not isinstance(me, dict) or not me.get('is_bot') or not isinstance(me.get('id'), int):
        return 10, 'No se confirmó una identidad válida de bot.'
    chat, error = call('getChat', {'chat_id': int(chat_id)})
    if error:
        if error == 'network':
            return 12, 'No se pudo verificar el chat por un problema de conexión. Reintentá.'
        return 11, 'El chat no está disponible para este bot. Abrí tu bot en Telegram, tocá Iniciar y volvé a introducir los datos.'
    if not isinstance(chat, dict) or chat.get('id') != int(chat_id) or chat.get('type') != 'private':
        return 11, 'El ChatID no corresponde a un chat privado accesible por este bot.'
    # Chat actions verify delivery permission without leaving a message in the chat.
    sent, error = call('sendChatAction', {'chat_id': int(chat_id), 'action': 'typing'})
    if error or sent is not True:
        return (12, 'La comprobación de conexión no terminó. Reintentá.') if error == 'network' else (11, 'El bot no puede escribir en ese chat. Tocá Iniciar o desbloquealo y reintentá.')
    return 0, 'Token y ChatID verificados. No se envió ningún mensaje.'


def write_environment(path: Path, token: str, chat_id: str, proxy='', test_base='', allow_test=False):
    if not TOKEN.fullmatch(token) or not chat_id.isdigit() or int(chat_id) <= 0:
        raise ValueError('No se escribe una configuración con datos inválidos.')
    # Import only after the venv dependencies have been installed.
    from dotenv import dotenv_values
    previous = dotenv_values(path) if path.is_file() else {}
    values = {key: value for key, value in previous.items() if value is not None and re.fullmatch(r'[A-Z][A-Z0-9_]*', key)}
    values.update(BOT_TOKEN=token, ALLOWED_USER_IDS=chat_id, DATA_DIR='data')
    values.setdefault('MAX_DURATION', '1200')
    values.pop('TRANSIENT_MESSAGE_TTL', None)
    values.setdefault('MESSAGE_TTL_SECONDS', '90')
    values.setdefault('AUDIO_CACHE_DIR', 'data/audio-cache')
    values.setdefault('AUDIO_CACHE_MB', '512')
    values.setdefault('AUDIO_CACHE_DAYS', '7')
    values.setdefault('AUDIO_FORMAT', 'm4a')
    if proxy:
        if proxy != 'socks5h://127.0.0.1:40000':
            raise ValueError('El proxy del instalador no es el endpoint local esperado.')
        values['YOUTUBE_PROXY'] = proxy
        values['YOUTUBE_WARP_RECOVERY'] = '1'
    else:
        values.pop('YOUTUBE_PROXY', None)
        values.pop('YOUTUBE_WARP_RECOVERY', None)
    if test_base:
        base = api_root(test_base, allow_test)
        if base == 'https://api.telegram.org':
            raise ValueError('El modo de prueba requiere un servidor local explícito.')
        values.update(MUSICBOT_INSTALL_TEST_MODE='1', BOT_API_BASE_URL=base+'/bot', BOT_API_FILE_BASE_URL=base+'/file/bot')
    else:
        for key in ('MUSICBOT_INSTALL_TEST_MODE', 'BOT_API_BASE_URL', 'BOT_API_FILE_BASE_URL'):
            values.pop(key, None)
    def quote(value):
        return "'" + str(value).replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r') + "'"
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix='.env.install-', delete=False) as stream:
            pending = Path(stream.name)
            stream.write('# Configuración privada generada por instalarbotmusica.sh\n')
            for key, value in values.items():
                stream.write(key + '=' + quote(value) + '\n')
        pending.chmod(0o600)
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('validate', 'write-env', 'check-api', 'check-warp'))
    parser.add_argument('--api-root', default='https://api.telegram.org')
    parser.add_argument('--allow-test-api', action='store_true')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--proxy', default='')
    args = parser.parse_args()
    try:
        if args.action == 'check-api':
            api_root(args.api_root, args.allow_test_api)
            return
        if args.action == 'check-warp':
            def command(argument):
                try:
                    result=subprocess.run(['/usr/bin/warp-cli','--accept-tos',argument],capture_output=True,text=True,timeout=15)
                    return result.returncode==0,result.stdout
                except (OSError,subprocess.TimeoutExpired):return False,''
            ok,settings=command('settings')
            if not ok or not re.search(r'Mode:\s+WarpProxy on port 40000(?:\s|$)',settings):
                raise ValueError('WARP no está en modo proxy local.')
            ok,status=command('status')
            if not ok or not re.search(r'Status update:\s+Connected(?:\s|$)',status):
                if not command('connect')[0]:raise ValueError('No se pudo conectar el proxy.')
                for _ in range(15):
                    time.sleep(1);ok,status=command('status')
                    if ok and re.search(r'Status update:\s+Connected(?:\s|$)',status):break
                else:raise ValueError('El proxy no terminó de conectar.')
            return
        token, chat = read_pair()
        if args.action == 'validate':
            status, message = check_credentials(token, chat, args.api_root, args.allow_test_api)
            print(message, flush=True)
            raise SystemExit(status)
        if args.destination is None:
            raise ValueError('Falta el destino de configuración.')
        write_environment(args.destination, token, chat, args.proxy,
                          args.api_root if args.allow_test_api else '', args.allow_test_api)
        print('Configuración privada guardada.', flush=True)
    except (ValueError, UnicodeError, OSError):
        print('No se pudo completar el paso privado de configuración. No se imprimen credenciales.', file=sys.stderr)
        raise SystemExit(13)

if __name__ == '__main__':
    main()

# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Configure an isolated MEGA session; credentials are read from stdin only."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from musicbot.mega_transfer import MegaError, MegaTransfer, setup_session


def read_credentials(stream):
    raw = stream.read(16385)
    if len(raw) > 16384:
        raise MegaError('credentials')
    try:
        if b'\0' in raw:
            values = raw.decode('utf-8').split('\0')
            if values[-1] == '':
                values.pop()
            if len(values) not in {2, 3}:
                raise ValueError
            return {'email': values[0], 'password': values[1], 'auth_code': values[2] if len(values) == 3 else None}
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) - {'email', 'password', 'auth_code'}:
            raise ValueError
        return {'email': data['email'], 'password': data['password'], 'auth_code': data.get('auth_code')}
    except (ValueError, UnicodeError, KeyError, TypeError):
        raise MegaError('credentials') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'check'])
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--remote-dir')
    args = parser.parse_args(argv)
    try:
        if args.action == 'setup':
            credentials = read_credentials(sys.stdin.buffer)
            result = asyncio.run(setup_session(args.home, remote_dir=args.remote_dir, **credentials))
        else:
            transfer = MegaTransfer(args.home, args.remote_dir)
            asyncio.run(transfer.check())
            result = {'ok': True, 'remote_dir': transfer.remote_dir}
        print(json.dumps(result))
        return 0
    except MegaError as error:
        print(json.dumps({'ok': False, 'kind': error.kind, 'message': error.detail}))
        return {'credentials': 10, 'mfa': 11}.get(error.kind, 12)
    except Exception:
        print(json.dumps({'ok': False, 'kind': 'configuration', 'message': 'No se pudo configurar MEGA.'}))
        return 12


if __name__ == '__main__':
    raise SystemExit(main())

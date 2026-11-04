# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""A shared bounded MP3/M4A cache without user names or credentials."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
_FORMATS = frozenset({'mp3', 'm4a'})


class AudioCache:
    def __init__(self, root: Path, max_bytes: int = 512 * 1024 * 1024,
                 ttl_seconds: float = 7 * 86400):
        self.root = Path(root)
        if self.root.is_symlink() or max_bytes <= 0 or ttl_seconds <= 0:
            raise ValueError('La configuración de caché de audio no es válida.')
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self.ttl_seconds = float(ttl_seconds)
        self._last_access_ns = 0
        if os.name == 'posix':
            self.root.chmod(0o700)

    def _read_manifest(self, manifest):
        # Manifest contents can only select a fixed extension, never a path.
        if manifest.is_symlink():
            return None
        try:
            if not manifest.is_file() or manifest.stat().st_size > 2048:
                return None
            data = json.loads(manifest.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _paths(self, video_id, audio_format=None):
        if not isinstance(video_id, str) or not _ID.fullmatch(video_id):
            raise ValueError('Identificador de audio inválido.')
        manifest = self.root / (video_id + '.json')
        if audio_format is None:
            data = self._read_manifest(manifest)
            audio_format = data.get('format', 'mp3') if data is not None else 'mp3'
            if not isinstance(audio_format, str) or audio_format not in _FORMATS:
                audio_format = 'mp3'
        if not isinstance(audio_format, str) or audio_format not in _FORMATS:
            raise ValueError('Formato de audio inválido.')
        return self.root / (video_id + '.' + audio_format), manifest

    def _manifest(self, video_id):
        _, manifest = self._paths(video_id, 'mp3')
        data = self._read_manifest(manifest)
        if data is None:
            return None
        try:
            audio_format = data.get('format', 'mp3')
            if not isinstance(audio_format, str) or audio_format not in _FORMATS:
                return None
            audio, _ = self._paths(video_id, audio_format)
            if (audio.is_symlink() or not audio.is_file()
                    or data.get('id') != video_id or not isinstance(data.get('bytes'), int)
                    or isinstance(data['bytes'], bool) or not 0 < data['bytes'] <= self.max_bytes
                    or not isinstance(data.get('stored_at'), (float, int))
                    or isinstance(data['stored_at'], bool)
                    or not 0 <= time.time() - data['stored_at'] < self.ttl_seconds
                    or not re.fullmatch(r'[a-f0-9]{64}', str(data.get('sha256', '')))
                    or audio.stat().st_size != data['bytes']):
                return None
            data['format'] = audio_format
            return data
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def format_for(self, video_id: str) -> str | None:
        """Return the stored format, including MP3 manifests from older versions."""
        data = self._manifest(video_id)
        return data['format'] if data is not None else None

    def _access_stamp(self, previous=0):
        previous = previous if isinstance(previous, int) and not isinstance(previous, bool) else 0
        self._last_access_ns = max(time.time_ns(), self._last_access_ns + 1, previous + 1)
        return self._last_access_ns

    def _touch(self, video_id, data, manifest):
        # Store access order independently of filesystem mtime granularity.
        # Keep old manifests readable; an access upgrades one entry in place.
        current = self._manifest(video_id)
        if current is None or current['sha256'] != data['sha256'] or current['stored_at'] != data['stored_at']:
            return
        current['accessed_ns'] = self._access_stamp(current.get('accessed_ns', 0))
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix='incoming-', mode='w',
                                             encoding='utf-8', delete=False) as output:
                pending = Path(output.name)
                json.dump(current, output, separators=(',', ':'))
            if os.name == 'posix':
                pending.chmod(0o600)
            os.replace(pending, manifest)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

    def copy_to(self, video_id: str, destination: Path, max_bytes: int) -> bool:
        """Copy and verify into the caller's own temporary directory.

        Returning a separate file keeps an active upload safe from cache eviction.
        If another process replaces an entry concurrently, its hash cannot pass
        as this manifest's audio and the caller simply performs a fresh download.
        """
        data = self._manifest(video_id)
        if data is None or data['bytes'] > max_bytes:
            return False
        audio, manifest = self._paths(video_id, data['format'])
        destination = Path(destination)
        # If a concurrent replacement changed formats after format_for(), a
        # miss is safer than writing M4A bytes into the caller's MP3 filename.
        if destination.suffix.lower().lstrip('.') in _FORMATS and destination.suffix.lower() != '.' + data['format']:
            return False
        try:
            digest = hashlib.sha256()
            count = 0
            with audio.open('rb') as source, Path(destination).open('wb') as output:
                while chunk := source.read(256 * 1024):
                    count += len(chunk)
                    if count > min(max_bytes, self.max_bytes):
                        raise ValueError('El audio excedió el límite.')
                    digest.update(chunk)
                    output.write(chunk)
            if count != data['bytes'] or digest.hexdigest() != data['sha256']:
                raise ValueError('La copia de audio no coincide con su registro.')
            try:
                self._touch(video_id, data, manifest)
            except OSError:
                pass
            return True
        except (OSError, ValueError):
            Path(destination).unlink(missing_ok=True)
            return False

    def put(self, video_id: str, source: Path) -> bool:
        old_audio, manifest = self._paths(video_id)
        source = Path(source)
        audio_format = source.suffix.lower().lstrip('.')
        if audio_format not in _FORMATS:
            return False
        audio, manifest = self._paths(video_id, audio_format)
        try:
            size = source.stat().st_size
            if source.is_symlink() or not 0 < size <= self.max_bytes:
                return False
            self.prune()
            pending_audio = pending_manifest = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.root, prefix='incoming-', delete=False) as output:
                    pending_audio = Path(output.name)
                    digest = hashlib.sha256()
                    count = 0
                    with source.open('rb') as stream:
                        while chunk := stream.read(256 * 1024):
                            count += len(chunk)
                            if count > self.max_bytes:
                                raise ValueError('El audio excedió el límite.')
                            digest.update(chunk)
                            output.write(chunk)
                if count != size:
                    return False
                payload = dict(id=video_id, format=audio_format, bytes=size, sha256=digest.hexdigest(), stored_at=time.time(),
                               accessed_ns=self._access_stamp())
                with tempfile.NamedTemporaryFile(dir=self.root, prefix='incoming-', mode='w',
                                                 encoding='utf-8', delete=False) as output:
                    pending_manifest = Path(output.name)
                    json.dump(payload, output, separators=(',', ':'))
                if os.name == 'posix':
                    pending_audio.chmod(0o600)
                    pending_manifest.chmod(0o600)
                os.replace(pending_audio, audio)
                os.replace(pending_manifest, manifest)
                if old_audio != audio:
                    old_audio.unlink(missing_ok=True)
                self.prune()
                return self._manifest(video_id) is not None
            finally:
                for pending in (pending_audio, pending_manifest):
                    if pending is not None:
                        pending.unlink(missing_ok=True)
        except (OSError, ValueError):
            return False

    def _remove(self, audio: Path, manifest: Path):
        for item in (manifest, audio):
            # unlink removes a symlink itself; no target path is followed.
            try:
                item.unlink(missing_ok=True)
            except OSError:
                pass

    def prune(self):
        entries = []
        for manifest in self.root.glob('*.json'):
            if not _ID.fullmatch(manifest.stem):
                continue
            audio, _ = self._paths(manifest.stem)
            try:
                data = self._manifest(manifest.stem)
                if data is None:
                    self._remove(audio, manifest)
                else:
                    stamp = data.get('accessed_ns', manifest.stat().st_mtime_ns)
                    if not isinstance(stamp, int) or isinstance(stamp, bool) or not 0 <= stamp <= 10**20:
                        stamp = manifest.stat().st_mtime_ns
                    self._last_access_ns = max(self._last_access_ns, stamp)
                    entries.append((stamp, data['bytes'], audio, manifest))
            except OSError:
                continue
        total = sum(row[1] for row in entries)
        for _last_access, size, audio, manifest in sorted(entries):
            if total <= self.max_bytes:
                break
            self._remove(audio, manifest)
            total -= size
        # Failed writes or interrupted replacements never become cache hits.
        # Keep very recent orphan files so another process can finish its write.
        cutoff = time.time() - 300
        for audio in self.root.iterdir():
            if audio.suffix not in {'.mp3', '.m4a'}:
                continue
            if not _ID.fullmatch(audio.stem):
                continue
            manifest = self.root / (audio.stem + '.json')
            try:
                expected_audio, _ = self._paths(audio.stem)
                if audio.stat().st_mtime < cutoff and (not manifest.exists() or audio != expected_audio):
                    # An obsolete format must not remove the current manifest.
                    audio.unlink(missing_ok=True)
            except OSError:
                pass
        for pending in self.root.glob('incoming-*'):
            try:
                if pending.stat().st_mtime < cutoff:
                    pending.unlink(missing_ok=True)
            except OSError:
                pass

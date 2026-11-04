# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Bounded YouTube access, with explicit failures and disposable audio files."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import tempfile
from time import monotonic
import unicodedata
from urllib.parse import urlsplit

from .warp_recovery import LOCAL_WARP_PROXY, WarpRecovery
from .audio_cache import AudioCache

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Explicit clients available in yt-dlp 2026.10.26. An empty value retains
# yt-dlp's own defaults; these are configuration choices, not CLI fragments.
PLAYER_CLIENTS = frozenset({"web", "web_safari", "web_embedded", "web_music",
                            "web_creator", "android", "android_vr", "ios",
                            "visionos", "mweb", "tv", "tv_simply", "tv_downgraded", "tv_samsung"})
MAX_COOKIE_BYTES = 1024 * 1024
SEARCH_CACHE_SECONDS = 180
SEARCH_CACHE_ENTRIES = 128
FAST_AUDIO_EXTRACTOR_ARGS = "youtube:player_client=visionos;skip=hls,dash"


def _format_unavailable(text: str) -> bool:
    return any(message in str(text).lower() for message in (
        "requested format is not available", "requested formats are not available"))


def validate_player_client(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    clients = str(value).strip().split(",")
    if len(clients) > 4 or any(client not in PLAYER_CLIENTS for client in clients):
        raise ProviderError("configuration", "El cliente de YouTube configurado no es válido.")
    return ",".join(clients)


def validate_proxy(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        if (not isinstance(value, str) or len(value) > 4096
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)
                or "\\" in value):
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https", "socks5", "socks5h"}
                or not parts.hostname or parts.path not in {"", "/"}
                or parts.query or parts.fragment or "?" in value or "#" in value
                or any(ch in parts.hostname for ch in (';', '@', '"', "'"))
                or parts.netloc.rsplit("@", 1)[-1].endswith(":")):
            raise ValueError
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError
        return value
    except (TypeError, ValueError, AttributeError):
        # Validation errors intentionally omit the URL, hostname and credentials.
        raise ProviderError("configuration", "La conexión externa de YouTube configurada no es válida.") from None


def validate_cookie_file(value: str | Path | None) -> Path | None:
    if value is None or not str(value).strip():
        return None
    try:
        candidate = Path(value).expanduser()
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError
        info = candidate.stat()
        if not 0 < info.st_size <= MAX_COOKIE_BYTES:
            raise ValueError
        if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError
        with candidate.open("r", encoding="utf-8-sig") as stream:
            header = stream.readline().rstrip("\r\n")
            if header not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
                raise ValueError
            cookies = 0
            for line in stream:
                line = line.rstrip("\r\n")
                if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                    continue
                fields = line.removeprefix("#HttpOnly_").split("\t")
                if (len(fields) != 7 or fields[1] not in {"TRUE", "FALSE"}
                        or fields[3] not in {"TRUE", "FALSE"}
                        or not fields[4].lstrip("-").isdigit()
                        or not fields[0] or any(ch.isspace() for ch in fields[0])
                        or not fields[2].startswith("/") or not fields[5]):
                    raise ValueError
                cookies += 1
            if not cookies:
                raise ValueError
        return candidate.resolve(strict=True)
    except (OSError, ValueError, TypeError, UnicodeError):
        # Cookie parsers can otherwise echo the rejected line, including secrets.
        raise ProviderError("configuration", "El archivo de sesión de YouTube no es válido, no se puede leer o sus permisos no son privados.") from None


def safe_detail(text: str) -> str:
    text = re.sub(r"https?://[^\s]+", "[URL]", str(text))
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return text[-1800:]


def classify_error(text: str) -> str:
    value = text.lower()
    if "captcha" in value or "not a bot" in value or "confirm you're not" in value:
        return "captcha"
    if any(part in value for part in ("not made this video available in your country", "not available in your country",
                                      "not available in your region", "blocked in your country",
                                      "blocked it in your country", "not available in your location",
                                      "blocked in your region", "not available from your location",
                                      "geo-restricted", "geo restricted", "georestrictederror",
                                      "not available in this country", "not available in this region",
                                      "not available in your geographic region")):
        return "geo_restricted"
    if ("429" in value or "too many requests" in value or "rate limit" in value
            or "this content isn't available, try again later" in value.replace("\u2019", "'")):
        return "rate_limited"
    if "403" in value or "forbidden" in value:
        return "http_403"
    if "402" in value or "payment required" in value:
        return "http_402"
    if any(s in value for s in ("sign in", "login required", "authentication", "cookies", "age-restricted", "members-only")):
        return "auth_required"
    if any(s in value for s in ("404", "unavailable", "private video", "removed", "not available")):
        return "unavailable"
    if "timed out" in value or "timeout" in value:
        return "timeout"
    if any(s in value for s in ("connection", "network", "unable to download", "resolve", "certificate")):
        return "network"
    return "unknown"


class ProviderError(Exception):
    def __init__(self, kind: str, detail: str, *, format_unavailable: bool = False):
        self.kind = kind
        # Keep only this non-sensitive compatibility signal when proxy/account
        # diagnostics are hidden. Public error kinds and messages stay stable.
        self.format_unavailable = bool(format_unavailable or _format_unavailable(detail))
        self.detail = safe_detail(detail)
        super().__init__(self.detail)


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    duration: int | None
    url: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> Track:
        return cls(**{key: value[key] for key in ("id", "title", "artist", "duration", "url")})


def track_from_entry(entry: dict) -> Track | None:
    video_id = str(entry.get("id", ""))
    if not VIDEO_ID.fullmatch(video_id) or entry.get("live_status") in ("is_live", "is_upcoming"):
        return None
    duration = entry.get("duration")
    return Track(video_id, str(entry.get("title") or "Sin título")[:240],
                 str(entry.get("artist") or entry.get("uploader") or entry.get("channel") or "YouTube")[:160],
                 int(duration) if duration is not None else None,
                 f"https://www.youtube.com/watch?v={video_id}")


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        # Kill the session even if yt-dlp has already exited: ffmpeg can still
        # hold its inherited pipes and temporary files open.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif proc.returncode is None:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(killer.wait(), 5)
            except asyncio.TimeoutError:
                killer.kill()
                await killer.wait()
        except OSError:
            pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    await proc.wait()


async def _read_bounded(stream, cap: int) -> bytes:
    chunks = []
    length = 0
    while True:
        part = await stream.read(65536)
        if not part:
            return b"".join(chunks)
        length += len(part)
        if length > cap:
            raise ProviderError("unknown", "La respuesta de YouTube excedió el límite de diagnóstico.")
        chunks.append(part)


async def run_ytdlp(arguments: list[str], timeout: int = 120, *,
                    cookie_file: str | Path | None = None,
                    player_client: str | None = None,
                    proxy: str | None = None) -> tuple[str, str]:
    cookie_file = validate_cookie_file(cookie_file)
    player_client = validate_player_client(player_client)
    proxy = validate_proxy(proxy)
    base = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-colors",
            "--no-progress", "--socket-timeout", "20", "--retries", "1",
            "--extractor-retries", "1", "--fragment-retries", "1"]
    if proxy is not None:
        base += ["--proxy", proxy]
    if cookie_file is not None:
        base += ["--cookies", str(cookie_file)]
    if player_client is not None:
        base += ["--extractor-args", "youtube:player_client=" + player_client]
    if shutil.which("deno"):
        base += ["--js-runtimes", "deno"]
    elif shutil.which("node"):
        base += ["--js-runtimes", "node"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *base, *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"start_new_session": True} if os.name == "posix" else {}))
    except OSError as exc:
        if proxy is not None:
            raise ProviderError("network", "No se pudo iniciar la consulta de YouTube con la conexión configurada.") from None
        raise ProviderError("network", str(exc)) from exc
    readers = [asyncio.create_task(_read_bounded(proc.stdout, 2 * 1024 * 1024)),
               asyncio.create_task(_read_bounded(proc.stderr, 512 * 1024))]
    async def collect():
        out, err = await asyncio.gather(*readers)
        await proc.wait()
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    try:
        out, err = await asyncio.wait_for(collect(), timeout)
    except asyncio.TimeoutError as exc:
        await asyncio.shield(_stop(proc))
        raise ProviderError("timeout", f"YouTube no respondió dentro de {timeout} segundos.") from exc
    except BaseException:
        await asyncio.shield(_stop(proc))
        raise
    finally:
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
    if proc.returncode:
        detail = err or out or f"yt-dlp terminó con código {proc.returncode}"
        if proxy is not None:
            raise ProviderError(classify_error(detail), "No se pudo completar la consulta de YouTube con la conexión configurada.",
                                format_unavailable=_format_unavailable(detail))
        if cookie_file is not None:
            raise ProviderError(classify_error(detail), "YouTube rechazó la consulta autenticada. Revisá la sesión configurada.")
        raise ProviderError(classify_error(detail), detail)
    if proxy is not None and err:
        err = "YouTube devolvió advertencias con la conexión configurada."
    elif cookie_file is not None and err:
        err = "YouTube devolvió advertencias en la consulta autenticada."
    return out, err


class YouTubeProvider:
    def __init__(self, temp_root: Path, max_duration=1200, max_bytes=48 * 1024 * 1024,
                 *, cookie_file: str | Path | None = None, player_client: str | None = None,
                 proxy: str | None = None, recoverer=None, audio_cache_dir: str | Path | None = None,
                 audio_format: str | None = None):
        self.proxy = validate_proxy(os.environ.get("YOUTUBE_PROXY") if proxy is None else proxy)
        self.cookie_file = validate_cookie_file(os.environ.get("YOUTUBE_COOKIE_FILE") if cookie_file is None else cookie_file)
        self.player_client = validate_player_client(os.environ.get("YOUTUBE_PLAYER_CLIENT") if player_client is None else player_client)
        self.audio_format = os.environ.get("AUDIO_FORMAT", "m4a") if audio_format is None else audio_format
        if self.audio_format not in {"m4a", "mp3"}:
            raise ProviderError("configuration", "El formato de audio configurado debe ser m4a o mp3.")
        self.temp_root = Path(temp_root)
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.max_duration = int(max_duration)
        self.max_bytes = int(max_bytes)
        self.search_gate = asyncio.Semaphore(2)
        self.download_gate = asyncio.Semaphore(1)
        self._access_gate = asyncio.Lock()
        self._blocked_until = 0.0
        self._blocked_kind = None
        self.last_audio_metrics: dict = {}
        self.audio_cache = None
        cache_dir = os.environ.get("AUDIO_CACHE_DIR", "") if audio_cache_dir is None else audio_cache_dir
        if cache_dir:
            try:
                cache_mb = int(os.environ.get("AUDIO_CACHE_MB", "512"))
                cache_days = float(os.environ.get("AUDIO_CACHE_DAYS", "7"))
                if not 1 <= cache_mb <= 4096 or not math.isfinite(cache_days) or not 0 < cache_days <= 30:
                    raise ValueError
                self.audio_cache = AudioCache(Path(cache_dir), cache_mb * 1024 * 1024, cache_days * 86400)
            except (OSError, ValueError, TypeError):
                raise ProviderError("configuration", "La caché local de audio no se pudo configurar.") from None
        self.recoverer = None
        recovery_enabled = os.environ.get("YOUTUBE_WARP_RECOVERY", "").strip().lower() in {"1", "true"}
        if self.proxy == LOCAL_WARP_PROXY and (recovery_enabled or recoverer is not None):
            self.recoverer = recoverer if recoverer is not None else WarpRecovery(self.temp_root.parent / "warp-recovery.json")
        self.last_warnings: list[str] = []
        # Short-lived metadata only: audio availability is checked at download.
        self._search_cache = OrderedDict()
        self._search_pending: dict[str, asyncio.Task] = {}

    def _access_options(self) -> dict:
        # Searches deliberately remain anonymous: no account needed to list results.
        return {key: value for key, value in (("cookie_file", self.cookie_file),
                                              ("player_client", self.player_client),
                                              ("proxy", self.proxy)) if value is not None}

    def _check_access_cooldown(self):
        remaining = self._blocked_until - monotonic()
        if remaining > 0:
            error = ProviderError(self._blocked_kind, "YouTube mantiene una limitación general temporal. Las copias guardadas siguen disponibles.")
            error.retry_after = math.ceil(remaining)
            raise error

    def _record_access_failure(self, error):
        # These responses apply to a session/IP, rather than to a single title.
        # Never treat a regional, age, private-video or format error as global.
        seconds = {'captcha': 300, 'rate_limited': 120, 'http_402': 300}.get(error.kind)
        if seconds is not None:
            self._blocked_until = monotonic() + seconds
            self._blocked_kind = error.kind
            error.retry_after = seconds

    async def _run_access(self, arguments: list[str], timeout: int = 120) -> tuple[str, str]:
        options = self._access_options()
        if timeout != 120:
            options["timeout"] = timeout
        # Serial metadata/download access prevents queued requests from each
        # triggering the same challenge and reconnection after a global reject.
        async with self._access_gate:
            self._check_access_cooldown()
            try:
                try:
                    return await run_ytdlp(arguments, **options)
                except ProviderError as exc:
                    if exc.kind != "captcha" or self.recoverer is None:
                        raise
                    if not await self.recoverer.recover():
                        raise
                # Retry this exact video once only; no alternate guessing here.
                return await run_ytdlp(arguments, **options)
            except ProviderError as exc:
                self._record_access_failure(exc)
                raise

    def _search_finished(self, key: str, task: asyncio.Task) -> None:
        if self._search_pending.get(key) is task:
            del self._search_pending[key]
        if task.cancelled():
            return
        # Retrieve errors even when all callers were cancelled; failures never
        # enter the cache and a later request can retry normally.
        if task.exception() is not None:
            return
        tracks, warnings = task.result()
        now = monotonic()
        for stale in [name for name, entry in self._search_cache.items() if entry[0] <= now]:
            del self._search_cache[stale]
        self._search_cache[key] = (now + SEARCH_CACHE_SECONDS, tuple(tracks), tuple(warnings))
        self._search_cache.move_to_end(key)
        while len(self._search_cache) > SEARCH_CACHE_ENTRIES:
            self._search_cache.popitem(last=False)

    async def _search_uncached(self, query: str) -> tuple[list[Track], list[str]]:
        async with self.search_gate:
            out, err = await run_ytdlp(["--flat-playlist", "--dump-single-json", "--skip-download",
                                       "--playlist-end", "6", "--", f"ytsearch6:{query}"])
        try:
            payload = json.loads(out)
            tracks = [track_from_entry(item) for item in payload.get("entries", []) if item]
            tracks = [track for track in tracks if track is not None][:6]
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError("unknown", "YouTube devolvió una respuesta de búsqueda inválida.") from exc
        warnings = [safe_detail(line) for line in err.splitlines() if line.strip()]
        return tracks, warnings

    async def search(self, query: str) -> list[Track]:
        query = " ".join(unicodedata.normalize("NFKC", query).split())
        if not query or len(query) > 200:
            raise ProviderError("invalid_query", "Escribí una búsqueda de entre 1 y 200 caracteres.")
        key = query.casefold()
        cached = self._search_cache.get(key)
        if cached is not None:
            expires, tracks, warnings = cached
            if expires > monotonic():
                self._search_cache.move_to_end(key)
                self.last_warnings = list(warnings)
                return list(tracks)
            del self._search_cache[key]
        task = self._search_pending.get(key)
        if task is None:
            task = asyncio.create_task(self._search_uncached(query))
            self._search_pending[key] = task
            task.add_done_callback(lambda done: self._search_finished(key, done))
        # One cancelled Telegram request must not cancel the shared lookup.
        tracks, warnings = await asyncio.shield(task)
        self.last_warnings = list(warnings)
        return list(tracks)

    async def probe(self, video_id: str) -> tuple[Track, list[str]]:
        if not VIDEO_ID.fullmatch(video_id):
            raise ProviderError("invalid_query", "Identificador de video inválido.")
        out, err = await self._run_access(["--no-playlist", "--dump-single-json", "--skip-download",
                                   "--", f"https://www.youtube.com/watch?v={video_id}"])
        try:
            track = track_from_entry(json.loads(out))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError("unknown", "Metadatos inválidos.") from exc
        if track is None:
            raise ProviderError("unavailable", "Este video no está disponible como canción.")
        return track, [safe_detail(line) for line in err.splitlines() if line.strip()]

    @asynccontextmanager
    async def audio(self, track: Track):
        if not VIDEO_ID.fullmatch(track.id):
            raise ProviderError("invalid_query", "Identificador de video inválido.")
        if track.duration is not None and track.duration > self.max_duration:
            raise ProviderError("too_long", f"El audio supera los {self.max_duration // 60} minutos permitidos.")
        waiting = monotonic()
        async with self.download_gate:
            fast_profile = self.cookie_file is None and self.player_client is None
            metrics = dict(cache_hit=False, queue_wait_seconds=round(monotonic() - waiting, 4),
                           fast_profile=False, profile_fallback=False, audio_format=self.audio_format)
            self.last_audio_metrics = metrics
            started = monotonic()
            # Each caller owns its yielded copy. Eviction can never remove an
            # audio from an active Telegram upload or leave partial downloads.
            with tempfile.TemporaryDirectory(prefix="audio-", dir=self.temp_root) as folder:
                target = Path(folder)
                audio = target / f"audio.{self.audio_format}"
                if self.audio_cache is not None:
                    cached_format = await asyncio.to_thread(self.audio_cache.format_for, track.id)
                    hit = False
                    # The fast default can reuse a legacy MP3 immediately;
                    # an explicit MP3 request must not silently return M4A.
                    if cached_format == "mp3" or (self.audio_format == "m4a" and cached_format == "m4a"):
                        cached_audio = target / f"audio.{cached_format}"
                        hit = await asyncio.to_thread(self.audio_cache.copy_to, track.id, cached_audio, self.max_bytes)
                    metrics['cache_read_seconds'] = round(monotonic() - started, 4)
                    if hit:
                        metrics.update(cache_hit=True, audio_format=cached_format,
                                       prepare_seconds=round(monotonic() - started, 4))
                        yield cached_audio
                        return
                download_started = monotonic()
                metrics['fast_profile'] = fast_profile

                def arguments_for(audio_format, destination):
                    arguments = [
                        "--no-playlist", "--max-filesize", str(self.max_bytes),
                        "--concurrent-fragments", "2", "--abort-on-unavailable-fragments",
                        "--match-filter", f"!is_live & duration <= {self.max_duration}",
                        "--format", "bestaudio[ext=m4a]" if audio_format == "m4a" else "bestaudio/best",
                        "--extract-audio", "--audio-format", audio_format]
                    if audio_format == "mp3":
                        arguments += ["--audio-quality", "192K", "--postprocessor-args",
                                      "ExtractAudio+ffmpeg_o:-compression_level 5"]
                    return [*arguments, "--output", str(destination / "audio.%(ext)s"),
                            "--", f"https://www.youtube.com/watch?v={track.id}"]

                arguments = arguments_for(self.audio_format, target)
                try:
                    try:
                        selected_arguments = (["--extractor-args", FAST_AUDIO_EXTRACTOR_ARGS, *arguments]
                                              if fast_profile else arguments)
                        await self._run_access(selected_arguments, timeout=240)
                    except ProviderError as exc:
                        # Retry once with normal client selection and MP3 only
                        # for format/client compatibility. Never switch paths
                        # after verification/rate/auth/country failures.
                        compatible = (exc.kind in {"http_403", "unavailable"}
                                      or (exc.kind == "unknown" and exc.format_unavailable))
                        if not (compatible and (fast_profile or self.audio_format == "m4a")):
                            raise
                        metrics.update(profile_fallback=True, audio_format="mp3")
                        fallback = target / "fallback"
                        fallback.mkdir()
                        audio = fallback / "audio.mp3"
                        await self._run_access(arguments_for("mp3", fallback), timeout=240)
                except ProviderError as exc:
                    metrics.update(error_kind=exc.kind, prepare_seconds=round(monotonic() - started, 4))
                    raise
                metrics['download_convert_seconds'] = round(monotonic() - download_started, 4)
                if not audio.is_file() or audio.stat().st_size <= 0:
                    raise ProviderError("unavailable", "No se obtuvo un archivo de audio; puede superar los límites de duración o tamaño.")
                if audio.stat().st_size > self.max_bytes:
                    raise ProviderError("too_large", "El audio supera el tamaño permitido para Telegram.")
                if self.audio_cache is not None:
                    cache_started = monotonic()
                    metrics['cache_stored'] = await asyncio.to_thread(self.audio_cache.put, track.id, audio)
                    metrics['cache_write_seconds'] = round(monotonic() - cache_started, 4)
                metrics.update(bytes=audio.stat().st_size, prepare_seconds=round(monotonic() - started, 4))
                yield audio

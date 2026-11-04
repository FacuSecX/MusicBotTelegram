# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Find accessible copies of the same song, without changing the saved original.

Candidates retain their real IDs and metadata. Callers can offer them for the
user to choose, or explicitly report a version replacement in playlist playback.
"""
from __future__ import annotations

import asyncio
import math
import re
import unicodedata

from .provider import ProviderError, Track, VIDEO_ID

_MEDIA_WORDS = frozenset({
    'official', 'oficial', 'video', 'audio', 'lyric', 'lyrics', 'letra', 'letras',
    'hd', 'hq', '4k', '1080p', '720p', 'remastered', 'remasterizado', 'visualizer',
    'videoclip', 'vevo', 'topic',
})
_STOP_ERRORS = frozenset({
    'captcha', 'rate_limited', 'http_402', 'http_403', 'network', 'timeout',
    'configuration',
})
_VERSION_PATTERNS = {
    'live': r'\b(?:live|en vivo|ao vivo|en directo|concert|concierto)\b',
    'remix': r'\b(?:remix|rework|mashup)\b',
    'cover': r'\b(?:cover|covers|tributo|tribute)\b',
    'karaoke': r'\bkaraoke\b',
    'instrumental': r'\binstrumental\b',
    'acoustic': r'\b(?:acoustic|acustic[oa]|unplugged)\b',
    'speed': r'\b(?:sped up|speed up|slowed|nightcore|daycore)\b',
}


def _normalized(text: str) -> str:
    normalized = unicodedata.normalize('NFKD', text.casefold())
    return ''.join(ch for ch in normalized if not unicodedata.combining(ch))


def _word_list(text: str) -> list[str]:
    words = re.findall(r'[^\W_]+', _normalized(text))
    result = []
    for word in words:
        if word.endswith('vevo') and len(word) > 4:
            word = word[:-4]
        if word.startswith('oficial') and len(word) > 7:
            word = word[7:]
        if word.startswith('official') and len(word) > 8:
            word = word[8:]
        if word not in _MEDIA_WORDS:
            result.append(word)
    return result


def _words(text: str) -> set[str]:
    return set(_word_list(text))


_VERSION_WORDS = frozenset({'live', 'en', 'vivo', 'ao', 'directo', 'concert', 'concierto',
                            'remix', 'rework', 'mashup', 'cover', 'covers', 'tributo', 'tribute',
                            'karaoke', 'instrumental', 'acoustic', 'acustico', 'acustica',
                            'unplugged', 'sped', 'up', 'speed', 'slowed', 'nightcore', 'daycore'})


def _identity(original: Track) -> tuple[set[str], set[str]]:
    # A labelled official channel may have the label's name rather than the
    # artist's. Prefer the explicit "Artist - Song" title when it is present.
    split = re.split(r'\s+[-–—|]\s+|\s*//\s*', original.title, maxsplit=1)
    if len(split) == 2 and _words(split[0]) and _words(split[1]):
        artist, song = _words(split[0]), _words(split[1])
    else:
        artist = _words(original.artist) - {'youtube'}
        song = _words(original.title) - artist
    return artist, (song - _VERSION_WORDS) or song or _words(original.title)


def _versions(title: str) -> set[str]:
    normalized = _normalized(title)
    return {kind for kind, pattern in _VERSION_PATTERNS.items() if re.search(pattern, normalized)}


def _matches(original: Track, candidate: Track) -> bool:
    artist, song = _identity(original)
    candidate_title = _words(candidate.title)
    if not artist or not song or not candidate_title:
        return False
    if _versions(original.title) != _versions(candidate.title):
        return False
    required = len(song) if len(song) <= 3 else math.ceil(len(song) * 0.8)
    if len(song & candidate_title) < required:
        return False
    identity = candidate_title | _words(candidate.artist)
    # Requiring the artist words prevents a matching song title by a different
    # artist; generic lyrics-channel names are fine if the title names the artist.
    if not artist <= identity:
        return False
    if original.duration and candidate.duration:
        tolerance = max(45, original.duration * 0.2)
        if abs(candidate.duration - original.duration) > tolerance:
            return False
    return True


def _queries(original: Track) -> list[str]:
    initial = original.title.strip()[:200]
    artist, _song = _identity(original)
    words = _word_list(original.title)
    # Keep word order for names; only add an artist missing from the title.
    if not artist <= set(words):
        words = _word_list(original.artist) + words
    compact = ' '.join(words)
    if compact:
        compact = (compact + ' audio')[:200]
    return [initial] + ([compact] if compact and compact.casefold() != initial.casefold() else [])


def find_cached(original: Track, tracks, limit: int = 3) -> list[Track]:
    """Return matching Telegram-cached copies without contacting YouTube.

    A file_id identifies an existing Telegram upload, not a new availability
    claim about YouTube. The sending caller still handles stale Telegram IDs.
    """
    limit = min(int(limit), 3)
    if limit <= 0:
        return []
    found = []
    seen = {original.id}
    for row in tracks:
        try:
            if not isinstance(row, dict) or not isinstance(row.get("file_id"), str) or not row["file_id"].strip():
                continue
            candidate = Track.from_dict(row)
            if (not isinstance(candidate.id, str) or not VIDEO_ID.fullmatch(candidate.id)
                    or candidate.id in seen or not isinstance(candidate.title, str)
                    or not candidate.title.strip() or not isinstance(candidate.artist, str)
                    or candidate.url != "https://www.youtube.com/watch?v=" + candidate.id
                    or (candidate.duration is not None and
                        (not isinstance(candidate.duration, int) or isinstance(candidate.duration, bool)
                         or candidate.duration < 0))):
                continue
            if not _matches(original, candidate):
                continue
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        seen.add(candidate.id)
        found.append(candidate)
        if len(found) >= limit:
            break
    return found


async def find_available(provider, original: Track, limit: int = 3,
                         max_probes: int = 4, *, spacing: float = 1, cached_tracks=()) -> list[Track]:
    """Find verified copies with at most two searches and four total probes.

    A second normalized query is used only if the first yields no accessible
    matching version. Global access/network failures stop immediately. Metadata
    probes confirm accessibility; a successful download must still be verified
    by the caller before calling a candidate playable. Matching Telegram-cached
    copies take priority and return immediately without any YouTube requests.
    """
    limit, max_probes = min(int(limit), 3), min(int(max_probes), 4)
    if limit <= 0 or not original.title.strip():
        return []
    cached = find_cached(original, cached_tracks, limit)
    if cached:
        return cached
    if max_probes <= 0:
        return []
    spacing = float(spacing)
    if not math.isfinite(spacing) or spacing < 0:
        raise ValueError('La pausa entre consultas debe ser un número finito positivo o cero.')
    seen = {original.id}
    available = []
    probes = 0
    for query in _queries(original):
        candidates = await provider.search(query)
        for candidate in candidates:
            if candidate.id in seen:
                continue
            seen.add(candidate.id)
            if not _matches(original, candidate):
                continue
            if probes >= max_probes or len(available) >= limit:
                break
            if probes and spacing:
                await asyncio.sleep(spacing)
            probes += 1
            try:
                verified, _warnings = await provider.probe(candidate.id)
            except ProviderError as exc:
                if exc.kind in _STOP_ERRORS:
                    raise
                continue
            if verified.id != candidate.id or not _matches(original, verified):
                continue
            available.append(verified)
        if available or probes >= max_probes:
            break
    return available

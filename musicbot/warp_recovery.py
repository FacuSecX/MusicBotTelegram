# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Bounded recovery of an explicitly enabled local WARP proxy session.

Only the already configured proxy session is reconnected. This module never
changes WARP's mode, routes, registration, account or selected country.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import tempfile
from time import time

try:
    import fcntl
except ImportError:  # Production is Linux; unit tests also run on Windows.
    fcntl = None

LOCAL_WARP_PROXY = "socks5h://127.0.0.1:40000"
_PROXY_MODE = re.compile(r"Mode:\s+WarpProxy on port 40000(?:\s|$)")
_CONNECTED = re.compile(r"Status update:\s+Connected(?:\s|$)")


class WarpRecovery:
    def __init__(self, state_path: Path, cooldown: float = 300):
        self.state_path = Path(state_path)
        self.cooldown = max(float(cooldown), 300)
        self._lock = asyncio.Lock()
        self._pending = None
        self._last_attempt = None

    async def _command(self, *arguments: str) -> tuple[bool, str]:
        # Fixed executable and fixed call sites; no user input becomes an arg.
        try:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/warp-cli", "--accept-tos", *arguments,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except OSError:
            return False, ""
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            return process.returncode == 0, output.decode("utf-8", "replace")[:8192]
        except BaseException as exc:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            if isinstance(exc, asyncio.TimeoutError):
                return False, ""
            raise

    def _record_attempt(self, now: float) -> bool:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            if self.state_path.is_file():
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                self._last_attempt = float(saved["last_attempt"])
                if not math.isfinite(self._last_attempt):
                    return False
            if self._last_attempt is not None and now - self._last_attempt < self.cooldown:
                return False
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                             dir=self.state_path.parent, prefix="warp-recovery-") as stream:
                pending = Path(stream.name)
                json.dump({"last_attempt": now}, stream)
            try:
                if os.name == "posix":
                    pending.chmod(0o600)
                os.replace(pending, self.state_path)
            finally:
                pending.unlink(missing_ok=True)
            self._last_attempt = now
            return True
        except (OSError, ValueError, TypeError, KeyError):
            # Without a durable cooldown, fail closed instead of reconnecting.
            return False

    @contextmanager
    def _process_lock(self):
        stream = None
        acquired = False
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.state_path.with_suffix(".lock").open("a+")
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if stream is not None:
                if acquired and fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()

    def _finished(self, task) -> None:
        if self._pending is task:
            self._pending = None
        if not task.cancelled():
            task.exception()

    async def recover(self) -> bool:
        if self._pending is None:
            self._pending = asyncio.create_task(self._recover_once())
            self._pending.add_done_callback(self._finished)
        # Cancelling a caller must not leave WARP between disconnect/connect.
        return await asyncio.shield(self._pending)

    async def _recover_once(self) -> bool:
        async with self._lock:
            with self._process_lock() as acquired:
                if not acquired or not self._record_attempt(time()):
                    return False
                ok, settings = await self._command("settings")
                if not ok or not _PROXY_MODE.search(settings):
                    return False
                ok, _ = await self._command("disconnect")
                if not ok:
                    return False
                ok, _ = await self._command("connect")
                if not ok:
                    return False
                for attempt in range(5):
                    ok, status = await self._command("status")
                    if ok and _CONNECTED.search(status):
                        return True
                    if attempt < 4:
                        await asyncio.sleep(1)
                return False

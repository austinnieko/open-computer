"""Unified system events WebSocket: fs watching + port scanning.

Replaces the old watch.py with a single multiplexed event stream.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers.polling import PollingObserver

from cptr.utils import fswatch
from cptr.utils.terminal import manager
from cptr.utils.config import check_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])

# ── Filesystem watcher ────────────────────────────────────────────


def _create_observer():
    if platform.system() == "Darwin":
        # Prefer the native FSEvents observer on macOS – it fires only on
        # real filesystem changes instead of polling (which generates
        # constant false-positive "modified" events from stat calls).
        try:
            from watchdog.observers.fsevents import FSEventsObserver

            return FSEventsObserver()
        except ImportError:
            # If the C-extension isn't available, fall back to polling
            # with a longer interval to reduce noise.
            return PollingObserver(timeout=3)
    return Observer()


class _ChangeCollector(FileSystemEventHandler):
    """Collects fs events into a set for debounced delivery."""

    def __init__(self) -> None:
        self._changes: set[str] = set()
        self._lock = threading.Lock()

    # Paths that should never trigger a user-visible refresh
    _IGNORED_SEGMENTS = fswatch.EXCLUDED_DIR_NAMES

    def _record(self, event: FileSystemEvent) -> None:
        path = event.src_path
        # Skip noisy internal paths that shouldn't trigger file browser refreshes
        parts = Path(path).parts
        if any(seg in self._IGNORED_SEGMENTS for seg in parts):
            return
        with self._lock:
            self._changes.add(str(Path(path).parent))
            self._changes.add(path)

    def on_created(self, event: FileSystemEvent) -> None:
        self._record(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._record(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        # Ignore directory-modified events – these fire on any child
        # change (which we already capture via on_created/on_deleted)
        # and on metadata-only updates like atime from stat calls.
        if event.is_directory:
            return
        self._record(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._record(event)
        if hasattr(event, "dest_path"):
            with self._lock:
                self._changes.add(str(Path(event.dest_path).parent))
                self._changes.add(event.dest_path)

    def drain(self) -> list[str]:
        with self._lock:
            paths = list(self._changes)
            self._changes.clear()
        return paths


class _Subscriber:
    """One connection's pending changes: merged, bounded, never dropped.

    A slow client doesn't get a growing queue or silently lose events: its
    changes merge into one set, and past a cap they collapse to "the root
    changed", which makes it refresh once.
    """

    __slots__ = ("root", "paths", "event")

    def __init__(self, root: str) -> None:
        self.root = root
        self.paths: set[str] = set()
        self.event = asyncio.Event()

    def push(self, paths: list[str]) -> None:
        if self.paths != {self.root}:  # not already collapsed
            self.paths.update(paths)
            if len(self.paths) > fswatch.MAX_PENDING_PER_ROOT:
                self.paths = {self.root}
        self.event.set()

    def take(self) -> list[str]:
        paths = list(self.paths)
        self.paths.clear()
        self.event.clear()
        return paths


class _WatchEntry(NamedTuple):
    drain: Callable[[], list[str]]
    close: Callable[[], None]
    subscribers: set[_Subscriber]


_watch_registry: dict[str, _WatchEntry] = {}
_watch_registry_lock = asyncio.Lock()


def _start_watch(resolved: str) -> _WatchEntry:
    """Start one shared watch for ``resolved`` (runs in a worker thread)."""
    if fswatch.is_supported():
        # Linux: pruned inotify watches (see cptr.utils.fswatch for why not
        # watchdog's recursive observer).
        watcher = fswatch.shared_watcher()
        watcher.add_root(resolved)
        return _WatchEntry(
            lambda: watcher.drain(resolved), lambda: watcher.remove_root(resolved), set()
        )

    observer = _create_observer()
    observer.daemon = True
    handler = _ChangeCollector()
    watch = observer.schedule(handler, resolved, recursive=True)
    observer.start()

    def close() -> None:
        try:
            observer.unschedule(watch)
        except Exception:
            pass
        try:
            observer.stop()
            observer.join(timeout=2)
        except Exception:
            pass

    return _WatchEntry(handler.drain, close, set())


async def _subscribe_to_path(path: str) -> tuple[str, _Subscriber]:
    """Subscribe this connection to a shared filesystem watch."""
    resolved = str(Path(path).resolve())
    sub = _Subscriber(resolved)

    async with _watch_registry_lock:
        entry = _watch_registry.get(resolved)
        if entry is None:
            # The first walk of a large tree must not block the event loop.
            entry = await asyncio.to_thread(_start_watch, resolved)
            _watch_registry[resolved] = entry
        entry.subscribers.add(sub)

    return resolved, sub


async def _unsubscribe_from_path(path: str, sub: _Subscriber) -> None:
    """Remove a filesystem watch subscription and stop idle watches."""
    async with _watch_registry_lock:
        entry = _watch_registry.get(path)
        if entry is None:
            return

        entry.subscribers.discard(sub)
        if entry.subscribers:
            return

        _watch_registry.pop(path, None)
        try:
            await asyncio.to_thread(entry.close)
        except Exception:
            logger.debug("Closing fs watch on %s failed", path, exc_info=True)


async def _dispatch_fs_changes() -> None:
    """Fan out debounced filesystem changes from shared watchers."""
    while True:
        await asyncio.sleep(1.0)

        async with _watch_registry_lock:
            entries = list(_watch_registry.items())

        for _path, entry in entries:
            paths = entry.drain()
            if not paths:
                continue

            if sys.platform == "win32":
                paths = [p.replace("\\", "/") for p in paths]

            for sub in list(entry.subscribers):
                sub.push(paths)


_fs_dispatch_task: Optional[asyncio.Task] = None


async def _ensure_fs_dispatcher() -> None:
    global _fs_dispatch_task
    if _fs_dispatch_task is None or _fs_dispatch_task.done():
        _fs_dispatch_task = asyncio.create_task(_dispatch_fs_changes())


async def _fs_watcher_loop(ws: WebSocket, initial_path: str, path_holder: dict) -> None:
    """Watch filesystem and push fs_change events."""
    target = str(Path(initial_path).resolve())
    path_holder["current"] = target

    try:
        await _ensure_fs_dispatcher()
        current_path, sub = await _subscribe_to_path(target)
    except Exception as e:
        logger.warning(f"Failed to watch {target}: {e}")
        return

    try:
        while True:
            # Check if path changed (from receive loop)
            new_path = path_holder.get("pending")
            if new_path:
                del path_holder["pending"]
                resolved = str(Path(new_path).resolve())
                if resolved != path_holder["current"] and Path(resolved).is_dir():
                    await _unsubscribe_from_path(current_path, sub)
                    current_path, sub = await _subscribe_to_path(resolved)
                    path_holder["current"] = resolved
                    logger.info(f"FS watch path changed to {resolved}")

            try:
                await asyncio.wait_for(sub.event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            paths = sub.take()
            if not paths:
                continue
            try:
                await ws.send_json({"type": "fs_change", "paths": paths})
            except Exception:
                break
    finally:
        await _unsubscribe_from_path(current_path, sub)


# ── Port scanning ─────────────────────────────────────────────────

# System processes to ignore
_IGNORED_PROCESSES = {
    "sshd",
    "cupsd",
    "mDNSResponder",
    "rapportd",
    "systemd-resolve",
    "avahi-daemon",
    "dnsmasq",
    "launchd",
    "systemd",
    "ntpd",
    "bluetoothd",
    "AirPlayXPCHelper",
    "ControlCenter",
}

# Ports that are almost certainly system services
_SYSTEM_PORTS = {22, 53, 80, 443, 631, 5353}


async def _get_ppid(pid: int) -> int:
    """Get parent PID. Cross-platform."""
    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "wmic", "process", "where", f"ProcessId={pid}",
                "get", "ParentProcessId", "/value",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            for line in stdout.decode(errors="replace").splitlines():
                if line.startswith("ParentProcessId="):
                    return int(line.split("=", 1)[1])
            return 0
        elif sys.platform == "linux":
            def _read_ppid():
                with open(f"/proc/{pid}/stat") as f:
                    return int(f.read().split()[3])
            return await asyncio.to_thread(_read_ppid)
        else:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-o", "ppid=", "-p", str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            return int(stdout.decode(errors="replace").strip())
    except Exception:
        return 0


async def _find_session_for_pid(pid: int) -> Optional[str]:
    """Walk up the process tree to find which terminal session spawned this PID."""
    current = pid
    visited: set[int] = set()
    while current > 1 and current not in visited:
        visited.add(current)
        for session in manager._sessions.values():
            # Unix: match by child PID
            if session._pid == current:
                return session.session_id
            # Windows: winpty process has a .pid attribute
            if session._process is not None:
                try:
                    if getattr(session._process, "pid", None) == current:
                        return session.session_id
                except Exception:
                    pass
        current = await _get_ppid(current)
    return None


async def _get_process_name(pid: int) -> str:
    """Get process name from PID."""
    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            line = stdout.decode(errors="replace").strip()
            if line and line.startswith('"'):
                return line.split('"')[1]
            return "unknown"
        elif sys.platform == "linux":
            def _read_comm():
                with open(f"/proc/{pid}/comm") as f:
                    return f.read().strip()
            return await asyncio.to_thread(_read_comm)
        else:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-o", "comm=", "-p", str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            name = stdout.decode(errors="replace").strip()
            # macOS returns full path, extract basename
            return os.path.basename(name) if name else "unknown"
    except Exception:
        return "unknown"


async def _scan_ports_darwin() -> list[dict]:
    """Scan listening ports on macOS using lsof."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "lsof", "-iTCP", "-sTCP:LISTEN", "-nP", "-F", "pcn",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return []

        ports = []
        current_pid = 0
        current_process = ""
        for line in stdout.decode(errors="replace").splitlines():
            if line.startswith("p"):
                current_pid = int(line[1:])
            elif line.startswith("c"):
                current_process = line[1:]
            elif line.startswith("n"):
                # Format: n*:PORT or nhost:PORT
                addr = line[1:]
                if ":" in addr:
                    port_str = addr.rsplit(":", 1)[1]
                    try:
                        port = int(port_str)
                        ports.append(
                            {
                                "port": port,
                                "pid": current_pid,
                                "process": current_process,
                            }
                        )
                    except ValueError:
                        pass
        return ports
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
        return []


async def _scan_ports_linux() -> list[dict]:
    """Scan listening ports on Linux using /proc/net/tcp."""
    ports = []
    try:
        def _read_proc_net():
            with open("/proc/net/tcp") as f:
                return f.readlines()[1:]  # skip header

        lines = await asyncio.to_thread(_read_proc_net)
        for line in lines:
            parts = line.split()
            if parts[3] == "0A":  # LISTEN state
                local = parts[1]
                port = int(local.split(":")[1], 16)
                inode = int(parts[9])
                # Find PID for this inode
                pid = await _inode_to_pid(inode)
                process = await _get_process_name(pid) if pid else "unknown"
                ports.append({"port": port, "pid": pid or 0, "process": process})
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
    return ports


async def _inode_to_pid(inode: int) -> Optional[int]:
    """Map a socket inode to a PID on Linux."""
    def _scan():
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    fd_dir = f"/proc/{entry}/fd"
                    for fd in os.listdir(fd_dir):
                        try:
                            link = os.readlink(f"{fd_dir}/{fd}")
                            if f"socket:[{inode}]" in link:
                                return int(entry)
                        except (OSError, ValueError):
                            continue
                except (OSError, PermissionError):
                    continue
        except Exception:
            pass
        return None
    return await asyncio.to_thread(_scan)


async def _scan_ports_windows() -> list[dict]:
    """Scan listening ports on Windows using netstat."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "netstat", "-ano", "-p", "TCP",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        ports = []
        for line in stdout.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTENING" in parts:
                local = parts[1]
                if ":" in local:
                    port_str = local.rsplit(":", 1)[1]
                    try:
                        port = int(port_str)
                        pid = int(parts[-1])
                        ports.append(
                            {
                                "port": port,
                                "pid": pid,
                                "process": await _get_process_name(pid),
                            }
                        )
                    except ValueError:
                        pass
        return ports
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
        return []


async def _scan_ports() -> list[dict]:
    """Scan listening ports. Cross-platform."""
    system = platform.system()
    if system == "Darwin":
        raw = await _scan_ports_darwin()
    elif system == "Linux":
        raw = await _scan_ports_linux()
    elif system == "Windows":
        raw = await _scan_ports_windows()
    else:
        return []

    # Get our own PID to filter ourselves out
    our_pid = os.getpid()

    # Deduplicate by port and filter
    seen: set[int] = set()
    filtered = []
    for entry in raw:
        port = entry["port"]
        if port in seen:
            continue
        seen.add(port)

        # Skip system ports and our own process
        if port in _SYSTEM_PORTS:
            continue
        if entry["pid"] == our_pid:
            continue
        if entry["process"] in _IGNORED_PROCESSES:
            continue

        # Session attribution: only include ports spawned by our terminals
        if entry["pid"]:
            session_id = await _find_session_for_pid(entry["pid"])
            if session_id:
                entry["session_id"] = session_id
                filtered.append(entry)
            # else: not from a cptr terminal, skip
        # pid=0 or no pid: skip

    return filtered


_shared_scan: tuple[float, list[dict]] | None = None
_shared_scan_lock = asyncio.Lock()
_SHARED_SCAN_TTL = 2.5


async def _scan_ports_shared() -> list[dict]:
    """One port scan shared by every connected tab.

    A scan starts lsof plus a ps per process ancestor, and each websocket runs
    its own loop, so with several tabs open the server was spawning bursts of
    processes every few seconds. Only ports opened from our terminals are
    reported, so with no terminal open there is nothing to find.
    """
    global _shared_scan
    if not manager._sessions:
        return []
    async with _shared_scan_lock:
        now = time.monotonic()
        if _shared_scan and now - _shared_scan[0] < _SHARED_SCAN_TTL:
            return _shared_scan[1]
        result = await _scan_ports()
        _shared_scan = (time.monotonic(), result)
        return result


async def _port_scanner_loop(ws: WebSocket) -> None:
    """Periodically scan ports and push add/remove events."""
    known: dict[int, dict] = {}  # port -> info

    while True:
        await asyncio.sleep(3)

        try:
            current_ports = {p["port"]: p for p in await _scan_ports_shared()}
        except Exception as e:
            logger.warning(f"Port scan error: {e}")
            continue

        # Detect new ports
        for port, info in current_ports.items():
            if port not in known:
                try:
                    await ws.send_json(
                        {
                            "type": "port_added",
                            "port": info["port"],
                            "pid": info["pid"],
                            "process": info["process"],
                            "session_id": info.get("session_id"),
                        }
                    )
                except Exception:
                    return

        # Detect removed ports
        for port in list(known.keys()):
            if port not in current_ports:
                try:
                    await ws.send_json(
                        {
                            "type": "port_removed",
                            "port": port,
                        }
                    )
                except Exception:
                    return

        known = current_ports


# ── Receive loop ──────────────────────────────────────────────────


async def _receive_loop(ws: WebSocket, path_holder: dict) -> None:
    """Handle incoming messages from the client."""
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "watch_path":
                new_path = msg.get("path")
                if new_path:
                    path_holder["pending"] = new_path
    except (WebSocketDisconnect, Exception):
        pass


# ── WebSocket endpoint ────────────────────────────────────────────


@router.websocket("/ws")
async def events_ws(
    websocket: WebSocket, path: str = Query("/", description="Initial fs watch path")
):
    """Unified system events WebSocket.

    Pushes:
      - {"type": "fs_change", "paths": [...]}
      - {"type": "port_added", "port": N, "pid": N, "process": "...", "session_id": "..." | null}
      - {"type": "port_removed", "port": N}

    Receives:
      - {"type": "watch_path", "path": "..."}: change fs watch directory
    """
    target = Path(path).resolve()
    if not target.is_dir():
        await websocket.close(code=4000, reason="Not a directory")
        return

    # Auth check for WebSocket
    client_host = websocket.client.host if websocket.client else "127.0.0.1"
    token = websocket.cookies.get("cptr_session") or websocket.query_params.get("token")
    auth = check_access(client_host=client_host, jwt_token=token)
    if auth is None:
        await websocket.close(code=4001, reason="unauthorized")
        return

    await websocket.accept()
    logger.info(f"Events WS connected, watching {target}")

    path_holder: dict = {}

    fs_task = asyncio.create_task(_fs_watcher_loop(websocket, str(target), path_holder))
    port_task = asyncio.create_task(_port_scanner_loop(websocket))
    recv_task = asyncio.create_task(_receive_loop(websocket, path_holder))

    try:
        done, pending = await asyncio.wait(
            [fs_task, port_task, recv_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.error(f"Events WS error: {e}")
    finally:
        logger.info("Events WS disconnected")

"""mcp_control.py — lifecycle for the embedded MCP server (see mcp_server.py).

Qt-free.  The GUI owns one McpServerController; start() spins up uvicorn on a
background daemon thread bound to 127.0.0.1 with a per-start bearer token,
stop() shuts it down.  Everything heavy (mcp, uvicorn, starlette, pydantic)
is imported inside start(), so the app never pays the import cost — and can
run without the packages installed at all — unless AI access is enabled.

Port and token are normally regenerated on every start() — the safe default,
since a fresh token means a stopped/restarted server can never be reached
with a stale credential. **Developer-mode persisted credentials** (opt-in,
`start(ctx, persist_dev=True)`) trade that rotation away for convenience: an
external MCP client config (e.g. `claude mcp add`) only has to be entered
once instead of after every app restart, since port+token stay constant.
Written in plaintext to `dev_mcp_credentials.json` (same dev/frozen-path
convention as `research_store.py`) — never enable on a shared or untrusted
machine. Gated behind the "Developer mode" checkbox in Preferences (off by
default, own warning text there); this module itself doesn't judge, it just
does what the caller asks.
"""

import json
import os
import secrets
import socket
import sys
import threading
import time


def dev_credentials_path() -> str:
    """Location of dev_mcp_credentials.json (next to exe when frozen, else
    config/) — same convention as research_store.store_path()."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable),
                             "dev_mcp_credentials.json")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config", "dev_mcp_credentials.json")


def _load_dev_credentials() -> tuple[int | None, str | None]:
    try:
        with open(dev_credentials_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data.get('port'), data.get('token')
    except Exception:
        return None, None


def _save_dev_credentials(port: int, token: str) -> None:
    path = dev_credentials_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'port': port, 'token': token}, f, indent=2)


class McpServerController:
    def __init__(self):
        self._server = None      # uvicorn.Server
        self._thread: threading.Thread | None = None
        self.port: int | None = None
        self.token: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, ctx, persist_dev: bool = False) -> tuple[int, str]:
        """Start serving *ctx* (a mcp_server.CaseContext).  Returns
        (port, token).  Raises on failure — including ImportError when the
        optional mcp/uvicorn packages aren't bundled in this build.

        persist_dev=True reuses the port+token last saved to
        dev_credentials_path() (or a fresh free port when that exact port is
        still unavailable after a brief retry — token still reused) instead
        of regenerating both every call, and re-saves after picking them.
        See module docstring."""
        if self.running:
            return self.port, self.token
        import uvicorn                       # lazy — optional dependency
        from mcp_server import build_http_app

        token = None
        port = None
        if persist_dev:
            port, token = _load_dev_credentials()
            if port is not None and not self._port_free_retry(port):
                port = None      # still taken after retrying — genuinely gone
        self.token = token or secrets.token_urlsafe(16)
        self.port = port if port is not None else self._free_port()
        if persist_dev:
            _save_dev_credentials(self.port, self.token)
        app = build_http_app(ctx, self.token)
        config = uvicorn.Config(app, host='127.0.0.1', port=self.port,
                                log_level='warning', access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, name='ffs-mcp-http', daemon=True)
        self._thread.start()
        return self.port, self.token

    def stop(self) -> None:
        """Signal shutdown and wait briefly; the thread is a daemon, so a
        stuck shutdown can never block app exit."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        self.port = None
        self.token = None

    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}/mcp'

    def config_snippet(self) -> str:
        """mcpServers JSON for HTTP-capable clients (LM Studio, Claude Code,
        mcp-remote shim for Claude Desktop)."""
        import json
        return json.dumps({
            'mcpServers': {
                'ffs-explorer': {
                    'url': self.url(),
                    'headers': {'Authorization': f'Bearer {self.token}'},
                }
            }
        }, indent=2)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]

    @staticmethod
    def _port_free(port: int) -> bool:
        """Mirrors the SO_REUSEADDR asyncio/uvicorn itself binds with — a
        bare bind() (no SO_REUSEADDR) can report a port as taken purely
        because a just-closed connection to it is lingering in TIME_WAIT,
        even though nothing is actually listening there anymore, which
        would wrongly discard a persisted dev port on every reconnect."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', port))
                return True
            except OSError:
                return False

    @staticmethod
    def _port_free_retry(port: int, attempts: int = 5, delay: float = 0.2) -> bool:
        """Same check as _port_free, retried briefly before giving up.

        Confirmed real cause of a persisted dev port silently drifting to a
        new value: relaunching this app can start a new process before the
        PREVIOUS one has actually released its listening socket yet (the
        old process may still be mid-shutdown) — _port_free's SO_REUSEADDR
        only rules out a TIME_WAIT false-positive, it does nothing for a
        genuinely-still-bound socket from a process that hasn't exited yet.
        Abandoning the persisted port on the very first check silently
        breaks every already-configured external MCP client (Claude Code's
        `claude mcp add`, LM Studio's mcp.json, ...) still pointed at the
        old port, with no obvious symptom beyond "the connection is red"
        and no indication why. A short bounded retry (~1s total) rides out
        that ordinary startup race; a port that's still taken after this
        many attempts is presumably held by something else entirely, and
        falling back to a fresh port remains correct in that case."""
        for _ in range(attempts):
            if McpServerController._port_free(port):
                return True
            time.sleep(delay)
        return False

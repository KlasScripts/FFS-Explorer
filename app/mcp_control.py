"""mcp_control.py — lifecycle for the embedded MCP server (see mcp_server.py).

Qt-free.  The GUI owns one McpServerController; start() spins up uvicorn on a
background daemon thread bound to 127.0.0.1 with a per-start bearer token,
stop() shuts it down.  Everything heavy (mcp, uvicorn, starlette, pydantic)
is imported inside start(), so the app never pays the import cost — and can
run without the packages installed at all — unless AI access is enabled.
"""

import secrets
import socket
import threading


class McpServerController:
    def __init__(self):
        self._server = None      # uvicorn.Server
        self._thread: threading.Thread | None = None
        self.port: int | None = None
        self.token: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, ctx) -> tuple[int, str]:
        """Start serving *ctx* (a mcp_server.CaseContext).  Returns
        (port, token).  Raises on failure — including ImportError when the
        optional mcp/uvicorn packages aren't bundled in this build."""
        if self.running:
            return self.port, self.token
        import uvicorn                       # lazy — optional dependency
        from mcp_server import build_http_app

        self.token = secrets.token_urlsafe(16)
        self.port = self._free_port()
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

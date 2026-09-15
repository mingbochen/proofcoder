"""Loopback HTTP server that presents one local ProofCoder session in a browser.

The server is deliberately small and standard-library only. It frames JSON for
:mod:`proofcoder.web.api`, serves three static assets, and enforces the access
rules that make a local agent safe to expose to a browser page:

* it binds a loopback interface by default;
* every ``/api`` request must carry the per-process session token, which is only
  ever written into the page served from this origin;
* ``Host`` and ``Origin`` must match the address the server is listening on, so a
  remote page cannot drive the agent through DNS rebinding or a cross-site form.

None of this is an operating-system sandbox. It only keeps the existing local
command and file authority reachable from this machine's own browser page.
"""

from __future__ import annotations

import json
import secrets
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlsplit

from proofcoder.web.api import (
    ApiRequest,
    ApiResponse,
    ApiRouter,
    ConnectivityClientFactory,
    error_response,
)
from proofcoder.web.runs import BrowserRunManager, ClientFactory

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BODY_BYTES = 256 * 1024
TOKEN_HEADER = "X-ProofCoder-Token"
TOKEN_PLACEHOLDER = "__PROOFCODER_SESSION_TOKEN__"
STATIC_ROOT = Path(__file__).resolve().parent / "static"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

_STATIC_ROUTES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)


class ServerAddressError(Exception):
    """The requested local address could not be bound."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ProofCoderHTTPServer(ThreadingHTTPServer):
    """Threaded loopback server that never reuses a still-bound local address."""

    daemon_threads = True
    # Windows SO_REUSEADDR lets an unrelated process bind a port another socket is
    # already listening on, which would let it answer for this token-protected origin.
    allow_reuse_address = False

    router: ApiRouter
    token: str
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    request_logger: Callable[[str], None] | None


@dataclass(slots=True)
class WebServer:
    """One bound local server plus the values its caller needs to open a browser."""

    http: _ProofCoderHTTPServer
    sessions: BrowserRunManager
    token: str
    host: str
    port: int
    _serving: threading.Event = field(default_factory=threading.Event)

    @property
    def url(self) -> str:
        """Return the loopback URL that serves the interface."""

        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}/"

    def serve_forever(self) -> None:
        """Serve until :meth:`shutdown` is called from another thread."""

        self._serving.set()
        try:
            self.http.serve_forever(poll_interval=0.2)
        finally:
            self._serving.clear()

    def shutdown(self) -> None:
        """Stop accepting requests, cancel running agent work, and close the socket.

        ``BaseServer.shutdown`` waits on an event that only ``serve_forever`` sets, so
        calling it on a server that was bound but never served would block forever.
        A caller that binds and then gives up must still be able to release the port.
        """

        if self._serving.is_set():
            self.http.shutdown()
        self.sessions.shutdown()
        self.http.server_close()


def create_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    workspace: Path | None = None,
    client_factory: ClientFactory | None = None,
    connectivity_factory: ConnectivityClientFactory | None = None,
    sessions: BrowserRunManager | None = None,
    token: str | None = None,
    allow_browse: bool = True,
    request_logger: Callable[[str], None] | None = None,
) -> WebServer:
    """Bind one local server and wire it to a fresh session manager and router."""

    manager = sessions
    if manager is None:
        manager = (
            BrowserRunManager(environ=environ)
            if client_factory is None
            else BrowserRunManager(environ=environ, client_factory=client_factory)
        )
    router_kwargs: dict[str, object] = {
        "sessions": manager,
        "environ": environ,
        "cwd": cwd,
        "default_workspace": workspace,
        "allow_browse": allow_browse,
    }
    if connectivity_factory is not None:
        router_kwargs["connectivity_factory"] = connectivity_factory
    router = ApiRouter(**router_kwargs)  # type: ignore[arg-type]

    try:
        server = _ProofCoderHTTPServer((host, port), _RequestHandler)
    except OSError as error:
        raise ServerAddressError(
            "ADDRESS_UNAVAILABLE",
            f"could not listen on {host}:{port}; choose a free port with --port",
        ) from error

    bound_host, bound_port = server.server_address[0], server.server_address[1]
    bound_host = bound_host if isinstance(bound_host, str) else str(bound_host)
    bound_port = int(bound_port)
    server.router = router
    server.token = token or secrets.token_urlsafe(32)
    server.allowed_hosts = _allowed_hosts(host, bound_host, bound_port)
    server.allowed_origins = _allowed_origins(server.allowed_hosts)
    server.request_logger = request_logger
    return WebServer(
        http=server,
        sessions=manager,
        token=server.token,
        host=_display_host(host, bound_host),
        port=bound_port,
    )


def serve(server: WebServer) -> None:
    """Run one prepared server until the process is interrupted."""

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


class _RequestHandler(BaseHTTPRequestHandler):
    """Frame JSON and static responses under the local access rules."""

    server_version = "ProofCoder"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    _cached_assets: ClassVar[dict[str, bytes]] = {}
    _asset_lock: ClassVar[threading.Lock] = threading.Lock()

    @property
    def _app(self) -> _ProofCoderHTTPServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:
        """Serve one static asset or read-only API resource."""

        self._handle("GET")

    def do_POST(self) -> None:
        """Handle one state-changing API request."""

        self._handle("POST")

    def do_HEAD(self) -> None:
        """Reject HEAD explicitly rather than leaking a default implementation."""

        self._send_json(error_response(405, "METHOD_NOT_ALLOWED", "method is not supported"))

    def log_message(self, format: str, *args: object) -> None:
        """Route access logs through the injected logger instead of stderr."""

        logger = getattr(self._app, "request_logger", None)
        if logger is not None:
            logger(format % args)

    def _handle(self, method: str) -> None:
        split = urlsplit(self.path)
        path = unquote(split.path)
        if not self._host_allowed():
            self._send_json(
                error_response(403, "HOST_NOT_ALLOWED", "request Host header is not local")
            )
            return
        if path.startswith("/api"):
            self._handle_api(method, path, split.query)
            return
        if method != "GET":
            self._send_json(error_response(405, "METHOD_NOT_ALLOWED", "method is not supported"))
            return
        self._handle_static(path)

    def _handle_api(self, method: str, path: str, query: str) -> None:
        if not self._origin_allowed():
            self._send_json(
                error_response(403, "ORIGIN_NOT_ALLOWED", "request Origin is not this server")
            )
            return
        if not self._token_valid():
            self._send_json(
                error_response(401, "INVALID_TOKEN", "the local session token is missing or wrong")
            )
            return
        body: Mapping[str, object] = {}
        if method == "POST":
            parsed = self._read_json_body()
            if isinstance(parsed, ApiResponse):
                self._send_json(parsed)
                return
            body = parsed
        request = ApiRequest(
            method=method,
            path=path,
            query={
                key: values[-1] for key, values in parse_qs(query, keep_blank_values=True).items()
            },
            body=body,
        )
        try:
            response = self._app.router.handle(request)
        except Exception:
            self._send_json(
                error_response(500, "INTERNAL_ERROR", "the local server could not handle this")
            )
            return
        self._send_json(response)

    def _handle_static(self, path: str) -> None:
        route = _STATIC_ROUTES.get(path)
        if route is None:
            self._send_json(error_response(404, "NOT_FOUND", "unknown path"))
            return
        filename, content_type = route
        try:
            payload = self._asset(filename)
        except OSError:
            self._send_json(
                error_response(500, "ASSET_UNAVAILABLE", "a bundled asset could not be read")
            )
            return
        if filename == "index.html":
            payload = payload.replace(
                TOKEN_PLACEHOLDER.encode("utf-8"),
                self._app.token.encode("utf-8"),
            )
        self._send_bytes(HTTPStatus.OK, content_type, payload)

    def _asset(self, filename: str) -> bytes:
        """Read one bundled asset once per process."""

        with self._asset_lock:
            cached = self._cached_assets.get(filename)
            if cached is not None:
                return cached
        payload = (STATIC_ROOT / filename).read_bytes()
        with self._asset_lock:
            self._cached_assets[filename] = payload
        return payload

    def _read_json_body(self) -> Mapping[str, object] | ApiResponse:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length) if raw_length else 0
        except ValueError:
            return error_response(400, "INVALID_LENGTH", "Content-Length must be an integer")
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            return error_response(413, "BODY_TOO_LARGE", "the request body exceeds the local limit")
        if length == 0:
            return {}
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";")[0].strip().lower() != "application/json":
            return error_response(415, "UNSUPPORTED_MEDIA_TYPE", "send application/json")
        try:
            raw = self.rfile.read(length)
        except OSError:
            return error_response(400, "BODY_READ_ERROR", "the request body could not be read")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error_response(400, "INVALID_JSON", "body must be JSON")
        if not isinstance(parsed, dict):
            return error_response(400, "INVALID_JSON", "body must be a JSON object")
        return parsed

    def _host_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        return host.strip().lower() in self._app.allowed_hosts

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin.strip().rstrip("/").lower() in self._app.allowed_origins

    def _token_valid(self) -> bool:
        supplied = self.headers.get(TOKEN_HEADER, "")
        return secrets.compare_digest(supplied, self._app.token)

    def _send_json(self, response: ApiResponse) -> None:
        payload = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
        self._send_bytes(response.status, "application/json; charset=utf-8", payload)

    def _send_bytes(self, status: int | HTTPStatus, content_type: str, payload: bytes) -> None:
        try:
            self.send_response(int(status))
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            for name, value in _SECURITY_HEADERS:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # A browser that navigated away mid-poll is normal, not a server fault.
            self.close_connection = True


def _allowed_hosts(requested_host: str, bound_host: str, port: int) -> frozenset[str]:
    """Build the exact Host header values this server answers to."""

    names = {requested_host, bound_host}
    if requested_host in LOOPBACK_HOSTS or bound_host in LOOPBACK_HOSTS:
        names |= {"127.0.0.1", "localhost", "::1"}
    allowed: set[str] = set()
    for name in names:
        if not name:
            continue
        literal = f"[{name}]" if ":" in name else name
        allowed.add(f"{literal}:{port}".lower())
        if port == 80:
            allowed.add(literal.lower())
    return frozenset(allowed)


def _allowed_origins(hosts: frozenset[str]) -> frozenset[str]:
    return frozenset(f"http://{host}" for host in hosts)


def _display_host(requested_host: str, bound_host: str) -> str:
    """Prefer the name the user asked for so the printed URL stays recognizable."""

    if requested_host in {"", "0.0.0.0", "::"}:
        return DEFAULT_HOST
    return requested_host or bound_host


def free_port(host: str = DEFAULT_HOST) -> int:
    """Return one currently free local port for tests and ``--port 0`` callers."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])

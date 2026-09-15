"""Offline tests for the loopback HTTP server and its access rules.

Each test binds an ephemeral loopback port and speaks plain HTTP, so the token,
Host, Origin, body, and static-asset behaviour is verified against the real
handler rather than a mock.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import FunctionCall, ModelResponse, ToolCall
from proofcoder.web.server import (
    MAX_REQUEST_BODY_BYTES,
    TOKEN_HEADER,
    TOKEN_PLACEHOLDER,
    ServerAddressError,
    WebServer,
    create_server,
    free_port,
)

SENSITIVE_SENTINEL = "never-echo-this-server-value"
REASONING = "hidden-reasoning-must-stay-private"
ENVIRON = {"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL}


def _response(*, content: str | None = None, calls: tuple[ToolCall, ...] = ()) -> ModelResponse:
    return ModelResponse(
        content=content,
        reasoning_content=REASONING,
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=calls,
    )


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(id=call_id, function=FunctionCall(name=name, arguments=json.dumps(arguments)))


class _Client:
    """Minimal HTTP client that sends the local headers the server requires."""

    def __init__(self, server: WebServer) -> None:
        self._server = server

    def request(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        headers: Mapping[str, str] | None = None,
        raw_body: bytes | None = None,
        send_token: bool = True,
    ) -> tuple[int, bytes, http.client.HTTPMessage]:
        connection = http.client.HTTPConnection("127.0.0.1", self._server.port, timeout=30)
        try:
            request_headers = {"Host": f"127.0.0.1:{self._server.port}"}
            if send_token:
                request_headers[TOKEN_HEADER] = self._server.token
            payload: bytes | None = raw_body
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                request_headers["Content-Type"] = "application/json"
            request_headers.update(headers or {})
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            return response.status, response.read(), response.headers
        finally:
            connection.close()

    def json(self, method: str, path: str, **kwargs: object) -> tuple[int, dict[str, object]]:
        status, raw, _ = self.request(method, path, **kwargs)  # type: ignore[arg-type]
        return status, json.loads(raw.decode("utf-8"))


@pytest.fixture
def server(tmp_path: Path) -> Iterator[WebServer]:
    scripted = ScriptedClient(
        [
            _response(
                content="creating",
                calls=(_call("create-1", "create_file", {"path": "made.py", "content": "x\n"}),),
            ),
            _response(
                content="finishing",
                calls=(
                    _call(
                        "finish-1",
                        "finish_task",
                        {"summary": "made it", "changed_files": ["made.py"]},
                    ),
                ),
            ),
        ]
    )
    bound = create_server(
        host="127.0.0.1",
        port=0,
        environ=ENVIRON,
        cwd=tmp_path,
        workspace=tmp_path,
        client_factory=lambda config: scripted,
    )
    thread = threading.Thread(target=bound.serve_forever, daemon=True)
    thread.start()
    try:
        yield bound
    finally:
        bound.shutdown()
        thread.join(timeout=10)


# ---------- static assets ----------


def test_the_page_receives_the_session_token_only_from_this_origin(server: WebServer) -> None:
    status, raw, headers = _Client(server).request("GET", "/", send_token=False)
    page = raw.decode("utf-8")

    assert status == 200
    assert server.token in page
    assert TOKEN_PLACEHOLDER not in page
    assert headers["Content-Type"].startswith("text/html")
    assert headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/index.html", "text/html"),
        ("/app.js", "text/javascript"),
        ("/styles.css", "text/css"),
        ("/favicon.svg", "image/svg+xml"),
    ],
)
def test_each_bundled_asset_is_served(server: WebServer, path: str, content_type: str) -> None:
    status, raw, headers = _Client(server).request("GET", path, send_token=False)

    assert status == 200
    assert raw
    assert headers["Content-Type"].startswith(content_type)


def test_the_page_never_references_an_external_origin(server: WebServer) -> None:
    client = _Client(server)
    page = client.request("GET", "/", send_token=False)[1].decode("utf-8")
    script = client.request("GET", "/app.js", send_token=False)[1].decode("utf-8")
    style = client.request("GET", "/styles.css", send_token=False)[1].decode("utf-8")

    for source in (page, script, style):
        assert "http://" not in source.replace("http://www.w3.org/2000/svg", "")
        assert "https://" not in source


def test_unknown_static_paths_and_traversal_are_refused(server: WebServer) -> None:
    client = _Client(server)

    missing, _ = client.json("GET", "/missing.html", send_token=False)
    traversal, _ = client.json("GET", "/../pyproject.toml", send_token=False)
    nested, _ = client.json("GET", "/static/app.js", send_token=False)

    assert missing == 404
    assert traversal == 404
    assert nested == 404


def test_static_paths_reject_state_changing_methods(server: WebServer) -> None:
    client = _Client(server)

    assert client.json("POST", "/", body={})[0] == 405
    status, _raw, _ = client.request("HEAD", "/")
    assert status == 405


def test_a_missing_asset_reports_a_safe_error(
    server: WebServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import proofcoder.web.server as server_module

    monkeypatch.setattr(server_module._RequestHandler, "_cached_assets", {})

    def unreadable(self: Path) -> bytes:
        raise OSError("asset gone")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    status, body = _Client(server).json("GET", "/styles.css", send_token=False)

    assert status == 500
    assert body["error"]["code"] == "ASSET_UNAVAILABLE"  # type: ignore[index]


# ---------- access control ----------


def test_api_requests_require_the_session_token(server: WebServer) -> None:
    client = _Client(server)

    missing = client.json("GET", "/api/status", send_token=False)
    wrong = client.json("GET", "/api/status", headers={TOKEN_HEADER: "not-the-token"})

    assert missing[0] == 401
    assert wrong[0] == 401
    assert wrong[1]["error"]["code"] == "INVALID_TOKEN"  # type: ignore[index]


def test_a_foreign_host_header_is_refused(server: WebServer) -> None:
    status, body = _Client(server).json(
        "GET",
        "/api/status",
        headers={"Host": "attacker.example.com"},
    )

    assert status == 403
    assert body["error"]["code"] == "HOST_NOT_ALLOWED"  # type: ignore[index]


def test_a_foreign_origin_is_refused(server: WebServer) -> None:
    status, body = _Client(server).json(
        "GET",
        "/api/status",
        headers={"Origin": "http://attacker.example.com"},
    )

    assert status == 403
    assert body["error"]["code"] == "ORIGIN_NOT_ALLOWED"  # type: ignore[index]


def test_the_servers_own_origin_is_accepted(server: WebServer) -> None:
    status, _ = _Client(server).json(
        "GET",
        "/api/status",
        headers={"Origin": f"http://127.0.0.1:{server.port}"},
    )

    assert status == 200


def test_a_loopback_alias_host_is_accepted(server: WebServer) -> None:
    status, _ = _Client(server).json(
        "GET",
        "/api/status",
        headers={"Host": f"localhost:{server.port}"},
    )

    assert status == 200


# ---------- request bodies ----------


def test_json_bodies_must_declare_their_media_type(server: WebServer) -> None:
    status, body = _Client(server).json(
        "POST",
        "/api/doctor",
        raw_body=b'{"offline": true}',
        headers={"Content-Type": "text/plain", "Content-Length": "17"},
    )

    assert status == 415
    assert body["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"  # type: ignore[index]


def test_malformed_json_is_refused(server: WebServer) -> None:
    status, body = _Client(server).json(
        "POST",
        "/api/doctor",
        raw_body=b"{not json",
        headers={"Content-Type": "application/json", "Content-Length": "9"},
    )

    assert status == 400
    assert body["error"]["code"] == "INVALID_JSON"  # type: ignore[index]


def test_a_non_object_json_body_is_refused(server: WebServer) -> None:
    payload = b"[1, 2, 3]"
    status, body = _Client(server).json(
        "POST",
        "/api/doctor",
        raw_body=payload,
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
    )

    assert status == 400
    assert body["error"]["code"] == "INVALID_JSON"  # type: ignore[index]


def test_an_oversized_body_is_refused_before_it_is_read(server: WebServer) -> None:
    status, body = _Client(server).json(
        "POST",
        "/api/doctor",
        raw_body=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(MAX_REQUEST_BODY_BYTES + 1),
        },
    )

    assert status == 413
    assert body["error"]["code"] == "BODY_TOO_LARGE"  # type: ignore[index]


def test_an_invalid_content_length_is_refused(server: WebServer) -> None:
    status, body = _Client(server).json(
        "POST",
        "/api/doctor",
        raw_body=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "two"},
    )

    assert status == 400
    assert body["error"]["code"] == "INVALID_LENGTH"  # type: ignore[index]


def test_a_body_less_post_is_accepted(server: WebServer) -> None:
    status, body = _Client(server).json("POST", "/api/doctor")

    assert status == 200
    assert body["offline"] is False or body["offline"] is True


def test_an_unexpected_router_failure_is_reported_safely(
    server: WebServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding(request: object) -> object:
        raise RuntimeError(f"leaking {SENSITIVE_SENTINEL}")

    monkeypatch.setattr(server.http.router, "handle", exploding)
    status, body = _Client(server).json("GET", "/api/status")

    assert status == 500
    assert body["error"]["code"] == "INTERNAL_ERROR"  # type: ignore[index]
    assert SENSITIVE_SENTINEL not in json.dumps(body)


# ---------- full run over HTTP ----------


def test_a_run_streams_to_completion_over_http(server: WebServer, tmp_path: Path) -> None:
    client = _Client(server)

    status, started = client.json(
        "POST",
        "/api/runs",
        body={"workspace": str(tmp_path), "task": "create made.py"},
    )
    run_id = started["run"]["run_id"]  # type: ignore[index]
    collected: list[dict[str, object]] = []
    cursor = 0
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        _, page = client.json("GET", f"/api/runs/{run_id}/events?cursor={cursor}&wait=5")
        cursor = int(page["cursor"])  # type: ignore[arg-type]
        collected.extend(page["events"])  # type: ignore[arg-type]
        if page["done"]:
            break
    else:
        raise AssertionError("the run did not finish within the test timeout")

    assert status == 201
    assert [item["event_type"] for item in collected][-1] == "termination"
    assert (tmp_path / "made.py").read_text(encoding="utf-8") == "x\n"
    serialized = json.dumps(collected)
    assert SENSITIVE_SENTINEL not in serialized
    assert REASONING not in serialized

    _, listed = client.json("GET", f"/api/traces?workspace={tmp_path}")
    assert listed["traces"][0]["task"] == "create made.py"  # type: ignore[index]


def test_a_run_can_be_cancelled_over_http(tmp_path: Path) -> None:
    started_signal = threading.Event()
    release = threading.Event()

    class _GatedClient:
        def complete(self, messages: object, tools: object = ()) -> ModelResponse:
            started_signal.set()
            release.wait(10)
            return _response(content="released")

    bound = create_server(
        host="127.0.0.1",
        port=0,
        environ=ENVIRON,
        cwd=tmp_path,
        workspace=tmp_path,
        client_factory=lambda config: _GatedClient(),
    )
    thread = threading.Thread(target=bound.serve_forever, daemon=True)
    thread.start()
    client = _Client(bound)
    try:
        _, started = client.json(
            "POST",
            "/api/runs",
            body={"workspace": str(tmp_path), "task": "cancel me"},
        )
        run_id = started["run"]["run_id"]  # type: ignore[index]
        assert started_signal.wait(10)
        status, cancelled = client.json("POST", f"/api/runs/{run_id}/cancel", body={})
        release.set()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            _, detail = client.json("GET", f"/api/runs/{run_id}")
            if detail["run"]["status"] == "finished":  # type: ignore[index]
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the cancelled run did not finish")
    finally:
        release.set()
        bound.shutdown()
        thread.join(timeout=10)

    assert status == 200
    assert cancelled["cancelled"] is True
    assert detail["run"]["termination_reason"] == "interrupted"  # type: ignore[index]
    assert detail["run"]["exit_code"] == 130  # type: ignore[index]


# ---------- binding ----------


def test_a_taken_port_reports_a_safe_address_error(tmp_path: Path) -> None:
    first = create_server(host="127.0.0.1", port=0, environ=ENVIRON, cwd=tmp_path)
    try:
        with pytest.raises(ServerAddressError) as error:
            create_server(host="127.0.0.1", port=first.port, environ=ENVIRON, cwd=tmp_path)
    finally:
        first.shutdown()

    assert error.value.code == "ADDRESS_UNAVAILABLE"
    assert "--port" in str(error.value)


def test_the_reported_url_matches_the_bound_port(tmp_path: Path) -> None:
    bound = create_server(host="127.0.0.1", port=0, environ=ENVIRON, cwd=tmp_path)
    try:
        assert bound.url == f"http://127.0.0.1:{bound.port}/"
        assert bound.port != 0
        assert len(bound.token) >= 32
    finally:
        bound.shutdown()


def test_an_explicit_port_is_honoured(tmp_path: Path) -> None:
    port = free_port()
    bound = create_server(host="127.0.0.1", port=port, environ=ENVIRON, cwd=tmp_path)
    try:
        assert bound.port == port
    finally:
        bound.shutdown()


def test_a_supplied_token_and_session_manager_are_used(tmp_path: Path) -> None:
    from proofcoder.web.runs import BrowserRunManager

    manager = BrowserRunManager(environ=ENVIRON, client_factory=lambda config: ScriptedClient([]))
    bound = create_server(
        host="127.0.0.1",
        port=0,
        environ=ENVIRON,
        cwd=tmp_path,
        sessions=manager,
        token="fake-fixed-local-session-value",
    )
    try:
        assert bound.token == "fake-fixed-local-session-value"
        assert bound.sessions is manager
    finally:
        bound.shutdown()


def test_request_logging_is_routed_to_the_injected_logger(tmp_path: Path) -> None:
    lines: list[str] = []
    bound = create_server(
        host="127.0.0.1",
        port=0,
        environ=ENVIRON,
        cwd=tmp_path,
        request_logger=lines.append,
    )
    thread = threading.Thread(target=bound.serve_forever, daemon=True)
    thread.start()
    try:
        _Client(bound).request("GET", "/", send_token=False)
    finally:
        bound.shutdown()
        thread.join(timeout=10)

    assert any("GET /" in line for line in lines)


def test_serve_stops_the_session_manager(tmp_path: Path) -> None:
    from proofcoder.web.server import serve

    bound = create_server(host="127.0.0.1", port=0, environ=ENVIRON, cwd=tmp_path)
    stopper = threading.Thread(target=lambda: (time.sleep(0.2), bound.http.shutdown()))
    stopper.start()

    serve(bound)
    stopper.join(timeout=10)

    assert bound.sessions.active_count() == 0

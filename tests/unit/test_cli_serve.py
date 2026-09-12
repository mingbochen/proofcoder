"""Offline tests for the ``proofcoder serve`` command.

The command is exercised with an injected server factory so argument bounds,
workspace resolution, the non-loopback warning, and shutdown behaviour are covered
without binding a port, plus one real bind to prove the wiring is complete.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

import proofcoder.cli as cli
from proofcoder.web.server import ServerAddressError, WebServer, create_server

SENSITIVE_SENTINEL = "never-print-this-serve-value"


def _console() -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    return Console(file=stream, force_terminal=False, color_system=None, width=200), stream


def test_serve_reports_its_address_and_never_prints_the_token(tmp_path: Path) -> None:
    console, stream = _console()
    created: dict[str, object] = {}
    served: list[WebServer] = []

    def factory(**kwargs: object) -> WebServer:
        created.update(kwargs)
        return create_server(host="127.0.0.1", port=0, environ={}, cwd=tmp_path)

    code = cli._run_server(
        host="127.0.0.1",
        port=0,
        workspace_argument=str(tmp_path),
        allow_browse=True,
        open_browser=False,
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        console=console,
        server_factory=factory,
        serve_forever=served.append,
    )
    output = stream.getvalue()
    server = served[0]
    server.shutdown()

    assert code == 0
    assert len(served) == 1
    assert f"SERVE http://127.0.0.1:{server.port}/" in output
    assert f"workspace={tmp_path}" in output
    assert server.token not in output
    assert SENSITIVE_SENTINEL not in output
    assert created["allow_browse"] is True
    assert created["workspace"] == tmp_path


def test_serve_warns_when_the_bind_is_not_loopback(tmp_path: Path) -> None:
    console, stream = _console()
    served: list[WebServer] = []

    def factory(**kwargs: object) -> WebServer:
        return create_server(host="127.0.0.1", port=0, environ={}, cwd=tmp_path)

    code = cli._run_server(
        host="0.0.0.0",  # the warning under test is exactly about this bind
        port=0,
        workspace_argument=None,
        allow_browse=True,
        open_browser=False,
        environ={},
        cwd=tmp_path,
        console=console,
        server_factory=factory,
        serve_forever=served.append,
    )
    output = stream.getvalue()
    served[0].shutdown()

    assert code == 0
    assert "WARN serve:" in output
    assert "non-loopback" in output


def test_serve_does_not_warn_on_loopback(tmp_path: Path) -> None:
    console, stream = _console()
    served: list[WebServer] = []

    cli._run_server(
        host="localhost",
        port=0,
        workspace_argument=None,
        allow_browse=False,
        open_browser=False,
        environ={},
        cwd=tmp_path,
        console=console,
        server_factory=lambda **kwargs: create_server(
            host="127.0.0.1", port=0, environ={}, cwd=tmp_path
        ),
        serve_forever=served.append,
    )
    served[0].shutdown()

    assert "WARN serve:" not in stream.getvalue()


def test_serve_defaults_the_workspace_to_the_current_directory(tmp_path: Path) -> None:
    console, stream = _console()
    served: list[WebServer] = []
    created: dict[str, object] = {}

    def factory(**kwargs: object) -> WebServer:
        created.update(kwargs)
        return create_server(host="127.0.0.1", port=0, environ={}, cwd=tmp_path)

    cli._run_server(
        host="127.0.0.1",
        port=0,
        workspace_argument=None,
        allow_browse=True,
        open_browser=False,
        environ={},
        cwd=tmp_path,
        console=console,
        server_factory=factory,
        serve_forever=served.append,
    )
    served[0].shutdown()

    assert created["workspace"] == tmp_path
    assert f"workspace={tmp_path}" in stream.getvalue()


def test_serve_accepts_a_relative_workspace(tmp_path: Path) -> None:
    (tmp_path / "child").mkdir()
    console, _ = _console()
    served: list[WebServer] = []
    created: dict[str, object] = {}

    def factory(**kwargs: object) -> WebServer:
        created.update(kwargs)
        return create_server(host="127.0.0.1", port=0, environ={}, cwd=tmp_path)

    cli._run_server(
        host="127.0.0.1",
        port=0,
        workspace_argument="child",
        allow_browse=True,
        open_browser=False,
        environ={},
        cwd=tmp_path,
        console=console,
        server_factory=factory,
        serve_forever=served.append,
    )
    served[0].shutdown()

    assert created["workspace"] == tmp_path / "child"


def test_serve_rejects_a_missing_workspace(tmp_path: Path) -> None:
    console, stream = _console()

    def forbidden(**kwargs: object) -> WebServer:
        raise AssertionError("the server must not be created for a bad workspace")

    code = cli._run_server(
        host="127.0.0.1",
        port=0,
        workspace_argument=str(tmp_path / "absent"),
        allow_browse=True,
        open_browser=False,
        environ={},
        cwd=tmp_path,
        console=console,
        server_factory=forbidden,
    )

    assert code == 2
    assert "FAIL serve: workspace must be an existing directory" in stream.getvalue()


def test_serve_reports_an_unavailable_address(tmp_path: Path) -> None:
    console, stream = _console()

    def factory(**kwargs: object) -> WebServer:
        raise ServerAddressError("ADDRESS_UNAVAILABLE", "could not listen on 127.0.0.1:8765")

    code = cli._run_server(
        host="127.0.0.1",
        port=8765,
        workspace_argument=None,
        allow_browse=True,
        open_browser=False,
        environ={},
        cwd=tmp_path,
        console=console,
        server_factory=factory,
    )

    assert code == 2
    assert "FAIL serve: ADDRESS_UNAVAILABLE" in stream.getvalue()


def test_serve_can_open_the_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    console, _ = _console()
    served: list[WebServer] = []

    cli._run_server(
        host="127.0.0.1",
        port=0,
        workspace_argument=None,
        allow_browse=True,
        open_browser=True,
        environ={},
        cwd=tmp_path,
        console=console,
        server_factory=lambda **kwargs: create_server(
            host="127.0.0.1", port=0, environ={}, cwd=tmp_path
        ),
        serve_forever=served.append,
    )
    server = served[0]
    server.shutdown()

    assert opened == [server.url]


def test_a_failing_browser_launcher_does_not_fail_the_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webbrowser

    def exploding(url: str) -> bool:
        raise RuntimeError("no browser here")

    monkeypatch.setattr(webbrowser, "open", exploding)
    console, _ = _console()
    served: list[WebServer] = []

    code = cli._run_server(
        host="127.0.0.1",
        port=0,
        workspace_argument=None,
        allow_browse=True,
        open_browser=True,
        environ={},
        cwd=tmp_path,
        console=console,
        server_factory=lambda **kwargs: create_server(
            host="127.0.0.1", port=0, environ={}, cwd=tmp_path
        ),
        serve_forever=served.append,
    )
    served[0].shutdown()

    assert code == 0


def test_the_default_runner_stops_the_server_after_an_interrupt(tmp_path: Path) -> None:
    server = create_server(host="127.0.0.1", port=0, environ={}, cwd=tmp_path)

    class _Interrupting:
        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def shutdown(self) -> None:
            server.shutdown()

    cli._serve_forever(_Interrupting())  # type: ignore[arg-type]

    assert server.sessions.active_count() == 0


def test_an_interrupt_during_serve_returns_the_interrupt_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, stream = _console()

    def interrupting(bound: WebServer) -> None:
        bound.shutdown()
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_serve_forever", interrupting)

    code = cli.main(
        ["serve", "--port", "0", "--workspace", str(tmp_path)],
        environ={},
        cwd=tmp_path,
        console=console,
    )
    output = stream.getvalue()

    assert code == 130
    assert "SERVE http://127.0.0.1:" in output
    assert "SERVE stopped" in output


def test_main_binds_a_real_local_server_for_the_serve_command(tmp_path: Path) -> None:
    console, stream = _console()
    bound: list[WebServer] = []

    def capture(server: WebServer) -> None:
        bound.append(server)
        server.shutdown()

    code = cli.main(
        ["serve", "--port", "0", "--workspace", str(tmp_path), "--no-browse"],
        environ={},
        cwd=tmp_path,
        console=console,
        serve_forever=capture,
    )

    assert code == 0
    assert bound[0].port != 0
    assert f"SERVE http://127.0.0.1:{bound[0].port}/" in stream.getvalue()


@pytest.mark.parametrize("port", ["-1", "65536", "many"])
def test_port_arguments_are_bounded(port: str) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["serve", "--port", port])


def test_serve_arguments_have_local_defaults() -> None:
    args = cli.build_parser().parse_args(["serve"])

    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.workspace is None
    assert args.no_browse is False
    assert args.open is False

"""Offline tests for doctor and both CLI entry points."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from rich.console import Console

import proofcoder.cli as cli
from proofcoder.config import ProofCoderConfig
from proofcoder.errors import DeepSeekAPIError
from proofcoder.protocol import ModelResponse

SENSITIVE_SENTINEL = "never-print-this-value"
REASONING_SENTINEL = "internal-reasoning-must-stay-private"


class _SuccessClient:
    def check_connection(self) -> ModelResponse:
        return ModelResponse(
            content="response-content-is-not-doctor-output",
            reasoning_content=REASONING_SENTINEL,
            finish_reason="stop",
            usage=None,
        )


class _FailureClient:
    def check_connection(self) -> ModelResponse:
        raise DeepSeekAPIError(f"unsafe detail: {SENSITIVE_SENTINEL}")


def _run_cli(
    argv: list[str],
    *,
    environ: Mapping[str, str],
    cwd: Path,
    client_factory: Callable[[ProofCoderConfig], object],
) -> tuple[int, str]:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=120)
    code = cli.main(
        argv,
        environ=environ,
        cwd=cwd,
        console=console,
        client_factory=client_factory,
    )
    return code, stream.getvalue()


def test_doctor_offline_passes_without_creating_client(tmp_path: Path) -> None:
    called = False

    def forbidden_factory(config: ProofCoderConfig) -> object:
        nonlocal called
        called = True
        raise AssertionError(config)

    code, output = _run_cli(
        ["doctor", "--offline"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        client_factory=forbidden_factory,
    )

    assert code == 0
    assert called is False
    assert "skipped in offline mode" in output
    assert SENSITIVE_SENTINEL not in output


def test_online_doctor_success_hides_response_and_reasoning(tmp_path: Path) -> None:
    def factory(config: ProofCoderConfig) -> _SuccessClient:
        assert config.api_key == SENSITIVE_SENTINEL
        return _SuccessClient()

    code, output = _run_cli(
        ["doctor"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        client_factory=factory,
    )

    assert code == 0
    assert "connection succeeded" in output
    assert SENSITIVE_SENTINEL not in output
    assert REASONING_SENTINEL not in output
    assert "response-content-is-not-doctor-output" not in output


def test_online_doctor_failure_is_safe_and_nonzero(tmp_path: Path) -> None:
    def factory(config: ProofCoderConfig) -> _FailureClient:
        assert config.api_key == SENSITIVE_SENTINEL
        return _FailureClient()

    code, output = _run_cli(
        ["doctor"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        client_factory=factory,
    )

    assert code == 1
    assert "request failed" in output
    assert SENSITIVE_SENTINEL not in output


def test_missing_online_api_key_is_a_clear_configuration_error(tmp_path: Path) -> None:
    code, output = _run_cli(
        ["doctor"],
        environ={},
        cwd=tmp_path,
        client_factory=lambda config: _SuccessClient(),
    )

    assert code == 1
    assert "DEEPSEEK_API_KEY is required" in output


def test_doctor_redacts_secret_from_other_displayed_configuration(tmp_path: Path) -> None:
    code, output = _run_cli(
        ["doctor"],
        environ={
            "DEEPSEEK_API_KEY": SENSITIVE_SENTINEL,
            "DEEPSEEK_MODEL": f"model-{SENSITIVE_SENTINEL}",
        },
        cwd=tmp_path,
        client_factory=lambda config: _SuccessClient(),
    )

    assert code == 0
    assert SENSITIVE_SENTINEL not in output
    assert "model-[redacted]" in output


def test_failed_local_check_prevents_network_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called = False

    def forbidden_factory(config: ProofCoderConfig) -> object:
        nonlocal called
        called = True
        raise AssertionError(config)

    monkeypatch.setattr(cli.os, "access", lambda path, mode: False)
    code, output = _run_cli(
        ["doctor"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        client_factory=forbidden_factory,
    )

    assert code == 1
    assert called is False
    assert "FAIL Working directory" in output


def test_package_import_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cli.importlib,
        "import_module",
        lambda name: (_ for _ in ()).throw(ImportError(name)),
    )
    code, output = _run_cli(
        ["doctor", "--offline"],
        environ={},
        cwd=tmp_path,
        client_factory=lambda config: _SuccessClient(),
    )

    assert code == 1
    assert "FAIL ProofCoder import" in output


@pytest.mark.parametrize("module_entry", [True, False])
def test_help_is_available_from_both_entry_points(module_entry: bool) -> None:
    if module_entry:
        command = [sys.executable, "-m", "proofcoder", "--help"]
    else:
        executable = shutil.which("proofcoder")
        assert executable is not None
        command = [executable, "--help"]

    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert "doctor" in result.stdout

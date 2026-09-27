"""Mounted factory verification capability reports (#3375)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import anyio
from aiohttp import web
from aiohttp.test_utils import TestServer
from curie_runner import __main__ as boot
from curie_runner.config import RunnerConfig
from curie_runner.mcp_tool_capability import McpToolCapabilityProbe
from curie_runner.progress import PROGRESS_TOKEN_ENV, PROGRESS_URL_ENV

_BUDGET = '{"max_output_tokens_per_run": 10000, "max_usd_per_day": 1.0}'
_CHECK_COMMAND = "uv run pytest runner/tests -q"
_TOKEN = "sbx.example-progress-token.signature"


class _CapturedSession:
    def __init__(self, options: Any, events: list[str]) -> None:
        self.options = options
        self.events = events

    async def connect(self) -> None:
        self.events.append("model_started")

    async def query(self, _text: str) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def receive_turn(self) -> Any:
        if False:
            yield None


def _config(root: Path) -> RunnerConfig:
    plugin = root / "plugin"
    manifest = plugin / ".claude-plugin"
    manifest.mkdir(parents=True)
    (manifest / "plugin.json").write_text(
        json.dumps({"name": "factory-preflight", "version": "0.1.0"}),
        encoding="utf-8",
    )
    return RunnerConfig.from_env(
        {
            "CURIE_PLUGIN_DIR": str(plugin),
            "CURIE_SESSION_ID": "s-factory-preflight",
            "CURIE_SANDBOX_ID": "b-factory-preflight",
            "CURIE_BUDGET": _BUDGET,
        }
    )


def _workspace(root: Path) -> Path:
    workspace = root / "workspace"
    (workspace / ".git").mkdir(parents=True)
    return workspace


def _uv_on_path(root: Path, marker: Path) -> Path:
    bindir = root / "bin"
    bindir.mkdir()
    executable = bindir / "uv"
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" > '{marker}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return bindir


async def _boot_with_report(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    path: str,
    probe_marker: Path | None = None,
) -> tuple[list[tuple[dict[str, Any], str | None]], list[str], str | None]:
    workspace = _workspace(tmp_path)
    events: list[str] = []
    received: list[tuple[dict[str, Any], str | None]] = []
    app = web.Application()

    async def record(request: web.Request) -> web.Response:
        body = await request.json()
        received.append((body, request.headers.get("X-API-Key")))
        if probe_marker is not None:
            assert probe_marker.is_file()
            events.append("probe_executed")
        events.append("verification_posted")
        return web.json_response({"recorded": True}, status=201)

    app.router.add_post(
        "/v1/work-item-progress/example-request/verification", record
    )

    async with TestServer(app) as server:
        monkeypatch.setenv("PATH", path)
        monkeypatch.setenv(PROGRESS_URL_ENV, str(server.make_url(
            "/v1/work-item-progress/example-request"
        )))
        monkeypatch.setenv(PROGRESS_TOKEN_ENV, _TOKEN)

        class CapturedSession(_CapturedSession):
            def __init__(self, options: Any) -> None:
                super().__init__(options, events)

        monkeypatch.setattr(boot, "ClaudeAgentSession", CapturedSession)
        config = _config(tmp_path)
        runner = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: boot.build_runner(
                config,
                workspace_path=workspace,
                mcp_capability=McpToolCapabilityProbe(
                    complete=True,
                    has_potential_write_tool=False,
                    tool_count=0,
                ),
            ),
        )
        await runner.start()
        prompt = runner._session.options.system_prompt
        return received, events, prompt


async def _write_positive_boot(
    tmp_path: Path, monkeypatch: Any
) -> tuple[list[tuple[dict[str, Any], str | None]], list[str], str | None, Path]:
    marker = tmp_path / "uv-arguments"
    bindir = _uv_on_path(tmp_path, marker)
    received, events, prompt = await _boot_with_report(
        tmp_path, monkeypatch, path=str(bindir), probe_marker=marker
    )
    return received, events, prompt, marker


def test_mounted_factory_runs_uv_probe_and_posts_before_model_start(
    tmp_path: Path, monkeypatch: Any
) -> None:
    async def run() -> tuple[
        list[tuple[dict[str, Any], str | None]], list[str], str | None, Path
    ]:
        return await _write_positive_boot(tmp_path, monkeypatch)

    received, events, prompt, marker = anyio.run(run)

    assert marker.read_text(encoding="utf-8").splitlines() == ["--version"]
    assert len(received) == 1
    body, token = received[0]
    assert token == _TOKEN
    assert body == {
        "command": _CHECK_COMMAND,
        "outcome": "available",
        "missing_binaries": [],
        "blocked_services": [],
    }
    assert events == ["probe_executed", "verification_posted", "model_started"]
    assert prompt is not None
    assert _CHECK_COMMAND in prompt
    assert "in-sandbox verification is available" in prompt


def test_mounted_factory_reports_missing_uv_without_network_fallback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()

    async def run() -> tuple[list[tuple[dict[str, Any], str | None]], list[str], str | None]:
        return await _boot_with_report(tmp_path, monkeypatch, path=str(empty_path))

    received, events, prompt = anyio.run(run)

    assert len(received) == 1
    body, token = received[0]
    assert token == _TOKEN
    assert body == {
        "command": _CHECK_COMMAND,
        "outcome": "unavailable",
        "missing_binaries": ["uv"],
        "blocked_services": [],
    }
    assert events == ["verification_posted", "model_started"]
    assert prompt is not None
    assert _CHECK_COMMAND in prompt
    assert "in-sandbox verification is unavailable" in prompt
    assert "Do not request publication." in prompt

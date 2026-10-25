"""The availability probe must allow a slow but working OpenCode installation."""

import subprocess
import sys

import pytest

from procedo.studio.llm.opencode_backend import OpenCodeConfig
from procedo.studio.llm.server_health import ServerError
from procedo.studio.llm.server_manager import OpenCodeServerManager


@pytest.mark.parametrize("delay, allowance, available", [(5.2, 10, True), (0.3, 0.05, False)])
def test_version_probe_uses_startup_allowance(tmp_path, monkeypatch, delay, allowance, available):
    cli = tmp_path / "wsl.py"
    cli.write_text(
        f'import sys, time\nif "bash" in sys.argv: time.sleep({delay!r})\nprint("1.18.32")\n',
        encoding="utf-8",
    )
    real_run = subprocess.run

    def run_fake_wsl(command, **kwargs):
        return real_run([sys.executable, str(cli), *command[1:]], **kwargs)

    monkeypatch.setattr("procedo.studio.llm.server_manager.subprocess.run", run_fake_wsl)
    manager = OpenCodeServerManager(OpenCodeConfig(startup_timeout=allowance))
    assert manager.is_available() is available
    if available:
        assert manager.last_status.ok
    else:
        assert manager.last_status.error is ServerError.START_TIMEOUT
        assert "timed out" in manager.last_status.detail

"""The editor↔project-venv bridge asks the HOST where the venv is (share slice 1).

``<project>/.venv`` is gone: the venv is machine-local and lives under the per-user
app-data root. This bridge used to compose that path AND read the project root back
out of it (``python.parents[2] / "bundle"``). Both are pinned here — including the
one that matters on a bench: ``safe_off_remote``, the recovery safe-off the host
reaches through ``bench_recovery.recovery_safe_off``, must not refuse merely because
the venv moved.

Since one venv per BUNDLE, it moved again — into a pool shared by every project on
that bundle, which a limit in Settings can evict from — and, more sharply for this
bridge, an interpreter EXISTING stopped being proof there is one to run in: a killed
install leaves one sitting in an unfinished folder. So the bridge asks the host's
``existing_venv_python``, which answers only for a build that wrote ``.complete``,
and that is pinned here too.

Nothing here mocks the resolution: ``project_local_state`` really answers, with only
its ROOTS redirected into ``tmp_path`` so no test touches the real per-user app-data
folder. The child PROCESS is faked, because a stub interpreter cannot run a daemon.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from procedo.core import project_local_state, temp_root, venv_pool
from procedo.studio import _project_local_state
from procedo.studio.core import odb_inspect
from procedo.engine_client import engine, engine_session
from procedo.studio.pack_ops import pack_parsers

_PROJECT_ID = "aaaaaaaa-1111-2222-3333-444444444444"


@pytest.fixture
def local_root():
    """The sandboxed per-project state root ``tests/conftest.py`` installs for every
    test — named here only because these pins assert the media cache is UNDER it."""
    return project_local_state.local_root()


@pytest.fixture
def venvs_root():
    """The sandboxed ENVIRONMENT pool root, which is a different place: one folder
    per bundle, beside the per-project folders rather than inside one."""
    return project_local_state.venvs_root()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "config").mkdir(parents=True)
    (project / "config" / "config.json").write_text(
        json.dumps({"project_id": _PROJECT_ID}), encoding="utf-8"
    )
    # An INSTALLABLE bundle: a manifest and a requirements.txt are what an
    # environment is keyed on, so a bare ``bundle/`` folder has none at all.
    (project / "bundle" / "wheels").mkdir(parents=True)
    (project / "bundle" / "bundle.json").write_text(
        json.dumps({"id": "demo", "version": "1.0.0", "packs": []}), encoding="utf-8"
    )
    (project / "bundle" / "wheels" / "requirements.txt").write_text("", encoding="utf-8")
    return project


def _install_interpreter(project: Path, *, complete: bool = True) -> Path:
    """A stand-in interpreter where the HOST says this project's venv lives.

    Placed THROUGH ``venv_python``, so it cannot pin the platform layout itself —
    that is the host's function and the host suite spells the layout out
    (``test_the_interpreter_is_at_the_platforms_venv_layout``) — and through the
    pool's build protocol, because a folder assembled any other way is precisely
    what the bridge must refuse. With ``complete=False`` the build is left
    unfinished, which is what a killed install leaves behind.
    """
    key = venv_pool.key_for_project(project)
    folder = venv_pool.placement(project)
    venv_pool.begin_build(folder, key)
    python = project_local_state.venv_python(venv_pool.venv_in(folder))
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    if complete:
        venv_pool.finish_build(folder, key)
    return python


# --- resolution -------------------------------------------------------------


def test_the_bridge_finds_an_interpreter_for_a_project_with_no_dot_venv(venvs_root, tmp_path):
    project = _project(tmp_path)
    python = _install_interpreter(project)
    assert not (project / ".venv").exists()  # the old location is not even created

    venv = engine_session._resolve_project_python(project)

    assert venv.python == python
    assert venvs_root in python.parents


def test_an_unfinished_build_is_not_an_interpreter_to_dispatch_into(venvs_root, tmp_path):
    """The bridge asked whether the file EXISTED, which was enough when a venv was
    a folder this app made in one go. With a shared pool, a killed install leaves an
    interpreter that can import half a bundle — and an op dispatched into it answers
    wrongly and looks exactly like an op that answered rightly."""
    project = _project(tmp_path)
    python = _install_interpreter(project, complete=False)
    assert python.exists()  # ...and it is still not a venv

    with pytest.raises(engine_session.ParserUnavailable) as raised:
        engine_session._resolve_project_python(project)
    assert "machine-local venv" in str(raised.value)
    # The message still names where one WOULD be — that is what ``interpreter`` is for.
    assert str(python) in str(raised.value)


def test_the_bundle_dir_is_the_projects_even_though_the_venv_is_elsewhere(venvs_root, tmp_path):
    project = _project(tmp_path)
    python = _install_interpreter(project)

    venv = engine_session._resolve_project_python(project)

    assert venv.bundle_dir == project / "bundle"
    # The derivation this replaced — parents[2] of the interpreter — now names a
    # folder under the app-data root, so the child ran with an empty pack registry.
    assert python.parents[2] != project


def test_every_op_frame_carries_the_projects_bundle_dir(
    venvs_root, tmp_path, monkeypatch
):
    """An op goes to the project's TOOLS engine: the machine-local interpreter runs
    the daemon by path with ``--role tools``, and the frame names the bundle."""
    from procedo.engine_client import engine

    project = _project(tmp_path)
    python = _install_interpreter(project)
    daemons: list[_FakeDaemon] = []
    monkeypatch.setattr(engine, "_slots", {})
    monkeypatch.setattr(engine_session.subprocess, "Popen",
                        lambda argv, **_k: daemons.append(_FakeDaemon(argv)) or daemons[-1])

    pack_parsers.is_available(project)

    assert daemons[0].argv[0] == str(python)
    assert daemons[0].argv[1].endswith("_execute_op_subprocess.py")
    assert daemons[0].argv[2:] == ["--role", "tools"]
    assert daemons[0].frames[0]["op"] == "is_available"
    assert daemons[0].frames[0]["_bundle_dir"] == str(project / "bundle")


def test_no_venv_is_still_parser_unavailable(venvs_root, tmp_path):
    project = _project(tmp_path)  # nothing installed under the app-data root
    with pytest.raises(engine_session.ParserUnavailable) as raised:
        engine_session._resolve_project_python(project)
    assert "machine-local venv" in str(raised.value)


# --- the bundle-install stamp the editor watches -----------------------------


def test_the_install_stamp_is_asked_for_not_composed(venvs_root, tmp_path):
    """``project_controller.bundle_stamp_changed`` polls it as a belt-and-braces
    bundle-change signal. It moved out of ``<project>/config/`` with the venv it
    describes, and then again into the shared pool folder that venv lives in, so
    composed from the project it would name a file nothing writes — a watch that can
    never fire, which no green pin would ever show."""
    project = _project(tmp_path)
    stamp = _project_local_state.install_record(project)
    assert stamp == project_local_state.install_record(project)
    assert stamp.name == ".bundle_installed.json"
    assert venvs_root in stamp.parents and project not in stamp.parents


# --- the media cache the ODB panel SHARES with the main GUI ------------------


def test_the_odb_panel_reads_the_same_cache_the_main_gui_writes(local_root, tmp_path):
    """Found by extending the host's AST guard over this submodule — the sweep that
    only looked for ``.venv`` here missed it. A cache composed from the project path
    would have silently stopped being shared (and would write machine state onto the
    share) the moment the GUI's moved."""
    project = _project(tmp_path)
    shared = project_local_state.media_cache_dir(project) / "zoomed" / "images"
    shared.mkdir(parents=True)
    (shared / "R1.png").write_bytes(b"rendered by the main GUI")
    stale = project / ".media_cache" / "zoomed" / "images"
    stale.mkdir(parents=True)
    (stale / "R1.png").write_bytes(b"in-project, must be ignored")

    zoomed, _wide = odb_inspect.cached_image_paths(project, "R1", None)

    assert zoomed == shared / "R1.png"
    assert _project_local_state.media_cache(project) == project_local_state.media_cache_dir(
        project
    )


# --- the bench safe-off backstop --------------------------------------------


class _Pipe:
    """Just enough of a text pipe for ``RemoteSession._request``."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def readline(self) -> str:
        return self.lines.pop(0) if self.lines else ""


class _FakeDaemon:
    """Answers every frame with ok, recording the argv and the frames it was sent.

    Only the PROCESS is faked. Everything upstream of it — the host seam, the
    interpreter lookup, the bundle-dir resolution — is the real code path.
    """

    def __init__(self, argv) -> None:
        self.argv = argv
        self.frames: list[dict] = []
        self.returncode = None
        self.pid = 4242
        self.stdout = _Pipe()
        self.stderr = _Pipe()
        self.stdin = self

    def write(self, line: str) -> None:  # stdin
        self.frames.append(json.loads(line))
        self.stdout.lines.append(json.dumps({"ok": True, "log": []}) + "\n")

    def flush(self) -> None:  # stdin
        pass

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0


def test_safe_off_does_not_refuse_merely_because_the_venv_moved(
    venvs_root, tmp_path, monkeypatch
):
    """``bench_recovery.recovery_safe_off`` reaches exactly this. Before the fix it
    answered ``{"ok": False, "kind": "ParserUnavailable"}`` for every project on the
    bench, because it looked for a venv inside the project folder."""
    project = _project(tmp_path)
    python = _install_interpreter(project)
    daemons: list[_FakeDaemon] = []
    cwds: list[str] = []

    def fake_popen(argv, **kwargs):
        cwds.append(kwargs.get("cwd"))
        daemons.append(_FakeDaemon(argv))
        return daemons[-1]

    monkeypatch.setattr(engine_session.subprocess, "Popen", fake_popen)

    result = engine.safe_off_remote(
        [{"device": "PSU1", "etype": "psu", "channels": [1]}],
        {"PSU1": {"visa": "TCPIP0::10.0.0.5::INSTR"}},
        project,
    )

    assert result["ok"] is True
    assert daemons[0].argv[0] == str(python)  # launched the machine-local interpreter
    # …running the DAEMON, by file path; via ``-m`` it would import PySide6.
    assert daemons[0].argv[1].endswith("_execute_op_subprocess.py")
    assert daemons[0].frames[0]["cmd"] == "safe_off"
    assert daemons[0].frames[0]["_bundle_dir"] == str(project / "bundle")
    assert cwds == [str(temp_root.root())]  # the daemon's relative writes stay in app temp


def test_safe_off_still_refuses_when_there_really_is_no_venv(
    venvs_root, tmp_path, monkeypatch
):
    """The other half — the pin above is sensitive to exactly the thing that broke."""
    project = _project(tmp_path)  # no interpreter anywhere

    def never(*_args, **_kwargs):
        raise AssertionError("no daemon may be launched without a venv")

    monkeypatch.setattr(engine_session.subprocess, "Popen", never)

    result = engine.safe_off_remote([], {}, project)

    assert result["ok"] is False and result["kind"] == "ParserUnavailable"

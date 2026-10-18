"""Keeps the test suites out of the real %TEMP% and fails the run if anything gets in.

A pytest plugin both conftests register (the host loads this file from the editor
submodule, so there is one implementation).  Per process, an autouse session fixture
points TMP/TEMP/TMPDIR and ``tempfile.tempdir`` at a folder under pytest's basetemp
(xdist: one basetemp per worker; child processes inherit the env).  The controller
snapshots the real temp folder before any worker starts and, at session end, fails
the run for every new entry whose name carries one of our prefixes.  It does the same
for a ``results.*`` in the rootdir or the folder pytest was started from: a generated
test.py writes its results into its CWD unless told otherwise.

``tmp*`` is not ours: every Python process makes ``tmp*`` probe files, so a suite
running beside another one would fail on the other's.
"""

import os
import tempfile
from pathlib import Path

import pytest

OUR_PREFIXES = ("tpg_", "procedo", "render_")
RESULT_PREFIXES = ("results.",)
_ENV_NAMES = ("TMP", "TEMP", "TMPDIR")
_KEY = pytest.StashKey[list[tuple[Path, tuple[str, ...], set[str]]]]()


def _entries(folder: Path, prefixes: tuple[str, ...]) -> set[str]:
    return {p.name for p in folder.iterdir() if p.name.lower().startswith(prefixes)}


def _is_worker(config) -> bool:
    return hasattr(config, "workerinput")


def pytest_configure(config):
    if not _is_worker(config):
        watched = [(Path(tempfile.gettempdir()), OUR_PREFIXES)]
        watched += [(d, RESULT_PREFIXES) for d in {config.rootpath, config.invocation_params.dir}]
        config.stash[_KEY] = [(d, prefixes, _entries(d, prefixes)) for d, prefixes in watched]


@pytest.fixture(scope="session", autouse=True)
def _redirect_temp(tmp_path_factory):
    """Nothing a test (or the code under test) makes in a temp folder reaches the real one."""
    folder = tmp_path_factory.mktemp("tmp")
    saved_env = {name: os.environ.get(name) for name in _ENV_NAMES}
    saved_dir = tempfile.tempdir
    for name in _ENV_NAMES:
        os.environ[name] = str(folder)
    tempfile.tempdir = str(folder)
    yield folder
    tempfile.tempdir = saved_dir
    for name, value in saved_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def pytest_sessionfinish(session, exitstatus):
    if _is_worker(session.config) or _KEY not in session.config.stash:
        return
    for folder, prefixes, before in session.config.stash[_KEY]:
        new = sorted(_entries(folder, prefixes) - before)
        if new:
            print(f"\nTEMP GUARD: {len(new)} new entries in {folder}: {new[:20]}")
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

"""Test fixtures."""
import pytest
from workflow_editor.llm import (
    LLMRequest, LLMTask, NoneBackend, OpenCodeBackend, OpenCodeConfig
)

def _populate_pack_registry() -> None:
    """Populate the process-global pack registry for in-process parser tests.

    Post pack-pluggable cutover the bare wheel parse/render/reconstruct path
    resolves pack verbs/schema/equipment from a process-global registry. In
    production the consumer wires it from the project bundle; in this test
    suite there is no bundle, so register the in-repo packs directly from their
    self-describing attributes (PACK_PARSER / EQUIPMENT_CLAIMS / SCHEMA_FRAGMENT),
    mirroring the base pack's conftest. Best-effort: a no-op if the wheel or a
    pack is not importable, so it can never break a currently-passing test.
    """
    try:
        import importlib
        from rules_packager_base.rules.v2_0_2.parser._default_registry import (
            set_default_pack_parsers, register_schema_fragment,
            register_equipment_claims, clear_registry,
        )
    except Exception:
        return
    clear_registry()
    parsers = {}
    for module_path in ("labscpi_pack.rules.v2_0_1.parser",
                        "fncore_pack.rules.v2_0_1.parser"):
        try:
            mod = importlib.import_module(module_path)
        except Exception:
            continue
        claims = getattr(mod, "EQUIPMENT_CLAIMS", {}) or {}
        for etype in claims:
            parsers[etype] = mod.PACK_PARSER
        if claims:
            register_equipment_claims(claims)
        fragment = getattr(mod, "SCHEMA_FRAGMENT", None)
        if isinstance(fragment, dict):
            register_schema_fragment(fragment)
    if parsers:
        set_default_pack_parsers(parsers)


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires real LLM backend")
    _populate_pack_registry()


@pytest.fixture(autouse=True)
def _isolate_project_local_state(monkeypatch, tmp_path):
    """No editor test may build a venv or a media cache in the REAL per-user app-data
    root. The bridge (``workflow_editor._project_local_state``) asks the HOST seam
    where machine-local project state lives, and the host CREATES the folder as it
    resolves it, so the first editor test that gives a project a ``project_id``
    would otherwise land one in ``%LOCALAPPDATA%``. Mirrors the host suite's own
    autouse sandbox; a no-op in a standalone checkout with no host on sys.path.
    """
    try:
        from project_services import project_local_state
    except ImportError:
        yield
        return
    root = tmp_path / "_local_state"
    monkeypatch.setattr(project_local_state, "local_root", lambda: root)
    # The seam memoizes a resolved folder for the life of the PROCESS, so one test's
    # folder would otherwise be served to the next under a stale root.
    project_local_state.forget_resolved()
    yield
    project_local_state.forget_resolved()

@pytest.fixture
def sample_llm_request_minimal():
    return LLMRequest(
        task=LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=True,
        procedure_json=None, test_code=None, procedure_text=None,
        rules_content=None, session_summary=None, user_message=None
    )

@pytest.fixture
def sample_llm_request_simple():
    return LLMRequest(
        task=LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=False,
        procedure_json='{"name": "Test", "steps": [{"text": "Step 1"}]}',
        test_code=None, procedure_text=None, rules_content=None,
        session_summary=None, user_message=None
    )

@pytest.fixture
def sample_json_artifact():
    return '{"name": "LED Test", "steps": [{"text": "Connect LED"}]}'

@pytest.fixture
def sample_llm_response_json():
    return {
        "type": "llm_turn", "task": "generate_code_from_json",
        "assistant_message": "Done", "validation": {"status": "pass"},
        "proposals": {"test_code": {"mode": "replace", "content": "def test(): pass"}},
        "session_delta": {}
    }

@pytest.fixture
def available_backend():
    config = OpenCodeConfig()
    backend = OpenCodeBackend(config)
    if backend.is_available():
        yield backend
        if backend.is_running:
            backend.stop()
        return

    pytest.skip("No LLM backend available")

"""Test fixtures."""
import pytest
from . import temp_guard
from procedo.studio.llm import (
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
    config.pluginmanager.register(temp_guard)
    _register_per_user_sandbox(config)
    _populate_pack_registry()


def _register_per_user_sandbox(config):
    """The per-user folders, QSettings and environment are each test's — the host's one
    sandbox plugin (``procedo.core.testing``); none without the host, where this repo's CI
    runs its host-free gate (``tests/test_size_budgets.py``)."""
    try:
        from procedo.core import testing
    except ModuleNotFoundError as exc:
        if exc.name not in ("procedo", "procedo.core"):  # a broken sandbox must stop the run, not skip it
            raise
        return
    testing.register_plugin(config)

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


@pytest.fixture
def engine_in_process(monkeypatch):
    """The project's engine, answered in this process (a test has no project venv): every
    ``pack_parsers`` op, with or without a project open, is ``_engine_ops.run``'s answer,
    its frame crossing a JSON boundary as on the pipe."""
    import json

    from procedo.engine_client import _engine_ops
    from procedo.studio.pack_ops import pack_parsers

    def answer(_root, spec, timeout=None):
        return json.loads(json.dumps(_engine_ops.run(json.loads(json.dumps(spec)))))

    monkeypatch.setattr(pack_parsers, "_engine_call", answer)

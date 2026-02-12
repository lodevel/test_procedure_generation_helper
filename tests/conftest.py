"""Test fixtures."""
import os
import pytest
from workflow_editor.llm import (
    LLMRequest, LLMTask, NoneBackend, OpenCodeBackend, 
    ExternalAPIBackend, OpenCodeConfig
)

def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires real LLM backend")

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
    # Try OpenAI first
    if os.environ.get("OPENAI_API_KEY"):
        backend = ExternalAPIBackend(
            api_url="https://api.openai.com/v1",
            api_key=os.environ["OPENAI_API_KEY"],
            model="gpt-3.5-turbo"
        )
        if backend.is_available():
            yield backend
            if backend.is_running:
                backend.stop()
            return
    
    # Try OpenCode
    config = OpenCodeConfig()
    backend = OpenCodeBackend(config)
    if backend.is_available():
        yield backend
        if backend.is_running:
            backend.stop()
        return
    
    pytest.skip("No LLM backend available")

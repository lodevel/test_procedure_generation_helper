"""Integration tests with real LLM backend."""
import pytest
import time
from workflow_editor.llm import LLMRequest, LLMTask, LLMResponse

@pytest.mark.integration
class TestRealLLMConnection:
    def test_backend_available(self, available_backend):
        assert available_backend.is_available()
        print(f'\nUsing: {available_backend.name}')
    
    def test_backend_starts(self, available_backend):
        assert available_backend.start()
        assert available_backend.is_running
    
    def test_simple_request(self, available_backend, sample_llm_request_simple):
        available_backend.start()
        response = available_backend.send_request(sample_llm_request_simple)
        assert response is not None
        assert isinstance(response, LLMResponse)
        print(f'\nResponse: {response.assistant_message[:200]}')

@pytest.mark.integration
class TestEndToEnd:
    def test_generate_code_from_json(self, available_backend):
        request = LLMRequest(
            task=LLMTask.GENERATE_CODE_FROM_JSON, strict_mode=False,
            procedure_json='{"name": "Button Test", "steps": [{"text": "Press button A"}]}',
            test_code=None, procedure_text=None,
            rules_content='Use pytest. Add step markers.',
            session_summary=None, user_message=None
        )
        available_backend.start()
        response = available_backend.send_request(request)
        
        assert response is not None
        assert response.assistant_message
        print(f'\nMessage: {response.assistant_message}')
        
        if response.test_code:
            print(f'Code: {response.test_code.content[:300]}')
            assert len(response.test_code.content) > 0
    
    def test_review_json(self, available_backend):
        request = LLMRequest(
            task=LLMTask.REVIEW_JSON, strict_mode=True,
            procedure_json='{"name": "Test", "steps": [{"text": "Do something"}]}',
            test_code=None, procedure_text=None,
            rules_content=None, session_summary=None, user_message=None
        )
        available_backend.start()
        response = available_backend.send_request(request)
        
        assert response is not None
        assert response.assistant_message
        print(f'\nReview: {response.assistant_message[:200]}')
    
    def test_response_time(self, available_backend, sample_llm_request_simple):
        available_backend.start()
        start = time.time()
        response = available_backend.send_request(sample_llm_request_simple)
        elapsed = time.time() - start
        
        print(f'\nTime: {elapsed:.2f}s')
        assert elapsed < 60

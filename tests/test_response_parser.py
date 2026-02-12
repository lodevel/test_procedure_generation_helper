"""Tests for ResponseParser."""
import pytest
import json
from workflow_editor.llm import ResponseParser, LLMResponse, LLMTask

class TestResponseParserJsonExtraction:
    def test_extract_bare_json(self):
        parser = ResponseParser()
        raw = '{"type": "llm_turn", "task": "test"}'
        result = parser._extract_json(raw)
        # _extract_json returns string, not dict
        assert result and '"type"' in result and '"llm_turn"' in result
    
    def test_extract_markdown_wrapped_json(self):
        parser = ResponseParser()
        raw = '```json\n{"type": "llm_turn"}\n```'
        result = parser._extract_json(raw)
        assert result and 'llm_turn' in result
    
    def test_extract_code_block_json(self):
        parser = ResponseParser()
        raw = 'Some text\n```\n{"type": "llm_turn"}\n```\nMore text'
        result = parser._extract_json(raw)
        assert result and 'llm_turn' in result
    
    def test_no_json_returns_none(self):
        parser = ResponseParser()
        raw = 'Just plain text with no JSON'
        result = parser._extract_json(raw)
        assert result is None
    
    def test_malformed_json_returns_none(self):
        parser = ResponseParser()
        raw = '{"type": invalid json'
        result = parser._extract_json(raw)
        assert result is None

class TestResponseParserParsing:
    def test_parse_valid_response(self, sample_llm_response_json):
        parser = ResponseParser()
        raw = json.dumps(sample_llm_response_json)
        response = parser.parse(raw, LLMTask.GENERATE_CODE_FROM_JSON)
        assert isinstance(response, LLMResponse)
        assert response.assistant_message == "Done"
    
    def test_parse_missing_message_uses_default(self):
        parser = ResponseParser()
        raw = '{"type": "llm_turn", "task": "test"}'
        response = parser.parse(raw, None)
        assert isinstance(response, LLMResponse)
        # May have empty or default message
        assert response.assistant_message is not None
    
    def test_parse_with_proposal(self):
        parser = ResponseParser()
        data = {
            "type": "llm_turn",
            "assistant_message": "Done",
            "proposals": {
                "test_code": {"mode": "replace", "content": "def test(): pass"}
            }
        }
        raw = json.dumps(data)
        response = parser.parse(raw, LLMTask.GENERATE_CODE_FROM_JSON)
        assert response.test_code is not None
        assert response.test_code.mode == "replace"
    
    def test_parse_no_json_returns_error(self):
        parser = ResponseParser()
        raw = "Just text, no JSON"
        response = parser.parse(raw, None)
        assert not response.success
        assert response.error_message

class TestResponseParserProposals:
    def test_parse_proposal_with_mode(self):
        parser = ResponseParser()
        data = {
            "type": "llm_turn",
            "assistant_message": "Done",
            "proposals": {
                "test_code": {"mode": "replace", "content": "def test(): pass"}
            }
        }
        raw = json.dumps(data)
        response = parser.parse(raw, LLMTask.GENERATE_CODE_FROM_JSON)
        assert response.test_code is not None
    
    def test_parse_proposal_null_mode(self):
        parser = ResponseParser()
        data = {
            "type": "llm_turn",
            "assistant_message": "No changes",
            "proposals": {
                "test_code": {"mode": None, "content": None}
            }
        }
        raw = json.dumps(data)
        response = parser.parse(raw, LLMTask.REVIEW_CODE)
        assert isinstance(response, LLMResponse)
    
    def test_parse_json_proposal(self):
        parser = ResponseParser()
        data = {
            "type": "llm_turn",
            "assistant_message": "Created JSON",
            "proposals": {
                "procedure_json": {"mode": "replace", "content": {"name": "Test", "steps": []}}
            }
        }
        raw = json.dumps(data)
        response = parser.parse(raw, LLMTask.DERIVE_JSON_FROM_CODE)
        assert response.procedure_json is not None

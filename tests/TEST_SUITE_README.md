# LLM Integration Test Suite

## Overview

This test suite validates the LLM integration for the Workflow Editor, including:
- **PromptBuilder**: Constructs prompts for different LLM tasks
- **ResponseParser**: Parses LLM responses into structured data
- **Backend Integration**: Tests with real LLM backends (OpenCode or OpenAI)

## Test Structure

### Unit Tests (Always Run)
- `tests/test_prompt_builder.py` - 8 tests for prompt construction
- `tests/test_response_parser.py` - 12 tests for response parsing

**Status**: ✅ All 20 tests passing

### Integration Tests (Require LLM Backend)
- `tests/test_llm_integration.py` - 6 tests with real LLM

**Status**: ⏸️ Requires OpenCode server or OpenAI API key

## Running Tests

### Run Unit Tests Only
```powershell
pytest tests/test_prompt_builder.py tests/test_response_parser.py -v
```

### Run Integration Tests

#### Option A: Using OpenCode (WSL)
1. Start OpenCode server in WSL:
   ```bash
   opencode serve --port 4096 --hostname 127.0.0.1
   ```

2. Test connection:
   ```powershell
   python test_opencode_connection.py
   ```

3. Run tests:
   ```powershell
   pytest tests/test_llm_integration.py -v -s
   ```

#### Option B: Using OpenAI API
1. Set API key:
   ```powershell
   $env:OPENAI_API_KEY = "sk-proj-your-key"
   ```

2. Run tests:
   ```powershell
   pytest tests/test_llm_integration.py -v -s
   ```

### Run All Tests
```powershell
pytest tests/ -v
```

## Test Coverage

### PromptBuilder Tests
- ✅ All LLM tasks have instructions
- ✅ Output format schema in prompts
- ✅ Strict mode handling
- ✅ Force mode handling  
- ✅ JSON artifact embedding
- ✅ Session context inclusion
- ✅ Rules inclusion
- ✅ Code context inclusion

### ResponseParser Tests  
- ✅ JSON extraction from various formats
- ✅ Parse with expected task
- ✅ Handle malformed JSON
- ✅ Parse thinking sections
- ✅ Parse proposals sections
- ✅ Extract test code proposals
- ✅ Extract review results

### Integration Tests
- ⏸️ Backend availability detection
- ⏸️ Backend startup
- ⏸️ Simple LLM request/response
- ⏸️ End-to-end code generation
- ⏸️ JSON review workflow
- ⏸️ Response time measurement

## Files Created

```
tests/
├── __init__.py
├── conftest.py                  # Fixtures and backend detection
├── test_prompt_builder.py       # Unit tests for PromptBuilder
├── test_response_parser.py      # Unit tests for ResponseParser
└── test_llm_integration.py      # Integration tests with real LLM

test_opencode_connection.py      # Standalone connection tester
OPENCODE_SETUP.md               # OpenCode setup documentation
TEST_SUITE_README.md            # This file
```

## Backend Detection

The `available_backend` fixture in `conftest.py` automatically:

1. Checks for `OPENAI_API_KEY` environment variable
2. Falls back to OpenCode detection:
   - First checks if OpenCode server is already running
   - Then tries to detect opencode command in WSL
3. Skips integration tests if no backend available

## Troubleshooting

### "test_backend_available skipped"
**Cause**: No LLM backend detected  
**Fix**: Either start OpenCode server or set OpenAI API key

### "OpenCode not found in WSL PATH"
**Cause**: WSL PATH issues when calling from PowerShell  
**Fix**: Start OpenCode server manually in WSL terminal

### "Cannot connect to server"
**Cause**: OpenCode server not running  
**Fix**: Check if server is running: `wsl ps aux | grep opencode`

## Next Steps

1. ✅ Unit tests complete and passing
2. ⏸️ **Start OpenCode server** to run integration tests
3. ⏸️ Verify end-to-end workflows
4. ⏸️ Add CI/CD configuration for automated testing

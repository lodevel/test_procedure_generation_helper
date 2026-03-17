"""
Response Parser - Parses LLM responses into structured objects.

Handles the JSON response contract defined in spec Section 13.
Defensive parsing to handle malformed responses gracefully.
"""

import json
import re
from typing import Optional, Any

from .backend_base import (
    LLMResponse, 
    LLMTask, 
    LLMProposal, 
    TextPatch, 
    ValidationIssue
)


class ResponseParser:
    """
    Parses LLM responses following the contract in spec Section 13.
    
    Defensive parsing:
    - Always validate JSON schema
    - Handle missing/malformed fields gracefully
    - Extract what we can, report errors for what we can't
    """
    
    def parse(self, raw_response: str, expected_task: Optional[LLMTask]) -> LLMResponse:
        """
        Parse a raw LLM response string.
        
        Args:
            raw_response: The raw response from the LLM
            expected_task: The task we expected (for validation)
        
        Returns:
            Parsed LLMResponse object
        """
        response = LLMResponse(raw_response=raw_response)
        
        # Extract thinking/reasoning content (if present in OpenCode parts)
        response.thinking_content = self._extract_thinking(raw_response)
        
        # Try to extract JSON from response
        json_content = self._extract_json(raw_response)
        
        if json_content is None:
            # No valid JSON found - treat entire response as assistant message
            response.success = False
            response.error_message = "No valid JSON found in response"
            response.assistant_message = self._extract_text_message(raw_response)
            return response
        
        try:
            data = json.loads(json_content)
        except json.JSONDecodeError as e:
            response.success = False
            response.error_message = f"Invalid JSON: {e}"
            response.assistant_message = self._extract_text_message(raw_response)
            return response
        
        # Parse the response data
        return self._parse_response_data(data, response, expected_task)
    
    def _extract_thinking(self, raw: str) -> str:
        """Extract thinking/reasoning content from OpenCode's parts array.
        
        Looks for parts with type "thinking" or "reasoning" and concatenates
        their text content.
        
        Args:
            raw: Raw response string
            
        Returns:
            Concatenated thinking content, or empty string if none found
        """
        try:
            data = json.loads(raw.strip())
            if not isinstance(data, dict) or "parts" not in data:
                return ""
            
            thinking_parts = []
            for part in data.get("parts", []):
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type not in ("thinking", "reasoning"):
                    continue
                # Try both "content" and "text" field names
                text = part.get("content") or part.get("text") or ""
                if isinstance(text, str) and text.strip():
                    thinking_parts.append(text.strip())
            
            return "\n\n".join(thinking_parts)
        except (json.JSONDecodeError, ValueError, TypeError):
            return ""
    
    def _extract_json(self, raw: str) -> Optional[str]:
        """
        Extract JSON from raw response.
        
        Handles cases where JSON might be wrapped in:
        - OpenCode's response structure (parts array with thinking/text content)
        - Markdown code blocks
        - Direct JSON
        """
        # First, check if this is OpenCode's wrapped format
        try:
            opencode_data = json.loads(raw.strip())
            if isinstance(opencode_data, dict) and "parts" in opencode_data:
                # Extract from OpenCode parts array
                for part in opencode_data.get("parts", []):
                    if not isinstance(part, dict):
                        continue

                    part_type = part.get("type")
                    candidates: list[Any] = []

                    # Legacy and current naming variants across backends
                    if part_type in ("thinking", "reasoning"):
                        candidates.extend([part.get("content"), part.get("text")])
                    elif part_type == "text":
                        candidates.extend([part.get("text"), part.get("content")])

                    for candidate in candidates:
                        extracted = self._extract_json_from_candidate(candidate)
                        if extracted is not None:
                            return extracted

                # If we detected OpenCode format but found no JSON inside, return None
                # (don't fall through to parsing the wrapper itself)
                return None
        except json.JSONDecodeError:
            pass
        
        # Try direct JSON parse
        raw_stripped = raw.strip()
        if raw_stripped.startswith("{"):
            # Find matching closing brace
            brace_count = 0
            for i, char in enumerate(raw_stripped):
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        return raw_stripped[:i+1]
        
        # Try to extract from code block
        patterns = [
            r"```json\s*\n(.*?)\n```",
            r"```\s*\n(.*?)\n```",
            r"\{[\s\S]*\}",  # Any JSON object
        ]
        
        for pattern in patterns:
            match = re.search(pattern, raw, re.DOTALL)
            if match:
                candidate = match.group(1) if match.lastindex else match.group(0)
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    continue
        
        return None

    def _extract_json_from_candidate(self, candidate: Any) -> Optional[str]:
        """Extract a valid JSON object string from a part candidate value."""
        if candidate is None:
            return None

        if isinstance(candidate, dict):
            return json.dumps(candidate)

        if not isinstance(candidate, str):
            return None

        text = candidate.strip()
        if not text:
            return None

        # Direct JSON object
        if text.startswith("{"):
            try:
                json.loads(text)
                return text
            except json.JSONDecodeError:
                pass

        # Markdown fenced JSON / code block
        fenced_patterns = [
            r"```json\s*\n([\s\S]*?)\n```",
            r"```\s*\n([\s\S]*?)\n```",
        ]
        for pattern in fenced_patterns:
            match = re.search(pattern, text, re.DOTALL)
            if not match:
                continue
            candidate_json = match.group(1).strip()
            if not candidate_json.startswith("{"):
                continue
            try:
                json.loads(candidate_json)
                return candidate_json
            except json.JSONDecodeError:
                continue

        return None
    
    def _extract_text_message(self, raw: str) -> str:
        """Extract any text content from response as fallback message."""
        # First try to extract from OpenCode's parts array
        try:
            opencode_data = json.loads(raw.strip())
            if isinstance(opencode_data, dict) and "parts" in opencode_data:
                # Collect all text parts
                text_parts = []
                for part in opencode_data.get("parts", []):
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_content = part.get("text", "")
                        # Handle case where text is already a dict
                        if isinstance(text_content, dict):
                            # Extract assistant_message if available
                            if "assistant_message" in text_content:
                                text_parts.append(str(text_content["assistant_message"]))
                        elif text_content:
                            text_parts.append(text_content)
                
                if text_parts:
                    return "\n\n".join(text_parts)
        except json.JSONDecodeError:
            pass
        
        # Fallback: Remove code blocks and return text
        text = re.sub(r"```[\s\S]*?```", "", raw)
        text = text.strip()
        
        if text:
            return text[:500]  # Limit length
        
        return "Received response but could not parse it."
    
    def _parse_response_data(
        self, 
        data: dict, 
        response: LLMResponse,
        expected_task: Optional[LLMTask]
    ) -> LLMResponse:
        """Parse the JSON data into response object."""
        response.success = True
        
        # Basic fields
        response.assistant_message = data.get("assistant_message", "")
        response.strict_mode = data.get("strict_mode", True)
        
        # Task
        task_str = data.get("task", "")
        if task_str:
            try:
                response.task = LLMTask(task_str)
            except ValueError:
                pass  # Unknown task, ignore
        
        # Validation
        validation = data.get("validation", {})
        if validation:
            response.validation_status = validation.get("status", "")
            response.assumptions = validation.get("assumptions", [])
            
            for issue_data in validation.get("issues", []):
                response.issues.append(ValidationIssue(
                    severity=issue_data.get("severity", "warning"),
                    code=issue_data.get("code", ""),
                    message=issue_data.get("message", ""),
                    location=issue_data.get("location", ""),
                    suggested_fix=issue_data.get("suggested_fix", ""),
                ))
        
        # Proposals
        proposals = data.get("proposals", {})
        if proposals:
            response.procedure_json = self._parse_proposal(
                proposals.get("procedure_json")
            )
            response.test_code = self._parse_proposal(
                proposals.get("test_code")
            )
            response.procedure_text = self._parse_proposal(
                proposals.get("procedure_text")
            )
            
            # Text patches
            for patch_data in proposals.get("text_patches", []) or []:
                response.text_patches.append(TextPatch(
                    line_start=patch_data.get("line_start", 0),
                    line_end=patch_data.get("line_end", 0),
                    original=patch_data.get("original", ""),
                    proposed=patch_data.get("proposed", ""),
                    reason=patch_data.get("reason", ""),
                ))
        
        # Session delta
        response.session_delta = data.get("session_delta", {})
        
        return response
    
    def _parse_proposal(self, proposal_data: Optional[dict]) -> Optional[LLMProposal]:
        """Parse a proposal object."""
        if not proposal_data:
            return None
        
        mode = proposal_data.get("mode")
        content = proposal_data.get("content")
        
        if mode is None or content is None:
            return None
        
        return LLMProposal(mode=mode, content=content)
    
    def validate_proposal(self, proposal: LLMProposal, artifact_type: str) -> tuple[bool, str]:
        """
        Validate a proposal before showing to user.
        
        Returns (is_valid, error_message).
        """
        if proposal is None:
            return False, "No proposal provided"
        
        if proposal.mode not in ("replace", "patch"):
            return False, f"Invalid proposal mode: {proposal.mode}"
        
        if proposal.content is None:
            return False, "Proposal content is null"
        
        if artifact_type == "procedure_json":
            if not isinstance(proposal.content, dict):
                return False, "JSON proposal must be an object"
            if "name" not in proposal.content:
                return False, "JSON proposal missing 'name' field"
            if "steps" not in proposal.content:
                return False, "JSON proposal missing 'steps' field"
        
        elif artifact_type == "test_code":
            if not isinstance(proposal.content, str):
                return False, "Code proposal must be a string"
            if len(proposal.content.strip()) == 0:
                return False, "Code proposal is empty"
        
        elif artifact_type == "procedure_text":
            if not isinstance(proposal.content, str):
                return False, "Text proposal must be a string"
            if len(proposal.content.strip()) == 0:
                return False, "Text proposal is empty"
        
        return True, ""

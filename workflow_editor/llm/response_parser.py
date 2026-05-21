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


# Lines the LLM rewriter is forbidden from modifying. If the LLM-proposed
# procedure_text changes any of these, restore the original value before the
# diff dialog so the operator never reviews a change to a human-only field.
_HUMAN_ONLY_META_KEYS = ("requirement",)
_H1_RE = re.compile(r"^# .+$", re.MULTILINE)
# Match the body of the `## Meta` section: everything between `## Meta\n` and
# the next `## ` header (or end-of-file). Capture group 1 is the body.
_META_BLOCK_RE = re.compile(
    r"^## Meta\s*\n((?:.|\n)*?)(?=^## |\Z)", re.MULTILINE
)
_PACK_ANCHOR_RE = re.compile(
    r"^(?:fncore_pack|labscpi_pack):.*$", re.MULTILINE
)
# First `## ` section heading at line start. The header (test id + optional
# multi-line description) is everything before this match.
_FIRST_SECTION_RE = re.compile(r"^## ", re.MULTILINE)


def _find_first_section_offset(text: str) -> Optional[int]:
    """Return the byte offset of the first `## ` section heading, or None."""
    m = _FIRST_SECTION_RE.search(text)
    return m.start() if m else None


def _extract_header_block(text: str) -> Optional[str]:
    """Extract the operator-only header from `text`.

    Returns the H1 line plus any description lines that follow, with
    trailing blank lines stripped. Returns None if the text has no H1.
    """
    h1 = _H1_RE.search(text)
    if not h1:
        return None
    sec = _find_first_section_offset(text)
    if sec is None:
        block = text[h1.start():]
    else:
        block = text[h1.start():sec]
    # Strip trailing blank lines (they belong to the section separator).
    return block.rstrip("\n").rstrip()


def preserve_human_only_fields(original: str, proposed: str) -> str:
    """Splice human-only fields from `original` into `proposed`.

    Currently preserves:
      - Line 1 test id (`# <TEST_ID>`).
      - The description block (everything between line 1 and the first `## `
        section heading; trailing blank lines are part of the section
        separator, not the description).
      - Optional Meta keys listed in `_HUMAN_ONLY_META_KEYS` (e.g. `requirement:`).
        Restoration is scoped to the `## Meta` block — a `requirement:` token
        appearing as part of free text inside `## Steps` is left alone.

    If the LLM dropped a key the original had, it is reinserted. If the LLM
    added a key the original did not have, it is removed. If the LLM modified
    the value, the original value is restored. Other content passes through
    unchanged.
    """
    out = proposed

    # 1. Test ID + description — both are operator-only. Restore the entire
    # header block (line 1 H1 + zero or more description lines) from
    # `original`, replacing whatever the LLM put before the first `## `.
    orig_header = _extract_header_block(original)
    if orig_header is not None:
        prop_header_end = _find_first_section_offset(out)
        if prop_header_end is None:
            # Proposed has no `## ` heading — prepend the original header.
            out = orig_header + "\n\n" + out
        else:
            # Replace everything before the first `## ` with the original
            # header, plus the canonical single-blank-line separator.
            out = orig_header + "\n\n" + out[prop_header_end:]

    # 2. Human-only Meta keys — operate only inside the proposed Meta block.
    prop_meta = _META_BLOCK_RE.search(out)
    if prop_meta is None:
        # No Meta block in proposed — leave the rest alone; the validator
        # will surface the missing block.
        return out
    meta_start, meta_end = prop_meta.start(1), prop_meta.end(1)
    meta_body = out[meta_start:meta_end]

    orig_meta = _META_BLOCK_RE.search(original)
    orig_meta_body = orig_meta.group(1) if orig_meta else ""

    for key in _HUMAN_ONLY_META_KEYS:
        key_re = re.compile(rf"^{re.escape(key)}:.*$\n?", re.MULTILINE)
        # Strip every occurrence the LLM emitted (handles malformed duplicates).
        meta_body = key_re.sub("", meta_body)
        # If the original had this key, restore it in canonical position:
        # after the LAST of fncore_pack / labscpi_pack (so when both packs
        # are present, requirement: lands after fncore_pack, not between).
        orig_value_re = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
        orig_value = orig_value_re.search(orig_meta_body)
        if orig_value:
            anchors = list(_PACK_ANCHOR_RE.finditer(meta_body))
            if anchors:
                pos = anchors[-1].end()
                meta_body = (
                    meta_body[:pos] + "\n" + orig_value.group(0) + meta_body[pos:]
                )
            # else: malformed Meta in proposed; let the validator surface it.

    out = out[:meta_start] + meta_body + out[meta_end:]
    return out

"""
Step Marker Parser - Extracts step blocks from Python test code.

Parses `# Step N` markers to create a mapping between procedure steps
and code blocks for traceability.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class StepBlock:
    """A code block associated with a step marker."""
    step_number: int
    start_line: int  # 1-based, inclusive
    end_line: int    # 1-based, inclusive
    block_text: str
    marker_text: str  # The full marker line text
    
    @property
    def line_count(self) -> int:
        """Number of lines in this block."""
        return self.end_line - self.start_line + 1


class StepMarkerParser:
    """
    Parses step markers from Python test code.
    
    Regex pattern: ^\s*#\s*Step\s+(\d+)\b (case-insensitive)
    
    Algorithm:
    1. Read code lines
    2. Identify all markers with line numbers
    3. For each marker, block extends to next marker or EOF
    """
    
    # Pattern for step markers: # Step N
    STEP_PATTERN = re.compile(
        r"^\s*#\s*Step\s+(\d+)\b",
        re.IGNORECASE
    )
    
    def parse(self, code: str) -> list[StepBlock]:
        """
        Parse step markers from code.
        
        Returns list of StepBlock objects, sorted by step number.
        """
        lines = code.splitlines()
        markers: list[tuple[int, int, str]] = []  # (line_num, step_num, marker_text)
        
        # Find all markers
        for i, line in enumerate(lines):
            match = self.STEP_PATTERN.match(line)
            if match:
                step_num = int(match.group(1))
                markers.append((i + 1, step_num, line))  # 1-based line numbers
        
        if not markers:
            return []
        
        # Build step blocks
        blocks: list[StepBlock] = []
        total_lines = len(lines)
        
        for i, (start_line, step_num, marker_text) in enumerate(markers):
            # End line is line before next marker, or last line of file
            if i + 1 < len(markers):
                end_line = markers[i + 1][0] - 1
            else:
                end_line = total_lines
            
            # Extract block text (from marker line to end line)
            block_lines = lines[start_line - 1:end_line]
            block_text = "\n".join(block_lines)
            
            blocks.append(StepBlock(
                step_number=step_num,
                start_line=start_line,
                end_line=end_line,
                block_text=block_text,
                marker_text=marker_text
            ))
        
        # Sort by step number
        blocks.sort(key=lambda b: b.step_number)
        
        return blocks
    
    def get_step_numbers(self, code: str) -> list[int]:
        """Get list of step numbers found in code."""
        blocks = self.parse(code)
        return [b.step_number for b in blocks]
    
    def get_block_for_step(self, code: str, step_number: int) -> Optional[StepBlock]:
        """Get the code block for a specific step number."""
        blocks = self.parse(code)
        for block in blocks:
            if block.step_number == step_number:
                return block
        return None
    
    def find_missing_steps(
        self, 
        code: str, 
        expected_steps: list[int]
    ) -> list[int]:
        """
        Find step numbers that are expected but missing in code.
        
        Args:
            code: The Python code to check
            expected_steps: List of expected step numbers (e.g., [1, 2, 3, 4])
        
        Returns:
            List of missing step numbers
        """
        found_steps = set(self.get_step_numbers(code))
        expected_set = set(expected_steps)
        return sorted(expected_set - found_steps)
    
    def find_extra_steps(
        self, 
        code: str, 
        expected_steps: list[int]
    ) -> list[int]:
        """
        Find step numbers in code that are not expected.
        
        Args:
            code: The Python code to check
            expected_steps: List of expected step numbers
        
        Returns:
            List of extra step numbers
        """
        found_steps = set(self.get_step_numbers(code))
        expected_set = set(expected_steps)
        return sorted(found_steps - expected_set)
    
    def get_code_before_first_step(self, code: str) -> str:
        """Get code that appears before the first step marker (setup code)."""
        lines = code.splitlines()
        
        for i, line in enumerate(lines):
            if self.STEP_PATTERN.match(line):
                return "\n".join(lines[:i])
        
        # No steps found, return all code
        return code
    
    def get_code_after_last_step(self, code: str) -> str:
        """Get code that appears after the last step block (teardown code)."""
        blocks = self.parse(code)
        if not blocks:
            return ""
        
        last_block = max(blocks, key=lambda b: b.end_line)
        lines = code.splitlines()
        
        if last_block.end_line < len(lines):
            return "\n".join(lines[last_block.end_line:])
        
        return ""
    
    def create_mapping_summary(self, code: str) -> str:
        """
        Create a human-readable summary of step mappings.
        
        Useful for traceability view and debugging.
        """
        blocks = self.parse(code)
        
        if not blocks:
            return "No step markers found in code."
        
        lines = ["Step Mapping Summary:", "=" * 40]
        
        for block in blocks:
            lines.append(
                f"Step {block.step_number}: "
                f"lines {block.start_line}-{block.end_line} "
                f"({block.line_count} lines)"
            )
        
        return "\n".join(lines)

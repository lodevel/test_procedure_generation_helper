"""
Local Validators - JSON and Python code validation.

These run locally without LLM, providing quick syntax checks.
"""

import json
import py_compile
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Any
from enum import Enum


class ValidationSeverity(Enum):
    """Severity level for validation issues."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    """A single validation issue."""
    severity: ValidationSeverity
    message: str
    location: str = ""
    code: str = ""
    
    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary."""
        return {
            "severity": self.severity.value,
            "message": self.message,
            "location": self.location,
            "code": self.code,
        }


@dataclass
class ValidationResult:
    """Result of a validation operation."""
    is_valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    
    @property
    def has_errors(self) -> bool:
        """Check if there are any errors."""
        return any(i.severity == ValidationSeverity.ERROR for i in self.issues)
    
    @property
    def has_warnings(self) -> bool:
        """Check if there are any warnings."""
        return any(i.severity == ValidationSeverity.WARNING for i in self.issues)
    
    def add_error(self, message: str, location: str = "", code: str = "") -> None:
        """Add an error issue."""
        self.issues.append(ValidationIssue(
            severity=ValidationSeverity.ERROR,
            message=message,
            location=location,
            code=code
        ))
        self.is_valid = False
    
    def add_warning(self, message: str, location: str = "", code: str = "") -> None:
        """Add a warning issue."""
        self.issues.append(ValidationIssue(
            severity=ValidationSeverity.WARNING,
            message=message,
            location=location,
            code=code
        ))


class JsonValidator:
    """
    Validates procedure.json content.
    
    Checks:
    - Valid JSON syntax
    - Top-level object
    - Recommended keys present
    - Steps and expected are arrays of objects with text keys
    """
    
    RECOMMENDED_KEYS = {"name", "description", "board", "equipment", "steps", "expected"}
    REQUIRED_KEYS = {"name", "steps"}  # Minimal required keys
    
    def validate(self, content: str) -> ValidationResult:
        """Validate JSON content."""
        result = ValidationResult(is_valid=True)
        
        # Check if empty
        if not content.strip():
            result.add_error("JSON content is empty")
            return result
        
        # Try to parse JSON
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            result.add_error(
                f"Invalid JSON syntax: {e.msg}",
                location=f"line {e.lineno}, column {e.colno}",
                code="JSON_PARSE_ERROR"
            )
            return result
        
        # Check top-level is object
        if not isinstance(data, dict):
            result.add_error(
                "JSON must be an object (not array or primitive)",
                code="JSON_NOT_OBJECT"
            )
            return result
        
        # Check required keys
        for key in self.REQUIRED_KEYS:
            if key not in data:
                result.add_error(
                    f"Missing required key: '{key}'",
                    code="MISSING_REQUIRED_KEY"
                )
        
        # Check recommended keys
        for key in self.RECOMMENDED_KEYS - self.REQUIRED_KEYS:
            if key not in data:
                result.add_warning(
                    f"Missing recommended key: '{key}'",
                    code="MISSING_RECOMMENDED_KEY"
                )
        
        # Validate steps array
        if "steps" in data:
            self._validate_steps_array(data["steps"], "steps", result)
        
        # Validate expected array
        if "expected" in data:
            self._validate_steps_array(data["expected"], "expected", result)
        
        # Validate equipment array
        if "equipment" in data and not isinstance(data["equipment"], list):
            result.add_warning(
                "'equipment' should be an array",
                location="equipment",
                code="EQUIPMENT_NOT_ARRAY"
            )
        
        return result
    
    def _validate_steps_array(
        self, 
        steps: Any, 
        field_name: str, 
        result: ValidationResult
    ) -> None:
        """Validate steps or expected array."""
        if not isinstance(steps, list):
            result.add_error(
                f"'{field_name}' must be an array",
                location=field_name,
                code=f"{field_name.upper()}_NOT_ARRAY"
            )
            return
        
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                result.add_warning(
                    f"'{field_name}[{i}]' should be an object",
                    location=f"{field_name}[{i}]",
                    code=f"{field_name.upper()}_ITEM_NOT_OBJECT"
                )
            elif "text" not in step:
                result.add_warning(
                    f"'{field_name}[{i}]' is missing 'text' key",
                    location=f"{field_name}[{i}]",
                    code=f"{field_name.upper()}_MISSING_TEXT"
                )


class CodeValidator:
    """
    Validates test.py content using py_compile.
    
    Checks:
    - Python syntax is valid
    """
    
    def validate(self, content: str) -> ValidationResult:
        """Validate Python code content."""
        result = ValidationResult(is_valid=True)
        
        # Check if empty
        if not content.strip():
            result.add_error("Code content is empty")
            return result
        
        # Write to temp file and compile
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                delete=False,
                encoding="utf-8"
            ) as f:
                f.write(content)
                temp_path = Path(f.name)
            
            try:
                py_compile.compile(str(temp_path), doraise=True)
            except py_compile.PyCompileError as e:
                # Extract line number from error
                error_msg = str(e)
                result.add_error(
                    f"Python syntax error: {error_msg}",
                    code="PY_COMPILE_ERROR"
                )
            finally:
                temp_path.unlink()
        except Exception as e:
            result.add_error(
                f"Failed to validate code: {e}",
                code="VALIDATION_ERROR"
            )
        
        return result
    
    def validate_file(self, file_path: Path) -> ValidationResult:
        """Validate Python code from a file."""
        result = ValidationResult(is_valid=True)
        
        if not file_path.exists():
            result.add_error(f"File not found: {file_path}")
            return result
        
        try:
            py_compile.compile(str(file_path), doraise=True)
        except py_compile.PyCompileError as e:
            result.add_error(
                f"Python syntax error: {e}",
                code="PY_COMPILE_ERROR"
            )
        
        return result

"""
Local Validators - Python code validation.

These run locally without LLM, providing quick syntax checks.
"""

import py_compile
from pathlib import Path
from procedo.core.paths import exists_quiet
from dataclasses import dataclass, field
from typing import Optional
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
        self.issues.append(
            ValidationIssue(
                severity=ValidationSeverity.ERROR, message=message, location=location, code=code
            )
        )
        self.is_valid = False

    def add_warning(self, message: str, location: str = "", code: str = "") -> None:
        """Add a warning issue."""
        self.issues.append(
            ValidationIssue(
                severity=ValidationSeverity.WARNING, message=message, location=location, code=code
            )
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

        # Compile in memory, worded as py_compile words it: py_compile of a temp
        # file also left its .pyc in the temp folder's __pycache__, one per check.
        try:
            compile(
                content.encode("utf-8"), "test.py", "exec"
            )  # bytes: a BOM reads as py_compile reads it
        except Exception as e:
            error_msg = str(py_compile.PyCompileError(type(e), e, "test.py"))
            result.add_error(f"Python syntax error: {error_msg}", code="PY_COMPILE_ERROR")

        return result

    def validate_file(self, file_path: Path) -> ValidationResult:
        """Validate Python code from a file."""
        result = ValidationResult(is_valid=True)

        if not exists_quiet(file_path):
            result.add_error(f"File not found: {file_path}")
            return result

        try:
            py_compile.compile(str(file_path), doraise=True)
        except py_compile.PyCompileError as e:
            result.add_error(f"Python syntax error: {e}", code="PY_COMPILE_ERROR")

        return result

"""
Session State - Manages LLM session state per test folder.

Stored in: tests/<test_name>/.llm_session.json

This is tool-generated and can be cleaned.
"""

import json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Any


@dataclass
class Question:
    """An open or resolved question."""
    id: str
    question: str
    why_needed: str = ""
    answer: Optional[str] = None  # None = open, string = resolved


@dataclass
class Decision:
    """A decision made during the session."""
    id: str
    decision: str
    why: str = ""


@dataclass
class Issue:
    """A validation issue."""
    severity: str  # "error" or "warning"
    code: str
    message: str
    location: str = ""
    suggested_fix: str = ""


@dataclass
class SessionState:
    """
    LLM session state for a single test.
    
    This captures the context that should be passed to the LLM
    to maintain coherence across multiple calls.
    """
    # User intent for this test
    intent: str = ""
    
    # Assumptions made by LLM
    assumptions: list[str] = field(default_factory=list)
    
    # Decisions made during authoring
    decisions: list[Decision] = field(default_factory=list)
    
    # Questions (open and resolved)
    open_questions: list[Question] = field(default_factory=list)
    resolved_questions: list[Question] = field(default_factory=list)
    
    # Last coherence check results
    last_check_results: list[Issue] = field(default_factory=list)
    last_check_timestamp: Optional[str] = None
    
    # Validation issues for the current test (flat list, not per-tab)
    validation_issues: list[dict] = field(default_factory=list)
    
    # Artifact hashes for staleness detection
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    artifact_timestamps: dict[str, str] = field(default_factory=dict)
    
    # File path
    _file_path: Optional[Path] = field(default=None, repr=False)
    
    def set_file_path(self, test_dir: Path) -> None:
        """Set the session file path."""
        self._file_path = test_dir / ".llm_session.json"
    
    def load(self) -> bool:
        """Load session state from disk. Returns True if loaded."""
        if self._file_path is None or not self._file_path.exists():
            return False
        
        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
            self._from_dict(data)
            return True
        except (json.JSONDecodeError, KeyError):
            return False
    
    def save(self) -> None:
        """Save session state to disk."""
        if self._file_path is None:
            raise ValueError("No file path set for session state")
        
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_path.write_text(
            json.dumps(self._to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    
    def _to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "intent": self.intent,
            "assumptions": self.assumptions,
            "decisions": [asdict(d) for d in self.decisions],
            "open_questions": [asdict(q) for q in self.open_questions],
            "resolved_questions": [asdict(q) for q in self.resolved_questions],
            "last_check_results": [asdict(i) for i in self.last_check_results],
            "last_check_timestamp": self.last_check_timestamp,
            "validation_issues": self.validation_issues,
            "artifact_hashes": self.artifact_hashes,
            "artifact_timestamps": self.artifact_timestamps,
        }
    
    def _from_dict(self, data: dict[str, Any]) -> None:
        """Load from dictionary."""
        self.intent = data.get("intent", "")
        self.assumptions = data.get("assumptions", [])
        self.decisions = [
            Decision(**d) for d in data.get("decisions", [])
        ]
        self.open_questions = [
            Question(**q) for q in data.get("open_questions", [])
        ]
        self.resolved_questions = [
            Question(**q) for q in data.get("resolved_questions", [])
        ]
        self.last_check_results = [
            Issue(**i) for i in data.get("last_check_results", [])
        ]
        self.last_check_timestamp = data.get("last_check_timestamp")
        # Support both old per-tab format and new flat format
        vi = data.get("validation_issues", None)
        if vi is None:
            # Migrate: flatten old per-tab dict into a single list
            old = data.get("tab_validation_issues", {})
            vi = [issue for issues in old.values() for issue in issues] if old else []
        self.validation_issues = vi
        self.artifact_hashes = data.get("artifact_hashes", {})
        self.artifact_timestamps = data.get("artifact_timestamps", {})
    
    def apply_delta(self, delta: dict[str, Any]) -> None:
        """Apply a session_delta from an LLM response."""
        if not delta:
            return
        
        # Update intent if provided
        if delta.get("intent"):
            self.intent = delta["intent"]
        
        # Add new open questions
        for q in delta.get("open_questions", []):
            # Validate that q is a dict, not a string
            if not isinstance(q, dict):
                print(f"[WARNING] Skipping malformed open_question (expected dict, got {type(q).__name__}): {q}")
                continue
            question = Question(
                id=q["id"],
                question=q["question"],
                why_needed=q.get("why_needed", "")
            )
            # Avoid duplicates
            if not any(oq.id == question.id for oq in self.open_questions):
                self.open_questions.append(question)
        
        # Resolve questions
        for rq in delta.get("resolved_questions", []):
            # Validate that rq is a dict, not a string
            if not isinstance(rq, dict):
                print(f"[WARNING] Skipping malformed resolved_question (expected dict, got {type(rq).__name__}): {rq}")
                continue
            for i, oq in enumerate(self.open_questions):
                if oq.id == rq["id"]:
                    oq.answer = rq["answer"]
                    self.resolved_questions.append(oq)
                    self.open_questions.pop(i)
                    break
        
        # Add decisions
        for d in delta.get("decisions_added", []):
            # Validate that d is a dict, not a string
            if not isinstance(d, dict):
                print(f"[WARNING] Skipping malformed decision (expected dict, got {type(d).__name__}): {d}")
                continue
            decision = Decision(
                id=d["id"],
                decision=d["decision"],
                why=d.get("why", "")
            )
            if not any(dd.id == decision.id for dd in self.decisions):
                self.decisions.append(decision)
    
    def update_check_results(self, issues: list[dict[str, Any]]) -> None:
        """Update last coherence check results."""
        self.last_check_results = [
            Issue(
                severity=i.get("severity", "warning"),
                code=i.get("code", ""),
                message=i.get("message", ""),
                location=i.get("location", ""),
                suggested_fix=i.get("suggested_fix", "")
            )
            for i in issues
        ]
        self.last_check_timestamp = datetime.now().isoformat()
    
    def get_summary_for_llm(self) -> str:
        """Get a compact summary to include in LLM prompts."""
        lines = []
        
        if self.intent:
            lines.append(f"Intent: {self.intent}")
        
        if self.assumptions:
            lines.append("Assumptions:")
            for a in self.assumptions:
                lines.append(f"  - {a}")
        
        if self.decisions:
            lines.append("Decisions:")
            for d in self.decisions:
                lines.append(f"  - {d.decision}")
        
        if self.open_questions:
            lines.append("Open questions:")
            for q in self.open_questions:
                lines.append(f"  - [{q.id}] {q.question}")
        
        return "\n".join(lines) if lines else ""
    
    def clear(self) -> None:
        """Clear all session state."""
        self.intent = ""
        self.assumptions = []
        self.decisions = []
        self.open_questions = []
        self.resolved_questions = []
        self.last_check_results = []
        self.last_check_timestamp = None
        self.artifact_hashes = {}
        self.artifact_timestamps = {}

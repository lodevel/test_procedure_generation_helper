"""
Artifact Manager - Handles loading, saving, and tracking of test artifacts.

Canonical artifacts:
- procedure.json (source of truth for procedure)
- test.py (source of truth for execution)

Tool-generated:
- procedure_text.md (draft/scratch space)
- .llm_session.json (session state)
"""

import json
import os
import tempfile
import shutil
from pathlib import Path
from enum import Enum, auto
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any


class ArtifactType(Enum):
    """Types of artifacts managed by the editor."""
    PROCEDURE_JSON = auto()
    TEST_CODE = auto()
    PROCEDURE_TEXT = auto()


@dataclass
class Artifact:
    """Represents a single artifact with content and metadata."""
    artifact_type: ArtifactType
    content: str = ""
    original_content: str = ""  # Content when loaded/saved
    last_modified: Optional[datetime] = None
    file_path: Optional[Path] = None
    needs_resync: bool = False  # Needs to be re-sent to LLM to correct its context
    
    @property
    def is_dirty(self) -> bool:
        """Check if artifact has unsaved changes."""
        return self.content != self.original_content
    
    @property
    def exists_on_disk(self) -> bool:
        """Check if artifact file exists."""
        return self.file_path is not None and self.file_path.exists()
    
    def mark_clean(self) -> None:
        """Mark artifact as saved (not dirty)."""
        self.original_content = self.content
    
    def mark_needs_resync(self) -> None:
        """Mark that this artifact needs to be re-sent to LLM."""
        self.needs_resync = True
    
    def mark_synced(self) -> None:
        """Mark that this artifact has been sent to LLM."""
        self.needs_resync = False


@dataclass
class ArtifactManager:
    """
    Manages test artifacts for a single test folder.
    
    Handles loading, saving, dirty tracking, and staleness detection.
    """
    test_dir: Optional[Path] = None
    
    # Artifacts
    procedure_json: Artifact = field(default_factory=lambda: Artifact(ArtifactType.PROCEDURE_JSON))
    test_code: Artifact = field(default_factory=lambda: Artifact(ArtifactType.TEST_CODE))
    procedure_text: Artifact = field(default_factory=lambda: Artifact(ArtifactType.PROCEDURE_TEXT))
    
    # File names
    PROCEDURE_JSON_NAME = "procedure.json"
    TEST_CODE_NAME = "test.py"
    PROCEDURE_TEXT_NAME = "procedure_text.md"
    SESSION_FILE_NAME = ".llm_session.json"
    
    def set_test_dir(self, test_dir: Path) -> None:
        """Set the test directory and update file paths."""
        self.test_dir = test_dir
        self.procedure_json.file_path = test_dir / self.PROCEDURE_JSON_NAME
        self.test_code.file_path = test_dir / self.TEST_CODE_NAME
        self.procedure_text.file_path = test_dir / self.PROCEDURE_TEXT_NAME
    
    def detect_artifacts(self) -> dict[ArtifactType, bool]:
        """Detect which artifacts exist on disk."""
        return {
            ArtifactType.PROCEDURE_JSON: self.procedure_json.exists_on_disk,
            ArtifactType.TEST_CODE: self.test_code.exists_on_disk,
            ArtifactType.PROCEDURE_TEXT: self.procedure_text.exists_on_disk,
        }
    
    def load_all(self) -> None:
        """Load all existing artifacts from disk."""
        if self.test_dir is None:
            return
        
        for artifact in [self.procedure_json, self.test_code, self.procedure_text]:
            if artifact.exists_on_disk:
                self._load_artifact(artifact)
    
    def _load_artifact(self, artifact: Artifact) -> None:
        """Load a single artifact from disk."""
        if artifact.file_path is None:
            return
        
        # Check if file was deleted
        if not artifact.file_path.exists():
            if artifact.content:  # Had content before, now deleted
                artifact.content = ""
                artifact.original_content = ""
                artifact.mark_needs_resync()  # LLM needs to know it was deleted
            return
        
        try:
            content = artifact.file_path.read_text(encoding="utf-8")
            artifact.content = content
            artifact.original_content = content
            artifact.last_modified = datetime.fromtimestamp(
                artifact.file_path.stat().st_mtime
            )
        except Exception as e:
            raise IOError(f"Failed to load {artifact.file_path}: {e}")
    
    def save_artifact(self, artifact_type: ArtifactType) -> None:
        """Save a single artifact to disk with atomic write."""
        artifact = self._get_artifact(artifact_type)
        if artifact.file_path is None:
            raise ValueError(f"No file path set for {artifact_type}")
        
        self._atomic_write(artifact.file_path, artifact.content)
        artifact.mark_clean()
        artifact.mark_needs_resync()  # Mark for resync so LLM sees manual changes
        artifact.last_modified = datetime.now()
    
    def save_all(self) -> list[ArtifactType]:
        """Save all dirty artifacts. Returns list of saved artifact types."""
        saved = []
        for artifact_type in ArtifactType:
            artifact = self._get_artifact(artifact_type)
            if artifact.is_dirty:
                self.save_artifact(artifact_type)
                saved.append(artifact_type)
        return saved
    
    def _atomic_write(self, file_path: Path, content: str) -> None:
        """Write content to file atomically (write temp, then rename)."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write to temp file in same directory
        temp_fd, temp_path = tempfile.mkstemp(
            dir=file_path.parent,
            prefix=f".{file_path.name}.",
            suffix=".tmp"
        )
        os.close(temp_fd)  # Close the file descriptor to avoid Windows file lock
        temp_path = Path(temp_path)
        
        try:
            temp_path.write_text(content, encoding="utf-8")
            # Replace original file
            shutil.move(str(temp_path), str(file_path))
        except Exception:
            # Clean up temp file on failure
            if temp_path.exists():
                temp_path.unlink()
            raise
    
    def _get_artifact(self, artifact_type: ArtifactType) -> Artifact:
        """Get artifact by type."""
        mapping = {
            ArtifactType.PROCEDURE_JSON: self.procedure_json,
            ArtifactType.TEST_CODE: self.test_code,
            ArtifactType.PROCEDURE_TEXT: self.procedure_text,
        }
        return mapping[artifact_type]
    
    def get_content(self, artifact_type: ArtifactType) -> str:
        """Get content of an artifact."""
        return self._get_artifact(artifact_type).content
    
    def set_content(self, artifact_type: ArtifactType, content: str) -> None:
        """Set content of an artifact."""
        self._get_artifact(artifact_type).content = content
    
    def is_dirty(self, artifact_type: ArtifactType) -> bool:
        """Check if an artifact has unsaved changes."""
        return self._get_artifact(artifact_type).is_dirty
    
    def has_any_dirty(self) -> bool:
        """Check if any artifact has unsaved changes."""
        return any(self._get_artifact(t).is_dirty for t in ArtifactType)
    
    def mark_needs_resync(self, artifact_type: ArtifactType) -> None:
        """Mark an artifact as needing resync with LLM."""
        self._get_artifact(artifact_type).mark_needs_resync()
    
    def mark_synced(self, artifact_type: ArtifactType) -> None:
        """Mark an artifact as synced with LLM."""
        self._get_artifact(artifact_type).mark_synced()
    
    def should_include_in_prompt(self, artifact_type: ArtifactType) -> bool:
        """Check if artifact should be included in LLM prompt (dirty or needs resync)."""
        artifact = self._get_artifact(artifact_type)
        return artifact.is_dirty or artifact.needs_resync
    
    def get_json_parsed(self) -> Optional[dict[str, Any]]:
        """Get procedure.json content as parsed dict, or None if invalid."""
        try:
            return json.loads(self.procedure_json.content)
        except json.JSONDecodeError:
            return None
    
    def set_json_from_dict(self, data: dict[str, Any]) -> None:
        """Set procedure.json content from a dict."""
        self.procedure_json.content = json.dumps(data, indent=2, ensure_ascii=False)
    
    def get_staleness_info(self) -> dict[str, Optional[datetime]]:
        """Get last modified timestamps for staleness detection."""
        return {
            "procedure_json": self.procedure_json.last_modified,
            "test_code": self.test_code.last_modified,
            "procedure_text": self.procedure_text.last_modified,
        }
    
    def get_cleanable_files(self) -> list[Path]:
        """Get list of tool-generated files that can be cleaned."""
        if self.test_dir is None:
            return []
        
        cleanable = []
        
        # Known tool-generated files
        tool_files = [
            self.PROCEDURE_TEXT_NAME,
            self.SESSION_FILE_NAME,
        ]
        
        for name in tool_files:
            path = self.test_dir / name
            if path.exists():
                cleanable.append(path)
        
        # Also check for other patterns
        patterns = ["*.mapping.md", "*.draft.*", ".llm_cache"]
        for pattern in patterns:
            cleanable.extend(self.test_dir.glob(pattern))
        
        return cleanable
    
    def get_all_non_canonical_files(self) -> list[Path]:
        """Get all files except procedure.json and test.py for Clean All."""
        if self.test_dir is None:
            return []
        
        canonical = {self.PROCEDURE_JSON_NAME, self.TEST_CODE_NAME}
        non_canonical = []
        
        for item in self.test_dir.iterdir():
            if item.name not in canonical:
                non_canonical.append(item)
        
        return non_canonical

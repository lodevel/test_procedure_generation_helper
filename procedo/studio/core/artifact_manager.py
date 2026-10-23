"""
Artifact Manager - Handles loading, saving, and tracking of test artifacts.

Canonical artifacts:
- procedure.json (source of truth for procedure)
- test.py (source of truth for execution)

Tool-generated:
- procedure_text.md (draft/scratch space)
- .llm_session.json (session state)
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional

from .fileio import atomic_write_text
from .session_state import read_sync_baseline
from .sync_utils import normalize_for_hash
from .test_id import force_json_id, force_text_id_line, sanitize_test_id


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
    _exclusion_patterns: list[re.Pattern[str]] = field(default_factory=list, repr=False)
    #: Called on the GUI thread once a save's refreshed media were written, with the JSON as it
    #: was saved: True when the open test's controller holds this test's sync baseline (and took
    #: the refresh into it); otherwise the session file is re-baselined here.
    on_media_refreshed: Optional[Callable[[str], bool]] = field(default=None, repr=False)
    #: The window's loading cue (a ``LoadingCue``) while a save's media refresh runs; None: none.
    media_cue: Any = field(default=None, repr=False)
    
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

        # Enforce test id == sanitized folder name (folder is the single
        # source of truth). Rewrite the '# <id>' header line of the text
        # and the top-level "id" of the JSON so a hand-authored id can
        # never drift from the folder. Mirror the correction back into
        # artifact.content so the editor matches disk.
        artifact.content = self._enforce_folder_id(artifact_type, artifact.content)

        atomic_write_text(artifact.file_path, artifact.content)
        from . import test_sync                  # imports this module: not at module level
        test_sync.remember_written(self.test_dir, artifact.file_path.name, artifact.content,
                                   self._exclusion_patterns)
        artifact.mark_clean()
        artifact.mark_needs_resync()  # Mark for resync so LLM sees manual changes
        artifact.last_modified = datetime.now()
        if artifact_type is ArtifactType.PROCEDURE_JSON:
            self._refresh_media(artifact)
    
    def _enforce_folder_id(self, artifact_type: ArtifactType, content: str) -> str:
        """Force the procedure id to the sanitized folder name.

        Delegates to module-level :func:`enforce_folder_id` — the single
        enforcement rule, shared with the workspace tab's disk hasher.
        """
        names = {
            ArtifactType.PROCEDURE_JSON: self.PROCEDURE_JSON_NAME,
            ArtifactType.TEST_CODE: self.TEST_CODE_NAME,
            ArtifactType.PROCEDURE_TEXT: self.PROCEDURE_TEXT_NAME,
        }
        return enforce_folder_id(self.test_dir, names[artifact_type], content)

    def _refresh_media(self, artifact: Artifact) -> None:
        """Derived data follows its source: a saved procedure.json that did not
        come from the parser (a hand edit, an accepted LLM proposal) gets its
        step media recomputed by the pack's one catalogue, off the GUI thread,
        and written again; on parser output this changes nothing. The save
        itself never waits for it. The refresh is dropped when the content
        changed since (typed meanwhile, it stays unsaved), and a pack that
        cannot run is logged (a save never fails for a picture). Content that
        is not a JSON object is left as written."""
        saved = artifact.content
        try:
            doc = json.loads(saved)
        except ValueError:
            return
        if not isinstance(doc, dict):
            return
        from ..pack_ops import engine_async, engine_session, pack_parsers
        root = self.test_dir.parent.parent if self.test_dir is not None else None
        if root is None or not (root / "tests").is_dir():
            root = None                      # no project layout: the editor's own pack

        def done(answer) -> None:
            try:
                refreshed = json.dumps(answer(), indent=2, ensure_ascii=False)
            except engine_session.ParserUnavailable as exc:
                logging.getLogger(__name__).warning("media not refreshed on save: %s", exc)
                return
            if refreshed == saved or artifact.content != saved or artifact.original_content != saved:
                return
            from . import test_sync                  # imports this module: not at module level
            patterns = self._exclusion_patterns
            acknowledged = (read_sync_baseline(self.test_dir) is not None
                            and not test_sync.is_out_of_sync(self.test_dir, patterns))
            atomic_write_text(artifact.file_path, refreshed)
            test_sync.remember_written(self.test_dir, artifact.file_path.name, refreshed, patterns)
            artifact.content = artifact.original_content = refreshed
            held = self.on_media_refreshed is not None and self.on_media_refreshed(saved)
            if acknowledged and not held:            # derived data must not un-sync a coherent test
                test_sync.mark_in_sync(self.test_dir, patterns)

        engine_async.call(root, lambda: pack_parsers.refresh_media(doc, root), done,
                          cue=self.media_cue, cue_label="Refreshing media\u2026")

    def save_all(self) -> list[ArtifactType]:
        """Save all dirty artifacts. Returns list of saved artifact types."""
        saved = []
        for artifact_type in ArtifactType:
            artifact = self._get_artifact(artifact_type)
            if artifact.is_dirty:
                self.save_artifact(artifact_type)
                saved.append(artifact_type)
        return saved
    
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

    def set_exclusion_patterns(self, patterns: list[re.Pattern[str]]) -> None:
        """Set equipment-configuration patterns to exclude from sync hashing."""
        self._exclusion_patterns = patterns

    # ---- Content hashing for sync detection ----

    @staticmethod
    def _hash_content(content: str) -> str:
        """Compute a SHA-256 hex digest of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Files tracked for sync detection. procedure_text.md is included
    # because operator edits to the canonical text imply the JSON /
    # code derived from it may now be stale — the user expects the
    # workspace ⚠️ flag in that case too.
    _SYNC_TRACKED_NAMES: tuple[str, ...] = (
        "procedure.json", "test.py", "procedure_text.md",
    )

    def _sync_tracked_mapping(self) -> dict[str, "Artifact"]:
        return {
            self.PROCEDURE_JSON_NAME: self.procedure_json,
            self.TEST_CODE_NAME: self.test_code,
            self.PROCEDURE_TEXT_NAME: self.procedure_text,
        }

    def compute_hashes(self) -> dict[str, str]:
        """Compute hashes for all loaded canonical artifacts.

        Returns a dict keyed by artifact file name (e.g. 'procedure.json').
        Only includes artifacts that have non-empty content.

        Hashes come from :func:`enforced_sync_hash` (folder-id enforcement
        first) so they reflect what ``save_artifact`` will actually write:
        a folder-id correction at save time is not a real divergence and
        must never flip a user-acknowledged in-sync state. Idempotent
        for already-correct ids, no-op for test.py.
        """
        return {name: self.sync_hash(name, artifact.content)
                for name, artifact in self._sync_tracked_mapping().items() if artifact.content}

    def sync_hash(self, name: str, content: str) -> str:
        """The sync hash of ``content`` as this test's tracked file ``name``."""
        return enforced_sync_hash(self.test_dir, name, content, self._exclusion_patterns)

    def check_external_changes(self, stored_hashes: dict[str, str]) -> list[str]:
        """Compare current on-disk content against stored hashes.

        Returns a list of file names whose disk content differs from the
        stored hash (i.e. were edited externally since last save/load).
        """
        changed: list[str] = []
        for name, artifact in self._sync_tracked_mapping().items():
            if not artifact.exists_on_disk:
                continue
            try:
                disk_content = artifact.file_path.read_text(encoding="utf-8")
            except Exception:
                continue
            # Same folder-id normalization as compute_hashes: an id that
            # merely differs from the folder is rewritten on the next
            # save and is not an external divergence.
            disk_hash = enforced_sync_hash(
                self.test_dir, name, disk_content, self._exclusion_patterns
            )
            stored = stored_hashes.get(name)
            if stored and disk_hash != stored:
                changed.append(name)
        return changed


def enforce_folder_id(test_dir: Optional[Path], filename: str, content: str) -> str:
    """Force the procedure id in *content* to the sanitized folder name.

    Single enforcement rule, keyed by canonical file name: the ``# <id>``
    header of procedure_text.md, the top-level ``"id"`` of procedure.json;
    no-op for other files or without a *test_dir*. Never raises: invalid
    JSON is left untouched.
    """
    if test_dir is None:
        return content
    folder_id = sanitize_test_id(test_dir.name)
    if filename == ArtifactManager.PROCEDURE_TEXT_NAME:
        return force_text_id_line(content, folder_id)
    if filename == ArtifactManager.PROCEDURE_JSON_NAME:
        return force_json_id(content, folder_id)
    return content


def enforced_sync_hash(
    test_dir: Optional[Path],
    filename: str,
    content: str,
    exclusion_patterns: list[re.Pattern[str]],
) -> str:
    """THE sync-hash pipeline: enforce folder id, normalize, SHA-256.

    Every sync baseline / comparison hash must come from here — both
    ArtifactManager (loaded artifacts + disk checks) and the workspace
    Tests list (raw disk files of unloaded tests) — so a wrong on-disk
    id hashes identically on every path.
    """
    content = enforce_folder_id(test_dir, filename, content)
    normalised = normalize_for_hash(content, filename, exclusion_patterns)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()

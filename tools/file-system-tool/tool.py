"""
FileSystemTool — Read-only file system access with path whitelisting.

Provides safe, audited read access to a curated set of directories.
No write operations are permitted; any attempt raises NotImplementedError.

Whitelisted paths:
  /var/data/migration/
  /tmp/migration-work/
  ./docs/
  ./monitoring/
  ./architecture/

Usage:
    tool = FileSystemTool()
    content = tool.read_file("/var/data/migration/tenant-abc/config.json")
    entries = tool.list_directory("./docs/")
    matches = tool.search_files("*.yaml", "./architecture/")
"""

from __future__ import annotations

import glob as _glob
import logging
import os
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Whitelisted base paths
# ---------------------------------------------------------------------------

# Paths are normalised to absolute form at module load time so that
# relative-path comparisons remain stable regardless of CWD changes.
_REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/Users/oscarvalois/Documents/Github/s-agent")).resolve()

WHITELISTED_PATHS: Final[list[str]] = [
    "/var/data/migration/",
    "/tmp/migration-work/",
    str(_REPO_ROOT / "docs") + "/",
    str(_REPO_ROOT / "monitoring") + "/",
    str(_REPO_ROOT / "architecture") + "/",
]

# Maximum size of a single file read (4 MB)
_MAX_FILE_BYTES: Final[int] = 4 * 1024 * 1024

# Maximum entries returned by list_directory
_MAX_LIST_ENTRIES: Final[int] = 500

# Maximum search results returned by search_files
_MAX_SEARCH_RESULTS: Final[int] = 200


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_and_check(path: str) -> Path:
    """
    Resolve *path* to an absolute path and verify it falls within a whitelisted
    prefix. Raises PermissionError if not whitelisted.

    Path traversal attempts (``../``) are defeated by resolving through
    ``Path.resolve()``, which follows symlinks and eliminates `..` components.
    """
    resolved = Path(path).resolve()
    resolved_str = str(resolved)

    for allowed in WHITELISTED_PATHS:
        # Normalise allowed prefix (remove trailing slash for comparison)
        allowed_norm = allowed.rstrip("/")
        if resolved_str == allowed_norm or resolved_str.startswith(allowed_norm + "/") or resolved_str.startswith(allowed_norm + os.sep):
            return resolved

    logger.warning("Access denied to path outside whitelist: %s (resolved: %s)", path, resolved_str)
    raise PermissionError(
        f"Path '{path}' is outside all whitelisted directories. "
        f"Whitelisted: {WHITELISTED_PATHS}"
    )


def _check_write_attempted(operation: str) -> None:
    """
    Always raises NotImplementedError to prevent any write operation.
    This is a safety backstop — write operations must never be added to this tool.
    """
    raise NotImplementedError(
        f"FileSystemTool is READ-ONLY. Write operation '{operation}' is not permitted."
    )


# ---------------------------------------------------------------------------
# FileSystemTool
# ---------------------------------------------------------------------------


class FileSystemTool:
    """
    Read-only file system tool with path whitelisting.

    All methods perform:
    1. Path resolution and whitelist check (PermissionError on violation).
    2. The requested read operation.
    3. Structured return value.

    No write methods exist. If write operations are attempted on the class
    directly, NotImplementedError is raised.
    """

    # ------------------------------------------------------------------
    # Permitted read operations
    # ------------------------------------------------------------------

    def read_file(self, path: str) -> str:
        """
        Read and return the content of a whitelisted file.

        Args:
            path: Absolute or relative path to the file.

        Returns:
            File content as a UTF-8 string.

        Raises:
            PermissionError: If *path* is not within a whitelisted directory.
            FileNotFoundError: If the file does not exist.
            IsADirectoryError: If *path* refers to a directory.
            OSError: On other I/O errors.
            ValueError: If the file exceeds the maximum allowed size.
        """
        resolved = _resolve_and_check(path)

        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {path} (resolved: {resolved})")

        if resolved.is_dir():
            raise IsADirectoryError(f"Path is a directory, not a file: {path}")

        file_size = resolved.stat().st_size
        if file_size > _MAX_FILE_BYTES:
            raise ValueError(
                f"File '{path}' is {file_size:,} bytes, which exceeds the "
                f"maximum allowed size of {_MAX_FILE_BYTES:,} bytes."
            )

        content = resolved.read_text(encoding="utf-8")
        logger.debug("read_file: %s (%d bytes)", resolved, len(content.encode("utf-8")))
        return content

    def list_directory(self, path: str) -> list[str]:
        """
        List the immediate contents of a whitelisted directory.

        Returns paths relative to *path*, sorted alphabetically.
        Directories are suffixed with ``/`` to distinguish them from files.

        Args:
            path: Absolute or relative path to the directory.

        Returns:
            List of entry names (files and directories) within *path*.

        Raises:
            PermissionError: If *path* is not within a whitelisted directory.
            NotADirectoryError: If *path* refers to a file.
            FileNotFoundError: If *path* does not exist.
        """
        resolved = _resolve_and_check(path)

        if not resolved.exists():
            raise FileNotFoundError(f"Directory not found: {path} (resolved: {resolved})")

        if not resolved.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {path}")

        entries: list[str] = []
        for entry in sorted(resolved.iterdir()):
            # Only include entries that remain within the whitelist
            try:
                _resolve_and_check(str(entry))
            except PermissionError:
                # The entry itself (e.g. a symlink) points outside the whitelist
                continue

            name = entry.name + ("/" if entry.is_dir() else "")
            entries.append(name)

            if len(entries) >= _MAX_LIST_ENTRIES:
                logger.warning(
                    "list_directory: result truncated at %d entries for %s",
                    _MAX_LIST_ENTRIES,
                    resolved,
                )
                break

        logger.debug("list_directory: %s → %d entries", resolved, len(entries))
        return entries

    def search_files(self, pattern: str, directory: str) -> list[str]:
        """
        Search for files matching *pattern* within a whitelisted *directory*.

        Uses ``glob`` syntax (e.g. ``*.yaml``, ``**/*.md``). The search is
        confined to *directory* and will not escape the whitelist.

        Args:
            pattern: Glob pattern to match against file names (relative to *directory*).
            directory: Root directory for the search. Must be whitelisted.

        Returns:
            List of absolute file paths matching the pattern, up to
            ``_MAX_SEARCH_RESULTS`` entries, sorted lexicographically.

        Raises:
            PermissionError: If *directory* is not within a whitelisted directory.
            NotADirectoryError: If *directory* refers to a file.
            FileNotFoundError: If *directory* does not exist.
            ValueError: If *pattern* is empty or contains absolute path components.
        """
        if not pattern:
            raise ValueError("pattern must be a non-empty string")

        # Reject patterns that look like absolute paths or contain traversal
        if pattern.startswith("/") or "\\" in pattern:
            raise ValueError(
                f"pattern must be a relative glob pattern, got: {pattern!r}"
            )

        resolved_dir = _resolve_and_check(directory)

        if not resolved_dir.exists():
            raise FileNotFoundError(f"Directory not found: {directory} (resolved: {resolved_dir})")

        if not resolved_dir.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {directory}")

        # Perform the glob search
        full_pattern = str(resolved_dir / pattern)
        raw_matches = sorted(_glob.glob(full_pattern, recursive=True))

        results: list[str] = []
        for match_str in raw_matches:
            match_path = Path(match_str)
            if not match_path.is_file():
                # Skip directories matched by ** patterns
                continue
            try:
                _resolve_and_check(match_str)
            except PermissionError:
                # Match escaped the whitelist via symlink — skip
                continue
            results.append(str(match_path))
            if len(results) >= _MAX_SEARCH_RESULTS:
                logger.warning(
                    "search_files: results truncated at %d for pattern %r in %s",
                    _MAX_SEARCH_RESULTS,
                    pattern,
                    resolved_dir,
                )
                break

        logger.debug(
            "search_files: pattern=%r directory=%s → %d results",
            pattern,
            resolved_dir,
            len(results),
        )
        return results

    # ------------------------------------------------------------------
    # Blocked write operations — safety backstop
    # ------------------------------------------------------------------

    def write_file(self, *args: object, **kwargs: object) -> None:
        """Write operations are not permitted. Always raises NotImplementedError."""
        _check_write_attempted("write_file")

    def delete_file(self, *args: object, **kwargs: object) -> None:
        """Delete operations are not permitted. Always raises NotImplementedError."""
        _check_write_attempted("delete_file")

    def create_directory(self, *args: object, **kwargs: object) -> None:
        """Directory creation is not permitted. Always raises NotImplementedError."""
        _check_write_attempted("create_directory")

    def move_file(self, *args: object, **kwargs: object) -> None:
        """Move operations are not permitted. Always raises NotImplementedError."""
        _check_write_attempted("move_file")

    def copy_file(self, *args: object, **kwargs: object) -> None:
        """Copy operations are not permitted. Always raises NotImplementedError."""
        _check_write_attempted("copy_file")

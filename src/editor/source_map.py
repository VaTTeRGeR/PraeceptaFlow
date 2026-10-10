import os
import re
from pathlib import Path

from editor.export import MIN_HASH_LENGTH

# A tag comment in any comment syntax: only punctuation (or `rem`) before the tag, `end` after it for the end marker.
_MARKER = re.compile(rf"^\s*(?:rem\b|\W{{1,4}})\s*\[([0-9a-f]{{{MIN_HASH_LENGTH},}})\]\s*(end\b)?", re.IGNORECASE)
# The description and the graph hold tags too, but they are not source code.
_SKIPPED_SUFFIXES = {".pd", ".pfg", ".notes"}
_SKIPPED_DIRS = {"__pycache__", "node_modules", "venv"}
_MAX_BYTES = 1_000_000


def tag_sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """(tag, first line, last line) of every tagged section, lines 0-based; markers are part of the section."""
    open_at: dict[str, int] = {}
    found = []
    for number, line in enumerate(lines):
        marker = _MARKER.match(line)
        if not marker:
            continue
        tag = marker[1].lower()
        if marker[2]:
            if tag in open_at:
                found.append((tag, open_at.pop(tag), number))
            continue
        if tag in open_at:  # no end comment before the next start: the code runs up to it
            found.append((tag, open_at.pop(tag), number - 1))
        open_at[tag] = number
    found += [(tag, start, len(lines) - 1) for tag, start in open_at.items()]
    return found


def source_files(folder: Path):
    """Yield (path, lines) of the text files below folder that may hold generated code."""
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIPPED_DIRS)
        for name in sorted(names):
            path = Path(root) / name
            if path.suffix.lower() in _SKIPPED_SUFFIXES:
                continue
            try:
                if path.stat().st_size <= _MAX_BYTES:
                    yield path, path.read_text(encoding="utf-8-sig").splitlines()
            except (OSError, UnicodeDecodeError):
                continue  # binary or unreadable: not source code


def scan(folder: Path) -> list[tuple[Path, list[str], list[tuple[str, int, int]]]]:
    """(path, lines, [(tag, first, last)]) for every file below folder that holds tag comments.

    shortcut: reads every candidate file on each call, cache by mtime if projects get large.
    """
    result = []
    for path, lines in source_files(folder):
        sections = tag_sections(lines)
        if sections:
            result.append((path, lines, sorted(sections, key=lambda section: section[1])))
    return result

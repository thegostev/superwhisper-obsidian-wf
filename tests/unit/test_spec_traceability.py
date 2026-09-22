"""Spec traceability tests (LAG-732).

The spec's Conventions section makes requirement identifiers stable and asks
tests to reference them by identifier. That contract only holds if every
identifier cited anywhere in the repository resolves to a spec entry: a
dangling identifier records a real requirement — WD-13's Full Disk Access
grant is a deployment prerequisite that kills the watchdog's first tick —
in a place no traceability row reaches.
"""

import re
from pathlib import Path

PROJECT_DIR = Path(__file__).parents[2]
SPEC = PROJECT_DIR / "docs" / "specs" / "self-healing-health-check.md"

#: Requirement groups defined by the spec: heartbeat, health check, watchdog,
#: preflight, escalation.
REQUIREMENT_ID = re.compile(r"\b((?:HB|HC|WD|PF|ES)-\d+[a-z]?)\b")
#: A spec entry is a list item whose first bold run is the identifier.
DEFINITION = re.compile(r"^- \*\*((?:HB|HC|WD|PF|ES)-\d+[a-z]?)\*\*", re.MULTILINE)

SCANNED_SUFFIXES = frozenset({".md", ".py", ".sh", ".template", ".yaml", ".yml", ".toml"})
SKIPPED_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "htmlcov", ".mypy_cache", ".pytest_cache"})


def _defined_requirements() -> set[str]:
    """Identifiers the spec defines as entries."""
    return set(DEFINITION.findall(SPEC.read_text(encoding="utf-8")))


def _referenced_requirements() -> dict[str, set[Path]]:
    """Identifiers cited anywhere in the repository, mapped to their citing files."""
    references: dict[str, set[Path]] = {}
    for path in PROJECT_DIR.rglob("*"):
        if path.suffix not in SCANNED_SUFFIXES or not path.is_file():
            continue
        if SKIPPED_DIRS & set(path.relative_to(PROJECT_DIR).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        for identifier in set(REQUIREMENT_ID.findall(text)):
            references.setdefault(identifier, set()).add(path.relative_to(PROJECT_DIR))
    return references


class TestRequirementIdentifiers:
    def test_every_cited_identifier_resolves_to_a_spec_entry(self) -> None:
        """LAG-732 acceptance: no identifier is cited without a spec entry."""
        defined = _defined_requirements()
        dangling = {
            identifier: sorted(str(p) for p in files)
            for identifier, files in _referenced_requirements().items()
            if identifier not in defined
        }
        assert dangling == {}, f"identifiers cited but absent from {SPEC.name}: {dangling}"

    def test_the_spec_defines_each_identifier_once(self) -> None:
        """Duplicate entries would make an identifier ambiguous, not stable."""
        found = DEFINITION.findall(SPEC.read_text(encoding="utf-8"))
        duplicates = sorted({i for i in found if found.count(i) > 1})
        assert duplicates == [], f"duplicate spec entries: {duplicates}"


class TestFullDiskAccessRequirement:
    """The identifier LAG-732 found dangling, and what its entry must carry."""

    def test_entry_exists_and_names_the_grant_and_the_failure(self) -> None:
        entry = next(
            (line for line in SPEC.read_text(encoding="utf-8").splitlines() if line.startswith("- **WD-13**")),
            None,
        )
        assert entry is not None, "WD-13 has no spec entry"
        assert "Full Disk Access" in entry
        assert "Operation not permitted" in entry

    def test_entry_has_a_traceability_row(self) -> None:
        rows = [
            line for line in SPEC.read_text(encoding="utf-8").splitlines() if line.startswith("|") and "WD-13" in line
        ]
        assert rows, "WD-13 has no row in the traceability table"
        assert any("manual" in row for row in rows), "the TCC grant has no automatable test; the row must say so"

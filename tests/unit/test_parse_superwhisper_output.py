"""Unit tests for parse_superwhisper_output().

All tests are pure Python — no Superwhisper process required.
"""

import pytest

from pipeline import PermanentFileError, parse_superwhisper_output


def _make(category="PERSONAL", filename="Test Meeting", analysis="## Notes\n- point"):
    return f"CATEGORY: {category}\nFILENAME: {filename}\n\n{analysis}"


def test_happy_path(mock_superwhisper_output):
    output = mock_superwhisper_output(category="PERSONAL", filename="Team Sync", analysis="## Summary\nAll good.")
    category, filename, analysis = parse_superwhisper_output(output)
    assert category == "PERSONAL"
    assert filename == "Team Sync"
    assert "All good." in analysis


def test_category_normalised_to_uppercase():
    output = _make(category="personal")
    category, _, _ = parse_superwhisper_output(output)
    assert category == "PERSONAL"


def test_unknown_category_falls_back_to_default():
    output = _make(category="FOOBAR")
    category, _, _ = parse_superwhisper_output(output)
    assert category == "DEFAULT"


def test_empty_filename_falls_back():
    output = "CATEGORY: WORK\nFILENAME: \n\n## Notes\n- point"
    _, filename, _ = parse_superwhisper_output(output)
    assert filename == "Unknown Meeting"


def test_filename_slashes_replaced():
    output = _make(filename="Work/Project")
    _, filename, _ = parse_superwhisper_output(output)
    assert "/" not in filename
    assert filename == "Work-Project"


def test_filename_colons_replaced():
    output = _make(filename="Meeting: Notes")
    _, filename, _ = parse_superwhisper_output(output)
    assert ":" not in filename


def test_missing_analysis_body_raises():
    output = "CATEGORY: WORK\nFILENAME: Title\n\n"
    with pytest.raises(PermanentFileError):
        parse_superwhisper_output(output)


def test_analysis_content_preserved():
    analysis_body = "## Topic\n- 🤝 Decision reached\n- ▶️ Action item"
    output = _make(analysis=analysis_body)
    _, _, analysis = parse_superwhisper_output(output)
    assert analysis == analysis_body


def test_whitespace_stripped_from_category_and_filename():
    output = "CATEGORY:   PERSONAL  \nFILENAME:   My Meeting  \n\n## Notes"
    category, filename, _ = parse_superwhisper_output(output)
    assert category == "PERSONAL"
    assert filename == "My Meeting"


def test_real_prompt_format():
    """Matches the exact output format from the meeting.json prompt."""
    output = (
        "CATEGORY: TEAM\n"
        "FILENAME: Sprint Review - Timeline, Backstage, Jenkins\n"
        "\n"
        "**Jenkins migration**\n"
        "- Marcin pushed back on Q1 deadline\n"
        "- Action item: draft cutoff comms by Mon\n"
    )
    category, filename, analysis = parse_superwhisper_output(output)
    assert category == "TEAM"
    assert filename == "Sprint Review - Timeline, Backstage, Jenkins"
    assert "Jenkins migration" in analysis
    assert "CATEGORY" not in analysis
    assert "FILENAME" not in analysis

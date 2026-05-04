"""Smoke tests to verify test infrastructure works."""


def test_tmp_output_dir_fixture(tmp_output_dir):
    """Verify the tmp_output_dir fixture creates the expected structure.

    With single-file output, files land directly in the category folder.
    """
    categories = ["WORK", "PERSONAL", "DEFAULT"]
    for cat in categories:
        assert (tmp_output_dir / cat).is_dir()


def test_sample_state_fixture(sample_state):
    """Verify the sample_state fixture has expected structure."""
    assert "processed" in sample_state
    assert len(sample_state["processed"]) == 1
    entry = list(sample_state["processed"].values())[0]
    assert entry["status"] == "complete"
    assert entry["category"] == "WORK"


def test_state_file_fixture(state_file):
    """Verify the state_file fixture creates a readable JSON file."""
    import json

    data = json.loads(state_file.read_text())
    assert "processed" in data


def test_mock_superwhisper_output_fixture(mock_superwhisper_output):
    """Verify the mock_superwhisper_output factory produces the expected format."""
    output = mock_superwhisper_output()
    assert "CATEGORY: PERSONLIG" in output
    assert "FILENAME: Test Meeting" in output
    assert "Test analysis content." in output


def test_mock_superwhisper_output_custom(mock_superwhisper_output):
    """Verify the factory accepts custom arguments."""
    output = mock_superwhisper_output(
        category="PERSONAL",
        filename="Custom Meeting",
        analysis="Custom analysis.",
    )
    assert "PERSONAL" in output
    assert "Custom Meeting" in output
    assert "Custom analysis." in output

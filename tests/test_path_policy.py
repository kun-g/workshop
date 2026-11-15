import pytest
from workshop.path_policy import only_ignored_paths


@pytest.mark.parametrize("path", ["docs/a.txt", "docs/sub/a.json", "tests/test_a.py", "services/app/tests/test_a.py", "a.md", "nested/a.md"])
def test_configured_non_runtime_paths(path):
    assert only_ignored_paths([{"filename": path}], ["docs/", "**/tests/", "*.md"])


@pytest.mark.parametrize("path", ["src/tests_support.py", "docs.py", "scripts/sync-production.sh", "manifests/app.yaml"])
def test_other_paths_need_release(path):
    assert not only_ignored_paths([{"filename": path}], ["docs/", "**/tests/", "*.md"])


def test_rename_must_match_both_paths_and_config_is_required():
    assert not only_ignored_paths([{"filename": "docs/code.py", "previous_filename": "src/code.py"}], ["docs/"])
    assert not only_ignored_paths([{"filename": "docs/code.py", "status": "renamed"}], ["docs/"])
    assert not only_ignored_paths([], [])
    with pytest.raises(ValueError):
        only_ignored_paths([{"filename": "../docs/code.py"}], ["docs/"])

from spawnd.cli import _format_pr_body


def test_format_pr_body_is_readable_markdown_without_raw_json() -> None:
    body = _format_pr_body(
        "run-1",
        "contributor",
        {
            "branch": "spawnd/run-1/contributor",
            "commit_sha": "a" * 40,
            "base_ref": "origin/main",
            "base_sha": "b" * 40,
            "changed_files_count": 2,
            "insertions_count": 5,
            "deletions_count": 1,
            "patch_artifact_id": "patch-1",
            "diff_stats": {"committed_shortstat": "2 files changed, 5 insertions(+), 1 deletion(-)"},
        },
    )

    assert body.startswith("## Summary\n")
    assert "- Run: `run-1`" in body
    assert "- Agent: `contributor`" in body
    assert "- Branch: `spawnd/run-1/contributor`" in body
    assert "## Changes" in body
    assert "- Files changed: 2" in body
    assert "## Verification" in body
    assert "`spawnd status run-1`" in body
    assert "patch-1" in body
    assert '{"run_id"' not in body

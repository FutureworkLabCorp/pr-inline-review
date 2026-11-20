"""review_state helpers run without GitHub access."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import review_state as S  # noqa: E402


def inc(*files, status="ahead"):
    return {"status": status, "files": [
        {"filename": f, "status": st, "additions": a, "deletions": d} for f, st, a, d in files]}


def test_round_one_and_two():
    assert S.discovery(1, None)[0] == "n/a"
    assert S.discovery(2, inc(("a.py", "modified", 3, 1)))[0] == "run"


def test_small_increment_skips_discovery_from_round_three():
    verdict, why = S.discovery(4, inc(("a.py", "modified", 4, 2)))
    assert verdict == "skip" and "6 lines" in why


def test_large_increment_new_code_file_or_rewrite_runs_discovery():
    assert S.discovery(3, inc(("a.py", "modified", 150, 60)))[0] == "run"
    assert S.discovery(3, inc(("b.py", "added", 10, 0)))[0] == "run"
    assert S.discovery(3, inc(("notes.md", "added", 10, 0)))[0] == "skip"
    assert S.discovery(3, inc(("a.py", "modified", 1, 1), status="diverged"))[0] == "run"


def test_clean_cuts_markers_folded_blocks_and_badges():
    body = ("<!-- codex-review-state:v1 {} -->\n**<sub><sub>![P1 Badge](https://x/y.svg)</sub></sub>"
            "  Restore the gate**\n\n<details><summary>About</summary>long text</details>\nUseful?")
    assert S.clean(body) == "** Restore the gate** Useful?"
    assert S.clean("x" * 300, 20).endswith("…") and len(S.clean("x" * 300, 20)) == 20


def test_thread_lines_carry_state_fingerprint_votes_and_last_reply():
    nodes = [{"isResolved": False, "isOutdated": False, "path": "a.py", "line": 3, "comments": {
        "totalCount": 2, "nodes": [
            {"databaseId": 11, "author": {"login": "bot"},
             "body": "title <!-- pr-inline-review:fp=0123456789ab -->",
             "reactionGroups": [{"content": "THUMBS_DOWN", "reactors": {"totalCount": 1}}]},
            {"databaseId": 12, "author": {"login": "dev"}, "body": "Addressed in abc",
             "reactionGroups": []}]}},
        {"isResolved": True, "isOutdated": False, "path": "b.py", "line": None,
         "comments": {"totalCount": 0, "nodes": []}}]
    assert S.thread_lines(nodes) == [
        "OPEN a.py:3 #11 fp=0123456789ab @bot [THUMBS_DOWN=1]: title"
        " || 1 repl(y/ies), last @dev: Addressed in abc"]


def test_clean_removes_nested_folded_blocks():
    body = "before <details>a <details>b</details> c</details> after"
    assert S.clean(body) == "before after"


def test_review_lines_show_bodies_and_skip_empty_ones():
    reviews = [
        {"user": {"login": "lead"}, "state": "CHANGES_REQUESTED", "commit_id": "abcdef1234567890",
         "body": "Please split this PR. <!-- pr-inline-review:v1 {\"sha\":\"x\"} -->"},
        {"user": {"login": "bot"}, "state": "COMMENTED", "commit_id": "1234", "body": ""}]
    assert S.review_lines(reviews) == ["@lead CHANGES_REQUESTED abcdef123456: Please split this PR."]


def test_siblings_follow_a_symlinked_skill_directory(tmp_path):
    real = tmp_path / "standalone" / "pr-inline-review"
    real.mkdir(parents=True)
    layout = tmp_path / "layout"
    layout.mkdir()
    for d, f in [("code-review-sweep", "sweep.py"), ("code-review-sweep", "finding-gate.md"),
                 ("review-rediagnose", "SKILL.md")]:
        (layout / d).mkdir(exist_ok=True)
        (layout / d / f).write_text("")
    (layout / "pr-inline-review").symlink_to(real)
    # The sweep entry point resolves the link, so the siblings beside the link do not count.
    assert len(S.siblings(str(layout / "pr-inline-review"))) == 3


def test_siblings_reports_missing_dependencies(tmp_path):
    skill = tmp_path / "pr-inline-review"
    skill.mkdir()
    (tmp_path / "code-review-sweep").mkdir()
    (tmp_path / "code-review-sweep" / "sweep.py").write_text("")
    assert S.siblings(str(skill)) == ["code-review-sweep/finding-gate.md",
                                      "review-rediagnose/SKILL.md"]

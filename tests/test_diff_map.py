"""
Tests for review_lib. Runnable two ways:
    python tests/test_diff_map.py      (no dependency)
    pytest tests/                      (if pytest is installed)
"""
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import review_lib as R  # noqa: E402


FIXTURE = os.path.join(HERE, "fixtures", "sample.diff")
with open(FIXTURE, encoding="utf-8") as fh:
    DIFF = fh.read()


# --------------------------------------------------------------------------- #
# parse_diff
# --------------------------------------------------------------------------- #

def test_parse_files_and_paths():
    dm = R.parse_diff(DIFF)
    assert set(dm) == {"src/app/core/worker.py", "src/app/data/schemas.py"}


def test_new_line_numbers_are_correct():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    # added block occupies new lines 193..198
    assert fd.right[193].kind == "add"
    assert fd.right[193].text == "            key: value"
    assert fd.right[196].kind == "add"
    assert fd.right[196].text.strip().startswith('"team_uuid"')
    # context after the added block continues numbering
    assert fd.right[199].kind == "ctx"
    assert fd.right[201].text == "        return task.id"


def test_old_line_numbers_and_deletions():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    assert fd.left[193].kind == "del"
    assert fd.left[194].kind == "del"
    # deleted lines never appear on the RIGHT side
    assert 193 not in {n for n, l in fd.right.items() if l.kind == "del"}


def test_all_added_lines_share_one_hunk():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    assert fd.right_hunk(193) == fd.right_hunk(198) == 0


# --------------------------------------------------------------------------- #
# resolve_anchor
# --------------------------------------------------------------------------- #

def test_anchor_valid_single_line():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    a = R.resolve_anchor(fd, {"path": "src/app/core/worker.py", "line": 196})
    assert a.valid and a.line == 196 and a.side == "RIGHT"
    assert a.start_line is None
    assert not a.warnings


def test_anchor_snaps_near_miss():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    # 203 is 2 lines past the last commentable line (201) — a near-miss, corrected
    a = R.resolve_anchor(fd, {"path": "src/app/core/worker.py", "line": 203})
    assert a.valid
    assert a.line == 201
    assert any("snapped" in w for w in a.warnings)


def test_anchor_rejects_far_snap():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    # 999 is nowhere near the diff — a mislocated finding, not a delivery target
    a = R.resolve_anchor(fd, {"path": "src/app/core/worker.py", "line": 999})
    assert not a.valid
    assert any("re-anchor" in w for w in a.warnings)


def test_anchor_keeps_valid_multiline_range():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    a = R.resolve_anchor(fd, {
        "path": "src/app/core/worker.py",
        "start_line": 193, "line": 198})
    assert a.start_line == 193 and a.line == 198
    assert a.start_side == "RIGHT"


def test_anchor_drops_range_when_start_not_in_diff():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    a = R.resolve_anchor(fd, {
        "path": "src/app/core/worker.py",
        "start_line": 5, "line": 196})       # 5 is above the hunk
    assert a.start_line is None              # range dropped, single-line kept
    assert a.line == 196
    assert any("not in diff" in w for w in a.warnings)


def test_anchor_drops_reversed_range():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    a = R.resolve_anchor(fd, {
        "path": "src/app/core/worker.py",
        "start_line": 198, "line": 193})     # start > line
    assert a.start_line is None
    assert any("dropping range" in w for w in a.warnings)


# --------------------------------------------------------------------------- #
# build_comment_body
# --------------------------------------------------------------------------- #

def test_body_has_severity_prefix_and_suggestion_fence():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    a = R.resolve_anchor(fd, {"path": "src/app/core/worker.py", "line": 196})
    body = R.build_comment_body({
        "severity": "high", "category": "type-safety",
        "title": "UUID dropped by isinstance filter",
        "explanation": "The `isinstance(value, str)` filter silently drops UUIDs.",
        "suggestion": ['                "team_uuid": str(task.kwargs.get("team_uuid")),'],
    }, fd, a)
    assert body.startswith(R._BADGE["HIGH"])
    assert "**[HIGH] type-safety** — UUID dropped" in body
    assert "```suggestion" in body
    assert "isinstance" in body


def test_body_flags_indent_mismatch_in_warnings_not_body():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    a = R.resolve_anchor(fd, {"path": "src/app/core/worker.py", "line": 196})
    # target line 196 has 16 leading spaces; give the suggestion 4
    body = R.build_comment_body({
        "severity": "MEDIUM", "category": "bug", "title": "x",
        "suggestion": ['    "team_uuid": str(...),'],
    }, fd, a)
    # the mismatch is surfaced to the console (anchor.warnings), never posted in the body
    assert "indent" not in body
    assert any("indent mismatch" in w for w in a.warnings)


def test_body_warns_suggestion_shorter_than_range():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    a = R.resolve_anchor(fd, {"path": "src/app/core/worker.py",
                              "start_line": 193, "line": 198})
    # 6-line range, 1-line suggestion: Apply would delete the other 5 lines
    R.build_comment_body({
        "severity": "HIGH", "category": "bug", "title": "x",
        "suggestion": ['            key: value'],
    }, fd, a)
    assert any("kept lines" in w for w in a.warnings)


def test_body_warns_left_side_suggestion():
    fd = R.parse_diff(DIFF)["src/app/core/worker.py"]
    a = R.resolve_anchor(fd, {"path": "src/app/core/worker.py",
                              "line": 193, "side": "LEFT"})
    assert a.valid                         # deleted line, commentable on LEFT
    R.build_comment_body({
        "severity": "LOW", "title": "x",
        "suggestion": ["anything"],
    }, fd, a)
    assert any("not applyable" in w for w in a.warnings)


def test_event_request_changes_on_critical():
    assert R.decide_event([{"severity": "CRITICAL"}]) == "REQUEST_CHANGES"


def test_body_defaults_bad_severity_to_medium():
    body = R.build_comment_body({"severity": "URGENT", "title": "x"})
    assert body.startswith(R._BADGE["MEDIUM"])
    assert "**[MEDIUM]" in body


# --------------------------------------------------------------------------- #
# build_review_payload
# --------------------------------------------------------------------------- #

def test_payload_event_and_comment_count():
    dm = R.parse_diff(DIFF)
    findings = [
        {"path": "src/app/core/worker.py", "line": 196, "severity": "HIGH",
         "category": "type-safety", "title": "a", "explanation": "b"},
        {"path": "src/app/data/schemas.py", "line": 13, "severity": "LOW",
         "category": "pattern", "title": "c", "explanation": "d"},
    ]
    payload, skipped, warns = R.build_review_payload(findings, dm, "summary")
    assert payload["event"] == "REQUEST_CHANGES"     # a HIGH exists
    assert len(payload["comments"]) == 2
    assert skipped == []


def test_payload_skips_finding_for_file_not_in_diff():
    dm = R.parse_diff(DIFF)
    findings = [{"path": "does/not/exist.py", "line": 3, "severity": "LOW",
                 "title": "x"}]
    payload, skipped, warns = R.build_review_payload(findings, dm, "s")
    assert len(payload["comments"]) == 0
    assert len(skipped) == 1
    assert "not in diff" in skipped[0]["_reason"]


def test_event_approve_when_no_findings():
    assert R.decide_event([]) == "APPROVE"
    assert R.decide_event([{"severity": "LOW"}]) == "COMMENT"


def test_payload_event_override():
    dm = R.parse_diff(DIFF)
    findings = [{"path": "src/app/core/worker.py", "line": 196,
                 "severity": "HIGH", "title": "a"}]
    payload, _, _ = R.build_review_payload(findings, dm, "s", event="COMMENT")
    assert payload["event"] == "COMMENT"     # override beats decide_event


def test_summary_body_is_model_markdown_plus_table():
    import review_post as P
    body = P.summary_body(
        [{"severity": "HIGH"}, {"severity": "LOW"}],
        "Overview prose.\n\n**Verdict: fix the HIGH before merge.**")
    # model markdown passes through untouched; script adds only the count table
    assert "Overview prose." in body
    assert "**Verdict: fix the HIGH before merge.**" in body
    assert f"| {P.SEV_ICON['HIGH']} HIGH | 1 |" in body
    assert f"| {P.SEV_ICON['LOW']} LOW | 1 |" in body
    assert "머지 전" not in body and "LGTM" not in body   # no hardcoded verdict prose


# --------------------------------------------------------------------------- #
# review state and posting guards
# --------------------------------------------------------------------------- #

def test_fingerprint_ignores_line_and_whitespace():
    a = {"path": "x.py", "line": 10, "category": "bug", "title": "Lost  UUID"}
    b = {"path": "x.py", "line": 42, "category": "BUG", "title": "lost uuid "}
    assert R.fingerprint(a) == R.fingerprint(b)
    assert R.fingerprint(a) != R.fingerprint({**a, "path": "y.py"})


def test_inline_comment_carries_fingerprint():
    dm = R.parse_diff(DIFF)
    f = {"path": "src/app/core/worker.py", "line": 196, "severity": "LOW",
         "title": "a"}
    payload, _, _ = R.build_review_payload([f], dm, "s")
    assert R.parse_fingerprint_marker(payload["comments"][0]["body"]) == R.fingerprint(f)


def test_state_marker_round_trips_through_summary_body():
    import review_post as P
    findings = [{"path": "x.py", "category": "bug", "title": "t", "severity": "HIGH"}]
    body = P.summary_body(findings, "prose", head_sha="abc123")
    state = R.parse_state_marker(body)
    assert state == {"sha": "abc123", "findings": [R.fingerprint(findings[0])]}
    assert R.parse_state_marker("no marker here") is None


def test_summary_body_lists_skipped_findings():
    import review_post as P
    skipped = [{"path": "a.py", "line": 7, "severity": "HIGH", "title": "far anchor",
                "explanation": "why", "_reason": "not in diff"}]
    body = P.summary_body(skipped, "", skipped)
    assert "`a.py:7`" in body and "far anchor" in body and "why" in body


def test_own_pr_downgrades_request_changes_only():
    assert R.event_for_author("REQUEST_CHANGES", own_pr=True)[0] == "COMMENT"
    assert R.event_for_author("REQUEST_CHANGES", own_pr=False) == ("REQUEST_CHANGES", None)
    assert R.event_for_author("COMMENT", own_pr=True) == ("COMMENT", None)


def test_check_head_refuses_moved_head():
    import review_post as P
    meta = {"headRefOid": "deadbeef" * 5}
    assert P.check_head(meta, "deadbeef") is None          # short sha prefix
    assert P.check_head(meta, None) is None                # not pinned: no check
    assert "다름" in P.check_head(meta, "cafebabe")


# --------------------------------------------------------------------------- #
# bare-python runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

"""
Tests for sweep.py's pure scanners. Runnable two ways:
    python tests/test_sweep.py      (Python 3.11+, for tomllib)
    pytest tests/
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

try:
    import sweep as S  # noqa: E402
except ModuleNotFoundError as e:  # tomllib is 3.11+
    if __name__ == "__main__":
        print(f"SKIP: sweep.py needs Python 3.11+ ({e})")
        sys.exit(0)
    raise

LANGS = S.load_config()[0]


def _diff(old_path, hunks, new_path=None):
    new = "/dev/null" if new_path == "/dev/null" else f"b/{new_path or old_path}"
    return "\n".join([f"diff --git a/{old_path} b/{old_path}", f"--- a/{old_path}", f"+++ {new}", *hunks]) + "\n"


# --------------------------------------------------------------------------- #
# REMOVED
# --------------------------------------------------------------------------- #

def test_deleted_guard_is_listed():
    d = _diff("src/app/x.py", ["@@ -10,2 +10,0 @@", "-    if user is None:", "-        raise PermissionError()"])
    rows = S.scan_removed(d, LANGS)
    assert rows == ["src/app/x.py:10\tif user is None:", "src/app/x.py:11\traise PermissionError()"], rows


def test_moved_guard_is_not_listed():
    d = _diff("src/app/x.py", ["@@ -10 +10,0 @@", "-    raise PermissionError()", "@@ -30,0 +29 @@", "+        raise PermissionError()"])
    assert S.scan_removed(d, LANGS) == []


def test_plain_deleted_line_is_not_listed():
    d = _diff("src/app/x.py", ["@@ -10 +10,0 @@", "-    total = a + b"])
    assert S.scan_removed(d, LANGS) == []


def test_rewritten_test_is_not_listed_but_a_deleted_one_absorbs_its_checks():
    d = _diff("tests/test_x.py", [
        "@@ -5,3 +5,0 @@",
        "-def test_kept(tmp_path):",
        "-    assert a",
        "-    assert b",
        "@@ -20,3 +17,0 @@",
        "-def test_gone():",
        "-    assert c",
        "-    assert d",
        "@@ -30,0 +30 @@",
        "+def test_kept(monkeypatch):",
    ])
    rows = S.scan_removed(d, LANGS)
    assert rows[-1] == "tests/test_x.py:20\tdef test_gone():  (+2 checks deleted with it)", rows
    assert not any("test_kept" in r for r in rows)


def test_repeated_line_is_one_row():
    d = _diff("tests/test_x.py", ["@@ -3 +3,0 @@", '-@pytest.mark.parametrize("m", [1, 2])', "@@ -9 +8,0 @@", '-@pytest.mark.parametrize("m", [1, 2])'])
    rows = S.scan_removed(d, LANGS)
    assert rows == ['tests/test_x.py:3\t@pytest.mark.parametrize("m", [1, 2])  (also at 9)'], rows


def test_deleted_file_is_one_row():
    d = _diff("src/app/guard.py", ["@@ -1,3 +0,0 @@", "-def check(u):", "-    if not u:", "-        raise ValueError()"], new_path="/dev/null")
    assert S.scan_removed(d, LANGS) == ["src/app/guard.py\t(file deleted: 3 lines, 3 that enforced something)"]


def test_comment_is_not_a_guard():
    d = _diff("src/app/x.py", ["@@ -4 +4,0 @@", "-    # raise if the user is None"])
    assert S.scan_removed(d, LANGS) == []


# --------------------------------------------------------------------------- #
# DOC-*
# --------------------------------------------------------------------------- #

Q = '"' * 3  # a docstring fence, kept out of the literals below


def _docs(source, rngs, path="src/app/x.py"):
    hits = S.Hits()
    S.scan_docs(hits, path, source, rngs)
    return {cat: [r.split("\t")[1] for r in rows] for cat, rows in hits.by_cat.items()}


def test_narrating_docstring_is_flagged_on_added_lines_only():
    src = "def f():\n    " + Q + "Return the total.\n\n    Previously this dropped zeros.\n    " + Q + "\n    return 1\n"
    assert _docs(src, [(4, 4)]) == {"DOC-NARRATION": ["src/app/x.py:4"]}
    assert _docs(src, [(6, 6)]) == {}          # the docstring was not touched


def test_korean_narration_and_ticket_label_in_comments():
    src = "x = 1\n# 기존에는 0을 버렸는데 FUT-1234 이후 남긴다\ny = 2\n"
    got = _docs(src, [(2, 2)])
    assert got == {"DOC-NARRATION": ["src/app/x.py:2"], "DOC-LABEL": ["src/app/x.py:2"]}, got


def test_standard_names_are_not_labels():
    src = "# hashes with SHA-256 and dates in ISO-8601\nx = 1\n"
    assert _docs(src, [(1, 1)]) == {}


def test_numbered_steps_and_banners():
    src = "# 1. load\nx = 1\n# ------ section ------\n"
    assert _docs(src, [(1, 3)]) == {"DOC-STEPS": ["src/app/x.py:1", "src/app/x.py:3"]}


def test_args_block_is_a_restate_candidate():
    src = "def f(a: int) -> int:\n    " + Q + "Double it.\n\n    Args:\n        a: the int\n    " + Q + "\n    return a * 2\n"
    assert _docs(src, [(1, 7)]) == {"DOC-RESTATE": ["src/app/x.py:4"]}


def test_long_private_docstring_written_by_the_change():
    body = "\n".join(f"    line {i}." for i in range(5))
    src = "def _f():\n    " + Q + "Start.\n" + body + "\n    " + Q + "\n    return 1\n"
    assert _docs(src, [(1, 9)])["DOC-LONG"] == ["src/app/x.py:2"]
    assert "DOC-LONG" not in _docs(src, [(3, 3)])   # touched one line of an old docstring


def test_long_comment_block():
    src = "\n".join("# note" for _ in range(7)) + "\nx = 1\n"
    assert _docs(src, [(1, 8)]) == {"DOC-LONG": ["src/app/x.py:1"]}


def test_ts_comment_narration():
    src = "// this PR now handles nulls\nconst a = 1\n"
    assert _docs(src, [(1, 1)], path="web/a.ts") == {"DOC-NARRATION": ["web/a.ts:1"]}


# --------------------------------------------------------------------------- #
# DUP-*
# --------------------------------------------------------------------------- #

BLOCK = [
    "    response = client.post(url, json=payload, timeout=30)",
    "    response.raise_for_status()",
    "    data = response.json()['items']",
    "    return [normalize(item) for item in data]",
]


def _dup(added):
    hits = S.Hits()
    S._dup_in_diff(hits, added)
    return [r.split("\t", 1)[1] for r in hits.by_cat.get("DUP-IN-DIFF", [])]


def test_block_added_twice_is_one_row_per_copy():
    a = {n: t for n, t in enumerate(BLOCK, start=10)}
    b = {n: t for n, t in enumerate(BLOCK, start=50)}
    got = _dup({"src/app/a.py": a, "src/app/b.py": b})
    assert got == ["src/app/b.py:50\t4+ lines also added at src/app/a.py:10"], got


def test_trivial_repeats_are_not_duplicates():
    trivial = {n: t for n, t in enumerate(["    pass", "    return", "    )", "    else:"], start=1)}
    assert _dup({"src/app/a.py": trivial, "src/app/b.py": dict(trivial)}) == []


def test_test_paths_are_skipped():
    assert S._is_test_path("tests/unit_tests/x/test_a.py")
    assert S._is_test_path("web/src/a.test.tsx")
    assert not S._is_test_path("src/app/rag/ingest.py")


# --------------------------------------------------------------------------- #
# second caller hop
# --------------------------------------------------------------------------- #

def test_second_hop_skips_tests_private_and_common_callers():
    import types
    grep_out = "\n".join([
        "src/app/a.py:10:    total = changed()",      # caller `public_entry` -> followed
        "src/app/b.py:20:    x = changed()",          # caller `_private` -> not followed
        "src/app/c.py:30:    changed()",              # caller `run` -> not followed
        "tests/test_x.py:5:    changed()",            # test path -> not followed
        "src/app/d.py:1:def changed():",              # the definition itself
    ])
    enclosing = {"src/app/a.py": "public_entry", "src/app/b.py": "_private", "src/app/c.py": "run"}
    saved = (S.run, S._enclosing_func, S._callers)
    try:
        S.run = lambda cmd, **kw: types.SimpleNamespace(stdout=grep_out, returncode=0)
        S._enclosing_func = lambda path, line: types.SimpleNamespace(name=enclosing[path]) if path in enclosing else None
        S._callers = lambda name, path, glob, limit, *a: [f"src/app/api_{name}.py"]
        got = S._second_hop("changed", "src/app/d.py", "*.py", 5)
    finally:
        S.run, S._enclosing_func, S._callers = saved
    assert got == ["src/app/api_public_entry.py(via public_entry)"], got


# --------------------------------------------------------------------------- #
# bare-python runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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

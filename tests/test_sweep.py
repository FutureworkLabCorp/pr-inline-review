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

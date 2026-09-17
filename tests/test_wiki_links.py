"""
Tests for review_lib.expand_wiki_links. Runnable two ways:
    python tests/test_wiki_links.py    (no dependency)
    pytest tests/                      (if pytest is installed)
"""
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import review_lib as R  # noqa: E402


BASE = "https://github.com/OWNER/REPO/wiki/"


def _x(text, **kw):
    return R.expand_wiki_links(text, BASE, **kw)


# --------------------------------------------------------------------------- #
# the basic forms
# --------------------------------------------------------------------------- #

def test_bare_page_becomes_a_link():
    assert _x("see [[Skill-System]] now") == \
        "see [Skill-System](https://github.com/OWNER/REPO/wiki/Skill-System) now"


def test_labelled_form_keeps_the_label_and_links_the_page():
    # gollum order is [[Label|Page]], not the other way round
    assert _x("[[the map|Agent-Orchestration-Overview]]") == \
        "[the map](https://github.com/OWNER/REPO/wiki/Agent-Orchestration-Overview)"


def test_anchor_is_carried_into_the_url():
    assert _x("[[Decision-RAG-Dictionary-Design#D3]]") == \
        ("[Decision-RAG-Dictionary-Design#D3]"
         "(https://github.com/OWNER/REPO/wiki/Decision-RAG-Dictionary-Design#D3)")


def test_spaces_are_slugged_the_way_github_does():
    assert _x("[[Low Code React Agents]]") == \
        "[Low Code React Agents](https://github.com/OWNER/REPO/wiki/Low-Code-React-Agents)"


def test_several_links_on_one_line():
    out = _x("[[A]] and [[B]]")
    assert out.count("https://github.com/OWNER/REPO/wiki/") == 2


def test_text_without_brackets_is_returned_unchanged():
    src = "no wiki reference here"
    assert _x(src) is src


# --------------------------------------------------------------------------- #
# code must survive verbatim — prose *about* this syntax is the common case
# --------------------------------------------------------------------------- #

def test_inline_code_span_is_left_alone():
    assert _x("write `[[Page]]` in the wiki") == "write `[[Page]]` in the wiki"


def test_fenced_block_is_left_alone():
    src = "before [[A]]\n```\n[[A]]\n```\nafter [[A]]"
    out = _x(src)
    assert out.splitlines()[2] == "[[A]]"
    assert out.count("wiki/A)") == 2


def test_tilde_fence_is_left_alone():
    out = _x("~~~\n[[A]]\n~~~")
    assert "[[A]]" in out and "wiki/A)" not in out


def test_a_backtick_fence_does_not_close_a_tilde_fence():
    # mismatched closers used to end the block early and expand the rest
    out = _x("~~~\n```\n[[A]]\n~~~\n[[B]]")
    assert "[[A]]" in out
    assert "wiki/B)" in out


# --------------------------------------------------------------------------- #
# unknown-page reporting
# --------------------------------------------------------------------------- #

def test_unknown_page_is_reported_but_still_linked():
    seen = []
    out = _x("[[Nope]] [[Skill-System]]",
             known_pages={"Skill-System"}, on_unknown=seen.append)
    assert seen == ["Nope"]
    assert out.count("https://github.com/OWNER/REPO/wiki/") == 2


def test_no_known_pages_means_no_reporting():
    seen = []
    _x("[[Anything]]", on_unknown=seen.append)
    assert seen == []


def test_the_anchor_is_not_part_of_the_page_name_being_checked():
    seen = []
    _x("[[Skill-System#4]]", known_pages={"Skill-System"}, on_unknown=seen.append)
    assert seen == []


# --------------------------------------------------------------------------- #
# degenerate input
# --------------------------------------------------------------------------- #

def test_empty_page_name_is_left_alone():
    assert _x("[[   ]]") == "[[   ]]"


def test_empty_text_is_safe():
    assert _x("") == ""
    assert R.expand_wiki_links(None, BASE) is None


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

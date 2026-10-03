#!/usr/bin/env python3
"""Sink-sweep discovery driver.

Collects candidate defect sites from a diff in a single invocation so the
reviewing agent spends context on judgement, not discovery.

Scanners and their patterns live in ``patterns.toml`` (loaded next to this
script), grouped per language. Adding a pattern or a whole language is a config
edit — the engine below is language-agnostic. AST-based checks that config
cannot express (e.g. Python loop isolation) stay here as named hooks referenced
from ``special_scanners``.

Usage:
    python sweep.py                 # auto: staged -> worktree -> merge-base
    python sweep.py --staged
    python sweep.py --worktree
    python sweep.py --base origin/develop
    python sweep.py --base origin/develop --caller-depth 2   # also callers of callers

Output (compact, TSV-ish):
    == HITS ==
    CATEGORY\tfile:line\tsnippet
    == SWEPT-NONE ==
    categories that were scanned and had zero hits
    == MANUAL (judge from diff) ==
    categories a script cannot detect; the agent judges them from the diff
    == REMOVED (...) ==
    file:line\tsnippet of a deleted line that enforced something (a raise, a guard,
    a check, a test), unless the same line was added elsewhere in the diff
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import tomllib


MAX_HITS_PER_CATEGORY = 20
MAX_SNIPPET_LEN = 160
MAX_REMOVED_ROWS = 40

CONFIG_PATH = Path(__file__).with_name("patterns.toml")


# --- Config model -----------------------------------------------------------


@dataclass
class Linter:
    command: list[str]
    parser: str
    code_map: dict[str, str]
    runner: str | None = None
    local_only: bool = False
    local_bin: str | None = None


@dataclass
class DeadCode:
    def_re: re.Pattern[str]
    grep_glob: str
    skip: re.Pattern[str] | None = None


@dataclass
class PatternRule:
    category: str
    regex: re.Pattern[str]
    exclude: re.Pattern[str] | None = None
    # Restricts the rule to files whose repo-relative path matches; a rule that only
    # holds for one tree (e.g. "imports under src/ are relative") stays quiet elsewhere.
    paths: re.Pattern[str] | None = None


@dataclass
class Lang:
    name: str
    extensions: tuple[str, ...]
    patterns: list[PatternRule]
    special: list[str] = field(default_factory=list)
    match_shebang: bool = False
    skip_lines: re.Pattern[str] | None = None
    linter: Linter | None = None
    dead_code: DeadCode | None = None
    # A deleted line matching this enforced something; the review asks where the new
    # code re-establishes it.
    removed: re.Pattern[str] | None = None


@dataclass
class UnitConfig:
    size_threshold: int = 120
    coverage_promote: float = 0.4
    callers_max: int = 5
    max_blocks: int = 3
    # 2 also lists the callers of each caller; a deeper round asks for it.
    caller_depth: int = 1


@dataclass
class DocsConfig:
    """What makes an added docstring or comment a candidate (patterns.toml [global])."""
    narration: re.Pattern[str] | None = None
    label: re.Pattern[str] | None = None
    steps: re.Pattern[str] | None = None
    restate: re.Pattern[str] | None = None
    long_docstring: int = 8
    long_private_docstring: int = 3
    long_comment_block: int = 6


@dataclass
class DupConfig:
    """Duplicate-change detection sizes (patterns.toml [global] dup_*)."""
    window: int = 4
    min_line: int = 40
    max_greps: int = 40


DUP = DupConfig()
# Test files: skipped by DUP-* (tests repeat setup and helper names on purpose) and by
# the second caller hop (a test calling a function is not a production call path).
TEST_PATHS: re.Pattern[str] | None = None
# Names too common to follow by text search: grep finds every unrelated `run`.
COMMON_NAMES: re.Pattern[str] | None = None
DUP_CATEGORIES = ("DUP-IN-DIFF", "DUP-NAME", "DUP-EXISTING")

# Set by load_config(); the special scanners share one signature and read it here.
DOCS = DocsConfig()
COMMENT_RE: dict[str, re.Pattern[str]] = {}


@dataclass
class JediConfig:
    enabled: bool = True
    roots: list[str] = field(default_factory=lambda: ["src"])
    sys_path: list[str] = field(default_factory=lambda: ["src"])
    max_symbols: int = 60


def load_config() -> tuple[list[Lang], list[str], UnitConfig, JediConfig]:
    with CONFIG_PATH.open("rb") as fh:
        raw = tomllib.load(fh)
    g = raw.get("global", {})
    manual = g.get("manual_categories", [])
    units = UnitConfig(
        size_threshold=int(g.get("unit_size_threshold", 120)),
        coverage_promote=float(g.get("unit_coverage_promote", 0.4)),
        callers_max=int(g.get("unit_callers_max", 5)),
        max_blocks=int(g.get("unit_max_blocks", 3)),
        caller_depth=int(g.get("unit_caller_depth", 1)),
    )
    global DOCS, DUP, TEST_PATHS, COMMON_NAMES

    def _re(key: str) -> re.Pattern[str] | None:
        return re.compile(g[key]) if key in g else None

    DOCS = DocsConfig(
        narration=_re("doc_narration_regex"),
        label=_re("doc_label_regex"),
        steps=_re("doc_steps_regex"),
        restate=_re("doc_restate_regex"),
        long_docstring=int(g.get("doc_long_docstring", 8)),
        long_private_docstring=int(g.get("doc_long_private_docstring", 3)),
        long_comment_block=int(g.get("doc_long_comment_block", 6)),
    )
    DUP = DupConfig(
        window=int(g.get("dup_window", 4)),
        min_line=int(g.get("dup_min_line", 40)),
        max_greps=int(g.get("dup_max_greps", 40)),
    )
    TEST_PATHS = _re("test_paths")
    COMMON_NAMES = _re("unit_common_names")
    jedi_cfg = JediConfig(
        enabled=bool(g.get("jedi_enabled", True)),
        roots=list(g.get("jedi_roots", ["src"])),
        sys_path=list(g.get("jedi_sys_path", ["src"])),
        max_symbols=int(g.get("jedi_max_symbols", 60)),
    )
    langs: list[Lang] = []
    for name, cfg in raw.items():
        if name == "global":
            continue
        patterns = [
            PatternRule(
                p["category"],
                re.compile(p["regex"]),
                re.compile(p["exclude"]) if "exclude" in p else None,
                re.compile(p["paths"]) if "paths" in p else None,
            )
            for p in cfg.get("patterns", [])
        ]
        linter = None
        if "linter" in cfg:
            lc = cfg["linter"]
            linter = Linter(
                command=list(lc["command"]),
                parser=lc["parser"],
                code_map=dict(lc.get("code_map", {})),
                runner=lc.get("runner"),
                local_only=bool(lc.get("local_only", False)),
                local_bin=lc.get("local_bin"),
            )
        dead_code = None
        if "dead_code" in cfg:
            dc = cfg["dead_code"]
            dead_code = DeadCode(
                def_re=re.compile(dc["def_regex"]),
                grep_glob=dc["grep_glob"],
                skip=re.compile(dc["skip_names"]) if "skip_names" in dc else None,
            )
        langs.append(
            Lang(
                name=name,
                extensions=tuple(cfg.get("extensions", [])),
                patterns=patterns,
                special=list(cfg.get("special_scanners", [])),
                match_shebang=bool(cfg.get("match_shebang", False)),
                skip_lines=re.compile(cfg["skip_lines"]) if "skip_lines" in cfg else None,
                linter=linter,
                dead_code=dead_code,
                removed=re.compile(cfg["removed_regex"]) if "removed_regex" in cfg else None,
            )
        )
        if "comment_regex" in cfg:
            for ext in cfg.get("extensions", []):
                COMMENT_RE[ext] = re.compile(cfg["comment_regex"])
    return langs, manual, units, jedi_cfg


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# --- Diff parsing -----------------------------------------------------------


def diff_args(mode: str, base: str | None) -> list[str]:
    if mode == "staged":
        return ["--cached"]
    if mode == "worktree":
        return []
    if mode == "base":
        mb = run(["git", "merge-base", base, "HEAD"]).stdout.strip()
        return [mb, "HEAD"]
    raise ValueError(mode)


def pick_mode(args) -> tuple[str, str | None]:
    if args.staged:
        return "staged", None
    if args.worktree:
        return "worktree", None
    if args.base:
        return "base", args.base
    # auto: staged -> worktree -> merge-base origin/develop
    if run(["git", "diff", "--cached", "--name-only"]).stdout.strip():
        return "staged", None
    if run(["git", "diff", "--name-only"]).stdout.strip():
        return "worktree", None
    return "base", "origin/develop"


def parse_added_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Map file -> list of (start, end) line ranges added by the diff."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    hunk_re = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            ranges.setdefault(current, [])
        elif line.startswith("+++ /dev/null"):
            current = None
        elif current and (m := hunk_re.match(line)):
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count > 0:
                ranges[current].append((start, start + count - 1))
    return {f: r for f, r in ranges.items() if r}


def _norm(text: str) -> str:
    return " ".join(text.split())


def parse_line_changes(diff_text: str) -> tuple[dict[str, list[tuple[int, str]]], set[str], set[str]]:
    """Deleted lines per old path, the normalized text of every added line, and the
    paths deleted outright. Reads a -U0 diff.

    An added definition also contributes `def:<name>`, so a deleted definition whose
    name comes back (a rewritten test, a changed signature) is not reported as gone.
    """
    removed: dict[str, list[tuple[int, str]]] = {}
    added: set[str] = set()
    deleted_files: set[str] = set()
    old_path: str | None = None
    old_no = 0
    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+")
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            old_path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("+++ "):
            if line.startswith("+++ /dev/null") and old_path:
                deleted_files.add(old_path)
        elif m := hunk_re.match(line):
            old_no = int(m.group(1))
        elif line.startswith("-"):
            if old_path:
                removed.setdefault(old_path, []).append((old_no, line[1:]))
            old_no += 1
        elif line.startswith("+"):
            added.add(_norm(line[1:]))
            name = _def_name(line[1:])
            if name:
                added.add(f"def:{name}")
    return removed, added, deleted_files


_TEST_DEF = re.compile(r"^\s*((async\s+)?def\s+test_|(it|test)(\.\w+)?\()")
_DEF_NAME = re.compile(r"^\s*(?:async\s+)?(?:def|function|class)\s+(\w+)|^\s*(?:it|test|describe)(?:\.\w+)?\(\s*['\"`]([^'\"`]+)")
# How far below a deleted test its deleted checks are taken as part of it.
_TEST_SPAN = 40


def _def_name(text: str) -> str | None:
    m = _DEF_NAME.match(text)
    return (m.group(1) or m.group(2)) if m else None


def _collapse_deleted_tests(path: str, guards: list[tuple[int, str]]) -> list[str]:
    """One row per deleted test, absorbing the checks deleted below it.

    A check deleted from a test that stays is its own row: the test got weaker.
    One deleted together with its test is the same fact told twice.
    """
    rows: list[str] = []
    test_row, test_line, absorbed = -1, 0, 0
    for n, t in guards:
        is_def = bool(_TEST_DEF.match(t))
        if test_row >= 0 and not is_def and n - test_line <= _TEST_SPAN:
            absorbed += 1
            rows[test_row] = rows[test_row].split("  (+")[0] + f"  (+{absorbed} checks deleted with it)"
            continue
        rows.append(f"{path}:{n}\t{t.strip()[:MAX_SNIPPET_LEN]}")
        test_row, test_line, absorbed = (len(rows) - 1, n, 0) if is_def else (-1, 0, 0)
    return rows


def _merge_repeats(rows: list[str]) -> list[str]:
    """The same deleted text at several lines of one file is one row with the others named."""
    first: dict[str, int] = {}
    more: dict[str, list[str]] = {}
    out: list[str] = []
    for row in rows:
        loc, _, text = row.partition("\t")
        if text in first:
            more.setdefault(text, []).append(loc.rsplit(":", 1)[-1])
            continue
        first[text] = len(out)
        out.append(row)
    for text, lines in more.items():
        out[first[text]] += f"  (also at {', '.join(lines)})"
    return out


def scan_removed(diff_text: str, langs: list[Lang]) -> list[str]:
    """Rows for deleted lines that enforced something and did not move elsewhere.

    The pattern scanners read added lines only, so a dropped guard is invisible to
    them: the code that is gone is exactly what no HIT can point at.
    """
    removed, added, deleted_files = parse_line_changes(diff_text)
    rows: list[str] = []
    for path, lines in removed.items():
        lang = lang_for_file(path, langs)
        if lang is None or lang.removed is None:
            continue
        guards = [
            (n, t) for n, t in lines
            if lang.removed.search(t)
            and not (lang.skip_lines and lang.skip_lines.search(t))
            and not t.lstrip().startswith("#")
            and _norm(t) not in added
            and f"def:{_def_name(t)}" not in added
        ]
        if not guards:
            continue
        if path in deleted_files:
            rows.append(f"{path}\t(file deleted: {len(lines)} lines, {len(guards)} that enforced something)")
            continue
        rows.extend(_merge_repeats(_collapse_deleted_tests(path, guards)))
    if len(rows) > MAX_REMOVED_ROWS:
        extra = len(rows) - MAX_REMOVED_ROWS
        rows = rows[:MAX_REMOVED_ROWS] + [f"-\t(+{extra} more — read the deletions in the diff)"]
    return rows


def in_ranges(lineno: int, rngs: list[tuple[int, int]]) -> bool:
    return any(a <= lineno <= b for a, b in rngs)


# --- Hit collection ---------------------------------------------------------


class Hits:
    def __init__(self) -> None:
        self.by_cat: dict[str, list[str]] = {}
        self.overflow: set[str] = set()

    def add(self, cat: str, file: str, line: int, snippet: str) -> None:
        rows = self.by_cat.setdefault(cat, [])
        if len(rows) >= MAX_HITS_PER_CATEGORY:
            self.overflow.add(cat)
            return
        snippet = snippet.strip()[:MAX_SNIPPET_LEN]
        rows.append(f"{cat}\t{file}:{line}\t{snippet}")


def scan_patterns(
    hits: Hits,
    file: str,
    lines: list[str],
    rngs: list[tuple[int, int]],
    patterns: list[PatternRule],
    skip_lines: re.Pattern[str] | None = None,
) -> None:
    patterns = [p for p in patterns if p.paths is None or p.paths.search(file)]
    for start, end in rngs:
        for lineno in range(start, min(end, len(lines)) + 1):
            text = lines[lineno - 1]
            if skip_lines is not None and skip_lines.search(text):
                continue
            for p in patterns:
                if p.exclude is not None and p.exclude.search(text):
                    continue
                if p.regex.search(text):
                    hits.add(p.category, file, lineno, text)


# --- Special (code) scanners: AST checks config can't express ----------------


def scan_loop_isolation(
    hits: Hits, file: str, source: str, rngs: list[tuple[int, int]]
) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return

    def unprotected_awaits(node: ast.AST, protected: bool):
        for child in ast.iter_child_nodes(node):
            child_protected = protected
            if isinstance(node, ast.Try) and child in node.body:
                child_protected = True
            if isinstance(child, (ast.For, ast.AsyncFor, ast.While)):
                # Inner loops are reported on their own.
                continue
            if isinstance(child, ast.Await) and not child_protected:
                yield child
            yield from unprotected_awaits(child, child_protected)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        loop_end = getattr(node, "end_lineno", node.lineno)
        touches_diff = any(not (b < node.lineno or a > loop_end) for a, b in rngs)
        if not touches_diff:
            continue
        body_awaits = [
            aw for stmt in node.body for aw in unprotected_awaits(stmt, False)
        ]
        if body_awaits:
            hits.add(
                "LOOP-ISOLATION",
                file,
                node.lineno,
                f"loop body awaits at line {body_awaits[0].lineno} with no per-item try/except",
            )


# name -> (function(hits, file, source, rngs), category it can emit)
DOC_CATEGORIES = ("DOC-NARRATION", "DOC-LABEL", "DOC-RESTATE", "DOC-STEPS", "DOC-LONG")
_DOC_FENCES = ("", '"' * 3, "'" * 3)


def _doc_line_hits(hits: Hits, file: str, lineno: int, text: str, docstring: bool) -> None:
    """Narration, labels, and (comments only) numbered steps / banners on one added line."""
    if DOCS.narration and DOCS.narration.search(text):
        hits.add("DOC-NARRATION", file, lineno, text)
    if DOCS.label and DOCS.label.search(text):
        hits.add("DOC-LABEL", file, lineno, text)
    if not docstring and DOCS.steps and DOCS.steps.search(text):
        hits.add("DOC-STEPS", file, lineno, text)


def _scan_docstrings(hits: Hits, file: str, source: str, lines: list[str], rngs: list[tuple[int, int]]) -> set[int]:
    """Judge added docstring lines; returns every docstring line so comments skip them."""
    doc_lines: set[int] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return doc_lines
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if not (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            continue
        start, end = body[0].lineno, body[0].end_lineno or body[0].lineno
        doc_lines.update(range(start, end + 1))
        added = [n for n in range(start, end + 1) if in_ranges(n, rngs)]
        if not added:
            continue
        for n in added:
            text = lines[n - 1]
            _doc_line_hits(hits, file, n, text, docstring=True)
            if DOCS.restate and DOCS.restate.search(text):
                hits.add("DOC-RESTATE", file, n, text)
        name = getattr(node, "name", "<module>")
        private = name.startswith("_") and not name.startswith("__")
        size = sum(1 for n in range(start, end + 1) if lines[n - 1].strip() not in _DOC_FENCES)
        limit = DOCS.long_private_docstring if private else DOCS.long_docstring
        # A long docstring the change mostly wrote; one it only touched is not its doing.
        if size > limit and len(added) * 2 >= end - start + 1:
            hits.add("DOC-LONG", file, start, f"{name}: docstring of {size} lines (> {limit})")
    return doc_lines


def scan_docs(hits: Hits, file: str, source: str, rngs: list[tuple[int, int]]) -> None:
    """Added docstrings and comments that tell the change's story instead of the code's.

    Mirrors linkBrain-server's .docs/Code-Conventions.md "Docstrings and comments":
    no narrating your own work, no labels whose meaning lives elsewhere, no
    numbered steps or banners, no Args/Returns that repeat the signature. Length is
    only a candidate there ("information rather than line count"), so DOC-LONG
    asks the question and never decides it. Only added lines are judged, so an old
    docstring next to the change is not blamed on it.
    """
    lines = source.splitlines()
    doc_lines = _scan_docstrings(hits, file, source, lines, rngs) if file.endswith(".py") else set()
    comment_re = COMMENT_RE.get(Path(file).suffix)
    if comment_re is None:
        return
    block: list[int] = []

    def flush() -> None:
        if len(block) > DOCS.long_comment_block:
            hits.add("DOC-LONG", file, block[0], f"comment block of {len(block)} lines (> {DOCS.long_comment_block})")
        block.clear()

    for start, end in rngs:
        for n in range(start, min(end, len(lines)) + 1):
            text = lines[n - 1]
            if n in doc_lines or not comment_re.search(text):
                flush()
                continue
            if block and block[-1] != n - 1:
                flush()
            block.append(n)
            _doc_line_hits(hits, file, n, text, docstring=False)
        flush()


SPECIAL_SCANNERS: dict[str, tuple] = {
    "loop_isolation": (scan_loop_isolation, "LOOP-ISOLATION"),
    "docs": (scan_docs, DOC_CATEGORIES),
}


# --- Type-aware reference resolution (jedi, Python) --------------------------
#
# Replaces the git-grep name search for DEAD-CODE and unit callers when jedi is
# importable. A lexical grep for a name both over-counts (a same-named symbol in
# an unrelated module looks like a reference) and under-counts (a symbol used only
# through `import foo as bar` is invisible under its original name). jedi resolves
# these from the definition. It is scoped to the configured roots for speed and
# degrades to git grep on any failure so behavior is never worse than before.


class JediRefs:
    def __init__(self, cfg: JediConfig) -> None:
        self.enabled = False
        self.reason = ""
        self.calls = 0
        self.max = cfg.max_symbols
        self.root_paths: list[Path] = []
        self._projects: list = []
        if not cfg.enabled:
            self.reason = "disabled in config"
            return
        try:
            import jedi
        except Exception:
            self.reason = "jedi not importable"
            return
        self._jedi = jedi
        repo = Path.cwd()
        sys_path = [str((repo / p).resolve()) for p in cfg.sys_path if (repo / p).is_dir()]
        for r in cfg.roots:
            rp = (repo / r).resolve()
            if not rp.is_dir():
                continue
            try:
                self._projects.append(jedi.Project(str(rp), added_sys_path=sys_path))
                self.root_paths.append(rp)
            except Exception:
                pass
        if self._projects:
            self.enabled = True
            self.reason = f"roots={','.join(str(p.name) for p in self.root_paths)}"
        else:
            self.reason = "no configured jedi_roots exist"

    def _in_scope(self, abspath: Path) -> bool:
        return any(
            abspath == rp or str(abspath).startswith(str(rp) + os.sep)
            for rp in self.root_paths
        )

    def references(
        self, deffile: str, name: str, def_line: int
    ) -> set[tuple[str, int]] | None:
        """References to the symbol as (relpath, line), excluding its own definition
        site. Returns None whenever the caller should fall back to git grep: jedi
        unavailable, budget exhausted, def outside the searched roots, or any error
        (a partial answer would be worse than an honest lexical one)."""
        if not self.enabled or (self.max and self.calls >= self.max):
            return None
        abspath = (Path.cwd() / deffile).resolve()
        if not self._in_scope(abspath):
            return None
        try:
            source = abspath.read_text(errors="replace")
        except OSError:
            return None
        lines = source.splitlines()
        if not (1 <= def_line <= len(lines)):
            return None
        col = lines[def_line - 1].find(name)
        if col < 0:
            return None
        self.calls += 1
        repo = str(Path.cwd())
        found: set[tuple[str, int]] = set()
        for proj in self._projects:
            try:
                script = self._jedi.Script(code=source, path=str(abspath), project=proj)
                refs = script.get_references(line=def_line, column=col, include_builtins=False)
            except Exception:
                return None
            for r in refs:
                mp = r.module_path
                if mp is None:
                    continue
                rel = os.path.relpath(str(mp), repo)
                if rel == deffile and r.line == def_line:
                    continue  # the definition itself
                found.add((rel, r.line))
        return found


def _locate_py_def(deffile: str, name: str) -> int | None:
    """1-based line of the `def`/`class NAME` in a Python file, or None."""
    try:
        lines = Path(deffile).read_text(errors="replace").splitlines()
    except OSError:
        return None
    pat = re.compile(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b")
    for i, ln in enumerate(lines, 1):
        if pat.match(ln):
            return i
    return None


# --- Dead code: added defs/exports with zero external references -------------


def scan_dead_code(
    hits: Hits, diff_text: str, langs: list[Lang], jr: JediRefs | None = None
) -> None:
    for lang in langs:
        dc = lang.dead_code
        if dc is None:
            continue
        defs: list[tuple[str, str]] = []  # (name, file)
        current: str | None = None
        for line in diff_text.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
            elif (
                current
                and Path(current).suffix in lang.extensions
                and (m := dc.def_re.match(line))
            ):
                name = m.group(1)
                if not (dc.skip and dc.skip.match(name)):
                    defs.append((name, current))

        for name, deffile in defs:
            jrefs = None
            if jr is not None and lang.name == "py":
                def_line = _locate_py_def(deffile, name)
                if def_line is not None:
                    jrefs = jr.references(deffile, name, def_line)

            if jrefs is not None:
                external = {r for r in jrefs if r[0] != deffile}
                if not jrefs:
                    hits.add("DEAD-CODE", deffile, 0, f"`{name}` has no references at all")
                elif not external:
                    hits.add(
                        "DEAD-CODE",
                        deffile,
                        0,
                        f"`{name}` referenced only inside its own file ({len(jrefs)} refs) — verify not orphaned",
                    )
                continue

            out = run(["git", "grep", "-n", "-I", rf"\b{name}\b", "--", dc.grep_glob])
            refs = [
                ln
                for ln in out.stdout.splitlines()
                if not re.search(rf"\b(def|class|function|const)\s+{name}\b", ln)
            ]
            external = [ln for ln in refs if not ln.startswith(deffile + ":")]
            if not refs:
                hits.add("DEAD-CODE", deffile, 0, f"`{name}` has no references at all")
            elif not external:
                hits.add(
                    "DEAD-CODE",
                    deffile,
                    0,
                    f"`{name}` referenced only inside its own file ({len(refs)} refs) — verify not orphaned",
                )


# --- Review units: which routines the agent must read fully ------------------
#
# Sink hits find anticipated defect classes; the unit list is the coverage floor
# that also surfaces the unanticipated (intent) ones — every routine the diff
# touches. Extraction is per-language (Python via stdlib ast, TypeScript via
# tree-sitter, best-effort) but reduces to one language-neutral FuncInfo model so
# the sizing/promotion decision below stays shared.


@dataclass
class FuncInfo:
    name: str
    start: int  # first line to read (includes decorators)
    lineno: int  # def/signature line, used for size and containment
    end: int
    blocks: list[tuple[int, int]]  # compound-statement block spans
    callees: list[str]


@dataclass
class Unit:
    kind: str  # func | block | module
    file: str
    start: int
    end: int
    info: str


def _callers(
    name: str,
    deffile: str,
    glob: str,
    limit: int,
    jr: JediRefs | None = None,
    def_line: int | None = None,
) -> list[str]:
    if jr is not None and def_line is not None and glob == "*.py":
        jrefs = jr.references(deffile, name, def_line)
        if jrefs is not None:
            files: list[str] = []
            for rel, _ln in sorted(jrefs):
                if rel == deffile or rel in files:
                    continue
                files.append(rel)
                if len(files) >= limit:
                    break
            return files
    out = run(["git", "grep", "-n", "-I", rf"\b{name}\b", "--", glob])
    files: list[str] = []
    for ln in out.stdout.splitlines():
        path = ln.split(":", 1)[0]
        if path == deffile or re.search(rf"\b(def|class|function|const)\s+{name}\b", ln):
            continue
        if path not in files:
            files.append(path)
        if len(files) >= limit:
            break
    return files


_FUNCS_CACHE: dict[str, list[FuncInfo] | None] = {}


def _enclosing_func(path: str, line: int) -> FuncInfo | None:
    """The innermost function around `line` in `path`, parsed once per file."""
    if path not in _FUNCS_CACHE:
        try:
            source = Path(path).read_text(errors="replace")
        except OSError:
            source = None
        if source is None:
            _FUNCS_CACHE[path] = None
        elif path.endswith(".py"):
            _FUNCS_CACHE[path] = py_extract_funcs(source)
        elif path.endswith((".ts", ".tsx")) and _ts_parser("typescript") is not None:
            _FUNCS_CACHE[path] = ts_extract_funcs(source, path.endswith(".tsx"))
        else:
            _FUNCS_CACHE[path] = None
    best = None
    for fi in _FUNCS_CACHE[path] or []:
        if fi.lineno <= line <= fi.end and (best is None or fi.lineno > best.lineno):
            best = fi
    return best


def _second_hop(name: str, deffile: str, glob: str, limit: int) -> list[str]:
    """Callers of the functions that call `name`, as `file(via caller)`.

    One hop shows who calls the changed function; a changed return shape, raised
    exception or precondition often breaks the caller's caller, which handled the
    old behaviour without knowing it.
    """
    out = run(["git", "grep", "-n", "-I", rf"\b{name}\b", "--", glob])
    seen: list[str] = []
    for ln in out.stdout.splitlines():
        path, no, text = (ln.split(":", 2) + ["", ""])[:3]
        if path == deffile or not no.isdigit() or re.search(rf"\b(def|class|function|const)\s+{name}\b", text):
            continue
        if _is_test_path(path):
            continue
        fi = _enclosing_func(path, int(no))
        # A private caller is called from its own module, which the reviewer reads anyway;
        # a common name would match every unrelated function of that name.
        if fi is None or fi.name == name or fi.name.startswith("_") or (COMMON_NAMES and COMMON_NAMES.match(fi.name)):
            continue
        for p2 in _callers(fi.name, path, glob, limit):
            entry = f"{p2}(via {fi.name})"
            if entry not in seen:
                seen.append(entry)
        if len(seen) >= limit * 2:
            break
    return seen[: limit * 2]


PY_BLOCK_NODES = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.AsyncFor, ast.AsyncWith)


def py_extract_funcs(source: str) -> list[FuncInfo] | None:
    """Python function units via stdlib ast. None if the file does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    funcs: list[FuncInfo] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        decos = [d.lineno for d in node.decorator_list]
        blocks = [
            (n.lineno, n.end_lineno or n.lineno)
            for n in ast.walk(node)
            if isinstance(n, PY_BLOCK_NODES)
        ]
        callees: set[str] = set()
        for c in ast.walk(node):
            if isinstance(c, ast.Call):
                fn = c.func
                if isinstance(fn, ast.Name):
                    callees.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    callees.add(fn.attr)
        funcs.append(
            FuncInfo(node.name, min([node.lineno, *decos]), node.lineno, end, blocks, sorted(callees))
        )
    return funcs


_TS_PARSERS: dict[str, object] = {}


def _ts_parser(kind: str):
    """Cached tree-sitter parser for 'typescript'/'tsx'; None if lib unavailable."""
    if kind not in _TS_PARSERS:
        try:
            import tree_sitter_typescript as tst
            from tree_sitter import Language, Parser

            grammar = tst.language_tsx if kind == "tsx" else tst.language_typescript
            _TS_PARSERS[kind] = Parser(Language(grammar()))
        except Exception:
            _TS_PARSERS[kind] = None
    return _TS_PARSERS[kind]


TS_FUNC_TYPES = {
    "function_declaration",
    "generator_function_declaration",
    "method_definition",
    "arrow_function",
    "function_expression",
}
TS_BLOCK_TYPES = {
    "if_statement",
    "for_statement",
    "for_in_statement",
    "while_statement",
    "do_statement",
    "try_statement",
    "switch_statement",
}


def _ts_end_line(node) -> int:
    # tree-sitter end_point is exclusive; column 0 means the node ended on the prior line.
    return node.end_point[0] if node.end_point[1] == 0 else node.end_point[0] + 1


def ts_extract_funcs(source: str, is_tsx: bool) -> list[FuncInfo] | None:
    """TypeScript function units via tree-sitter. None if the parser is unavailable."""
    parser = _ts_parser("tsx" if is_tsx else "typescript")
    if parser is None:
        return None
    data = source.encode(errors="replace")
    try:
        tree = parser.parse(data)
    except Exception:
        return None

    def text(n) -> str:
        return data[n.start_byte : n.end_byte].decode(errors="replace")

    def name_of(n) -> str:
        nm = n.child_by_field_name("name")
        if nm is not None:
            return text(nm)
        p = n.parent
        while p is not None:
            if p.type == "variable_declarator":
                key = p.child_by_field_name("name")
                return text(key) if key is not None else "<anon>"
            if p.type in ("public_field_definition", "pair", "property_signature"):
                key = p.child_by_field_name("name") or p.child_by_field_name("key")
                return text(key) if key is not None else "<anon>"
            if p.type in TS_FUNC_TYPES:
                break
            p = p.parent
        return "<anon>"

    def deco_start(n) -> int:
        start = n.start_point[0] + 1
        ps = n.prev_sibling
        while ps is not None and ps.type == "decorator":
            start = ps.start_point[0] + 1
            ps = ps.prev_sibling
        return start

    funcs: list[FuncInfo] = []

    def visit(node) -> None:
        if node.type in TS_FUNC_TYPES:
            lineno = node.start_point[0] + 1
            end = _ts_end_line(node)
            blocks: list[tuple[int, int]] = []
            callees: set[str] = set()

            def walk_body(n) -> None:
                if n.type in TS_BLOCK_TYPES:
                    blocks.append((n.start_point[0] + 1, _ts_end_line(n)))
                if n.type == "call_expression":
                    fn = n.child_by_field_name("function")
                    if fn is not None and fn.type == "identifier":
                        callees.add(text(fn))
                    elif fn is not None and fn.type == "member_expression":
                        prop = fn.child_by_field_name("property")
                        if prop is not None:
                            callees.add(text(prop))
                for ch in n.children:
                    walk_body(ch)

            walk_body(node)
            funcs.append(FuncInfo(name_of(node), deco_start(node), lineno, end, blocks, sorted(callees)))
        for ch in node.children:
            visit(ch)

    visit(tree.root_node)
    return funcs


def _units_for_file(
    f: str,
    rngs: list[tuple[int, int]],
    funcs: list[FuncInfo],
    glob: str,
    cfg: UnitConfig,
    jr: JediRefs | None = None,
) -> list[Unit]:
    units: list[Unit] = []
    by_func: dict[int, tuple[FuncInfo, list[tuple[int, int]]]] = {}
    for hs, he in rngs:
        enc = None
        for fi in funcs:
            if fi.lineno <= hs and fi.end >= he and (enc is None or fi.lineno > enc.lineno):
                enc = fi
        if enc is None:
            units.append(Unit("module", f, hs, he, "-"))
        else:
            by_func.setdefault(enc.lineno, (enc, []))[1].append((hs, he))

    for _, (fi, hunks) in sorted(by_func.items()):
        size = fi.end - fi.lineno + 1
        added = sum(b - a + 1 for a, b in hunks)
        coverage = added / size if size else 1.0
        callees = fi.callees[:8]
        callers = _callers(fi.name, f, glob, cfg.callers_max, jr, fi.lineno)
        info = f"callees={','.join(callees) or '-'} callers={','.join(callers) or '-'}"
        if cfg.caller_depth >= 2:
            info += f" callers2={','.join(_second_hop(fi.name, f, glob, cfg.callers_max)) or '-'}"
        blocks = sorted({_enclosing_block(fi, hs, he) for hs, he in hunks})
        if (
            size <= cfg.size_threshold
            or coverage >= cfg.coverage_promote
            or len(blocks) > cfg.max_blocks
        ):
            units.append(Unit("func", f, fi.start, fi.end, info))
        else:
            for bs, be in blocks:
                units.append(Unit("block", f, bs, be, f"(oversized-fn {fi.name} {size}L) {info}"))
    return units


def _enclosing_block(fi: FuncInfo, hs: int, he: int) -> tuple[int, int]:
    """Smallest compound-statement block in the function containing the hunk."""
    best = (fi.lineno, fi.end)
    span = best[1] - best[0]
    for bs, be in fi.blocks:
        if bs <= hs and be >= he and (be - bs) < span:
            best, span = (bs, be), be - bs
    return best


def scan_units(
    ranges: dict[str, list[tuple[int, int]]],
    by_lang: dict[str, list[str]],
    cfg: UnitConfig,
    jr: JediRefs | None = None,
) -> tuple[list[Unit], list[str]]:
    """Enumerate the routines the diff touches. Returns (units, warnings).

    Small functions are read whole; an oversized function touched only sparsely is
    read as each hunk's enclosing block instead (and flagged). It is promoted back to
    a whole read when the diff covers a large fraction of it, or when it would
    otherwise fragment into more than ``max_blocks`` slices (many tiny reads are worse
    than one whole read).
    """
    units: list[Unit] = []
    warnings: list[str] = []
    for f in by_lang.get("py", []):
        try:
            source = Path(f).read_text(errors="replace")
        except OSError:
            continue
        funcs = py_extract_funcs(source)
        if funcs is not None:
            units += _units_for_file(f, ranges.get(f, []), funcs, "*.py", cfg, jr)

    ts_files = by_lang.get("ts", [])
    if ts_files and _ts_parser("typescript") is None:
        warnings.append("TS units skipped — tree-sitter not available")
    else:
        for f in ts_files:
            try:
                source = Path(f).read_text(errors="replace")
            except OSError:
                continue
            funcs = ts_extract_funcs(source, f.endswith(".tsx"))
            if funcs is not None:
                units += _units_for_file(f, ranges.get(f, []), funcs, "*.ts*", cfg)
    return units, warnings


# --- External linter lanes (ruff / eslint / ...) ----------------------------

RUFF_LINE_RE = re.compile(r"^(.+?):(\d+):\d+:\s+(\w+)\s+(.*)$")


def resolve_cmd(command: list[str], runner: str | None) -> list[str] | None:
    if shutil.which(command[0]):
        return command
    if runner and shutil.which(runner):
        return [runner, "run", *command]
    return None


def map_code(code: str, code_map: dict[str, str]) -> str | None:
    # Longest key first so a specific ruleId wins over a prefix.
    for key in sorted(code_map, key=len, reverse=True):
        if code == key or code.startswith(key):
            return code_map[key]
    return None


def scan_linter(
    hits: Hits,
    files: list[str],
    ranges: dict[str, list[tuple[int, int]]],
    linter: Linter,
) -> str:
    """Return 'ok', 'none' (no files), or 'unavailable'."""
    if not files:
        return "none"
    cmd = resolve_cmd(linter.command, linter.runner)
    if cmd is None:
        return "unavailable"
    out = run([*cmd, *files])

    if linter.parser == "regex-line":
        for line in out.stdout.splitlines():
            m = RUFF_LINE_RE.match(line)
            if not m:
                continue
            file, lineno, code, msg = m.group(1), int(m.group(2)), m.group(3), m.group(4)
            file = str(Path(file))
            cat = map_code(code, linter.code_map)
            if cat and in_ranges(lineno, ranges.get(file, [])):
                hits.add(cat, file, lineno, f"{code} {msg}")
        return "ok"

    if linter.parser == "eslint-json":
        try:
            data = json.loads(out.stdout or "[]")
        except json.JSONDecodeError:
            return "unavailable"
        for entry in data:
            file = os.path.relpath(entry.get("filePath", ""))
            rngs = ranges.get(file, [])
            for msg in entry.get("messages", []):
                rid = msg.get("ruleId")
                if not rid:
                    continue
                cat = map_code(rid, linter.code_map)
                lineno = msg.get("line", 0)
                if cat and in_ranges(lineno, rngs):
                    hits.add(cat, file, lineno, f"{rid} {msg.get('message', '')}")
        return "ok"

    return "unavailable"


# --- File -> language dispatch ----------------------------------------------


def _has_sh_shebang(f: str) -> bool:
    p = Path(f)
    if p.suffix in (".py", ".md", ".toml", ".json", ".yaml", ".yml"):
        return False
    try:
        with p.open(errors="replace") as fh:
            first = fh.readline()
        return first.startswith("#!") and ("sh" in first or "bash" in first)
    except OSError:
        return False


def lang_for_file(f: str, langs: list[Lang]) -> Lang | None:
    suf = Path(f).suffix
    for lang in langs:
        if suf in lang.extensions:
            return lang
    for lang in langs:
        if lang.match_shebang and _has_sh_shebang(f):
            return lang
    return None


def scanned_categories(
    langs: list[Lang], active: set[str], ran_linters: set[str]
) -> list[str]:
    """Categories we actually ran a scanner for — only languages with >=1 file.

    Linter categories count only for linters that actually executed; a skipped
    local-only linter must not surface its categories as "swept, none".
    """
    seen: list[str] = []

    def add(c: str) -> None:
        if c not in seen:
            seen.append(c)

    for lang in langs:
        if lang.name not in active:
            continue
        for p in lang.patterns:
            add(p.category)
        for s in lang.special:
            cats = SPECIAL_SCANNERS[s][1]
            for c in cats if isinstance(cats, tuple) else (cats,):
                add(c)
        if lang.linter and lang.name in ran_linters:
            for v in lang.linter.code_map.values():
                add(v)
        if lang.dead_code:
            add("DEAD-CODE")
            for c in DUP_CATEGORIES:
                add(c)
    return seen


# --- Duplicate changes --------------------------------------------------------
#
# A change that repeats itself, or repeats code the repository already has, is a
# review question the line scanners cannot ask: each copy looks fine on its own.

_TRIVIAL = re.compile(r"^\s*([)}\]]+[,;]?|else:|try:|finally:|pass|return|break|continue|\*/|/\*\*|#.*|//.*)?\s*$")
_NOT_LOGIC = re.compile(r"^\s*(import\b|from\s+\S+\s+import\b|@|#|//|\*|(async\s+)?def\b|class\b|export\s+(async\s+)?(function|const|class)\b)")


def _is_test_path(path: str) -> bool:
    return bool(TEST_PATHS and TEST_PATHS.search(path))


def _added_lines(ranges: dict[str, list[tuple[int, int]]], files: list[str]) -> dict[str, dict[int, str]]:
    out: dict[str, dict[int, str]] = {}
    for f in files:
        if _is_test_path(f):
            continue
        try:
            lines = Path(f).read_text(errors="replace").splitlines()
        except OSError:
            continue
        out[f] = {n: lines[n - 1] for a, b in ranges[f] for n in range(a, min(b, len(lines)) + 1)}
    return out


def _dup_in_diff(hits: Hits, added: dict[str, dict[int, str]]) -> None:
    w = DUP.window
    seen: dict[tuple[str, ...], tuple[str, int]] = {}
    found: dict[str, set[int]] = {}
    for f, lines in added.items():
        for n in sorted(lines):
            window = [lines.get(n + k) for k in range(w)]
            if any(t is None for t in window):
                continue
            key = tuple(_norm(t) for t in window)  # type: ignore[arg-type]
            if sum(1 for t in key if len(t) >= 20 and not _TRIVIAL.match(t)) < 2:
                continue
            if key in seen:
                pf, pn = seen[key]
                if pf != f or abs(pn - n) >= w:
                    if (n - 1) not in found.get(f, set()):
                        hits.add("DUP-IN-DIFF", f, n, f"{w}+ lines also added at {pf}:{pn}")
                    found.setdefault(f, set()).add(n)
            else:
                seen[key] = (f, n)


def _def_line(path: str, name: str) -> int:
    pat = re.compile(rf"^(export\s+)?(async\s+)?(def|class|function|const)\s+{re.escape(name)}\b")
    try:
        for n, line in enumerate(Path(path).read_text(errors="replace").splitlines(), start=1):
            if pat.match(line):
                return n
    except OSError:
        pass
    return 0


def _dup_name(hits: Hits, diff_text: str, langs: list[Lang]) -> None:
    for lang in langs:
        dc = lang.dead_code
        if dc is None:
            continue
        current: str | None = None
        for line in diff_text.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
                continue
            if not (current and Path(current).suffix in lang.extensions) or _is_test_path(current):
                continue
            m = dc.def_re.match(line)
            # Module level only: two methods may share a name on purpose.
            if not m or line[1:2].isspace():
                continue
            name = m.group(1)
            if (dc.skip and dc.skip.match(name)) or name.startswith("test"):
                continue
            out = run(["git", "grep", "-n", "-I", "-E", rf"^(export\s+)?(async\s+)?(def|class|function|const)\s+{name}\b", "--", dc.grep_glob])
            others = [ln for ln in out.stdout.splitlines()
                      if not ln.startswith(f"{current}:") and not _is_test_path(ln.split(":", 1)[0])]
            if others:
                where = ", ".join(":".join(o.split(":", 2)[:2]) for o in others[:3])
                hits.add("DUP-NAME", current, _def_line(current, name), f"`{name}` is already defined at {where}")


def _dup_existing(hits: Hits, added: dict[str, dict[int, str]]) -> str | None:
    greps = 0
    for f, lines in added.items():
        glob = f"*{Path(f).suffix}" if Path(f).suffix else None
        if glob is None:
            continue
        skip_until = 0
        for n in sorted(lines):
            text, nxt = lines[n].strip(), lines.get(n + 1)
            if n <= skip_until or nxt is None or len(_norm(text)) < DUP.min_line or _NOT_LOGIC.match(text):
                continue
            if len(_norm(nxt)) < 20:
                continue
            if greps >= DUP.max_greps:
                return f"DUP-EXISTING stopped after {DUP.max_greps} lookups — read the rest of the diff for copied code"
            greps += 1
            out = run(["git", "grep", "-n", "-I", "-F", "-e", text, "--", glob])
            for hit in out.stdout.splitlines():
                path, ln, _ = hit.split(":", 2)
                ln_no = int(ln)
                if _is_test_path(path) or (path in added and ln_no in added[path]):
                    continue  # the hit is this change's own added code
                try:
                    other = Path(path).read_text(errors="replace").splitlines()
                except OSError:
                    continue
                if ln_no < len(other) and _norm(other[ln_no]) == _norm(nxt):
                    hits.add("DUP-EXISTING", f, n, f"also at {path}:{ln_no} (2+ consecutive lines)")
                    skip_until = n + DUP.window
                    break
    return None


def scan_duplicates(hits: Hits, diff_text: str, ranges: dict[str, list[tuple[int, int]]],
                    files: list[str], langs: list[Lang]) -> list[str]:
    """DUP-IN-DIFF, DUP-NAME and DUP-EXISTING; returns warnings."""
    added = _added_lines(ranges, files)
    _dup_in_diff(hits, added)
    _dup_name(hits, diff_text, langs)
    warn = _dup_existing(hits, added)
    return [warn] if warn else []


# --- Main --------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--worktree", action="store_true")
    ap.add_argument("--base")
    ap.add_argument(
        "--caller-depth",
        type=int,
        help="2 also lists each caller's callers in UNITS (round-2 deep pass); default from patterns.toml",
    )
    ap.add_argument(
        "--linter",
        action="store_true",
        help="Force all linters, including local-only ones (eslint), even if not auto-detected.",
    )
    args = ap.parse_args()

    langs, manual_categories, unit_cfg, jedi_cfg = load_config()
    if args.caller_depth:
        unit_cfg.caller_depth = args.caller_depth
    jr = JediRefs(jedi_cfg)

    mode, base = pick_mode(args)
    dargs = diff_args(mode, base)

    diff_text = run(["git", "diff", "--diff-filter=ACMR", "-U0", *dargs]).stdout
    if not diff_text.strip():
        print(f"# mode={mode} — no changes found")
        return 0

    ranges = parse_added_ranges(diff_text)
    hits = Hits()

    # Group changed files by language.
    by_lang: dict[str, list[str]] = {lang.name: [] for lang in langs}
    for f in ranges:
        if not Path(f).is_file():
            continue
        lang = lang_for_file(f, langs)
        if lang is not None:
            by_lang[lang.name].append(f)

    lang_by_name = {lang.name: lang for lang in langs}
    linter_warnings: list[str] = []
    ran_linters: set[str] = set()

    for name, files in by_lang.items():
        lang = lang_by_name[name]
        for f in files:
            source = Path(f).read_text(errors="replace")
            lines = source.splitlines()
            scan_patterns(hits, f, lines, ranges[f], lang.patterns, lang.skip_lines)
            for sname in lang.special:
                SPECIAL_SCANNERS[sname][0](hits, f, source, ranges[f])
        if lang.linter and files:
            lin = lang.linter
            cats = ", ".join(dict.fromkeys(lin.code_map.values()))
            # local-only linters (eslint) need the tree's node_modules; skip silently
            # when not present unless --linter forces the attempt.
            if lin.local_only and not args.linter and not (lin.local_bin and Path(lin.local_bin).exists()):
                linter_warnings.append(
                    f"{lin.command[0]} ({name}) — skipped (local-only; pass --linter to run): {cats}"
                )
            else:
                status = scan_linter(hits, files, ranges, lin)
                if status == "unavailable":
                    linter_warnings.append(f"{lin.command[0]} ({name}) — not scanned: {cats}")
                else:
                    ran_linters.add(name)

    scan_dead_code(hits, diff_text, langs, jr)
    code_files = [f for fs in by_lang.values() for f in fs]
    linter_warnings.extend(scan_duplicates(hits, diff_text, ranges, code_files, langs))

    # --- Report ---
    counts = ", ".join(f"{len(fs)} {n}" for n, fs in by_lang.items())
    print(f"# mode={mode}" + (f" base={base}" if base else ""))
    print(f"# files: {counts}, {len(ranges)} total changed")
    if jr.enabled:
        print(f"# jedi: type-aware refs active ({jr.reason}) for DEAD-CODE/callers")
    elif jedi_cfg.enabled:
        print(f"# WARNING: jedi ref lane inactive ({jr.reason}) — DEAD-CODE/callers via git grep")
    for w in linter_warnings:
        print(f"# WARNING: {w}")

    active = {name for name, fs in by_lang.items() if fs}
    scanned = scanned_categories(langs, active, ran_linters)

    print("== HITS ==")
    any_hit = False
    for cat in scanned:
        for row in hits.by_cat.get(cat, []):
            print(row)
            any_hit = True
        if cat in hits.overflow:
            print(
                f"{cat}\t-\t(truncated at {MAX_HITS_PER_CATEGORY} hits — pattern too broad, narrow manually)"
            )
    if not any_hit:
        print("(none)")

    swept_none = [c for c in scanned if c not in hits.by_cat]
    print("== SWEPT-NONE ==")
    print(", ".join(swept_none) if swept_none else "(none)")

    print("== MANUAL (judge from diff) ==")
    print(", ".join(manual_categories))

    full_diff = run(["git", "diff", "--diff-filter=ACMRD", "-U0", *dargs]).stdout
    removed_rows = scan_removed(full_diff, langs)
    print("== REMOVED (name what each enforced; where does the new code re-establish it?) ==")
    print("\n".join(removed_rows) if removed_rows else "(none)")

    units, unit_warnings = scan_units(ranges, by_lang, unit_cfg, jr)
    for w in unit_warnings:
        print(f"# WARNING: {w}")
    if units:
        print("== UNITS (read fully) ==")
        for u in units:
            print(f"{u.kind}\t{u.file}:{u.start}-{u.end}\t{u.info}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

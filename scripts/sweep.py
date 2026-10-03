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

Output (compact, TSV-ish):
    == HITS ==
    CATEGORY\tfile:line\tsnippet
    == SWEPT-NONE ==
    categories that were scanned and had zero hits
    == MANUAL (judge from diff) ==
    categories a script cannot detect; the agent judges them from the diff
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


@dataclass
class UnitConfig:
    size_threshold: int = 120
    coverage_promote: float = 0.4
    callers_max: int = 5
    max_blocks: int = 3


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
    )
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
            )
        )
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
SPECIAL_SCANNERS: dict[str, tuple] = {
    "loop_isolation": (scan_loop_isolation, "LOOP-ISOLATION"),
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
            add(SPECIAL_SCANNERS[s][1])
        if lang.linter and lang.name in ran_linters:
            for v in lang.linter.code_map.values():
                add(v)
        if lang.dead_code:
            add("DEAD-CODE")
    return seen


# --- Main --------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--worktree", action="store_true")
    ap.add_argument("--base")
    ap.add_argument(
        "--linter",
        action="store_true",
        help="Force all linters, including local-only ones (eslint), even if not auto-detected.",
    )
    args = ap.parse_args()

    langs, manual_categories, unit_cfg, jedi_cfg = load_config()
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

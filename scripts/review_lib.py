"""
review_lib — deterministic core for pr-inline-review.

The whole point of this module: keep every error-prone, mechanical part of
posting a GitHub inline review OUT of the LLM prompt and IN tested code.

Responsibilities (all pure functions, no network, no file I/O):
  1. parse_diff()            unified diff -> per-file line/side maps
  2. resolve_anchor()        a finding -> validated (start_line/start_side/line/side)
  3. build_comment_body()    a finding -> final markdown (severity prefix + ```suggestion)
  4. build_review_payload()  findings + diff -> GitHub reviews API payload (+ skipped)
  5. expand_wiki_links()     [[Page]] -> a real wiki URL (GitHub only resolves the
                             double-bracket form inside a wiki, not in a PR comment)
  6. state_marker() / parse_state_marker() / fingerprint()
                             hidden review state, so a re-review knows which head it
                             saw and which findings it raised
  7. event_for_author()      GitHub rejects REQUEST_CHANGES on your own PR
  8. review_round() / apply_round_policy()
                             which round this review is, and which findings it may
                             still post: no repeats, and a MEDIUM severity floor
                             from round 2 on so a deeper review still converges

The GitHub "Create a review" API accepts line-based coordinates
(path, line, side, start_line, start_side) inside comments[], so a single
batch call covers single-line, multi-line, and cross-side suggestions.
No `position` arithmetic anywhere — that was the old failure source.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# diff model
# --------------------------------------------------------------------------- #

@dataclass
class Line:
    """One commentable line in a diff."""
    kind: str          # "add" | "del" | "ctx"
    text: str          # line content without the +/-/space marker
    hunk: int          # hunk index within the file (0-based)
    old_no: Optional[int]  # line number in the old file (del/ctx)
    new_no: Optional[int]  # line number in the new file (add/ctx)


@dataclass
class FileDiff:
    path: str
    lines: list = field(default_factory=list)          # list[Line]
    right: dict = field(default_factory=dict)          # new_no -> Line (add|ctx)
    left: dict = field(default_factory=dict)           # old_no -> Line (del|ctx)

    def right_hunk(self, new_no: int) -> Optional[int]:
        ln = self.right.get(new_no)
        return ln.hunk if ln else None

    def left_hunk(self, old_no: int) -> Optional[int]:
        ln = self.left.get(old_no)
        return ln.hunk if ln else None


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(diff_text: str) -> dict:
    """Parse a unified diff (e.g. `gh pr diff`) into {path: FileDiff}."""
    files: dict = {}
    cur: Optional[FileDiff] = None
    hunk = -1
    old_no = new_no = 0

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git"):
            cur = None
            hunk = -1
        elif raw.startswith("+++ b/"):
            path = raw[6:].strip()
            cur = files.get(path) or FileDiff(path=path)
            files[path] = cur
            hunk = -1
        elif raw.startswith("+++ ") or raw.startswith("--- "):
            # /dev/null or old-file header; ignore
            continue
        elif raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            if not m or cur is None:
                continue
            hunk += 1
            old_no = int(m.group(1))
            new_no = int(m.group(3))
        elif cur is not None and hunk >= 0 and raw[:1] in ("+", "-", " "):
            marker, body = raw[:1], raw[1:]
            if marker == "+":
                ln = Line("add", body, hunk, None, new_no)
                cur.lines.append(ln)
                cur.right[new_no] = ln
                new_no += 1
            elif marker == "-":
                ln = Line("del", body, hunk, old_no, None)
                cur.lines.append(ln)
                cur.left[old_no] = ln
                old_no += 1
            else:  # context
                ln = Line("ctx", body, hunk, old_no, new_no)
                cur.lines.append(ln)
                cur.right[new_no] = ln
                cur.left[old_no] = ln
                old_no += 1
                new_no += 1
    return files


# --------------------------------------------------------------------------- #
# anchor resolution  (the part that used to be LLM mental math)
# --------------------------------------------------------------------------- #

@dataclass
class Anchor:
    path: str
    line: int
    side: str                       # "RIGHT" | "LEFT"
    start_line: Optional[int] = None
    start_side: Optional[str] = None
    warnings: list = field(default_factory=list)
    valid: bool = True

    def to_comment_fields(self) -> dict:
        d = {"path": self.path, "line": self.line, "side": self.side}
        if self.start_line is not None:
            d["start_line"] = self.start_line
            d["start_side"] = self.start_side or self.side
        return d


def _leading_ws(s: str) -> str:
    return s[: len(s) - len(s.lstrip(" \t"))]


# Beyond this distance a snapped comment lands somewhere the model never looked
# at — that is a wrong location, not a delivery. Placement is the model's
# judgement; the script only corrects near-misses and rejects the rest.
MAX_SNAP_DISTANCE = 10


def resolve_anchor(fd: FileDiff, finding: dict) -> Anchor:
    """
    Turn a finding's requested location into validated GitHub coordinates.

    finding keys used:
      path (str, required)
      line (int, required)  -- primary anchor, new-file line number by default
      side (str, optional)  -- "RIGHT" (default) | "LEFT"
      start_line (int, optional)  -- for multi-line ranges
      start_side (str, optional)

    Guarantees the returned Anchor is part of the diff (or marks it invalid),
    which is what prevents the 422 "line must be part of the diff" failures.
    """
    path = finding["path"]
    side = (finding.get("side") or "RIGHT").upper()
    line = int(finding["line"])
    warnings: list = []

    if side == "RIGHT":
        pool, hunk_of = fd.right, fd.right_hunk
    else:
        pool, hunk_of = fd.left, fd.left_hunk

    if line not in pool:
        # Anchor not directly commentable. Correct near-misses by snapping to
        # the nearest commentable line; anything farther is a mislocated
        # finding, so reject it and let the model re-anchor.
        candidates = sorted(pool)
        if not candidates:
            return Anchor(path, line, side, warnings=[
                f"{path}: no commentable {side} lines in diff"], valid=False)
        nearest = min(candidates, key=lambda x: abs(x - line))
        if abs(nearest - line) > MAX_SNAP_DISTANCE:
            return Anchor(path, line, side, warnings=[
                f"{path}:{line} not in diff on {side}; nearest commentable "
                f"line {nearest} is >{MAX_SNAP_DISTANCE} lines away — "
                "re-anchor the finding"], valid=False)
        warnings.append(
            f"{path}:{line} not in diff on {side}; snapped to {nearest}")
        line = nearest

    anchor = Anchor(path, line, side, warnings=warnings)

    # optional multi-line range
    if finding.get("start_line") is not None:
        start_line = int(finding["start_line"])
        start_side = (finding.get("start_side") or side).upper()
        s_pool = fd.right if start_side == "RIGHT" else fd.left
        if start_line not in s_pool:
            warnings.append(
                f"{path}: start_line {start_line} not in diff on {start_side}; "
                "dropping range, using single-line anchor")
        elif start_side == side and start_line > line:
            warnings.append(
                f"{path}: start_line {start_line} > line {line}; dropping range")
        elif start_side == side and hunk_of(start_line) != hunk_of(line):
            warnings.append(
                f"{path}: range {start_line}-{line} spans multiple hunks; "
                "dropping range")
        else:
            anchor.start_line = start_line
            anchor.start_side = start_side

    return anchor


# --------------------------------------------------------------------------- #
# body + suggestion formatting  (model never writes ```suggestion itself)
# --------------------------------------------------------------------------- #

_SEV = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}

# Gemini-style severity badges (same visual convention as the cloud review bots).
_BADGE = {
    "CRITICAL": "![CRITICAL](https://www.gstatic.com/codereviewagent/critical.svg)",
    "HIGH": "![HIGH](https://www.gstatic.com/codereviewagent/high-priority.svg)",
    "MEDIUM": "![MEDIUM](https://www.gstatic.com/codereviewagent/medium-priority.svg)",
    "LOW": "![LOW](https://www.gstatic.com/codereviewagent/low-priority.svg)",
}


# --------------------------------------------------------------------------- #
# wiki links
# --------------------------------------------------------------------------- #

_WIKI_LINK = re.compile(r"\[\[([^\[\]\n|]+)(?:\|([^\[\]\n]+))?\]\]")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+[^`]*`+)")


def expand_wiki_links(text: str, base_url: str, known_pages: Optional[set] = None,
                      on_unknown=None) -> str:
    """Rewrite gollum ``[[Page]]`` / ``[[Label|Page]]`` into ordinary markdown links.

    GitHub resolves the double-bracket form only inside a wiki. In a PR review body or
    an inline comment it stays literal text, so the reference reads as noise and nobody
    can follow it. Expanding here lets a finding cite the wiki in the wiki's own syntax
    and still land as a working link.

    ``base_url`` is the wiki root (``https://github.com/OWNER/REPO/wiki/``). A page name
    is slugged the way GitHub does it — spaces become hyphens — and a ``#anchor`` is
    carried through. When ``known_pages`` is given, a link outside it is still expanded
    (a local clone may simply be stale) but ``on_unknown(page)`` is called so the caller
    can warn.

    Code fences and inline code spans are left untouched: ``\u0060[[Page]]\u0060`` in prose
    about this syntax must survive verbatim.
    """
    if not text or "[[" not in text:
        return text

    def _one(m: "re.Match") -> str:
        label = m.group(1).strip()
        page = (m.group(2) or m.group(1)).strip()
        name, _, anchor = page.partition("#")
        name = name.strip()
        if not name:
            return m.group(0)
        if known_pages is not None and name not in known_pages and on_unknown:
            on_unknown(name)
        url = base_url + urllib.parse.quote(name.replace(" ", "-"), safe="-._~/")
        if anchor:
            url += "#" + urllib.parse.quote(anchor.strip(), safe="-._~")
        return f"[{label}]({url})"

    out, fence = [], ""
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if m:
            tok = m.group(1)[0] * 3
            fence = "" if fence == tok else (fence or tok)
            out.append(line)
            continue
        if fence:
            out.append(line)
            continue
        # split() with a capturing group alternates prose/code, code at odd indices
        parts = _INLINE_CODE.split(line)
        out.append("".join(part if i % 2 else _WIKI_LINK.sub(_one, part)
                           for i, part in enumerate(parts)))
    return "\n".join(out)


def build_comment_body(finding: dict, fd: Optional[FileDiff] = None,
                       anchor: Optional[Anchor] = None) -> str:
    """
    Assemble the final comment markdown from structured fields.

    finding keys:
      severity (str)      CRITICAL | HIGH | MEDIUM | LOW
      category (str)      security | bug | regression | lifecycle |
                          type-safety | extensibility | pattern | style
      title (str)
      explanation (str)   markdown; NO suggestion fence
      suggestion (list[str], optional)  replacement lines; wrapped here
    """
    sev = str(finding.get("severity", "MEDIUM")).upper()
    if sev not in _SEV:
        sev = "MEDIUM"
    cat = finding.get("category", "bug")
    title = finding.get("title", "").strip()
    header = f"{_BADGE[sev]} **[{sev}] {cat}** — {title}"

    parts = [header]
    explanation = (finding.get("explanation") or "").strip()
    if explanation:
        parts.append(explanation)

    sugg = finding.get("suggestion")
    if sugg:
        lines = sugg.splitlines() if isinstance(sugg, str) else list(sugg)
        if anchor is not None and anchor.side == "LEFT":
            # a LEFT-anchored line no longer exists in the new file, so GitHub
            # renders the fence but the Apply button can never work
            anchor.warnings.append(
                f"{anchor.path}:{anchor.line} suggestion anchored on LEFT (deleted "
                "line) — not applyable; describe the fix in explanation instead")
        if (anchor is not None and anchor.start_line is not None
                and anchor.start_side == "RIGHT" and anchor.side == "RIGHT"):
            span = anchor.line - anchor.start_line + 1
            if len(lines) < span:
                # Apply replaces the WHOLE range with the suggestion; a shorter
                # suggestion silently deletes the unmentioned lines unless the
                # shrink is intentional
                anchor.warnings.append(
                    f"{anchor.path}:{anchor.start_line}-{anchor.line} range spans "
                    f"{span} lines but suggestion has {len(lines)} — kept lines "
                    "must be included unless deletion is intended")
        # indentation sanity check against the anchored line in the diff
        if fd is not None and anchor is not None and anchor.side == "RIGHT":
            target = fd.right.get(anchor.start_line or anchor.line)
            if target and lines:
                want, got = _leading_ws(target.text), _leading_ws(lines[0])
                if want != got and anchor is not None:
                    # Surface to the console (anchor.warnings), NOT into the posted
                    # comment body — do not silently rewrite. Apply button breaks on
                    # indent mismatch, so the model/caller should fix the suggestion.
                    anchor.warnings.append(
                        f"{anchor.path}:{anchor.start_line or anchor.line} suggestion "
                        f"indent mismatch (target {len(want)} ws, first line {len(got)} ws)")
        block = "```suggestion\n" + "\n".join(lines) + "\n```"
        parts.append(block)

    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# payload assembly
# --------------------------------------------------------------------------- #

def decide_event(findings: list) -> str:
    """Default event policy; callers may override (e.g. review_post --event)."""
    if any(str(f.get("severity", "")).upper() in ("CRITICAL", "HIGH") for f in findings):
        return "REQUEST_CHANGES"
    if findings:
        return "COMMENT"
    return "APPROVE"


def build_review_payload(findings: list, diffmap: dict, summary_body: str,
                         event: Optional[str] = None) -> tuple:
    """
    Returns (payload_dict, skipped_list).

    payload_dict is ready for POST /repos/{repo}/pulls/{n}/reviews.
    skipped_list holds findings that could not be anchored, with reasons —
    these are reported to the console, never silently dropped.
    """
    comments = []
    skipped = []
    all_warnings = []

    for f in findings:
        fd = diffmap.get(f["path"])
        if fd is None:
            skipped.append({**f, "_reason": f"{f['path']} not in diff"})
            continue
        anchor = resolve_anchor(fd, f)
        if not anchor.valid:
            all_warnings.extend(anchor.warnings)
            skipped.append({**f, "_reason": "; ".join(anchor.warnings)})
            continue
        # build_comment_body may append an indent warning to anchor.warnings,
        # so collect warnings AFTER it runs (and keep them out of the posted body).
        body = build_comment_body(f, fd, anchor)
        all_warnings.extend(anchor.warnings)
        body += "\n\n" + fingerprint_marker(f)
        comments.append({**anchor.to_comment_fields(), "body": body})

    # Event reflects the full verdict: a CRITICAL/HIGH still requests changes even
    # if its inline anchor was skipped (it is surfaced in the summary + console).
    payload = {
        "body": summary_body,
        "event": event or decide_event(findings),
        "comments": comments,
    }
    return payload, skipped, all_warnings


# --------------------------------------------------------------------------- #
# review state and posting guards
# --------------------------------------------------------------------------- #

_STATE_RE = re.compile(r"<!-- pr-inline-review:v1 (\{.*?\}) -->")
_FP_RE = re.compile(r"<!-- pr-inline-review:fp=([0-9a-f]{12}) -->")


def fingerprint(finding: dict) -> str:
    """Stable id of a finding across review rounds.

    Line numbers are left out on purpose: they move with every push, while
    path + category + title survive one. A retitled finding gets a new id, which
    only costs the re-review a manual match, never a wrong one.
    """
    title = " ".join(str(finding.get("title", "")).lower().split())
    key = "\x1f".join([str(finding.get("path", "")),
                       str(finding.get("category", "")).lower(), title])
    return hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def fingerprint_marker(finding: dict) -> str:
    return f"<!-- pr-inline-review:fp={fingerprint(finding)} -->"


def parse_fingerprint_marker(body: str) -> Optional[str]:
    m = _FP_RE.search(body or "")
    return m.group(1) if m else None


def state_marker(head_sha: str, findings: list, round_no: Optional[int] = None) -> str:
    """Hidden line for the review body: the head this round reviewed and its findings.

    The next round reads it back to diff only `sha..HEAD` and to give a verdict on
    each earlier finding instead of rediscovering them.
    """
    data: dict = {"sha": head_sha, "findings": sorted({fingerprint(f) for f in findings})}
    if round_no is not None:
        data["round"] = round_no
    return f"<!-- pr-inline-review:v1 {json.dumps(data, separators=(',', ':'))} -->"


def parse_state_marker(body: str) -> Optional[dict]:
    m = _STATE_RE.search(body or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) and "sha" in data else None


def event_for_author(event: str, own_pr: bool) -> tuple:
    """Returns (event, note). GitHub answers 422 to REQUEST_CHANGES on your own PR.

    Before this guard the batch failed, the fallback posted every finding as a loose
    comment, and the closing summary review failed the same way without a word.
    """
    if own_pr and event == "REQUEST_CHANGES":
        return "COMMENT", ("자기 PR이라 REQUEST_CHANGES 불가 — COMMENT로 게시 "
                           "(머지 차단 의도는 _summary에 명시할 것)")
    return event, None


# --------------------------------------------------------------------------- #
# review rounds
# --------------------------------------------------------------------------- #

_SEV_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def review_round(markers: list) -> int:
    """One past the rounds already on the PR.

    Markers written before rounds were recorded carry no `round`, so their count
    is the floor; a recorded round wins when it is higher (a review was redone).
    """
    recorded = [m.get("round") for m in markers if isinstance(m.get("round"), int)]
    return max([len(markers)] + recorded) + 1


def round_floor(round_no: int) -> Optional[str]:
    """The lowest severity a NEW finding may have in this round.

    Round 1 has none. Later rounds look deeper, and a deeper look always finds
    more nits; without a floor the review never converges. The floor stops at
    MEDIUM: a design or resource problem found in round 3 is no less worth a
    thread than one found in round 2, and the nits are already cut at LOW.
    """
    if round_no <= 1:
        return None
    return "MEDIUM"


def apply_round_policy(findings: list, round_no: int, prior_fps: set) -> tuple:
    """Returns (kept, dropped), dropped as (finding, reason).

    A finding already raised in an earlier round is answered in its own thread,
    not posted again. A new finding below the round's floor is dropped unless it
    says the increment since the last review introduced it
    (`introduced_by_increment: true`): fresh code gets a first review at any level.
    """
    floor = round_floor(round_no)
    kept, dropped = [], []
    for f in findings:
        if fingerprint(f) in prior_fps:
            dropped.append((f, "an earlier round raised it; answer in that thread"))
            continue
        sev = str(f.get("severity", "MEDIUM")).upper()
        if floor and _SEV_RANK.get(sev, 1) < _SEV_RANK[floor] \
                and not f.get("introduced_by_increment"):
            dropped.append((f, f"below the round-{round_no} floor ({floor})"))
            continue
        kept.append(f)
    return kept, dropped

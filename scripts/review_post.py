#!/usr/bin/env python
"""
review_post.py — post (or dry-run) a GitHub inline review from structured findings.

The LLM's only job is to produce a findings JSON file. This script does every
mechanical, error-prone step deterministically:

  diff fetch -> parse -> anchor/validate -> format -> single batch POST -> 422 fallback

Usage:
  python review_post.py --repo OWNER/REPO --pr 634 --findings findings.json
  python review_post.py --repo OWNER/REPO --pr 634 --findings findings.json --dry-run
  python review_post.py --pr 634 --findings findings.json          # repo auto-detected
  cat findings.json | python review_post.py --repo OWNER/REPO --pr 634 --findings -

findings.json schema (a list; summary is optional and may be first object with "_summary"):
  [
    {"_summary": "free markdown for the review body: overview, verdict prose,\n"
                 "non-blocking remarks. The script appends only the severity table."},
    {
      "path": "src/app/core/worker.py",
      "line": 196,                 # new-file line (primary anchor); required
      "start_line": 193,           # optional, multi-line range
      "side": "RIGHT",             # optional (RIGHT default | LEFT)
      "start_side": "RIGHT",       # optional
      "severity": "HIGH",          # CRITICAL | HIGH | MEDIUM | LOW
      "category": "type-safety",
      "title": "...",
      "explanation": "markdown, no suggestion fence",
      "suggestion": ["replacement", "lines"]   # optional; script wraps in ```suggestion
    }
  ]

`_summary`, `title` and `explanation` may cite the wiki in its own `[[Page]]` /
`[[Label|Page]]` syntax; GitHub only resolves that inside a wiki, so this script
rewrites it to https://github.com/OWNER/REPO/wiki/Page before posting (--no-wiki-links
to keep it literal, --wiki-base for a wiki hosted elsewhere).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile


# Force UTF-8 stdout/stderr so emoji/em-dash never crash on Windows cp949 etc.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_lib as R  # noqa: E402


# --------------------------------------------------------------------------- #
# gh helpers
# --------------------------------------------------------------------------- #

def _run(cmd: list, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        sys.stderr.write(f"$ {' '.join(cmd)}\n{p.stderr}\n")
        raise SystemExit(2)
    return p


def detect_repo(required: bool = True):
    p = _run(["gh", "repo", "view", "--json", "nameWithOwner",
              "--jq", ".nameWithOwner"], check=False)
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    if required:
        raise SystemExit("Could not detect repo; pass --repo OWNER/REPO.")
    return None


def fetch_diff(repo: str, pr: int) -> str:
    return _run(["gh", "pr", "diff", str(pr), "--repo", repo]).stdout


def fetch_head_sha(repo: str, pr: int) -> str:
    return _run(["gh", "pr", "view", str(pr), "--repo", repo,
                 "--json", "commits", "--jq", ".commits[-1].oid"]).stdout.strip()


# --------------------------------------------------------------------------- #
# posting
# --------------------------------------------------------------------------- #

def post_review(repo: str, pr: int, payload: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(payload, fh)
        tmp = fh.name
    try:
        p = _run(["gh", "api", f"repos/{repo}/pulls/{pr}/reviews",
                  "--method", "POST", "--input", tmp], check=False)
        return {"ok": p.returncode == 0, "stdout": p.stdout, "stderr": p.stderr}
    finally:
        os.unlink(tmp)


def post_single_comment(repo: str, pr: int, head_sha: str, c: dict) -> dict:
    """Fallback: post one comment on its own so one bad anchor can't sink the batch."""
    cmd = ["gh", "api", f"repos/{repo}/pulls/{pr}/comments", "--method", "POST",
           "--field", f"commit_id={head_sha}",
           "--field", f"path={c['path']}",
           "--field", f"line={c['line']}",
           "--field", f"side={c.get('side', 'RIGHT')}",
           "--field", f"body={c['body']}"]
    if "start_line" in c:
        cmd += ["--field", f"start_line={c['start_line']}",
                "--field", f"start_side={c.get('start_side', c.get('side', 'RIGHT'))}"]
    p = _run(cmd, check=False)
    return {"ok": p.returncode == 0, "stderr": p.stderr}


# --------------------------------------------------------------------------- #
# wiki links
# --------------------------------------------------------------------------- #

def local_wiki_pages(root: str = "wiki"):
    """Page names of a wiki clone sitting next to the repo, or None if there is none.

    Only used to warn about a link nobody can follow — a missing clone must not turn
    into "every page is unknown", hence None rather than an empty set.
    """
    if not os.path.isdir(root):
        return None
    pages = {os.path.splitext(f)[0] for f in os.listdir(root) if f.endswith(".md")}
    return pages or None


def expand_wiki_links_in(findings: list, model_summary: str, base: str,
                         pages=None) -> tuple:
    """Expand [[Page]] in every model-authored string. Returns (summary, warnings)."""
    unknown = set()
    model_summary = R.expand_wiki_links(model_summary, base, pages, unknown.add)
    for f in findings:
        for key in ("title", "explanation"):
            if f.get(key):
                f[key] = R.expand_wiki_links(f[key], base, pages, unknown.add)
    return model_summary, [f"wiki page not in ./wiki: [[{p}]] — link posted anyway"
                           for p in sorted(unknown)]


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

SEV_ICON = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}


def summary_body(findings: list, model_summary: str) -> str:
    """Deterministic part of the review body: heading + severity count table.

    Everything judgement-flavoured — verdict prose, per-file overview, analysis
    scope, non-blocking remarks — is the model's job and arrives as free
    markdown in the findings `_summary` entry.
    """
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        sev = str(f.get("severity", "MEDIUM")).upper()
        # unknown severity is coerced to MEDIUM, matching build_comment_body
        counts[sev if sev in counts else "MEDIUM"] += 1
    lines = ["## Code Review", ""]
    if model_summary:
        lines += [model_summary.strip(), ""]
    lines += ["| 심각도 | 건수 |", "|--------|------|"]
    for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        lines.append(f"| {SEV_ICON[s]} {s} | {counts[s]} |")
    return "\n".join(lines)


def summary_only_plan(event: str, findings: list, model_summary: str):
    """What a review with nothing anchored inline will do, as (post?, reason).

    Two callers — the dry run and the real one — so a dry run cannot promise a
    posting the real run then skips. That mismatch is the whole reason this is a
    function and not an `if` in each branch.

    APPROVE is the one event that is never posted, which is what stops a self-
    approval. With no findings it is also the event `decide_event` picks, so a
    summary-only review takes an explicit `--event COMMENT` and never happens by
    accident.
    """
    if event == "APPROVE":
        return False, "이슈 없음 — 게시 생략(콘솔만). 요약만 남기려면 --event COMMENT."
    if findings:
        return True, f"인라인 앵커 없음(전부 skip) — 요약 리뷰만 게시 (event={event})."
    if model_summary:
        return True, f"인라인 없음(요약만) — 요약 리뷰만 게시 (event={event})."
    return False, "게시할 내용 없음(findings·_summary 둘 다 비어 있음) — 콘솔만."


def print_console(payload: dict, skipped: list, warns: list, dry: bool,
                  wiki_warns: list = ()):
    tag = "[dry-run] " if dry else ""
    print(f"\n{tag}PR 리뷰 — event={payload['event']} "
          f"comments={len(payload['comments'])} skipped={len(skipped)}")
    for c in payload["comments"]:
        first = c["body"].splitlines()[0]
        rng = f"{c.get('start_line', c['line'])}-{c['line']}" \
            if "start_line" in c else str(c["line"])
        print(f"  {c['path']}:{rng} [{c.get('side', 'RIGHT')}]  {first}")
    for s in skipped:
        print(f"  SKIP {s.get('path')}:{s.get('line')} — {s['_reason']}")
    if warns:
        print("\n  경고(좌표 보정):")
        for w in warns:
            print(f"    - {w}")
    if wiki_warns:
        print("\n  경고(위키 링크):")
        for w in wiki_warns:
            print(f"    - {w}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def load_findings(path: str) -> tuple:
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    data = json.loads(raw)
    model_summary = ""
    findings = []
    for item in data:
        if "_summary" in item:
            model_summary = item["_summary"]
        else:
            findings.append(item)
    return findings, model_summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Post a GitHub inline PR review.")
    ap.add_argument("--repo", help="OWNER/REPO (auto-detected if omitted)")
    ap.add_argument("--pr", type=int, help="PR number (omit for local dry-run)")
    ap.add_argument("--findings", required=True, help="findings JSON path or '-'")
    ap.add_argument("--event", choices=["REQUEST_CHANGES", "COMMENT"],
                    help="override the auto-decided event (APPROVE is not "
                         "postable — the tool must never self-approve)")
    ap.add_argument("--wiki-base",
                    help="wiki root for [[Page]] expansion "
                         "(default https://github.com/OWNER/REPO/wiki/)")
    ap.add_argument("--no-wiki-links", action="store_true",
                    help="leave [[Page]] literal instead of linking it")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    findings, model_summary = load_findings(args.findings)

    # No PR number -> local review, force dry-run (never posts).
    dry = args.dry_run or args.pr is None
    repo = args.repo or (detect_repo() if not dry or args.pr else "LOCAL/LOCAL")

    # Before anything renders a body: [[Page]] is wiki-only syntax and would post as
    # literal text. A local review has no repo to build the URL from, so fall back to
    # the checkout's own remote and leave the links alone if even that is unknown.
    wiki_warns = []
    if not args.no_wiki_links:
        base = args.wiki_base
        if not base:
            owner = repo if repo != "LOCAL/LOCAL" else detect_repo(required=False)
            base = f"https://github.com/{owner}/wiki/" if owner else None
        if base:
            model_summary, wiki_warns = expand_wiki_links_in(
                findings, model_summary, base.rstrip("/") + "/", local_wiki_pages())

    if args.pr is None:
        sys.stderr.write("no --pr: local dry-run (nothing will be posted)\n")
        # Without a PR we cannot fetch a diff; expect findings to be self-anchored
        # only for console listing. Print and exit.
        body = summary_body(findings, model_summary)
        print(body)
        for f in findings:
            print(f"\n  {f.get('path')}:{f.get('line')} "
                  f"[{str(f.get('severity','?')).upper()}] {f.get('title','')}")
        for w in wiki_warns:
            sys.stderr.write(f"  경고(위키 링크): {w}\n")
        return 0

    diff = fetch_diff(repo, args.pr)
    diffmap = R.parse_diff(diff)
    body = summary_body(findings, model_summary)
    payload, skipped, warns = R.build_review_payload(
        findings, diffmap, body, event=args.event)

    if dry:
        print_console(payload, skipped, warns, dry=True, wiki_warns=wiki_warns)
        if not payload["comments"]:
            print(summary_only_plan(payload["event"], findings, model_summary)[1])
        return 0

    if not payload["comments"]:
        # No inline anchors, so the summary body is the whole review. It is still worth
        # posting: a pass that reached a verdict belongs on the PR, not in a console
        # nobody else reads. `summary_only_plan` decides, and the dry run above printed
        # the same decision.
        post, reason = summary_only_plan(payload["event"], findings, model_summary)
        if post:
            _run(["gh", "api", f"repos/{repo}/pulls/{args.pr}/reviews",
                  "--method", "POST", "--field", f"body={body}",
                  "--field", f"event={payload['event']}"], check=False)
        print(reason)
        print_console(payload, skipped, warns, dry=False, wiki_warns=wiki_warns)
        return 0

    res = post_review(repo, args.pr, payload)
    if res["ok"]:
        out = json.loads(res["stdout"])
        print(f"게시 완료: {out.get('html_url')}")
        print_console(payload, skipped, warns, dry=False, wiki_warns=wiki_warns)
        return 0

    # batch failed (often a single bad anchor). Fall back to per-comment posting
    # so the good comments still land, and report which one broke.
    print(f"배치 게시 실패, 개별 폴백 시도: {res['stderr'].strip()[:200]}")
    head = fetch_head_sha(repo, args.pr)
    ok = bad = 0
    for c in payload["comments"]:
        r = post_single_comment(repo, args.pr, head, c)
        if r["ok"]:
            ok += 1
        else:
            bad += 1
            print(f"  실패 {c['path']}:{c['line']} — {r['stderr'].strip()[:160]}")
    # a summary review comment (no inline) so the event still registers
    _run(["gh", "api", f"repos/{repo}/pulls/{args.pr}/reviews", "--method", "POST",
          "--field", f"body={body}", "--field", f"event={payload['event']}"],
         check=False)
    print(f"개별 게시: 성공 {ok} / 실패 {bad} / skip {len(skipped)}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

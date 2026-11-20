#!/usr/bin/env python3
"""
review_state.py — everything a review round needs to start, in one call.

    uv run --no-project python scripts/review_state.py --repo OWNER/REPO --pr 634

Prints the PR head and base, the next round, the increment since the last reviewed
head, the outstanding issues from the last state marker, every review thread on one
line, who else commented, whether the sibling skills are installed, and whether this
round runs an independent discovery pass. One call replaces the separate PR, review,
comment, thread and compare queries, and bot boilerplate is cut from what is shown.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import review_lib as R  # noqa: E402
import review_post as P  # noqa: E402

CODE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".sh", ".c", ".cc", ".cpp", ".h", ".hpp",
            ".go", ".rs", ".java", ".kt", ".sql", ".toml", ".yaml", ".yml")
DISCOVERY_LINES = 200

THREADS_QUERY = """
query($owner:String!,$name:String!,$pr:Int!){repository(owner:$owner,name:$name){pullRequest(number:$pr){
  reviewThreads(first:100){nodes{isResolved isOutdated path line
    comments(first:50){totalCount nodes{databaseId author{login} body
      reactionGroups{content reactors{totalCount}}}}}}}}}"""


def clean(body: str, limit: int = 140) -> str:
    """Comment text without hidden markers, folded blocks, badges or markup noise."""
    text = re.sub(r"<!--.*?-->", " ", body or "", flags=re.S)
    inner = re.compile(r"<details>(?:(?!<details>).)*?</details>", re.S)
    while inner.search(text):  # innermost first, so nested blocks go whole
        text = inner.sub(" ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"</?(sub|sup|b|strong|br)\s*/?>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def thread_lines(nodes: list) -> list:
    out = []
    for t in nodes:
        state = "RESOLVED" if t.get("isResolved") else "OUTDATED" if t.get("isOutdated") else "OPEN"
        comments = (t.get("comments") or {}).get("nodes") or []
        if not comments:
            continue
        first, last = comments[0], comments[-1]
        fp = R.parse_fingerprint_marker(first.get("body") or "") or "-"
        votes = "".join(f" [{g['content']}={g['reactors']['totalCount']}]"
                        for g in first.get("reactionGroups") or [] if g["reactors"]["totalCount"])
        line = (f"{state} {t.get('path')}:{t.get('line')} #{first.get('databaseId')} fp={fp} "
                f"@{(first.get('author') or {}).get('login')}{votes}: {clean(first.get('body'))}")
        total = (t.get("comments") or {}).get("totalCount", len(comments))
        if total > 1:
            line += (f" || {total - 1} repl(y/ies), last @{(last.get('author') or {}).get('login')}: "
                     f"{clean(last.get('body'), 100)}")
        out.append(line)
    return out


def review_lines(reviews: list) -> list:
    """Review bodies, including summaries that carry no inline comment."""
    out = []
    for r in reviews:
        text = clean(r.get("body"), 160)
        if text:
            out.append(f"@{(r.get('user') or {}).get('login')} {r.get('state')} "
                       f"{(r.get('commit_id') or '')[:12]}: {text}")
    return out


def discovery(round_next: int, increment: dict | None, lines: int = DISCOVERY_LINES) -> tuple:
    """Whether this round runs an independent discovery pass, and why.

    Round 2 runs one over the whole PR. Later rounds run one only when the increment
    is large, adds code files, or rewrote history; a small fix is covered by the
    targeted passes, which cost a fraction of a second full review.
    """
    if round_next <= 1:
        return "n/a", "round 1 reviews the whole PR"
    if increment is None or increment.get("status") not in ("ahead", "identical"):
        return "run", "the last reviewed head is not an ancestor: full review"
    if round_next == 2:
        return "run", "first re-review: one unseeded pass over the whole PR"
    files = increment.get("files") or []
    changed = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    added = [f["filename"] for f in files
             if f.get("status") == "added" and f["filename"].endswith(CODE_EXT)]
    if added:
        return "run", "increment adds code files: " + ", ".join(added[:3])
    if changed >= lines:
        return "run", f"increment changes {changed} lines (>= {lines})"
    return "skip", f"increment changes {changed} lines in {len(files)} file(s): targeted passes only"


def siblings(skill_dir: str) -> list:
    root = os.path.dirname(os.path.realpath(skill_dir))  # as setup_check and sweep.py resolve it
    need = [("code-review-sweep", "sweep.py"), ("code-review-sweep", "finding-gate.md"),
            ("review-rediagnose", "SKILL.md")]
    return [f"{d}/{f}" for d, f in need if not os.path.isfile(os.path.join(root, d, f))]


def _gh_json(args: list):
    p = P._run(["gh", *args], check=False)
    if p.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:3])} failed: {p.stderr.strip()[:200]}")
    return json.loads(p.stdout) if p.stdout.strip() else {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Print the state a review round starts from.")
    ap.add_argument("--repo", help="OWNER/REPO (auto-detected if omitted)")
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--discovery-lines", type=int, default=DISCOVERY_LINES)
    a = ap.parse_args(argv)
    repo = a.repo or P.detect_repo()
    owner, name = repo.split("/", 1)

    meta = _gh_json(["pr", "view", str(a.pr), "-R", repo, "--json",
                     "title,state,isDraft,baseRefName,headRefName,headRefOid,author"])
    p = P._run(["gh", "api", "--paginate", f"repos/{repo}/pulls/{a.pr}/reviews"], check=False)
    if p.returncode != 0:
        raise SystemExit(f"Cannot read reviews; refusing to assume round 1: {p.stderr.strip()[:160]}")
    reviews = P._json_pages(p.stdout)
    markers = [m for m in (R.parse_state_marker(r.get("body") or "") for r in reviews) if m]
    round_next = R.review_round(markers)
    head = meta["headRefOid"]
    print(f"PR {repo}#{a.pr} {meta['state']} draft={meta['isDraft']} "
          f"author=@{meta['author']['login']} base={meta['baseRefName']} head={head}")
    print(f"title: {meta['title']}")
    missing = siblings(os.path.dirname(HERE))
    print("siblings: ok" if not missing else
          "siblings MISSING: " + ", ".join(missing) + " (run setup_check.py)")

    increment = None
    if markers:
        last = markers[-1]["sha"]
        p = P._run(["gh", "api", f"repos/{repo}/compare/{last}...{head}", "--jq",
                    "{status, files: [.files[] | {filename, status, additions, deletions}]}"],
                   check=False)
        # After a force push the old head may be gone; that is the full-review case.
        increment = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else \
            {"status": "unavailable", "files": []}
        files = increment.get("files") or []
        print(f"round: next={round_next} last_reviewed={last[:12]} compare={increment.get('status')}")
        for f in files[:15]:
            print(f"  inc {f['status']:8s} +{f['additions']}/-{f['deletions']} {f['filename']}")
        if len(files) > 15:
            print(f"  inc … {len(files) - 15} more file(s)")
        issues = markers[-1].get("issues")
        if issues is None:
            issues = [{"fingerprint": fp} for m in markers for fp in m.get("findings", [])]
        print(f"outstanding: {len(issues)} (one _prior_findings disposition each)")
        for i in issues:
            print(f"  {i['fingerprint']} {i.get('severity', '?')} {i.get('path', '')}:"
                  f"{i.get('line', '')} {i.get('title', '')}")
    else:
        print("round: next=1 (no state marker)")
    verdict, why = discovery(round_next, increment, a.discovery_lines)
    print(f"discovery: {verdict} — {why}")

    data = _gh_json(["api", "graphql", "-F", f"owner={owner}", "-F", f"name={name}",
                     "-F", f"pr={a.pr}", "-f", f"query={THREADS_QUERY}"])
    nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    lines = thread_lines(nodes)
    bodies = review_lines(reviews)
    print(f"reviews: {len(reviews)} ({len(bodies)} with a body)")
    for line in bodies:
        print("  " + line)
    print(f"threads: {len(lines)}")
    for line in lines:
        print("  " + line)

    p = P._run(["gh", "api", "--paginate", f"repos/{repo}/issues/{a.pr}/comments"], check=False)
    comments = P._json_pages(p.stdout) if p.returncode == 0 else []
    if comments:
        who = Counter(c["user"]["login"] for c in comments)
        print("issue comments: " + ", ".join(f"@{k} x{v}" for k, v in who.most_common()))
        for c in comments[-5:]:
            text = clean(c.get("body"), 160)
            if text:
                print(f"  @{c['user']['login']}: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

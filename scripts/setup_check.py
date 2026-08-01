#!/usr/bin/env python
"""
setup_check.py — verify the environment before running a review.

Run first when a review misbehaves. Cross-platform (uses `python`, not `python3`).

    python scripts/setup_check.py
    python scripts/setup_check.py --repo OWNER/REPO   # also check repo access
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

# Windows consoles default to a legacy codepage (e.g. cp949) that cannot encode
# em-dashes/emoji. Force UTF-8 so output never crashes cross-platform.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _run(cmd):
    try:
        # decode as UTF-8 (gh emits ✓/emoji); never let the locale codepage crash us
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None


def check(label, ok, detail=""):
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="also verify the active gh account can see this repo")
    args = ap.parse_args(argv)

    print("pr-inline-review · setup check\n")
    ok = True

    # 1. python >= 3.8 (dataclasses, f-strings)
    ok &= check(f"python {sys.version.split()[0]}",
                sys.version_info >= (3, 8),
                "need >= 3.8")

    # 2. gh installed
    gh = shutil.which("gh")
    ok &= check("gh CLI installed", bool(gh), gh or "install: https://cli.github.com")
    if not gh:
        print("\n결론: gh 미설치 — 리뷰 게시 불가.")
        return 1

    ver = _run(["gh", "--version"])
    if ver and ver.returncode == 0:
        print(f"         {ver.stdout.splitlines()[0]}")

    # 3. gh auth + active account
    auth = _run(["gh", "auth", "status"])
    authed = bool(auth) and auth.returncode == 0
    ok &= check("gh authenticated", authed)
    active = ""
    if authed:
        # gh writes auth status to stderr
        text = (auth.stdout or "") + (auth.stderr or "")
        for line in text.splitlines():
            if "Active account: true" in line:
                # the account name is on a nearby "Logged in to ... account X" line
                pass
            if "Logged in to" in line and "account" in line:
                active = line.strip()
        if active:
            print(f"         {active}")
        print("         주의: org 저장소는 org 접근 권한이 있는 계정이 active여야 함.")
        print("         전환: gh auth switch --user <handle>")

    # 4. optional repo access
    if args.repo:
        r = _run(["gh", "repo", "view", args.repo, "--json", "nameWithOwner",
                  "--jq", ".nameWithOwner"])
        can = bool(r) and r.returncode == 0
        ok &= check(f"repo access: {args.repo}", can,
                    "" if can else "active 계정 org 권한 확인 / gh auth switch")

    print("\n결론:", "환경 정상 — 리뷰 진행 가능." if ok else
          "일부 항목 실패 — 위 FAIL 해결 후 재실행.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

# PR inline review

Review a PR against its actual source tree, repository rules and existing discussion,
then post verified findings inline. `SKILL.md` owns the procedure; the scripts handle
coordinates, suggestions, posting and review state.

## Discovery and source selection

Install this skill beside `code-review-sweep`. `scripts/sweep.py` delegates to that
skill's canonical engine and `patterns.toml`, so both review entry points use the
same patterns. Design judgement references remain in `reference/`.

Discovery reports candidate hits, manually reviewed categories, removed guarantees
and changed routines. Read each changed routine and verify every candidate against
callers, guards and shared mechanisms. Matching text elsewhere does not establish
that a deleted guard or test still protects the same behavior.

The engine reads working files. Staged reviews require tracked working files to
match the index; base reviews require them to match HEAD. A mismatch or Git failure
stops the scan. Preserve local changes and review a clean checkout of the target.
Use `--worktree` only when the intended target is the unstaged diff.

For remote PRs, materialize the reviewed head in a detached worktree. Use the actual
PR base and head SHA, preserve existing edits, and follow the workspace's location
and retention rules.

## Depth, evidence and convergence

Round 1 covers the whole PR. Round 2 adds a fresh-context gap sweep, callers two hops
out and focused tests. Later rounds add independent review angles.

Depth never suppresses verified LOW or MEDIUM defects. Convergence comes from
excluding taste-only proposals and answering repeated findings in their existing
threads. `introduced_by_increment` is optional provenance, not an exemption from a
severity floor. `--depth` changes analysis effort; it does not change the publishing
script's recorded round.

Keep sweep coverage and investigation logs local. Published reviews contain the
verdict, blockers, material verification limits and concise inline findings.

Both skills use `../code-review-sweep/finding-gate.md`. A candidate must be factually
valid and actionable: cite the supported contract, credible input producer, material
consequence and a proportionate correction. Trusted operator configuration is
evaluated this way for every category, not only security. Reproducing an invented
input grammar is insufficient; a real supported-contract failure still matters.

Match repeated topics by invariant, root cause and affected workflow before exact
fingerprint matching. If successive fixes keep adding defenses for the same
requirement, pause that fix chain and recheck the requirement. A neutral discovery
pass chooses its own paths from the PR and rules; reconcile earlier candidates
afterward. Candidate verification is a separate, deliberately targeted pass.

## Findings and prior state

The findings file is a JSON list. New findings contain `path`, `line`, `severity`,
`category`, `title` and `explanation`; `start_line` and `suggestion` are optional.
An optional `_summary` entry supplies verdict prose.

On re-review, add one `_prior_findings` entry with exactly one disposition for each
outstanding fingerprint in the latest marker's `issues`. For legacy markers with
no `issues`, assess the union of recorded `findings`.

```json
[
  {"_summary": "The prior transaction failure remains unresolved."},
  {"_prior_findings": [
    {"fingerprint": "0123456789ab", "status": "open"}
  ]}
]
```

Valid statuses are `open`, `fixed`, `rebutted-accepted`, `rebutted-rejected`,
`outdated`, `unverified` and `withdrawn`. Closing as `fixed`, `rebutted-accepted` or
`withdrawn` requires an `evidence` string identifying the code or contract supporting
the decision. Use `withdrawn` for an earlier claim that fails the qualification gate;
do not require a code change merely to resolve that review state. All other states
remain unresolved. Legacy markers lack severity, so unresolved legacy
findings conservatively count as HIGH until assessed and closed.

The marker retains new and outstanding findings for the next round. Missing
dispositions stop publication rather than silently treating old issues as fixed.
Failure to fetch prior reviews also stops publication. `--fresh` deliberately
starts a review without prior state; do not use it to bypass unresolved findings.

## Posting behavior

Always dry-run with the reviewed commit before publishing:

```bash
uv run python "$SKILL_DIR/scripts/review_post.py" --repo OWNER/REPO --pr 123 \
  --commit "$HEAD_SHA" --findings findings.json --dry-run
```

The posting script:

- Rejects a moved head or a PR that is no longer open.
- Validates anchors and warns about nearby snapping. If suggestion coordinates
  change, or the range includes LEFT-side lines, removes the suggestion. Re-anchor
  and dry-run again before offering an Apply action.
- Expands wiki references into ordinary links and formats severity badges.
- Deduplicates inline comments without removing outstanding findings from the
  verdict. Unresolved HIGH/CRITICAL means REQUEST_CHANGES; otherwise COMMENT.
- Changes REQUEST_CHANGES to COMMENT for the author's own PR, which GitHub requires.
  The summary must still explain the blocker.
- Posts a summary and state marker even with no new inline findings. Findings that
  cannot be anchored remain readable in the summary. Automatic approval is disabled.
- Falls back to individual comments when the batch fails.

Explicit `--event REQUEST_CHANGES|COMMENT` overrides the computed event. Without a PR
number the script only prints a local dry run and makes no publication.

## Environment and verification

Ruff supplies the Python lint lane; ESLint uses the reviewed tree's local
`node_modules`; tree-sitter supplies TypeScript units; Jedi resolves Python
references, with a lexical fallback. Missing or failed lanes are reported as
unscanned, never as clean. GitHub access requires `gh` and repository authorization.

Run the standalone helper tests without application databases or LLM services:

```bash
uv run python "$SKILL_DIR/tests/test_diff_map.py"
uv run python "$SKILL_DIR/tests/test_wiki_links.py"
uv run python "$SKILL_DIR/tests/test_sweep.py"
uv run python "$SKILL_DIR/../code-review-sweep/tests/test_sweep.py"
```

These tests cover anchors, suggestion safety, verdicts, prior state, scanner
regressions and wiki link rendering. They do not publish to GitHub.

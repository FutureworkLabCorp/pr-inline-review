# Sweep categories — the question to ask at each row

`scripts/sweep.py` prints candidates; this file says how to judge them. Read only the
rows for categories that appear in `== HITS ==` or `== MANUAL ==`. A row is a candidate,
not a finding: most are benign, and each one gets a verdict.

## Script-scanned (judge every HIT row)

| Tag | Ask at each hit |
|-----|-----------------|
| `SSRF-OUTBOUND` | Is the target URL validated (allowlist / private-IP guard) **at this call**? A guard on one path does not cover another. Redirect targets and URLs read from a *response* are re-fetched unguarded. TOCTOU/DNS-rebinding: if the guard resolves the host but the client re-resolves at connect time, pin the validated IP. A URL whose host comes from config or an env var is trusted; only a caller-influenced host or scheme is a finding. |
| `LOOP-ISOLATION` | If one iteration raises, does the whole batch crash? Loops over external resources need per-item try/except (log + skip). |
| `EVENT-LOOP-BLOCKING` | (ruff ASYNC) Does a sync/blocking call run on the event loop? Needs `run_in_executor` / an async client. |
| `BOUNDARY-VALIDATION` | (ruff S) Judge each bandit hit in context: injection, path traversal, open redirect, insecure temp file. Many are style-level — grade honestly. |
| `LIFECYCLE-CREATE` | Is there a matching cleanup on **delete and on the error path**? Credentials, temp files, locks, cache/vault entries, external registrations. |
| `WRITE-AUTH` | Is the row fetch scoped by **ownership/role appropriate to a write**, not mere read-visibility? Activation/visibility gates are not ownership. |
| `DEAD-CODE` | 0 external uses = removal candidate. **Known false positives:** decorator-registered functions (pydantic `@field_validator`, route handlers, event hooks, celery tasks) are referenced by framework, not by name — check for a decorator before flagging. |
| `SILENT-FAILURE` | What does this catch turn into — a default, `None`, `continue`, a log line? **Name the specific errors it could hide** (e.g. `KeyError` from a schema change, `TimeoutError`, a programming error). A finding when the caller cannot tell failure from an empty result, or a bug would surface only as wrong data. Not a finding when the path is advisory (progress, metrics) and the error is logged with context. Catches silenced with `noqa` are excluded by the script. |
| `PY310-COMPAT` | The code stays 3.10-compatible (`.docs/Code-Conventions.md`, "Keep the code Python 3.10-compatible"). Is this a 3.11+ API or syntax with no 3.10 path? `typing_extensions` and a `try: import tomllib / except: import tomli` fallback are fine. An existing unguarded use elsewhere in `src/` does not make a new one acceptable. |
| `ABS-IMPORT` | `from src.app...` inside `src/` — must be relative. Only exception: an entrypoint that needs the absolute import for a side effect (`.docs/Code-Conventions.md`). |
| `CYPHER-COMPAT` | A Cypher query added or changed. Check its shape against `.docs/Compatible-ArcadeDB.md` (shapes ArcadeDB reads differently from Neo4j **without raising**). Did the PR run `make test-live-graph` on both backends? If not and the shape is on the list, that is the finding. |
| `DOC-NARRATION` | Does the docstring or comment tell the change's story — what the code used to do, what this PR fixed, "기존에는 … 이제는"? That belongs in the commit message or the PR; the next reader only sees prose that will not age. A finding unless the old behaviour is a trap a reader must know, and then the fix is to state it as a constraint, not as history. Usually LOW; MEDIUM when most of a docstring is history. |
| `DOC-LABEL` | A ticket, phase or decision code (`FUT-123`, `Phase 2`, `D5`) whose meaning lives somewhere the reader cannot open from the source. Say the reason in words. LOW. |
| `DOC-RESTATE` | An `Args:`/`Returns:` block. Does each entry add something the annotation does not (units, an invariant, who owns the value, what `None` means)? Entries that only repeat names and types go. LOW. |
| `DOC-STEPS` | A numbered step comment signals a function that wants splitting; a banner signals a module that does. Flag new ones only; ones already in the file stay. LOW. |
| `DOC-LONG` | Length alone is never the finding. Read it sentence by sentence: is each one information the code cannot give (a probed constraint, an observed external behaviour, why this way and not the obvious one)? Retelling, restating the code, or the change's history is the finding — quote the sentences to cut. A comment grown into a document belongs in `.docs/` or the wiki, with one sentence and a pointer left behind. |
| `SH-OUTBOUND` | Is the URL/host attacker-influenced? Is the response piped into a shell? |
| `SH-INJECTION` | Does `eval` / `bash -c` interpolate any external input? |
| `SH-UNSAFE-RM` | Can the variable be empty/unset (→ deletes the parent dir) or contain spaces/`..`? Is `set -u` in effect? |
| `SH-TEMPFILE` | Predictable name in a shared dir → symlink attack / clobbering. Use `mktemp`. |
| `UNSAFE-CAST` (ts) | An `as` assertion (not `as const`) — does it launder a real type mismatch the compiler would otherwise catch? `as unknown as X` and `as any` are the loudest. |
| `ANY-ESCAPE` (ts) | `any` disables all downstream type-checking. Prefer `unknown` + narrowing. |
| `NON-NULL-ASSERT` (ts) | `foo!.bar` swallows a real `null`/`undefined` at runtime. Is the value guaranteed non-null here? |
| `TS-SUPPRESS` (ts) | `@ts-ignore` / `@ts-nocheck` — justified with a reason, or masking a real bug? Prefer `@ts-expect-error` scoped to one line. |
| `DANGER-HTML` (ts) | `dangerouslySetInnerHTML` — is the HTML sanitized / from a trusted source? |
| `EFFECT-DEPS` · `HOOK-RULES` (ts, eslint) | A missing dep (stale closure) or a conditionally-called hook. Deliberate and safe? |
| `UNAWAITED-PROMISE` · `MISUSED-PROMISE` (ts, eslint) | A floating promise or a promise passed where a sync callback is expected. |
| `A11Y` (ts, eslint) | Real accessibility gap vs. a case the rule over-flags. |

## Manual (judge from the diff and the UNITS you read)

| Tag | Ask across the diff |
|-----|---------------------|
| `GRACEFUL-DEGRADATION` | If a non-essential subsystem called from a critical path (chat build, request handler, auth) throws, does the whole request die? |
| `NULL-TYPE-SAFETY` | Attribute/index access on `Optional`, `Union`, `dict \| Model`, or an external/JSON payload — can it be `None` / the other member / a missing key? Check the **declared type**. |
| `CONCURRENCY-ATOMICITY` | Writes split across two stores (DB + cache/vault), shared mutable state — consistent if one write fails or on rollback? |
| `RESOURCE-HELD-ACROSS-IO` | A DB session / lock / pooled connection held while `await`-ing a slow call? |
| `API-TYPE-CORRECTNESS` | Does the called method/attr exist for this type/version? (`getattr` on a `dict` returns the default, not the key.) **Verify against the installed version before flagging.** |
| `MERGE-KEY-COLLISION` | An identifier used as a dict/registry key merging multiple sources — unique across **all** sources? |

## Why the sweep exists

In one real review the predecessor skill downgraded one visible SSRF (correctly) but
never swept the other outbound calls — missing three real SSRF sinks, a batch-crashing
loop, a credential cleanup, and an `AttributeError` on `dict | None`. Precision was fine;
coverage was the failure.

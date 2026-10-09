# pr-inline-review

A PR code-review skill that analyzes a pull request from the local source tree and
posts the result as inline comments on GitHub. It aims to be deeper than a
diff-only cloud reviewer by reading the actual code around each change, tracing
call sites, applying the repo's own guidelines (`AGENTS.md`), and recognizing
prior review reactions so it never re-raises a settled point. `SKILL.md` holds the
full step-by-step procedure; this file explains *what* the skill does and *why*
the pieces are there.

## What it does

The model's job is judgment ("what is worth flagging"); the mechanical parts are
delegated so they can't go wrong by hand. The flow:

1. **Load the repo's review guidelines** (`AGENTS.md`) as the grading rubric.
2. **Read existing reviews/reactions** to skip already-discussed issues (`--fresh` disables).
3. **Collect the diff** and decide a source-access strategy (same-branch vs different-branch).
4. **Step 3-A — mechanical sink sweep** (`scripts/sweep.py`, see below).
5. **Read the source** around each change: full enclosing function, call sites, existing patterns, test gaps.
6. **Self-verification gate** — adversarially re-check every candidate and drop false positives before posting.
7. **Emit structured findings JSON** — the model's *only* output. `scripts/review_post.py` turns it into an inline review deterministically (or dry-run to console).

## Why a sink sweep (`scripts/sweep.py`)

A reviewer's judgment finds the *headline* issue but does not guarantee
**coverage** — whole categories of defects (SSRF sinks, un-isolated loops,
missing cleanup on error paths, write-authorization gaps, dead code, …) leak out
simply because nothing forces the reviewer to look for each one. The sweep is that
forcing function: it mechanically enumerates candidate sites for every category on
the added lines and makes the model judge each one, so a category can be a
*finding* or an explicit *"swept, none"* — never silently skipped. Discovery is
mechanical and cheap; the model spends its budget on judgment, not on grepping.

## Why review units (`== UNITS ==`)

Sink patterns only catch *anticipated* defect classes. The genuinely hard misses
are **intent bugs** — "the code does X but should do Y" — which no pattern engine
can express. `== UNITS ==` addresses that by enumerating **every routine the diff
touched** (Python via the stdlib `ast`, TypeScript/TSX via tree-sitter) as the
coverage floor for what to read: the reviewer must read each unit fully and mark it
a finding or "read, clean". This is what lets the review reach bugs the sweep is
blind to. Oversized functions touched only sparsely are read as the hunk's
enclosing block (and flagged) so the read cost stays bounded.

## Why jedi (type-aware references)

DEAD-CODE detection and a unit's `callers` list both need to answer "who
references this symbol". A lexical `git grep` for the name is wrong in both
directions: it **over-counts** (a same-named symbol in an unrelated module looks
like a reference) and **under-counts** (a symbol used only through
`import foo as bar` is invisible under its original name). `jedi` resolves
references from the definition itself, so the answer is type-accurate. It is scoped
to the configured source roots for speed and **degrades to `git grep` on any
failure**, so behavior is never worse than the lexical baseline — jedi only makes
it more precise when available.

## Why tree-sitter (TypeScript units)

`ast` gives Python units for free, but there is no stdlib parser for
TypeScript/TSX. `tree-sitter` provides the function/method/arrow boundaries and
call sites for `== UNITS ==` on TS files. It is **best-effort**: if the binding is
not in the environment, TS units are skipped with a warning and Python units still
appear.

## Why script-driven posting (`scripts/review_post.py`)

The error-prone half of an inline review is not *what* to flag but *where/how* to
attach it: line vs. side, single- vs. multi-line ranges, the ` ```suggestion `
fence, and the GitHub API call. Making the model compute those by hand is the main
source of broken anchors and 422s. So the model emits only **structured findings
JSON (new-file line numbers)**, and `review_post.py` + `review_lib.py` do the rest
deterministically:

- Parse the diff and **validate every anchor** against it; snap near-miss lines
  (≤10 away) to the nearest commentable line, reject farther ones for re-anchoring
  (placement is the model's judgement), drop ranges that span hunks — never a 422.
- Use GitHub's **line-based** review API (`line`/`side`/`start_line`/`start_side`)
  in a **single batch** — single-, multi-, and cross-side suggestions all in one
  call, no `position` arithmetic.
- Attach the Gemini-style severity SVG badge, the `**[SEV] category** — title`
  prefix, and the ` ```suggestion ` fence; surface (not silently rewrite)
  suggestion sanity issues to the console — indent mismatch, a suggestion shorter
  than the range it replaces (Apply deletes the unmentioned lines), and
  LEFT-anchored suggestions (never applyable).
- Build the review body as **model `_summary` markdown + deterministic severity
  table** — verdict prose stays with the model, only the counting is scripted.
- Decide the default event: **CRITICAL/HIGH → REQUEST_CHANGES, else COMMENT, none
  → APPROVE**, overridable via `--event` (also a judgement call). A clean PR is
  **not** auto-approved (console only), so the tool never self-approves its
  author's PR.
- **Pin the reviewed head** (`--commit`): if the author pushed while the review ran,
  the findings' line numbers point at other code, and a moved line often still sits
  inside some hunk, so the anchor check alone cannot notice. The script refuses to
  post in that case and posts the review against that exact `commit_id` otherwise.
- **Own PR:** GitHub rejects REQUEST_CHANGES on your own PR with a 422, which used to
  sink the batch and scatter the findings as loose comments. The event is lowered to
  COMMENT instead.
- **Nothing lost to anchoring:** findings that could not be anchored inline are
  written into the review body, not just counted.
- **Re-review state:** the review body carries a hidden marker with the reviewed head
  and each finding's fingerprint (path + category + title), and every inline comment
  carries its fingerprint. The next round diffs only `sha..HEAD` and gives a verdict on
  each earlier finding instead of rediscovering it.
- **Rounds go deeper but converge:** round 1 reviews the whole PR; round 2 adds a gap
  sweep, callers two hops out and a narrow test run; round 3+ fans out per-angle
  subagents. A deeper look always finds more nits, so from round 2 on a new finding must
  be at least MEDIUM, and repeats of earlier findings are dropped, both enforced by the
  posting script. The floor stops at MEDIUM: a design or resource problem found late is
  still worth a thread.
- **Beyond correctness:** the sweep also lists deleted guards and tests (`REMOVED`),
  duplicate changes (`DUP-*`) and docstrings that narrate the change or carry ticket
  codes (`DOC-*`); the review asks why the change exists and weighs YAGNI against
  optimization.
- On batch failure, **fall back to per-comment posting** so one bad anchor can't
  sink the rest. `scripts/setup_check.py` verifies `gh`/auth/repo-access first, and
  `tests/` cover the deterministic core.

## Dependencies (self-contained skill, env-only requirements)

`scripts/sweep.py` and its rule file `scripts/patterns.toml` are vendored here, so
the sweep runs **without depending on any other skill** (`sweep.py` loads the
colocated `patterns.toml`). The design-pattern judgement reference
(`reference/patterns.md`, from the codebase-review skill) is vendored too, so
design calls in Step 4 don't reach outside the skill either. The only remaining requirements live in the *project
environment*, resolved via `uv run`, and each degrades gracefully if absent:

| Requirement | Used for | If missing |
|-------------|----------|------------|
| `ruff` | Python lint lane (`ASYNC`, `S`) | lane skipped, warning |
| `eslint` (+ repo `node_modules`) | TypeScript lint lane (local-only; `--linter` to force) | lane skipped by default, warning |
| `tree-sitter` (+ `-typescript`) | TypeScript `== UNITS ==` | TS units skipped, warning |
| `jedi` | type-aware DEAD-CODE / callers | falls back to `git grep` |
| `gh` CLI | fetch diff / post review | required to post |

---

# pr-inline-review (한글)

PR을 **로컬 소스 트리 기준으로 분석**해 결과를 GitHub에 **인라인 코멘트로 게시**하는
코드리뷰 스킬이다. diff 텍스트만 보는 클라우드 리뷰어보다 깊이 있게 —
각 변경 주변의 실제 코드를 읽고, 호출부(call site)를 역추적하고, 저장소 자체
가이드(`AGENTS.md`)를 기준으로 삼고, 기존 리뷰 반응을 인식해 이미 정리된 지적을
다시 꺼내지 않는 것을 목표로 한다. 단계별 절차는 `SKILL.md`에 있고, 이 문서는
**무엇을 하는지**와 각 요소를 **왜 넣었는지**를 설명한다.

## 하는 일

모델은 "무엇을 지적할지"(판단)만 맡고, 기계적인 부분은 손으로 틀리지 않도록
스크립트에 위임한다. 흐름:

1. **저장소 리뷰 가이드 로드**(`AGENTS.md`) — 평가 기준.
2. **기존 리뷰·반응 읽기** — 이미 논의된 이슈 skip (`--fresh`로 비활성).
3. **diff 수집** + 소스 접근 전략 결정(same-branch / different-branch).
4. **Step 3-A — 기계적 sink 스윕**(`scripts/sweep.py`, 아래 설명).
5. **소스 정밀 분석** — 변경을 감싸는 함수 전체·호출부·기존 패턴·테스트 갭.
6. **자기검증 게이트** — 후보를 적대적으로 재검증해 게시 전 false positive 제거.
7. **구조화 findings JSON 산출** — 모델의 *유일한* 산출물. `scripts/review_post.py`가 이를 결정적으로 인라인 리뷰로 변환(또는 dry-run 콘솔).

## 왜 sink 스윕(`scripts/sweep.py`)인가

리뷰어의 판단은 "가장 눈에 띄는" 문제는 찾지만 **커버리지**를 보장하지 못한다 —
SSRF sink, 실패 격리 없는 루프, 에러 경로의 정리 누락, write 인가 갭, dead code 등
결함 **범주 전체**가 "아무도 그걸 찾으라고 강제하지 않아서" 통째로 새어나간다.
스윕이 그 강제 장치다: 추가된 줄에서 각 범주의 후보 site를 **기계적으로 전수 열거**
하고 모델이 하나씩 판정하게 해, 각 범주가 *finding*이거나 명시적 *"훑음, 없음"*이
되도록 한다 — 조용한 누락이 없다. 발견은 기계적·저비용이고, 모델은 grep이 아니라
판단에 예산을 쓴다.

## 왜 리뷰 유닛(`== UNITS ==`)인가

sink 패턴은 **미리 아는** 결함 범주만 잡는다. 정말 놓치기 쉬운 것은 **의도(intent)
버그** — "코드는 X를 하지만 Y였어야 한다" — 이고, 이는 어떤 패턴 엔진으로도 표현할
수 없다. `== UNITS ==`는 **diff가 건드린 모든 루틴**(Python=stdlib `ast`,
TypeScript/TSX=tree-sitter)을 열거해 "무엇을 읽을지"의 바닥선을 만든다: 리뷰어는 각
유닛을 통째로 읽고 finding 또는 "read, clean"으로 표시해야 한다. 이것이 스윕이 못
보는 버그에 닿는 통로다. 드문드문 수정된 거대 함수는 hunk를 감싸는 블록만 읽어(그리고
플래그를 달아) 읽기 비용에 상한을 둔다.

## 왜 jedi(타입인지 참조)인가

DEAD-CODE 판정과 유닛의 `callers` 목록은 "이 심볼을 누가 참조하나"에 답해야 한다.
이름으로 하는 lexical `git grep`은 양방향으로 틀린다: **과다 카운트**(무관한 모듈의
동명 심볼이 참조처럼 보임)와 **과소 카운트**(`import foo as bar`로만 쓰인 심볼은 원래
이름으로 안 보임). `jedi`는 정의로부터 참조를 해석하므로 타입 정확도가 있다. 속도를
위해 설정된 소스 루트로 범위를 한정하고, **실패 시 `git grep`으로 폴백**하므로
동작이 lexical 기준보다 나빠지지 않는다 — jedi는 있을 때 정확도만 올린다.

## 왜 tree-sitter(TypeScript 유닛)인가

`ast`로 Python 유닛은 공짜로 얻지만 TypeScript/TSX는 표준 파서가 없다.
`tree-sitter`가 TS 파일의 함수/메서드/화살표 경계와 호출부를 제공해 `== UNITS ==`를
만든다. **best-effort**다: 바인딩이 env에 없으면 TS 유닛은 경고와 함께 skip되고
Python 유닛은 그대로 나온다.

## 왜 스크립트 기반 게시(`scripts/review_post.py`)인가

인라인 리뷰에서 오류가 나는 절반은 "무엇을 지적할지"가 아니라 "**어디에 어떻게 붙일지**"다
— line vs side, 단일 vs 여러 줄 범위, ` ```suggestion ` 펜스, GitHub API 호출. 이걸
모델이 손으로 계산하게 하면 깨진 앵커와 422의 주원인이 된다. 그래서 모델은
**구조화 findings JSON(new-file 줄 번호)**만 내고, `review_post.py`+`review_lib.py`가
나머지를 결정적으로 처리한다:

- diff를 파싱해 **모든 앵커를 검증** — diff 밖 줄은 ±10줄 이내만 최근접 줄로 스냅,
  그보다 멀면 재앵커 대상으로 반려(위치 선정은 모델의 판단), hunk를 넘는 범위는 드롭.
  422가 나지 않는다.
- GitHub **line 기반** 리뷰 API(`line`/`side`/`start_line`/`start_side`)를 **단일 배치**로
  호출 — 단일·여러 줄·크로스사이드 suggestion을 한 번에, `position` 산술 없음.
- Gemini식 심각도 SVG 배지 + `**[SEV] category** — title` 접두어 + ` ```suggestion `
  펜스 자동 부착. suggestion 위생 문제는 본문에 넣지 않고 콘솔로 표면화(조용한 재작성
  안 함) — 들여쓰기 불일치, 범위보다 짧은 suggestion(Apply가 누락 줄을 삭제), LEFT 앵커
  suggestion(Apply 불가).
- 리뷰 본문 = **모델 `_summary` markdown + 결정적 심각도 집계표** — verdict 산문은
  모델 몫이고, 스크립트는 집계만 한다.
- event 기본 결정: **CRITICAL/HIGH → REQUEST_CHANGES, 그 외 → COMMENT, 없음 → APPROVE**,
  이것도 판단이라 `--event`로 오버라이드 가능. 깨끗한 PR은 **자동 승인하지 않고**(콘솔만)
  — 그래서 자기 PR을 self-approve하지 않는다.
- **리뷰한 head를 고정한다**(`--commit`). 리뷰 도중 작성자가 push하면 findings의 줄 번호가
  다른 코드를 가리키는데, 옮겨진 줄도 대개 어느 hunk 안에 있어서 앵커 검사만으로는 알아채지
  못한다. 그래서 head가 바뀌었으면 게시를 거부하고, 아니면 그 `commit_id`에 고정해 게시한다.
- **자기 PR:** GitHub은 자기 PR의 REQUEST_CHANGES를 422로 거부한다. 예전에는 이 때문에 배치가
  실패해 지적이 낱개 코멘트로 흩어졌다. 이제 event를 COMMENT로 낮춘다.
- **앵커 실패도 잃지 않는다:** 인라인으로 달지 못한 finding은 집계 숫자가 아니라 내용째
  리뷰 본문에 싣는다.
- **재리뷰 상태:** 리뷰 본문에 리뷰한 head와 finding fingerprint(path+category+title)를
  숨김 마커로 남기고, 인라인 코멘트마다 fingerprint를 붙인다. 다음 라운드는 `sha..HEAD`만
  보고 이전 지적마다 판정을 내린다 — 같은 걸 다시 찾지 않는다.
- **회차가 오를수록 깊어지되 수렴한다:** 1회차는 PR 전체, 2회차는 gap sweep·호출부 2홉·좁은
  테스트 실행을 더하고, 3회차부터는 각도별 서브에이전트를 쓴다. 깊이 볼수록 nit은 늘 더 나오므로
  2회차부터 신규 지적 하한을 MEDIUM으로 두고 이전 회차 지적의 반복을 뺀다. 둘 다 게시
  스크립트가 결정적으로 적용한다. 하한은 MEDIUM에서 멈춘다 — 늦게 찾은 설계·자원 문제도
  스레드로 다룰 가치가 있다.
- **정확성 밖도 본다:** 스윕이 지운 가드·테스트(`REMOVED`), 중복 수정(`DUP-*`), 고친 경위나
  티켓 번호를 늘어놓는 docstring(`DOC-*`)을 열거하고, 리뷰는 변경이 왜 있는지와 YAGNI 대
  최적화를 따진다.
- 배치 실패 시 **개별 코멘트 폴백**으로 앵커 하나가 나머지를 죽이지 않게 한다.
  `scripts/setup_check.py`가 `gh`/인증/repo 접근을 선검사하고, `tests/`가 결정적 코어를 커버한다.

## 의존성 (스킬은 자체 완결, env만 요구)

`scripts/sweep.py`와 규칙 파일 `scripts/patterns.toml`이 함께 동봉돼 있어 스윕은
**다른 스킬 없이** 동작한다(`sweep.py`가 옆의 `patterns.toml`을 로드). 설계 판단
레퍼런스(`reference/patterns.md`, codebase-review 스킬에서 가져옴)도 동봉돼 있어
Step 4의 설계 판단도 스킬 밖을 참조하지 않는다. 남는 요구사항은
`uv run`으로 해석되는 *프로젝트 env*뿐이고, 각각 없으면 우아하게 degrade한다:

| 요구사항 | 용도 | 없을 때 |
|----------|------|---------|
| `ruff` | Python 린트 레인(`ASYNC`, `S`) | 레인 skip, 경고 |
| `eslint` (+ 레포 `node_modules`) | TypeScript 린트 레인(local-only; `--linter`로 강제) | 기본 skip, 경고 |
| `tree-sitter` (+ `-typescript`) | TypeScript `== UNITS ==` | TS 유닛 skip, 경고 |
| `jedi` | 타입인지 DEAD-CODE / callers | `git grep`으로 폴백 |
| `gh` CLI | diff 수집 / 리뷰 게시 | 게시에 필수 |

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
7. **Post** the surviving findings as inline comments (or dry-run to console).

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

## Dependencies (self-contained skill, env-only requirements)

`scripts/sweep.py` and its rule file `scripts/patterns.toml` are vendored here, so
the sweep runs **without depending on any other skill** (`sweep.py` loads the
colocated `patterns.toml`). The only remaining requirements live in the *project
environment*, resolved via `uv run`, and each degrades gracefully if absent:

| Requirement | Used for | If missing |
|-------------|----------|------------|
| `ruff` | Python lint lane (`ASYNC`, `S`) | lane skipped, warning |
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
7. **게시** — 살아남은 finding을 인라인 코멘트로(또는 dry-run 콘솔).

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

## 의존성 (스킬은 자체 완결, env만 요구)

`scripts/sweep.py`와 규칙 파일 `scripts/patterns.toml`이 함께 동봉돼 있어 스윕은
**다른 스킬 없이** 동작한다(`sweep.py`가 옆의 `patterns.toml`을 로드). 남는 요구사항은
`uv run`으로 해석되는 *프로젝트 env*뿐이고, 각각 없으면 우아하게 degrade한다:

| 요구사항 | 용도 | 없을 때 |
|----------|------|---------|
| `ruff` | Python 린트 레인(`ASYNC`, `S`) | 레인 skip, 경고 |
| `tree-sitter` (+ `-typescript`) | TypeScript `== UNITS ==` | TS 유닛 skip, 경고 |
| `jedi` | 타입인지 DEAD-CODE / callers | `git grep`으로 폴백 |
| `gh` CLI | diff 수집 / 리뷰 게시 | 게시에 필수 |

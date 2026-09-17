---
name: pr-inline-review
description: >
  PR 코드리뷰를 수행하고 결과를 GitHub PR에 인라인 코멘트로 게시한다.
  로컬 소스 직접 분석·프로젝트 가이드 적용·기존 리뷰 반응 인식으로 클라우드 전용
  리뷰 에이전트보다 깊이 있는 리뷰를 제공한다. 내장 sink 스윕(scripts/sweep.py)이
  결함 범주를 전수 열거하고 diff가 건드린 모든 루틴(UNITS)을 읽기 바닥선으로 강제해
  누락을 막는다. 리뷰 깊이 모드는 없고 항상 정밀 분석하며, 게시 직전 자가 검증
  게이트로 false positive를 걸러낸다. dry-run 지원. 좌표·suggestion·게시는 전부
  scripts/의 테스트된 헬퍼가 결정적으로 처리한다.
---

# PR Inline Review Skill

이 스킬의 원칙: **모델은 "무엇을 지적할지"만 판단하고, "어디에 어떻게 붙일지"(line/side/줄수/suggestion 펜스/API)는 절대 손으로 계산하지 않는다.**
좌표·게시는 `scripts/review_post.py`가 diff를 파싱해 결정적으로 처리한다 (인라인 실수의 원천 제거).

모델이 만들어 내는 산출물은 단 하나 — **구조화된 findings JSON**. 그게 전부다.

---

## 인자 파싱

| 인자 | 예시 | 설명 |
|------|------|------|
| PR 번호/URL | `634`, `https://github.com/.../pull/634` | 없으면 로컬 브랜치 리뷰(항상 dry-run) |
| 저장소 | `FutureworkLabCorp/linkBrain-server` | 없으면 git remote 자동 탐지 |
| dry-run | `--dry-run`, `dry-run`, `콘솔`, `출력만`, `게시하지`, `테스트` | 게시 skip |
| fresh | `--fresh`, `fresh`, `기존 무시`, `skip-existing` | Step 1(기존 리뷰 조회) skip |

- **리뷰 깊이를 고르는 모드는 없다.** 항상 로컬 소스 열람 + call-site 역추적 + 테스트 갭까지 정밀 분석한다. `--dry-run`·`--fresh`는 깊이가 아니라 게시/중복처리 스위치일 뿐이다.
- PR 번호가 없으면 → 로컬 리뷰, **무조건 dry-run**.

---

## Step S. 셋업 체크 (최초 1회 / 문제 발생 시)

```bash
python scripts/setup_check.py --repo <OWNER/REPO>
```

`gh` 설치·인증·**active 계정의 org 접근 권한**·python 버전을 확인한다.
org 저장소인데 "Could not resolve repository"가 나오면 대개 active 계정 문제:
```bash
gh auth switch --user <org 권한 있는 handle>
```

---

## Step 0. 프로젝트 가이드 로드 (리뷰 기준)

리뷰 전에 저장소 규칙을 먼저 읽는다. 코드 평가 기준이 된다.

```bash
cat AGENTS.md 2>/dev/null                        # 1순위: Agent/리뷰 지침
ls docs/ 2>/dev/null                             # 2순위: 추가 가이드
cat CLAUDE.md 2>/dev/null                        # 3순위
```

메모할 기준: 최소 변경 원칙 / 기존 패턴 우선 / 보안·정확성·생명주기 우선 / 새 추상화는 real complexity를 제거할 때만 / 근거 없는 동의 금지.

**설계 패턴 레퍼런스(스킬 동봉):** `$SKILL_DIR/reference/patterns.md` — DB 컬럼 vs JSONB, hook/transaction 경계, ACL 적용 위치, RAG 파이프라인, lifecycle state machine 등 §1~§16 판단 기준. 전부 미리 읽지 말고 **Step 4에서 설계 판단이 걸리는 finding이 나올 때 해당 §만** 참조한다. (`$SKILL_DIR` = 이 SKILL.md가 있는 디렉토리의 절대경로)

### 프로젝트 스택 (linkBrain-server 기준)
Python, FastAPI, LangGraph, LangChain, PostgreSQL, SQLAlchemy, Alembic, Neo4j, Redis, Celery, Pydantic v2, mypy

---

## Step 0.5. 선행 분석 결과 수신 (있을 경우)

`/codebase-review` 등 다른 분석이 이미 완료됐다면 그 결과를 이슈 후보 목록으로 받아들인다 — Step 3-A 스윕의 HITS/UNITS와 **같은 성격의 후보 소스**다.

- 선행 분석의 리스크 항목을 Step 4의 출발점으로 삼는다(재발굴 중복 제거).
- Step 1에서 읽은 기존 GitHub 코멘트와 대조해 이미 제기된 것은 필터링한다.
- 선행 분석이 없으면 이 Step을 skip하고 Step 1로 이동한다.
- **후보일 뿐이다.** 선행 분석 항목도 Step 6 검증 게이트를 그대로 거친다(받아쓰지 않는다).

---

## Step 1. 기존 리뷰·반응 읽기 (중복 방지)  — `--fresh`면 skip

**클라우드 에이전트보다 유리한 첫 번째 이유: 이미 논의된 걸 다시 지적하지 않는다.**

```bash
gh pr view <PR> --repo <REPO> --json number,title,body,baseRefName,headRefName
gh api repos/<REPO>/pulls/<PR>/reviews --jq '.[] | "\(.user.login) \(.state): \(.body[0:200])"'
gh api "repos/<REPO>/pulls/<PR>/comments?sort=created&direction=desc&per_page=30" \
  --jq '.[] | "\(.path):\(.line) @\(.user.login) [+1=\(.reactions["+1"]) -1=\(.reactions["-1"])] \(.body[0:160])"'
gh api repos/<REPO>/issues/<PR>/comments --jq '.[] | "@\(.user.login): \(.body[0:240])"'
```

판단:
- 👎 반응 / PR author 반박 / resolved / 동일 파일·줄 동일 지적 → **해당 이슈 제외**.

---

## Step 2. diff 수집 (게시 스크립트가 다시 파싱하므로 여기선 "읽기"용)

```bash
gh pr diff <PR> --repo <REPO>
```

`+` 줄에만 집중한다. `-` 줄은 리포트하지 않는다.
> 좌표(line/side/position)는 절대 손으로 세지 않는다 — findings에는 **new-file 줄 번호만** 적고, 나머지는 `review_post.py`가 계산한다.

---

## Step 3. 소스 분석 (항상 정밀)

**클라우드보다 유리한 두 번째 이유: diff 텍스트만 보지 않는다.** 리뷰 깊이 모드는 없다 — 매번 아래를 전부 수행한다.

브랜치 전략 먼저 결정:
```bash
CUR=$(git rev-parse --abbrev-ref HEAD); echo "PR head=<headRefName> / current=$CUR"
```
- **same-branch:** Read 툴로 로컬 파일 직접 열람.
- **different-branch:** `git fetch origin <PR_HEAD> -q && git show origin/<PR_HEAD>:<path>` (파일 전체). call-site grep은 항상 로컬에서 가능.
  > **주의:** different-branch에서 `git diff --name-only <MB> HEAD`는 **현재 체크아웃 브랜치 기준**이라 사용 금지 — 변경 파일 목록은 Step 2의 diff(`+++ b/<path>`) 또는 스윕 출력에서 얻는다.

3-1. 변경 함수/클래스의 **주변 전체 컨텍스트**(hunk 밖 포함, 클래스/모듈 구조, import) 읽기.
3-2. **call-site 역추적**:
```bash
grep -rn "함수명\|클래스명" src/ --include="*.py" -l
```
타입 흐름 체크리스트:
- `isinstance(x, str)` / `if x` 필터 → 실제 caller가 다른 타입(UUID 등)을 넣을 수 있나?
- `json.dumps`↔`json.loads` 왕복 전후 타입이 바뀌나 (uuid.UUID→str)?
- in-memory 캐시 vs DB/Redis 로드 경로의 타입이 일치하나?

3-3. **기존 패턴 대조:** `grep -rn "pattern" src/` — 새 코드가 기존 패턴을 따르나, 불일치 추상화를 들여오나.
3-4. **테스트 갭:** 새 `+` 코드 경로(분기·루프·early return)를 열거하고 테스트 존재 여부 매핑. 미커버는 LOW/MEDIUM.
3-5. **외부 스토리지 값의 단위·타입을 주장하는 이슈**라면, 그 write 경로를 grep으로 확인한 뒤 리포트 (단위 오해 false-positive 방지).

---

## Step 3-A. 기계적 sink 스윕 (내장 `scripts/sweep.py`) — 커버리지 강제

수동 분석(Step 3)은 "가장 눈에 띄는" 문제는 찾지만 **커버리지**를 보장하지 못한다. 스윕은 그 강제 장치로, discovery를 기계화해 모델이 판단에만 집중하게 한다. **이 스킬에 내장**돼 있어(`scripts/sweep.py` + `scripts/patterns.toml`) 다른 스킬 의존이 없다.

```bash
SKILL_DIR=<이 SKILL.md가 있는 디렉토리의 절대경로>

# same-branch: 현재 워킹트리가 곧 PR head
uv run python "$SKILL_DIR/scripts/sweep.py" --base origin/develop

# different-branch: 브랜치 전환 없이 PR head를 detached worktree로 materialize
git fetch origin <PR_HEAD> -q
WT=$(mktemp -d)/pr-<PR>; git worktree add --detach "$WT" FETCH_HEAD -q
MB=$(git merge-base origin/develop FETCH_HEAD)
( cd "$WT" && uv run python "$SKILL_DIR/scripts/sweep.py" --base "$MB" )
git worktree remove "$WT" --force
```

출력 세 섹션을 이렇게 소비한다:
- **`== HITS ==` / `== MANUAL ==`** — Step 4 이슈 후보의 출발점(각 행은 후보일 뿐, 반드시 Step 6에서 검증). 카테고리별 전수라 "빠뜨림"을 막는다.
- **`== UNITS ==`** — diff가 건드린 **모든 루틴**(Python=`ast`, TS/TSX=tree-sitter). 이게 "무엇을 읽을지"의 **바닥선**이다: 패턴이 침묵한 의도(intent) 버그에 닿는 통로. 각 UNIT을 `Read(file, offset, limit)`로 읽고 Step 4에서 finding 또는 "read, clean"으로 처리한다. `func`=함수 통째(데코 포함), `block`=거대 함수의 hunk 블록만(+`oversized-fn` 플래그=자체 finding 후보), `module`=모듈 레벨. `callees`/`callers`는 HIT/타입경계가 필요를 만들 때만 한 홉 확장.

**린터 정책 (ruff / eslint).** ruff(Python)는 순수 정적 린트라 항상 돈다. **eslint(TypeScript)**는 트리의 `node_modules`(플러그인·파서·tsconfig)가 필요해 **local-only**다 — 스캔 트리에 `node_modules/.bin/eslint`가 있을 때만 자동 실행되고, 없으면 조용히 skip(경고 한 줄)된다. 그래서 worktree(리모트 PR)엔 `node_modules`가 없어 eslint는 기본 빠지고 regex·AST·dead-code·ruff는 정상 동작한다. eslint까지 강제하려면 원본 `node_modules`를 worktree에 심볼릭 링크한 뒤 `--linter`를 붙인다 — 이 비용(설치본 공유·타입 정보 로딩)이 부담되면 그대로 skip하고 **TS 정밀 린트는 해당 레포 자체 리뷰 스킬(예: AxFlow `pr-code-review`)에 맡긴다**.

> **env 의존만 남음(스킬 의존 아님)**: ruff(Python 린트)·eslint(TS 린트, local-only)·tree-sitter(TS UNITS)·jedi(타입인지 DEAD-CODE/callers)는 `uv run`으로 프로젝트 env에서 해석되고, 없으면 각 레인만 조용히 skip/폴백된다(§README). 스윕은 discovery만 담당 — 게시 좌표는 Step 7의 `review_post.py`가 별도로 계산한다.

---

## Step 4. 분석 기준 & false-positive 필터

### 우선순위 (AGENTS.md §Reviews)
1. 정확성(논리 버그·조건 오류·async·edge case) → 2. 동작 회귀 → 3. 보안(인증 우회·권한 bypass·null) → 4. 생명주기(트랜잭션 원자성·리소스 누수·phantom 상태) → 5. 누락 테스트 → 6. 타입 안전성 → 7. 확장성 → 8. 패턴 일관성 → 9. 스타일(LOW만).

설계 판단이 걸리는 finding(DB 모델 선택, hook vs transaction, ACL 위치, RAG lifecycle, state machine 등)은 `$SKILL_DIR/reference/patterns.md`의 해당 §를 근거로 삼는다.

### 보고 전 자기검증 (반드시)
- 이미 코드에서 처리됐나? (exists 체크·fallback·str() 변환 등)
- 이 경로가 실제 실행 가능한가?
- Step 1에서 이미 논의된 이슈인가?
- **근거 없는 칭찬 금지. 기본적으로 제안에 동의하지 않는다 (AGENTS.md).**

---

## Step 5. findings 초안 작성

지적 후보를 **리스트**로 정리한다(아직 최종본 아님 — Step 6에서 걸러진다). 첫 원소로 `_summary`(선택)를 넣을 수 있다 — 리뷰 본문에 들어갈 **자유 markdown**으로, PR 개요·verdict 산문·인라인로 안 가는 non-blocking 언급을 여기에 쓴다. 요약 산문은 모델의 몫이고, 스크립트는 그 뒤에 심각도 집계표만 결정적으로 부착한다.

```json
[
  {"_summary": "OCR 실패 알림 dedup 누락 + 초대 read_at 시맨틱 점검.\n\n**머지 전 HIGH 1건 해결 필요.** 나머지는 non-blocking."},
  {
    "path": "src/app/notifications/service.py",
    "line": 142,
    "start_line": 138,
    "severity": "HIGH",
    "category": "bug",
    "title": "OCR 실패 알림이 ref_key=None으로 무한 누적",
    "explanation": "failed 루프가 document_uuid=None으로 호출 → ref_key=None → Postgres에서 매번 새 행 INSERT.",
    "suggestion": ["    ref_key = f\"document_ocr:{document_name}\"", "    await _upsert_notification(db, user_uuid, ref_key, ...)"]
  }
]
```

**규칙 (엄수):**
- `line` = **new-file 줄 번호** (diff의 `+`/컨텍스트 줄). position 계산 금지.
- 여러 줄 교체는 `start_line`(같은 hunk 내, `line`보다 작거나 같음)만 추가. side는 기본 RIGHT.
- `suggestion`은 **교체 후 남길 줄들의 배열**. ` ```suggestion ` 펜스를 직접 쓰지 않는다 — 스크립트가 감싼다.
- **범위는 통째로 교체된다.** GitHub Apply는 `start_line`~`line` **전 줄을 삭제하고 suggestion 전체를 삽입**한다. 범위 안에서 유지할 줄도 suggestion에 반드시 포함한다 — 부분 교체에서 줄을 빠뜨리면 Apply가 그 줄을 지운다(스크립트가 범위보다 짧은 suggestion을 경고). 줄 삽입으로 suggestion 줄 수 > 범위 줄 수가 되는 건 정상.
- **side 선택:** `+`/컨텍스트 줄만이면 RIGHT(기본). `-`줄만 있는 위치에는 suggestion 불가(이미 삭제된 줄 — `explanation`으로 서술, 스크립트가 경고). `-`/`+` 혼합 블록 전체 교체는 `start_side: "LEFT"`(old 줄 번호) + `side: "RIGHT"`(new 줄 번호)로 가능.
- suggestion 각 줄의 **들여쓰기는 실제 파일과 정확히 일치**시킨다 (스크립트가 첫 줄 들여쓰기 불일치를 경고로 잡아준다).
- 설계 변경이 필요해 one-click suggestion이 불가능하면 `suggestion`을 빼고 `explanation`에 방향만 서술.
- **위키 인용은 `[[Page]]` / `[[라벨|Page]]` 로 쓴다.** GitHub 은 이 문법을 위키 안에서만 해석해서 PR 코멘트에는 그냥 글자로 남는데, `review_post.py` 가 게시 전에 `https://github.com/OWNER/REPO/wiki/Page` 로 펴 준다. `#앵커`도 따라간다. 코드 펜스와 인라인 코드 안은 건드리지 않으니 이 문법 자체를 설명할 때도 안전하다. 로컬에 `wiki/` 클론이 있으면 없는 페이지를 경고로 알려준다(링크는 그대로 붙는다).
- `category`: security | bug | regression | lifecycle | type-safety | extensibility | pattern | style

> diff **밖** 줄을 고쳐야 하면 suggestion 대신 `explanation`에 `Before/After` 코드블록으로 안내한다 (해당 줄은 인라인 앵커가 불가하므로).

---

## Step 6. 검증 게이트  ← 게시 전 false-positive 제거 (품질의 핵심)

**초안을 그대로 올리지 않는다.** Step 5의 후보 전체를 한 번에 놓고, 각 항목을 **적대적으로 다시 검증**한다. "내가 지적한 게 진짜 문제인가"를 스스로 반박해 보는 단계다. 리뷰봇 신뢰를 가장 크게 깎는 게 그럴듯하지만 틀린 지적이므로, 이 게이트가 체감 품질을 좌우한다.

각 후보에 대해 아래를 실제 코드(Step 3에서 읽은 소스·호출부)와 대조해 판정한다:

1. **진짜 버그인가?** — 주장한 실패 시나리오가 실제로 성립하는가. 구체적 입력/상태 → 잘못된 출력/크래시로 이어지는 경로를 댈 수 있나. 못 대면 → **드롭**.
2. **이미 처리됐나?** — 상위/하위에 exists 체크·fallback·try/except·타입 변환·기본값이 이미 있나. 있으면 → **드롭**.
3. **실행 경로가 도달 가능한가?** — dead code·불가능한 분기·호출되지 않는 함수면 → **드롭 또는 LOW 강등**.
4. **이미 논의됐나?** — Step 1의 기존 코멘트/반응과 중복이면 → **드롭**.
5. **근거 있는 심각도인가?** — 정확성/보안/생명주기 근거 없이 취향·스타일이면 → **LOW 강등 또는 드롭**. (AGENTS.md: 근거 없는 동의·지적 금지)
6. **suggestion이 실제로 맞나?** — 제안 코드가 컴파일/동작하고 주변 들여쓰기·시그니처와 일치하나. 어긋나면 → suggestion 제거하고 `explanation`만 남김.

**살아남은 항목만** 최종 `findings.json`으로 파일에 쓴다. 이게 모델의 유일한 산출물이다.
- 전부 드롭돼도 정상이다 — 지적할 게 없으면 빈 리스트(+`_summary`)로 둔다. 스크립트는 `APPROVE`로 판정하되 **자기 PR을 자동 승인하지 않도록** 게시는 생략하고 콘솔에만 출력한다.
- 억지로 개수를 채우지 않는다. 확신하는 것만 남긴다.

---

## Step 7. 게시 / dry-run  ← 스크립트가 전부 처리

findings를 파일로 저장한 뒤:

```bash
# dry-run (콘솔만, 게시 안 함)
python scripts/review_post.py --repo <REPO> --pr <PR> --findings findings.json --dry-run

# 실제 게시
python scripts/review_post.py --repo <REPO> --pr <PR> --findings findings.json
```

스크립트가 하는 일 (모델은 관여하지 않음):
- diff를 파싱해 각 finding의 `line/side/start_line`을 **검증** → diff 밖이면 **±10줄 이내만** 최근접 줄로 스냅, 그보다 멀면 skip (어디에 달지는 모델의 판단이므로 스크립트가 임의 이동하지 않는다 — skip 사유를 보고 모델이 재앵커).
- `_summary`·`title`·`explanation` 의 `[[Page]]` 를 위키 URL 로 변환(`--no-wiki-links` 로 끄고, 위키가 딴 데 있으면 `--wiki-base`).
- 심각도 SVG 배지(Gemini식 `![HIGH](...gstatic...)`) + `**[SEV] category** — title` 접두어와 ` ```suggestion ` 펜스를 자동 부착. 리뷰 본문 = 모델의 `_summary` markdown + 스크립트의 심각도 집계표.
- event 기본 결정: **CRITICAL/HIGH 있으면 REQUEST_CHANGES / MEDIUM·LOW만 COMMENT / 없으면 APPROVE**. 이것도 판단이므로 `--event REQUEST_CHANGES|COMMENT`로 오버라이드 가능(APPROVE는 게시 자체가 불가 — self-approve 방지).
- **line 기반 단일 배치**로 `/pulls/{pr}/reviews`에 1회 게시 (position 안 씀).
- 배치 실패 시 **개별 코멘트 폴백** — 앵커 하나가 깨져도 나머지는 살린다.
- 앵커 가능한 코멘트가 하나도 없어도 **요약 리뷰만 게시**해 verdict를 보존한다 — findings가 전부 skip된 경우든, 지적 없이 `_summary`만 남기는 경우든. 막히는 건 APPROVE 하나뿐이고(자기 PR 자동승인 불가), findings가 없으면 그게 기본 event라 요약만 올리려면 `--event COMMENT`를 명시해야 한다. dry-run도 같은 판정을 출력하므로, 게시될지 여부를 미리 볼 수 있다.
- dry-run·게시 모두 콘솔에 이슈 목록·skip·좌표 보정 경고(들여쓰기 불일치 포함)를 출력.

게시 후 스크립트가 출력한 리뷰 URL을 사용자에게 전달한다.

---

## 요약 흐름

```
setup_check → 가이드 로드 → (기존 리뷰 읽기) → diff 수집
   → sweep.py(HITS/MANUAL 후보 + UNITS 읽기 바닥선) → 소스 정밀 분석 → findings 초안
   → ★검증 게이트(false-positive 제거)★ → 최종 findings.json
   → review_post.py --dry-run 로 검증 → 이상 없으면 게시
```

두 축의 분업: **발견(discovery)** = `sweep.py`가 결함 범주 전수 + 읽을 루틴 열거로 커버리지를 강제하고, **게시(posting)** = `review_post.py`가 좌표·suggestion·API·event를 결정적으로 처리한다. 모델은 그 사이에서 **판단**(findings.json)만 만든다.

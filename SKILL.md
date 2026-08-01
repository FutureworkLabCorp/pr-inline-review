---
name: pr-inline-review
description: >
  PR 코드리뷰를 수행하고 결과를 GitHub PR에 인라인 코멘트로 게시한다.
  로컬 소스코드 직접 분석, 프로젝트 가이드 적용, 기존 리뷰 반응 인식을 통해
  클라우드 전용 리뷰 에이전트보다 깊이 있는 리뷰를 제공한다.
  dry-run 모드 지원: 게시 없이 콘솔 출력만.
---

# PR Inline Review Skill

## 인자 파싱

인자 문자열에서 다음을 추출한다.

| 인자 | 예시 | 설명 |
|------|------|------|
| PR 번호 또는 URL | `634` / `https://github.com/.../pull/634` | 필수 |
| 저장소 | `FutureworkLabCorp/linkBrain-server` | 없으면 git remote에서 자동 탐지 |
| dry-run 플래그 | `--dry-run` / `dry-run` / `콘솔 출력만` / `게시하지 말고` | 있으면 GitHub 게시 skip |
| fresh 플래그 | `--fresh` / `fresh` / `기존 무시` / `기존 커멘트 무시` | 있으면 Step 1 기존 코멘트 조회 skip |

**dry-run 트리거 키워드** (대소문자 무관):
`--dry-run`, `dry-run`, `콘솔`, `출력만`, `게시하지`, `테스트`

**fresh 트리거 키워드** (대소문자 무관):
`--fresh`, `fresh`, `기존 무시`, `기존 커멘트 무시`, `skip-existing`

로컬 브랜치 대상(PR 번호 없음)은 항상 dry-run으로 동작한다.

## 프로젝트 스택

Python, FastAPI, LangGraph, LangChain, PostgreSQL, SQLAlchemy, Alembic, Neo4j, Redis, Celery, Pydantic v2, mypy

---

## Step 0. 프로젝트 가이드 로드 (리뷰 전 필수)

리뷰 시작 전에 이 저장소의 규칙을 먼저 읽는다. 코드 평가의 기준이 된다.

```bash
# 1순위: Agent/LLM 행동 지침
cat AGENTS.md

# 2순위: 이 skill의 설계 패턴 레퍼런스
# .claude/skills/codebase-review/patterns.md
# .claude/skills/codebase-review/procedure.md

# 3순위: 추가 가이드 (존재하는 경우)
ls docs/
```

가이드에서 리뷰 기준으로 쓸 항목을 메모한다:
- 최소화 원칙 (Prefer minimal, focused changes)
- 기존 패턴 우선 (Follow existing project patterns)
- 보안·정확성·생명주기 이슈 우선 (AGENTS.md §Reviews)
- 새 추상화 기준 (real complexity 제거 시에만)
- 근거 없는 동의 금지 (Do not agree with proposed changes by default)

---

## Step 0.5. 선행 분석 결과 수신 (있을 경우)

`/codebase-review` 또는 다른 분석이 이미 완료됐다면 그 결과를 이슈 후보 목록으로 받아들인다.

- 선행 분석의 리스크 항목을 Step 4의 출발점으로 삼는다 (재발굴 중복 제거)
- Step 1에서 읽은 기존 GitHub 코멘트와 대조해 이미 제기된 것은 필터링한다
- 선행 분석이 없으면 이 Step을 skip하고 Step 1로 이동한다

---

## Step 1. 기존 PR 리뷰 및 반응 읽기 (중복 방지)

**fresh 플래그가 있으면 이 Step 전체를 skip하고 Step 2로 바로 이동한다.**
모든 이슈를 새로 발굴하고 기존 코멘트와의 중복을 허용한다.

**우리가 클라우드 에이전트보다 유리한 첫 번째 이유.**
이미 논의된 이슈를 다시 제기하지 않는다.

```bash
# PR 기본 정보
gh pr view <PR_NUMBER> --repo <OWNER/REPO> \
  --json number,title,body,baseRefName,headRefName

# 기존 리뷰 목록 (state: COMMENTED / APPROVED / CHANGES_REQUESTED)
gh api repos/<OWNER/REPO>/pulls/<PR_NUMBER>/reviews \
  | python3 -c "
import json,sys
reviews = json.load(sys.stdin)
for r in reviews:
    print(f'review_id={r[\"id\"]} user={r[\"user\"][\"login\"]} state={r[\"state\"]}')
    print(r['body'][:300])
    print()
"

# 최근 인라인 코멘트 30개 (오래된 코멘트는 이미 resolved됐거나 무관할 가능성이 높음)
gh api "repos/<OWNER/REPO>/pulls/<PR_NUMBER>/comments?sort=created&direction=desc&per_page=30" \
  | python3 -c "
import json,sys
comments = json.load(sys.stdin)
for c in comments:
    print(f'path={c[\"path\"]} line={c.get(\"line\")} author={c[\"user\"][\"login\"]}')
    print(c['body'][:200])
    if c.get('reactions', {}).get('total_count', 0) > 0:
        print(f'  reactions: +1={c[\"reactions\"][\"+1\"]} -1={c[\"reactions\"][\"-1\"]}')
    print()
"

# PR author의 reply 코멘트 (이슈에 대한 반박/설명 확인)
gh api repos/<OWNER/REPO>/issues/<PR_NUMBER>/comments \
  | python3 -c "
import json,sys
comments = json.load(sys.stdin)
for c in comments:
    print(f'author={c[\"user\"][\"login\"]}')
    print(c['body'][:300])
    print()
"
```

**읽은 후 판단:**
- 👎 반응이 달린 코멘트 → 작성자가 반박했거나 잘못된 지적으로 판단된 것 → **skip**
- `resolved` / PR author의 명시적 반박 → **skip**
- 이미 같은 파일·줄에 동일 패턴의 코멘트가 있으면 → **skip**

---

## Step 2. diff 수집 및 position 매핑

```bash
gh pr diff <PR_NUMBER> --repo <OWNER/REPO>
```

Python으로 diff를 파싱해 `{(path, new_lineno): position}` 매핑 구성:

```python
import re

def parse_diff_positions(diff_text):
    mapping = {}  # (path, new_lineno) -> position
    path = None
    pos = 0
    new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git"):
            path = None; pos = 0; new_line = 0
        elif raw.startswith("+++ b/"):
            path = raw[6:]
            pos = 0; new_line = 0
        elif raw.startswith("@@"):
            m = re.search(r'\+(\d+)', raw)
            if m:
                new_line = int(m.group(1)) - 1
            pos += 1
        elif path and raw[:1] in ("+", "-", " "):
            pos += 1
            if raw[:1] in ("+", " "):
                new_line += 1
                mapping[(path, new_line)] = pos
    return mapping
```

---

## Step 3. 로컬 소스코드 직접 분석

**우리가 클라우드 에이전트보다 유리한 두 번째 이유.**
diff 텍스트만 보지 않는다. 변경된 파일을 실제로 열어서 전체 컨텍스트를 파악한다.

### 3-0. 브랜치 일치 여부 확인 (소스 접근 전략 결정)

```bash
# Step 1에서 읽은 PR headRefName과 현재 로컬 브랜치를 비교
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
PR_HEAD=<headRefName from Step 1>

if [ "$CURRENT_BRANCH" = "$PR_HEAD" ]; then
    echo "same-branch: Read 툴로 로컬 파일 직접 접근 가능"
else
    echo "different-branch: gh API 또는 git fetch 필요"
fi
```

**브랜치 일치 시 (same-branch):** Read 툴로 로컬 파일 직접 접근.

**브랜치 불일치 시 (different-branch):** 두 가지 방법 중 선택:

```bash
# 방법 A: git fetch 후 git show (파일 전체 내용)
git fetch origin <PR_HEAD> -q
git show origin/<PR_HEAD>:<path/to/file>

# 방법 B: GitHub Contents API (체크아웃 없이)
gh api repos/<OWNER/REPO>/contents/<path>?ref=<PR_HEAD> \
  --jq '.content' | base64 -d
```

방법 A가 더 빠르고 call site grep도 로컬에서 가능하므로 기본으로 사용한다.
단, `git fetch`가 느리거나 불필요한 경우(파일 1~2개)는 방법 B도 무방하다.

> **주의:** 브랜치 불일치 시 `git diff --name-only $MERGE_BASE HEAD`는 현재 체크아웃 브랜치 기준이라 사용 금지. 파일 목록은 Step 2에서 얻은 diff에서 추출한다.

### 3-1. 변경 파일 목록 확인

**same-branch인 경우:**
```bash
MERGE_BASE=$(
  git merge-base origin/develop HEAD 2>/dev/null ||
  git merge-base origin/main HEAD 2>/dev/null
)
git diff --name-only $MERGE_BASE HEAD
```

**different-branch인 경우:**
Step 2에서 파싱한 diff의 `+++ b/<path>` 줄에서 파일 목록을 추출한다 (별도 git 명령 불필요).

### 3-2. 각 파일의 전체 내용 읽기

diff에서 변경된 함수/클래스를 확인한 뒤, 그 **주변 전체 컨텍스트**를 읽는다:
- 변경된 함수의 전체 본문 (hunk 경계 밖의 줄 포함)
- 함수가 속한 클래스/모듈의 전체 구조
- import 목록 전체

**same-branch:** Read 툴로 직접 읽는다.

**different-branch:** `git show origin/<PR_HEAD>:<path>` 결과를 분석한다.
단, call site 역추적(3-3)은 항상 현재 로컬 브랜치 기준 grep이 가능하다 — 호출자가 PR 브랜치가 아닌 쪽에 있으므로 오히려 정확하다.

### 3-3. Call site 역추적 (타입·패턴 추적의 핵심)

```bash
# 변경된 함수/클래스를 실제로 호출하는 곳 탐색
grep -rn "함수명\|클래스명" src/ --include="*.py" -l

# 타입 필터가 있으면 (isinstance, if value 등) 실제로 어떤 타입이 들어오는지 역추적
grep -n "team_uuid\|workspace_uuid" src/app/data/service.py | head -20
```

**타입 흐름 추적 체크리스트:**
- `isinstance(value, str)` / `if value` 필터가 있는가?
  - 그렇다면: 실제 call site에서 `str` 외의 타입(UUID 객체 등)이 들어올 수 있는가?
- 직렬화(json.dumps) → 역직렬화(json.loads) 왕복 경계가 있는가?
  - 그렇다면: 왕복 전후 타입이 달라지는가? (uuid.UUID → str 등)
- in-memory 캐시와 DB/Redis 로드 경로가 공존하는가?
  - 그렇다면: 두 경로의 타입이 일치하는가?

### 3-4. 기존 패턴 대조

```bash
# 유사한 패턴이 이미 코드베이스에 있는지 확인
grep -rn "pattern_keyword" src/ --include="*.py" | head -20
```

새 코드가 기존 패턴을 따르는지, 아니면 불일치하는 추상화를 도입하는지 판단한다.

### 3-5. 테스트 커버리지 갭 탐지

새로 추가된 `+` 코드 경로(함수 분기, 루프, early return)를 열거한 뒤, 테스트 파일에서 각 경로를 커버하는 테스트가 있는지 매핑한다.

```
| 신규 코드 경로              | 대응 테스트                         | 상태 |
|-----------------------------|-------------------------------------|------|
| single-doc avg_chunk_tokens | test_avg_chunk_tokens_computed_*    | ✅   |
| all-docs total_chars        | -                                   | ❌   |
```

커버되지 않은 경로가 있으면 LOW 또는 MEDIUM 이슈로 리포트한다.

---

## Step 4. 분석 기준 (AGENTS.md 기반)

diff의 `+` 줄에 집중. `-` 줄은 리포트하지 않는다. **근거 없는 칭찬 금지.**

### 우선순위 (AGENTS.md §Reviews 순서)

1. **정확성(correctness)** — 논리 버그, 조건 오류, async 문제, edge case
2. **동작 회귀(behavioral regression)** — 기존 기능이 조용히 깨지는가
3. **보안 리스크** — 인증 우회, 권한 bypass, null 체크 부재
4. **생명주기 이슈** — 트랜잭션 원자성, 리소스 누수, phantom 상태
5. **누락된 테스트** — 핵심 경로에 테스트가 없는가
6. **타입 안전성** — isinstance 필터 오류, 직렬화 타입 불일치, 스키마 과소 일반화
7. **확장성** — 너무 구체적인 타입 힌트, 하드코딩, 미래 타입 추가 시 깨짐
8. **패턴 일관성** — 기존 코드베이스 패턴과 불일치
9. **스타일** — LOW에만, 생략 가능

### 이슈 레코드 형식

```python
{
    "path": "src/app/core/worker.py",
    "new_lineno": 196,
    "severity": "HIGH",        # HIGH | MEDIUM | LOW
    "category": "type-safety", # security | bug | regression | lifecycle | type-safety | extensibility | pattern | style
    "title": "UUID object silently dropped by isinstance(value, str)",
    "body": "..."
}
```

### 보고 전 검증 (false positive 방지)

이슈를 보고하기 전에 **diff에서 직접 확인한다**:
- "이 문제가 이미 코드에서 처리됐는가?" (exists=True, fallback 값, str() 변환 등)
- "이 코드 경로가 실제로 실행 가능한가?"
- "Step 1에서 읽은 기존 코멘트에서 이미 논의된 이슈인가?"
- "외부 스토리지(LightRAG KV, Redis, 서드파티 API)에서 읽어온 값의 단위·타입을 주장하는 이슈라면, 해당 스토리지의 write 경로 코드를 grep해서 실제 저장 방식을 확인한 뒤 리포트한다." (단위 오해로 인한 false positive 방지)

---

## Step 5. 코멘트 본문 작성

### 형식

```markdown
**[HIGH] type-safety** — UUID object silently dropped by `isinstance(value, str)`

`task.kwargs`의 `team_uuid`가 `uuid.UUID` 객체로 들어오면 `isinstance(value, str)` 필터에서
탈락해 Redis metadata에 저장되지 않는다. 이후 `get_task()`는 해당 UUID를 phantom으로 판단해
None 반환 → 404.

실제 호출처(`upload_share_process`)에서 현재는 str이 들어오지만, 다른 caller가 추가되면
조용히 깨진다.

```suggestion
    payload = {
        key: str(value)
        for key, value in {
            "user_uuid": task.user_uuid,
            "team_uuid": task.kwargs.get("team_uuid"),
            "workspace_uuid": task.kwargs.get("workspace_uuid"),
        }.items()
        if value is not None and str(value)
    }
```
```

**핵심: `suggestion` 블록 사용**
- GitHub의 ` ```suggestion ` 문법을 항상 포함한다
- 리뷰어가 one-click으로 적용할 수 있는 수준의 구체적 수정안을 제시한다
- suggestion이 불가능한 경우(설계 변경 필요)만 텍스트 설명으로 대체

### suggestion 범위 지정 원칙

**핵심: "apply 후 파일에서 어떤 줄을 교체하는가"로 범위를 결정한다.**

GitHub는 범위 전체를 삭제하고 suggestion 줄 전체를 삽입한다.  
suggestion 본문에는 **교체 결과만** 넣는다 — 범위 밖에 있는 줄을 포함시키면 중복된다.

#### side 선택 기준

| 범위 내 줄 구성 | start_side | side |
|----------------|-----------|------|
| `+` 줄 또는 ` `(맥락) 줄만 | RIGHT | RIGHT |
| `-` 줄 + `+` 줄 혼합 | LEFT | RIGHT |
| `-` 줄만 | LEFT | LEFT — suggestion apply 불가 (이미 삭제된 줄) |

**suggestion이 apply 가능하려면 `side=RIGHT`이어야 한다.**

#### Case A — RIGHT→RIGHT

```
start_line=1512, start_side=RIGHT
line=1514,       side=RIGHT
```

GitHub는 R1512~R1514 전체를 삭제하고 suggestion 줄 전체를 삽입한다.  
범위 안에서 유지하고 싶은 줄은 suggestion에도 그대로 포함해야 한다.

줄을 삽입해서 suggestion 줄 수 > 범위 줄 수가 되는 것은 정상이다 — GitHub가 의도대로 처리한다.

**⚠️ 주의: 범위 안 일부 줄만 바꾸고 나머지는 유지하는 경우**  
suggestion 줄 수 = 범위 줄 수여야 한다.  
줄 수가 다르면 GitHub가 유지하려던 줄까지 삭제·추가한다.  
예: R1096~R1098(3줄) 중 R1097만 교체 → suggestion도 반드시 3줄.

예시 — R1512~R1514 범위에서 raise 앞에 줄 삽입 (줄 수 달라도 OK):
````markdown
```suggestion
        msg = f"no approved OCR corrections for {doc_id}; nothing to reindex"
        logger.error("[OCR] %s", msg)
        async with async_context_session() as db:
            await rag_service.update_document_status(
                db, existing_doc.uuid, RAGStatus.FAILED, error_message=msg,
            )
        raise RuntimeError(msg)
```
````

예시 — R1096~R1098 중 R1097만 교체 (줄 수 일치 필수):
````markdown
```suggestion
        "ack": (
            {"acked_at": ack.acked_at.isoformat(), "acked_by": ack.acked_by}
            if ack is not None
```
````

#### Case B — LEFT→RIGHT

```
start_line=1745, start_side=LEFT   ← old file(삭제 줄) 번호
line=1742,       side=RIGHT        ← new file(추가 줄) 번호
```

`-` 줄과 `+` 줄이 섞인 diff 블록 전체를 교체할 때 사용한다.  
`line - start_line + 1` 공식은 적용되지 않는다 (LEFT·RIGHT는 서로 다른 파일 기준).  
suggestion 본문에는 교체 후 파일에 남길 내용만 넣는다.

---

API 호출 시 `position` 대신 `start_line`/`line` 사용 (Step 6-4 참고).

**공백 일치 주의:** suggestion 내 들여쓰기가 실제 파일과 정확히 같아야 Apply 버튼이 올바르게 동작한다.

### diff 외부 줄 수정 안내

`suggestion` 블록은 **PR diff에 포함된 줄에만** 적용된다.
수정이 필요한 줄이 diff 밖에 있을 때는 unified diff 코드블록으로 안내한다.

**작성 절차:**
1. Read 툴로 해당 파일을 읽어 **정확한 줄 번호를 Read 출력에서 확인한다.** 줄 번호를 추측하지 않는다.
2. 변경 줄 위아래로 컨텍스트 3줄을 포함해 `@@` 헤더를 계산한다.

```
context = 3
old_start = change_line - context
old_count = context + (제거 줄 수) + context
new_count = context + (추가 줄 수) + context
```

3. 코멘트 본문에 ` ```diff ` 블록으로 삽입한다:

````markdown
```diff
@@ -4224,7 +4224,7 @@
     context_line_above_2
     context_line_above_1
-    old_code
+    new_code
     context_line_below_1
     context_line_below_2
```
````

변경 범위가 넓거나 `@@` 계산이 복잡하면 Before/After 블록으로 대체한다:

````markdown
File: `path/to/file.py`  line 4226

Before:
```python
    old_code
```

After:
```python
    new_code
```
````

심각도 접두어:
- `**[HIGH] security**` — 인증·권한·데이터 노출
- `**[HIGH] bug**` — 장애·데이터 손상
- `**[HIGH] type-safety**` — 타입 불일치로 인한 silent failure
- `**[MEDIUM] bug**` / `**[MEDIUM] lifecycle**` / `**[MEDIUM] extensibility**`
- `**[LOW] pattern**` / `**[LOW] style**`

---

## Step 6. 결과 출력 / GitHub PR Review 게시

**dry-run이면 6-1~6-4를 건너뛰고 콘솔 출력(6-5)만 수행한다.**

### 6-1. position 매핑

```python
for issue in issues:
    key = (issue["path"], issue["new_lineno"])
    pos = mapping.get(key)
    if pos is None:
        file_positions = sorted(p for (f, _), p in mapping.items() if f == issue["path"])
        pos = file_positions[0] if file_positions else 1
    issue["position"] = pos
```

### 6-2. 리뷰 이벤트 결정

| 조건 | event |
|------|-------|
| HIGH 이슈 존재 | `REQUEST_CHANGES` |
| MEDIUM 이하만 | `COMMENT` |
| 이슈 없음 | `APPROVE` |

### 6-3. 전체 리뷰 요약 (body)

```markdown
## Code Review

(PR 한 줄 요약). 아래 이슈를 인라인 코멘트로 표시했다.

| 심각도 | 건수 |
|--------|------|
| HIGH   | N    |
| MEDIUM | N    |
| LOW    | N    |

(HIGH 있으면) **머지 전 HIGH 이슈 해결 필요.**
(없으면) **Approve with comments.**

*분석 범위: 로컬 소스코드 직접 검수 + call site 역추적 + 기존 리뷰 반응 반영.*
```

### 6-4. API 호출

#### A. 배치 리뷰 (단일 줄 코멘트만 — `position` 기반)

```python
import json, subprocess, tempfile, os

def post_review(repo, pr_number, summary_body, event, issues):
    comments = [
        {"path": i["path"], "position": i["position"], "body": i["body"]}
        for i in issues
    ]
    payload = {"body": summary_body, "event": event, "comments": comments}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        tmp = f.name
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}/reviews",
             "--method", "POST", "--input", tmp],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            print(f"ERROR: {r.stderr}")
        else:
            review = json.loads(r.stdout)
            print(f"Posted: {review.get('html_url')}")
    finally:
        os.unlink(tmp)
```

#### B. 단일 코멘트 (여러 줄 선택 suggestion — `start_line`/`line` 기반)

여러 줄을 커버하는 suggestion은 reviews 배치 API가 지원하지 않는다.
`/pulls/{pr}/comments` 엔드포인트를 직접 호출해야 한다.

```bash
HEAD_SHA=$(gh pr view <PR_NUMBER> --repo <OWNER/REPO> \
  --json commits --jq '.commits[-1].oid')

# Case A — 동일 사이드 (RIGHT→RIGHT): 추가된 줄끼리 범위
gh api repos/<OWNER/REPO>/pulls/<PR_NUMBER>/comments \
  --method POST \
  --field commit_id="$HEAD_SHA" \
  --field path='path/to/file.py' \
  --field start_line=<new_file_시작줄> \
  --field start_side='RIGHT' \
  --field line=<new_file_끝줄> \
  --field side='RIGHT' \
  --field body='...(suggestion 포함 본문)...' \
  --jq '.html_url'

# Case B — 크로스사이드 (LEFT→RIGHT): 삭제 줄에서 추가 줄까지
gh api repos/<OWNER/REPO>/pulls/<PR_NUMBER>/comments \
  --method POST \
  --field commit_id="$HEAD_SHA" \
  --field path='path/to/file.py' \
  --field start_line=<old_file_시작줄> \
  --field start_side='LEFT' \
  --field line=<new_file_끝줄> \
  --field side='RIGHT' \
  --field body='...(suggestion 포함 본문)...' \
  --jq '.html_url'
```

- **Case A** `start_side=RIGHT, side=RIGHT`: 둘 다 new file 기준 줄 번호. suggestion 줄 수 = `line - start_line + 1` 이어야 함.
- **Case B** `start_side=LEFT, side=RIGHT`: start는 old file 기준, end는 new file 기준. 줄 번호 체계가 달라 줄 수 공식 불적용. suggestion은 해당 diff 블록 전체를 교체.
- suggestion 본문에는 Case A의 경우 `start_line`~`line` 범위의 **모든 줄**을 포함해야 Apply가 정상 동작

### 6-5. 422 오류 처리

`"Pull request review thread line must be part of the diff"` 발생 시:
해당 이슈의 position을 파일 내 첫 번째 diff position으로 교체한 뒤 재시도.

---

### 6-5. 콘솔 출력

dry-run 여부에 관계없이 항상 출력한다.

**dry-run 모드:**
```
[dry-run] PR #<번호> 리뷰 결과 (GitHub에 게시하지 않음)
- 이벤트 예정: REQUEST_CHANGES | COMMENT | APPROVE
- 이슈: N개 / 기존 리뷰로 skip: N개

이슈 목록:
  [HIGH]   src/app/core/worker.py:196  pos=52  — UUID object silently dropped
  [MEDIUM] src/app/data/api.py:521     pos=80  — workspace UUID None bypass
  [LOW]    src/app/data/schemas.py:12  pos=16  — result type too narrow
  skip     src/app/core/worker.py:205          — TTL fallback (이미 처리됨)

각 이슈의 본문은 아래에 순서대로 출력한다.
```

**게시 모드:**
```
PR #<번호> 리뷰 게시 완료
- 인라인 코멘트: N개 / 기존 리뷰로 skip: N개
- 이벤트: REQUEST_CHANGES | COMMENT | APPROVE
- URL: https://github.com/OWNER/REPO/pull/N#pullrequestreview-XXXXX

이슈 요약:
  [HIGH]   src/app/core/worker.py:196  — UUID object silently dropped
  [MEDIUM] src/app/data/api.py:521     — workspace UUID None bypass
  [LOW]    src/app/data/schemas.py:12  — result type too narrow
  skip     src/app/core/worker.py:205  — TTL fallback (이미 처리됨)
```

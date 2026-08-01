# 설계 패턴 레퍼런스

> SKILL.md Step 4에서 설계 판단이 필요할 때 이 페이지를 읽는다.
> 각 패턴은 판단 기준과 핵심 질문만 담는다.

---

## §1. DB 모델 판단

| 선택 | 조건 | 위험 |
|------|------|------|
| **컬럼** | 자주 필터/정렬/조인, 무결성 중요, 인덱스 필요 | migration 비용, 개념 조기 고정 |
| **JSONB/settings** | 초기 실험, 조회 빈도 낮음, 나중에 승격 예정 | FK/unique 제약 없음, 쿼리 성능 |
| **별도 테이블** | N:M 관계, 행 단위 audit, grant/revoke 이력, query ACL | 구현 비용, transaction 설계 |

판단 질문: 이 값은 자주 필터링하는가? audit이 필요한가? 나중에 자주 바뀌는 정책인가?

---

## §2. Policy/Protocol 분리

다음이면 DB schema보다 policy interface를 먼저 둔다:
- 정책이 plan/조직/등급에 따라 달라질 수 있다.
- role x security level 조합표가 바뀔 수 있다.
- 상태 전이에 따른 routing 제외 정책이 바뀔 수 있다.

판단 질문: 이 로직이 6개월 후에도 같을 것인가? 아니라면 policy로 분리한다.

---

## §3. Hook / Transaction 경계

| Hook에 넣기 좋은 것 | Transaction 안에 있어야 하는 것 |
|--------------------|-------------------------------|
| audit 기록, cache invalidation | core entity 무결성 |
| notification/email | 권한 체크의 핵심 조건 |
| RAG/indexing job enqueue | 실패 시 rollback 필수인 primary write |
| external system sync | |

판단 기준: 실패해도 core entity가 유효하고 재시도 가능하면 → hook/job. 실패하면 core entity 자체가 의미 없으면 → 같은 transaction.

---

## §4. Queue / Job / Task 분리

| 개념 | 문서에 필요한 것 |
|------|---------------|
| Queue | delivery 보장, 중복 가능성, timeout |
| Job | status, progress, retry, cancel, owner (DB에 persist) |
| Task | idempotency, dependency, failure handling |

핵심 원칙: "queued"와 "persisted"를 혼동하지 않는다. 사용자가 나중에 상태를 조회해야 하면 별도 job row가 필요하다.

판단 질문: job 상태가 DB에 persist되는가? 같은 job이 두 번 실행되어도 안전한가? cancel이 가능한가?

---

## §5. Runtime / Cache / Storage 경계

| 저장 위치 | 주의할 점 |
|---------|---------|
| In-memory | 재시작, multi-worker에서 사라짐 |
| TTL cache | 만료 후 사용자 흐름, cache miss 처리 |
| External cache (Redis) | eviction, serialization, TTL 정책 |
| Persistent DB | migration, transaction, audit 필요 |
| Object storage | key naming, ACL, lifecycle policy |
| Local filesystem | multi-worker/container에서 같은 경로 보장 여부 |

캐시에 넣으면 안 되는 것: 권한 허용/거부의 유일한 근거, durable job 상태, 복구 불가 token/secret.

---

## §6. Audit 설계

다음 이벤트는 반드시 audit 대상:
- 권한 role 변경, 사용자별 읽기 범위 변경
- 상위 등급 읽기 예외 부여
- 초대 생성/재발송/수락/만료
- 문서 security level 변경
- RAG 재인덱싱/삭제, 민감 문서 다운로드

Audit payload 최소 필드: `actor_user_uuid`, `action`, `target_type`, `target_uuid`, `before`, `after`, `reason_code`, `request_id`, `created_at`

주의: `before/after`에 token, secret, 원문 문서 내용 저장 금지.

---

## §7. 민감정보 저장 보호

| 데이터 | 권장 보호 |
|--------|---------|
| 비밀번호 | one-way password hashing (복호화 가능한 암호화 금지) |
| API key / OAuth access token | 저장 시 암호화 또는 secret store, 로그/응답/audit에 원문 금지 |
| OAuth refresh token | 저장 시 암호화, rotation 고려 |
| invite/reset token | 원문 대신 hash 저장 권장 |
| encryption key | 데이터와 같은 DB에 평문 저장 금지, KMS/key wrapping |

판단 질문: 서버가 원문을 다시 읽어야 하는가? 원문 없이 hash로 검증 가능한가? backup/log에 원문이 섞일 수 있는가?

---

## §8. ACL 적용 위치

| 위치 | 역할 |
|------|------|
| API layer | 인증 여부, 기본 route 접근 |
| service layer | business rule, role transition, self-demotion 차단 |
| **query layer** | 목록/검색 결과에서 접근 불가 row 제거 (핵심) |
| RAG layer | retrieval namespace/filter 제한 |
| frontend | disabled 표시 (보안 근거 아님) |

프론트 disabled는 보안 기능이 아니다. 서버가 같은 규칙을 재계산해야 한다.

RAG ACL 선택: namespace 격리(강함) > query metadata filter(세밀함) > post-filter(임시 보조). post-filter만 쓰면 graph/global retrieval 단계에서 민감정보가 이미 섞일 수 있다.

---

## §9. SQL 쿼리 레벨 ACL

"전체 조회 후 애플리케이션 필터"는 소량 데이터에서만 임시 허용.

SQL로 강제하기 좋은 것:
- 사용자의 TEAM membership 확인
- active 상태 필터
- `resource_uuid IN (...)`
- `security_level <= clearance_level` 단순 비교

인덱스 우선 후보: `(user_uuid, group_uuid)`, `(group_uuid, status)`, `(resource_uuid, created_at)`, `(target_type, target_uuid, created_at)`

---

## §10. 실제 사례로 결정하기

문서가 모호하면 추상 토론보다 사례를 만든다.

```
사용자:
소속:
역할:
읽어야 하는 문서:
쓰면 안 되는 문서:
실패하면 생기는 사고:
```

이 사례로 정책 결정이 좁혀지면 구현 범위를 정한다.

---

## §11. Search / Retrieval / RAG 유형 분리

| 유형 | 적합한 경우 |
|------|-----------|
| Structured filter | 상태, 날짜, owner, tag 필터 |
| Keyword search | 제목, 파일명, 짧은 설명 |
| Fulltext search | 긴 본문, 다국어 문서 |
| Semantic retrieval | 의미 기반 유사 문서 |
| RAG retrieval | LLM 답변 context 검색 |
| Graph retrieval | 관계 기반 확장 검색 |

RAG 주의사항:
- post-filter만으로는 graph retrieval 단계에서 민감정보가 이미 섞일 수 있다.
- chunk는 원문의 권한, 보안 등급, revision, 삭제 상태를 추적해야 한다.
- indexing lifecycle(`not_indexed` → `indexing` → `ready` → `stale` / `failed` / `excluded`)을 PRD에 명시해야 한다.

---

## §12. Versioning / Revision / History

| 개념 | 적합한 경우 |
|------|-----------|
| Optimistic lock version | 동시 수정 충돌 방지 |
| Revision history | 사용자가 보는 변경 이력 |
| Immutable snapshot | 승인, 외부 공유, 감사 |
| Event log | 재생, 감사, 원인 분석 |
| Policy version | 정책 변경 전후 결과 설명 |

판단 질문: 현재 값만 필요한가? 이전 버전을 보여야 하는가? 되돌리기는 새 revision인가 overwrite인가?

---

## §13. Lifecycle State Machine

boolean flag가 늘어나면 상태 조합이 깨진다. 승인/초대/업로드/인덱싱이 나오면 state machine을 명시한다.

확인 항목: State 목록, 가능한 Transition, Actor(누가 전이), Guard(조건), Side effect(hook/job/audit), Terminal state, Recovery 방법.

판단 질문: `soft delete`, `inactive`, `archived`, `disabled`가 서로 다른 의미인가? 상태 전이가 검색/billing/audit에 영향을 주는가?

---

## §14. Domain Event / Outbox / Integration

| 개념 | 목적 |
|------|------|
| Domain event | 내부에서 일어난 의미 있는 사건 → hook/job 입력 |
| Audit event | 나중에 설명해야 하는 보안/운영 기록 (수정 불가) |
| Outbox | transaction 후 외부 전달 보장 (retry, deduplication) |
| Webhook/notification | 외부 또는 사용자에게 알림 |

Outbox가 필요한 경우: DB write 성공 후 외부에 반드시 알려야 하고, worker 장애 후에도 전송 대기가 남아야 하는 경우.

판단 질문: 외부 전달 실패를 사용자가 볼 수 있어야 하는가? 중복 전달 시 idempotency key가 있는가?

---

## §15. Deletion / Retention / Archive

| 개념 | 적합한 경우 |
|------|-----------|
| Hard delete | 개인정보 삭제, 테스트 데이터 |
| Soft delete | 복구 가능, 감사 필요 |
| Archive | 일반 흐름에서 숨기지만 보존 |
| Retention | 일정 기간 보존 후 삭제 |
| Legal hold | 보존 기간 무관하게 삭제 차단 |

삭제 정책은 DB cascade만으로 결정하면 안 된다.

판단 질문: 삭제 후 검색/RAG/cache에서 언제 제외되는가? object storage와 DB row 삭제 순서는? 개인정보 삭제와 업무 audit 보존이 충돌하면 어느 정책이 우선인가?

---

## §16. RAG 파이프라인 패턴

§8·§11·§15에 분산된 RAG 관련 판단 기준을 한 곳에 모은 체크리스트. RAG 관련 변경 시 이 섹션을 우선 확인한다.

### 인덱싱 Lifecycle

상태 전이: `not_indexed → indexing → ready → stale / failed / excluded`

| 확인 항목 | 판단 기준 |
|----------|----------|
| 문서 삭제/업데이트 시 stale 전이되는가 | RAG store와 DB 상태가 함께 무효화되어야 한다 |
| failed 상태에서 재시도 경로가 있는가 | 자동 retry 또는 수동 재인덱싱 API 중 하나 |
| excluded(접근 차단) 진입·해제 조건이 명확한가 | policy 변경 시 기존 인덱싱 데이터를 일괄 재평가해야 하는가 |
| 인덱싱 중 문서 삭제 요청이 들어오면 어떻게 되는가 | race condition → 상태 lock 또는 cancel 처리 필요 |

### ACL 적용 계층 (§8 참조)

`namespace 격리(강) > query metadata filter(세밀) > post-filter(임시 보조)`

- post-filter만 쓰면 graph/hybrid retrieval 단계에서 이미 민감 데이터가 섞인다.
- retrieval 범위 결정(어떤 문서를 검색 대상으로 삼는가)은 반드시 서버에서 강제해야 한다.

### 청킹 파이프라인 변경 시 확인

| 확인 항목 | 판단 기준 |
|----------|----------|
| 청크 경계 변경이 기존 인덱싱 데이터와 호환되는가 | 청킹 로직 변경 후 기존 청크 재인덱싱 필요 여부 |
| 토큰 예산이 정확히 지켜지는가 | 메타데이터 prefix 포함 시 실제 content 예산 축소됨 |
| 청크에 문서 출처 정보가 포함되는가 | 검색 결과에서 LLM이 출처를 파악할 수 있어야 한다 |
| 빈 청크·마이크로 청크가 걸러지는가 | 내용 없는 청크는 retrieval noise가 됨 |
| 멀티모달 항목(이미지·표)이 텍스트 청킹에서 분리되는가 | 두 경로의 처리 완료 여부를 별도로 추적해야 한다 |

### 삭제 순서 (§15 참조)

삭제 순서가 뒤바뀌면 삭제된 문서가 retrieval에서 일시 노출된다.

권장 순서:
1. DB row 접근 차단 (soft-delete 또는 excluded 상태 전이)
2. vector store·graph store에서 청크/노드 제거
3. object storage 파일 삭제 (정책에 따라 보존 기간 이후)

판단 질문: 삭제 후 캐시가 있으면 언제 무효화되는가? 각 단계 실패 시 rollback 또는 보상 트랜잭션이 있는가?

### RAG 변경 시 테스트 최소 기준

| 변경 유형 | 최소 검증 |
|----------|---------|
| 청킹 로직 | 유닛 테스트(경계 조건) + 실제 문서로 청크 품질 육안 확인 |
| 인덱싱 파이프라인 | end-to-end: 문서 업로드 → `ready` 상태 도달 확인 |
| ACL/namespace 변경 | 다른 사용자/공간으로 retrieval 시 차단 확인 |
| 삭제 흐름 | 삭제 후 검색에서 해당 문서 청크가 반환되지 않는지 확인 |

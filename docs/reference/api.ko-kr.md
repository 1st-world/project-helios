언어: [English](api.md) | ***한국어***

[README](../../README.ko-kr.md) · [사용 가이드](usage.ko-kr.md) · [아키텍처와 저장 형식](architecture.ko-kr.md) · ***API***

# API 참조

Helios `0.4.0-alpha`의 주요 백엔드 계약입니다. README에 따라 로컬 서버를 실행하면 `/docs`에서 요청 본문과 현재 라우트 스키마를 볼 수 있습니다. 여기서는 실행 수명과 권한 규칙을 함께 설명합니다. 기본 서버에는 인증·사용자별 권한 분리가 없습니다.

## API 지원 범위

| 항목 | 현재 구현 |
| --- | --- |
| 생성 API | 리소스 또는 프로젝트 기준 주소의 `/openai/v1/responses`. 일반 답변·재생성·메모리 요약에 사용합니다. |
| 인증 | 리소스 API 키. Microsoft Entra ID 인증은 아직 구현하지 않았습니다. |
| 클라이언트 | Foundry endpoint를 지정한 OpenAI Python SDK. `model`에는 Deployment 이름을 보냅니다. |
| 모델·입력 | 해당 API와 전송하는 입력 형식·지시문을 지원하는 Deployment가 대상입니다. |
| 다른 API | Chat Completions·Anthropic Messages 호출이나 자동 API 전환은 제공하지 않습니다. |

Microsoft Foundry에는 OpenAI 이외의 모델도 있습니다. Helios는 모델명으로 제조사·용량·지원 기능을 판별하지 않으며, 위 호출 계약을 지원하는지를 기준으로 사용합니다. Foundry의 모든 모델과 입력 조합에 대한 실제 호환성을 검증한 것은 아닙니다.

Microsoft의 [v1 API와 모델 지원 안내](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle#model-support)는 Responses와 다른 v1 API의 지원을 구분합니다. 리소스·Deployment의 Responses 지원과 입력 조건은 [Microsoft Responses 안내](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses)를 확인합니다. Provider 문서의 모델별 조건을 앱 전체의 공통 제한으로 적용하지 않습니다.

## Endpoint 처리

프로필의 Endpoint는 리소스 루트 또는 Foundry 프로젝트의 API 기준 주소입니다. [AIService](../../services/ai_service.py)는 앞뒤 공백과 끝의 `/`을 정리한 뒤, 경로가 `/openai/v1`으로 끝나면 이를 유지하고 그렇지 않으면 `/openai/v1/`을 붙입니다. OpenAI SDK는 그 기준 주소 아래의 `responses`로 요청합니다. 기존 `/api/projects/<project-name>` 경로를 제거하거나 호스트 루트로 바꾸지 않습니다.

프로젝트 Endpoint의 조합 예시는 다음과 같습니다.

```text
Endpoint: https://your-resource.services.ai.azure.com/api/projects/your-project
SDK base: https://your-resource.services.ai.azure.com/api/projects/your-project/openai/v1/
POST:     https://your-resource.services.ai.azure.com/api/projects/your-project/openai/v1/responses
```

Microsoft의 [Foundry REST 참조](https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/aiproject)는 프로젝트 Endpoint와 `{endpoint}/openai/v1/responses` 경로를 명시합니다. 프로젝트 Endpoint를 일괄적으로 관리 전용 주소로 분류하지 않습니다. 개별 `/responses` 주소나 쿼리 매개변수를 붙인 호출 URL 대신 API 기준 주소를 입력하며, 실제 요청의 인증·모델·입력 지원 여부는 해당 배포에 따라 확인합니다.

## 대화별 모델 선택 API

대화 JSON은 다음 응답에 사용할 `profile_id`와 독립적인 `profile_revision`을 저장합니다. 자격 증명과 연결 설정은 프로필 저장소에서 공유하며 대화에 복제하지 않습니다. 새 대화는 생성 시점의 활성 프로필을 초기값으로 저장합니다. 이후 활성 프로필 변경은 기존 선택에 영향을 주지 않습니다. 모델 선택 저장은 본문 `version`·메모리·Workspace·메시지·대화 정렬 시각을 바꾸지 않습니다.

대화 목록·상세 조회와 `GET /api/conversations/{id}/profile`은 `profile_id`, `profile_revision`, `profile_available`을 반환합니다. `PATCH /api/conversations/{id}/profile`에 `{"profile_id":"<profile ID>","expected_profile_revision":0}`을 보내 선택을 저장합니다. 선택이 달라지면 갱신 번호가 증가하며 같은 값 저장은 유지합니다. 오래된 갱신 번호는 `409 conversation_profile_changed`, 존재하지 않는 프로필은 `404`입니다. 저장 실패는 이전 선택을 유지합니다.

기존 대화에서 `POST /api/chat`의 `profile_id`를 생략하면 저장된 선택을 사용합니다. 명시하면 해당 요청과 이후 응답의 선택으로 저장하며, 다른 대화나 전역 활성 프로필은 바꾸지 않습니다. 선택적인 `expected_profile_revision`은 다른 탭이 바꾼 모델로 전송하거나 오래된 선택으로 덮어쓰는 일을 제출 전에 막습니다. 새 대화에서 명시한 모델은 최초 선택으로 저장하고, 준비 실패 후 같은 초안을 재시도해도 그 선택을 유지합니다. 중복 전송·Paused 상태로 거절된 전송은 선택을 바꾸지 않습니다.

선택 필드가 없는 이전 대화는 목록·상세·선택 조회 또는 요청 시 마지막으로 확인 가능한 응답의 프로필로 한 번 초기화합니다. 도구 답변은 실행 기록의 프로필을 확인합니다. 응답에서 확인할 수 없으면 현재 활성 프로필을 초기값으로 저장합니다. 삭제된 프로필의 기록이 있으면 그 ID를 유지하고 `profile_available: false`로 알립니다. 선택이 없거나 사용할 수 없을 때는 자동 대체 없이 `409 conversation_profile_unavailable`로 전송을 거절합니다.

진행 중이거나 Paused인 실행의 다음 응답용 선택도 API로 변경할 수 있습니다. 이미 시작한 요청과 일회성 복구는 준비 당시 프로필을 유지하며 새 선택을 덮어쓰지 않습니다. Resume는 기본적으로 현재 실행 구간의 모델을 유지하고, 명시적인 모델 전환 요청만 새 프로필을 사용합니다. Composer는 대화별 선택을 저장하고, Connections의 기본값은 새 채팅을 초기화합니다. Paused 상태에서도 Composer에서 대화별 모델 선택을 저장할 수 있으며, 선택만으로 실행하지 않습니다. Resume 확인창은 현재 모델과 대상 모델·연결 호스트, 인계 범위와 누적 비용을 설명합니다. 확인 전후로 저장된 선택을 다시 조회하고 승인한 프로필 설정이 달라졌으면 재확인을 요구합니다. 취소하면 새 선택과 기존 Paused 지점을 모두 유지합니다. Connections의 기본값 변경도 가능합니다.

## 프로젝트 탐색 API

### Workspace 범위와 미리보기

개발용 `POST /api/chat`의 `explore_project: true`로 읽기 탐색을 명시적으로 선택합니다. 새 대화는 `POST /api/workspace`로 독립된 초안을 만들고, 반환된 `session_id`를 `workspace_session_id`로 전달해 `PUT /api/workspace/root`에서 폴더를 선택합니다. 첫 채팅에서 같은 식별자를 보내면 연결을 대화에 저장합니다. 저장된 대화는 `GET /api/workspace?conversation_id=<id>&include_entries=false`로 조회하고, 채팅·폴더 변경·문맥 및 구조 미리보기·파일 읽기·첨부 검사에 `conversation_id`와 현재 `workspace_revision`·`workspace_session_id`를 함께 보냅니다. Workspace가 필요하며 기본 프로젝트 개요는 자동 적용됩니다. 추가 도구 탐색은 별도 선택입니다. 전체 tree나 파일 본문·절대 root를 자동으로 넣지 않습니다. 식별자를 생략하는 기존 API의 임시 Workspace는 브라우저 대화의 선택과 분리됩니다.

`POST /api/workspace/context`에 같은 범위 필드를 보내면 자동 개요를 로컬에서 미리 봅니다. 모델 호출이나 MCP 프로세스는 시작하지 않습니다. 기존 `workspace_structure`와 `POST /api/workspace/structure`는 개발용 명시적 추가 구조 요청으로 유지합니다. 깊이·항목 수·절대 경로 옵션은 이 추가 요청에만 적용되며, `workspace_structure: null`은 기본 개요를 끄지 않습니다. 프로젝트 문맥 없이 대화하려면 Workspace 연결을 해제합니다.

아래 예시는 폴더 선택 응답의 revision이 `1`인 경우입니다. 실제 응답의 `revision`과 `session_id`를 사용하세요. 폴더 선택 시 revision이 증가하므로 연결 전 초안의 `0`을 연결된 요청의 고정값으로 쓰면 안 됩니다.

```json
{"prompt":"이 프로젝트의 설정 로딩을 설명해 주세요.","explore_project":true,"workspace_revision":1,"workspace_session_id":"<current session>","exploration_limits":null}
```

### 도구 프로토콜과 입력 정책

모델에는 `helios_project_list_files`, `helios_project_search_text`, `helios_project_read_file`만 등록합니다. 여러 함수 호출은 원래 순서대로 직렬 실행하며 모델 메시지·함수 호출·암호화된 reasoning 상태·해당 `call_id`의 원본 도구 결과를 후속 요청에 재전송합니다. 표시용 공개 reasoning 요약은 재전송하지 않습니다. 중간 응답의 텍스트는 최종 답변으로 합치지 않습니다. 저장된 결과와 참조 범위는 다음 턴과 완료한 실행의 메모리 요약에서도 사용하며, 현재 파일을 다시 읽어 과거 결과를 바꾸지 않습니다.

입력 추정과 명시적 Block에는 함수 정의·탐색 지침·누적 도구 결과도 포함합니다. 진행 중 실행은 요약하지 않으며 입력 초과·Provider 실패 뒤 자동 축소나 재요청을 하지 않습니다. 탐색은 `store: false`와 `reasoning.encrypted_content` 요청을 사용합니다. reasoning 항목이 반환됐는데 재사용할 암호화 상태가 없으면 호환성 오류로 중지합니다. 실제 Foundry Deployment의 지원은 별도 확인이 필요하며 모든 조합을 검증한 것은 아닙니다. 함수 계약은 [OpenAI function calling 문서](https://developers.openai.com/api/docs/guides/function-calling)를 참고하되 Foundry 지원 보장으로 해석하지 않습니다.

`exploration_limits`의 세 값은 모두 기본 `null`입니다. `max_model_calls`는 탐색 응답 호출 수를, `elapsed_time_target_seconds`와 `cost_goal_usd`는 작업 사이에서 확인할 활성 실행 시간·알려진 추정 비용 목표를 지정합니다. 시간·비용 목표는 진행 중인 API 호출의 강제 deadline이나 청구 상한이 아니며, 비용 정보가 불완전한 비용 목표 실행은 추가 호출 전에 중지합니다. 초기·백그라운드 대화 메모리 요약은 기존 동의·정책과 별도 사용량을 유지하며 이 탐색 응답 호출 목표에 포함하지 않습니다. 도구 자원 한도나 모델 용량을 이 값으로 바꾸지 않습니다.

### 진행과 사용량

SSE의 `start`/최종 `done`에는 `run_id`가, `work_progress`에는 단계·도구 호출 ID가 포함됩니다. `work_usage`는 호출별 사용량, `run_usage`는 별도 집계입니다. 최종 `done`과 `GET /api/conversations/{id}`의 도구 답변에는 기존 실행·사용량 저장소에서 계산한 표시용 `response_metadata`가 포함됩니다. 이 응답 전체 집계를 Message의 단일 호출 필드에 저장하거나 사용량 원장에 다시 기록하지 않습니다. 실패 응답도 실행 참조와 알려진 집계를 제공하며 저장소를 확인할 수 없으면 unknown으로 남깁니다. 연결·가격·정책·읽기 범위는 시작 시 고정하며, 대화 변경·삭제·Workspace 변경과 사용한 메모리 동의 철회는 후속 실행과 답변 채택을 중지합니다. 첫 제출 전 메모리가 철회되면 원본 이력으로 준비할 수 있지만, 이미 그 요약을 사용한 실행은 원본으로 갈아 끼워 계속하지 않습니다.

실행 이력과 활동 API의 추가 `failure` 필드는 허용된 오류 코드·분류·단계·응답 상태, HTTP 상태, request ID와 retry-after만 저장·반환합니다. 오류 원문·응답 본문·자격 증명은 포함하지 않습니다. 상세 진단 저장에 실패해도 기존 종료 처리와 점유 해제는 유지하며, 해당 필드가 없는 이전 실행은 보존된 상태와 reason에 따라 표시합니다.

### 취소와 상태 보존

중지는 요청 취소·연결 종료로 처리하고 MCP 정리까지 Workspace 점유를 유지합니다. 재생성은 새 실행이며 성공한 최종 답변과 첨부 참조만 기존 분기를 교체합니다. 실패·취소는 이전 분기를 보존합니다. 종료 상태 저장도 실패하면 점유는 해제하고 저장 상태를 unavailable로 표시하며, 재시작 시 남은 미완료 실행은 interrupted로 복구합니다. 탐색의 컨텍스트 오류에는 불완전 실행을 재개하는 일회성 요약 receipt를 만들지 않습니다. 입력을 검토한 새 탐색이 필요합니다. 최종 대화 JSON 저장 실패로 채택되지 않은 completed 실행이 남을 수 있으며 자동 채택·재실행하지 않습니다.

### 일시중지와 재개

`POST /api/work-runs/{run_id}/pause`는 `{ "conversation_id": "..." }`로 안전한 단계 경계에서의 일시중지를 요청합니다. 스트림은 정리 완료 후 `paused` 이벤트로 끝나며 최종 답변은 `done`으로 끝납니다. `/resume`는 같은 본문에 `expected_conversation_version`을 요구하고 기존 run의 SSE를 이어 보냅니다. `/stop`은 활성 또는 일시중지 실행에 명시적 취소를 저장하며 완료된 결과를 덮어쓰지 않습니다. Resume는 대화별 요청 슬롯과 원자적 체크포인트 소유권을 확인하므로 중복 호출을 시작하지 않습니다. 일시중지된 대화의 `/api/chat`과 메시지 편집은 `409 work_run_paused`로 거절하고 기존 체크포인트를 유지합니다. 대화 조회·실행 이력의 `paused_run`은 페이지와 무관한 잠금 상태를 제공합니다.

재개용 입력은 `work_runs.sqlite3`의 별도 테이블에 보존하며 연결 자격 증명은 복제하지 않습니다. 자동 읽기 범위의 파일·ignore 정책은 제한된 metadata manifest로, 명시적으로 선택한 Workspace 파일과 upload 원본은 SHA-256으로 검증합니다. metadata를 보존한 외부 내용 변경이나 검증 직후의 파일 시스템 경쟁까지 막는 snapshot은 아닙니다. 새 MCP 세션의 이전 cursor는 `cursor_expired`로 거부하고 모델에 범위부터 다시 읽도록 안내합니다.

### 실행 구간과 모델 인계

실행 저장소 형식 `0.3`은 원래 프로필·정책을 실행 구간에 연결하고 모델 호출과 도구 결과의 Provider 식별자를 구간별로 구분하며, 전환 구간의 준비 입력을 별도 비공개 테이블에 보존합니다. 기존 `0.1`·`0.2` 자료는 원래 snapshot·원문·사용량 연결을 유지하며 이전합니다. 체크포인트는 구간과 증가하는 일시중지 revision에 연결하고 매 실행 claim은 소유권 revision을 갱신하므로 오래된 요청이 새 실행에 호출을 기록하거나 종료시키지 않습니다. 이전 체크포인트도 원래 실행에서 재개할 수 있습니다. 대화 메시지의 실행 참조 형식은 `0.1`을 유지합니다.

모델 전환을 위한 인계 자료는 원래 준비 입력, 완료된 공개 메시지, 원본 도구 결과·참조 범위와 미실행 요청을 구분해 구성합니다. 원래 입력에 포함된 이전 작업까지 reasoning 전체를 제외하고 Provider 전용 참조를 정규화하며, 원본 저장 기록은 바꾸지 않습니다. 지원하지 않는 참조나 누락된 결과는 조용히 제외하지 않고 인계 준비를 거절합니다. 자료 생성은 모델 호출·도구 실행·자동 요약·체크포인트 소비를 만들지 않습니다.

다른 모델로 이어가려면 대화의 프로필 선택을 먼저 저장한 뒤 `/resume`에 `target_profile_id`, `expected_profile_revision`, `expected_checkpoint_revision`, `expected_execution_revision`을 함께 전달합니다. 프로필 revision은 대화의 선택 API에서, 체크포인트·실행 revision은 Paused run 메타데이터에서 확인합니다. 기존 `conversation_id`·`expected_conversation_version`도 필요합니다. 새 모델의 연결·메모리 정책과 입력·출력 예산을 적용하고 도구 정의·인계 이력·첨부를 포함해 검사합니다. 원본 입력이 메모리를 사용했는데 대상 모델이 이를 허용하지 않으면 출발 지점을 보존한 채 거절합니다. 경고 모드는 경고를 제공하며 명시적 Block만 입력 추정으로 차단합니다. 실행 중 다음 응답용 모델 선택을 다시 바꾸어도 이미 시작한 구간의 모델은 바뀌지 않습니다. Work process는 모델 전환 여부와 관계없이 기존 단계 목록을 시간순으로 표시합니다. Model call을 펼치면 일반 응답과 같은 footer에서 실제 사용 모델·호출 시작 시각·사용량을 확인하고 Details로 토큰 내역을 펼칩니다. 전체 사용량에는 실패한 전환 시도도 포함합니다. 파일 결과 링크는 실행 구간을 함께 지정하므로 서로 다른 모델이 같은 Provider 응답·도구 ID를 반환해도 해당 결과를 엽니다.

전환은 새 모델의 첫 정상 출력과 허용된 도구 요청을 저장한 뒤 원자적으로 확정합니다. 이전 모델의 미실행 도구는 먼저 실행하지 않고 새 모델이 판단하게 합니다. 연결·검사 실패나 첫 정상 출력 전 중단은 출발 Paused 지점과 원래 만료 기한을 보존하며, 시도한 호출·비용·활성 시간은 누적합니다. 이때 모델을 자동으로 재호출하거나 이전 모델로 자동 실행하지 않습니다. 명시적 Stop·원본 권한 철회는 전환 준비와 출발 체크포인트를 모두 폐기합니다. 새 모델이 이미 완료한 작업이 있으면 이전 지점으로 되돌리지 않습니다. 최종 응답 메타데이터는 실제 마지막 모델과 구간별 이력을 제공하며, 후속 질문·메모리 요약에는 여러 모델의 완료 이력을 reasoning 없이 재생합니다.

실행 활동의 `segment_id`는 구간별 도구 결과를 구분합니다. 응답·도구 ID가 겹치는 경우 `/evidence?response_id=...&call_id=...&segment_id=...`로 조회하며, 모호한 참조를 임의의 구간으로 연결하지 않습니다. 종료·만료·삭제 시 모든 구간의 비공개 입력과 추가 첨부 소유권을 해제하고 원문 기록과 사용량은 유지합니다.

연결 검증은 표시 이름을 제외한 실제 요청 설정을 비교합니다. 이름 변경 후에도 기존 체크포인트를 재개할 수 있으며, 해당 실행과 재개 호출의 기록에는 시작 당시 이름을 유지합니다. 이전 연결 검증 형식도 저장된 원래 이름으로 검증하되 자격 증명·모델·가격·권한 검사는 유지합니다.

### 요청 정리 상태

`GET /api/chat/status?conversation_id=<id>`는 해당 대화의 현재 요청 정리를 최대 10초 기다린 뒤 `conversation_id`와 `in_progress`를 반환합니다. 시작 이벤트 전의 새 채팅은 `workspace_session_id=<id>`로 조회해 초안이 채택된 대화를 찾을 수 있습니다. 정리 완료 이벤트를 기다리며 반복 폴링·Provider 호출·MCP 실행·폴더 조회를 하지 않습니다. 시간 초과 시에도 `in_progress: true`인 요청의 점유는 유지합니다. 취소나 재개를 실행하는 API가 아닙니다.

### 저장된 활동과 근거

`GET /api/conversations/{id}/work-runs`는 최신 실행 20개와 `next_before`를 반환하며 `before`로 이전 페이지를 조회합니다. `GET /api/conversations/{id}/work-runs/{run_id}?offset=0`는 모델 원문 없이 실행 정보·집계와 순서가 유지된 단계 30개·`next_offset`을 반환합니다. 두 응답의 실행 정보에는 현재 대화에서 검증된 `message_link.user_index`·`assistant_index`가 포함되며, 연결할 수 없는 위치는 `null`입니다. 새 실행은 질문의 위치·생성 시각·내용 체크섬을 내부에 기록해 질문 수정이나 분기 교체 후 잘못 연결되는 것을 방지합니다. 같은 경로의 `/evidence?response_id=...&call_id=...`는 해당 실행에 저장된 도구 결과 하나를 반환합니다. 모두 존재하는 소유 대화를 확인하고 임의 파일 경로나 다른 대화의 실행을 열지 않습니다. 이 조회는 Provider 요청이나 MCP 실행을 만들지 않습니다.

## 독립 첨부 API

네이티브 UI 선택은 `WS /api/attachments/picker/{token}`으로 연결합니다. 브라우저가 보낸 절대 경로를 읽지 않으며, 앱이 띄운 선택창에서 고른 파일만 기존 검사·저장 경로로 가져와 ID 참조를 반환합니다. 선택창의 시작 위치는 현재 대화 또는 Workspace 초안으로 결정합니다.

브라우저 Attach와 같은 준비 경로를 사용하는 백엔드 API를 제공합니다. `POST /api/attachments/uploads?filename=notes.txt`에 원본 bytes를 본문으로 보내면 앱이 발급한 UUID와 `reference`를 반환합니다. 파일명은 표시용 basename이며 서버의 절대 경로나 디렉터리를 받지 않습니다. 수신·검사는 기존 첨부 자원 제한을 적용하고 실패·취소된 업로드를 정리합니다.

`POST /api/attachments/inspect`와 `POST /api/chat`의 `attachment_sources`에 반환된 참조를 전달합니다.

```json
{"attachment_sources":[{"source":"upload","attachment_id":"<app-issued UUID>"}]}
```

Workspace 참조는 `{"source":"workspace","path":"notes.txt"}`이며 검사 결과에는 해당 Workspace의 revision과 session ID도 포함됩니다. 두 출처는 파일 개수·합계 bytes·형식 처리 정책을 공유합니다. 기존 `paths` 검사와 `attachment_files`·`workspace_file`·`workspace_files` 채팅 필드도 유지하며, 이 필드에는 Workspace 선택이 필요합니다.

`GET /api/attachments/uploads`는 보존 중인 사본 목록과 존재 여부를, `GET /api/attachments/uploads/{id}`는 사용할 수 있는 사본의 메타데이터를 반환합니다. API 클라이언트도 작성 중인 선택을 보존하려면 `PUT /api/attachments/drafts/{UUID}`에 `{"attachment_ids":["<upload UUID>"]}`를 보내고 24시간 이내에 갱신합니다. 빈 목록은 해당 초안의 참조를 해제합니다. 선택적인 `released_attachment_ids`는 최초 등록이 서버에 도달하지 않은 제거 사본도 해제하며, 다른 소유자나 유지할 선택의 참조는 보호합니다. 참조 없는 새 업로드에는 24시간의 유예가 있으며, 해제된 사본은 더 이상 사용하지 않으면 백그라운드에서 정리합니다. 초안 갱신 응답은 해제 상태의 저장을 확인하며, 물리적 파일 삭제 완료를 기다리지 않습니다. `DELETE /api/attachments/uploads/{id}`는 참조 없는 사본만 명시적으로 삭제합니다. 대화·초안·요청·유효한 재시도에서 사용 중이면 `409`, 삭제·유실된 ID를 읽으면 `410`을 반환합니다. UI에는 재사용 보관 목록이 없습니다.

채팅은 새 참조를 사용자 메시지에 저장하고, 재생성에서 `attachment_sources`를 생략하면 그 참조를 복원합니다. `[]`는 선택 해제이며 변경 참조는 재생성 성공 시 답변과 함께 저장됩니다. 기존 경로 필드의 재생성은 현재 요청의 선택을 사용합니다. 일회성 복구는 실패 요청의 준비 자료를 재사용하며 원본 삭제 시 해당 복구 자료도 폐기합니다. 이미지·PDF bytes와 추출 본문은 대화 JSON에 복제하지 않습니다.

Language: **한국어** | [English](README.en.md)

# Helios

Microsoft Foundry의 모델 Deployment에 자신의 API 키를 연결해 사용하는 로컬 웹 앱입니다. 대화와 설정을 로컬에 보관하고, 선택한 프로젝트 파일을 모델에 전달해 질문할 수 있습니다.

현재 버전은 **`0.2.0` 알파**입니다. 기본 채팅에서는 모델 용량을 추측해 입력을 줄이거나 제한하지 않고 Provider의 판단에 맡깁니다. 입력 예산·출력 제한·대화 메모리는 사용자가 선택하며, 로컬 파일 처리에는 별도의 자원 제한이 적용됩니다.

## 현재 기능

- 여러 연결 프로필 저장과 응답에 사용할 프로필 선택.
- 스트리밍 답변, Markdown·코드 강조, 대화 저장·이름 변경·삭제, 메시지 편집과 답변 재생성.
- 선택적인 Workspace 연결·해제와 프로젝트 구조 전달, Workspace 없이도 가능한 텍스트·이미지·PDF·Office 파일 첨부.
- 선택적인 입력 토큰 예산, 출력 제한, 대화 메모리 요약과 일회성 요약 복구.
- 응답·재생성·요약 호출의 사용량 기록, 프로필별 가격을 이용한 비용 추정, 기간·종류별 조회.
- 인터페이스 글꼴·텍스트 배율·시간대와 사용량 표시 설정.

`Work tools`의 `Planned` 항목과 비활성화된 설정은 아직 사용할 수 없습니다. 현재 UI 언어는 영어입니다.

## 시작하기

### 실행 환경

아래 설치 예시는 **Windows PowerShell과 Python 3.12** 기준입니다. 브라우저와, API 키로 Responses API를 호출할 수 있는 Microsoft Foundry Deployment가 필요합니다. Node.js나 별도의 프런트엔드 빌드 과정은 필요하지 않습니다.

### 설치와 실행

프로젝트 루트에서 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

브라우저에서 [Helios 열기](http://127.0.0.1:8000)를 선택합니다. 종료할 때는 실행한 터미널에서 `Ctrl+C`를 누릅니다.

기본 구성은 로그인이나 사용자별 권한 분리가 없는 로컬 단일 사용자 앱입니다. 서버는 한 프로세스로 실행하며, 여러 브라우저에서 접속해도 같은 프로필·대화·Workspace를 공유합니다.

### 첫 연결

1. **Settings → Connections → New profile**을 엽니다.
2. 구분하기 위한 **Profile name**, 실제 **Deployment name**, **Foundry endpoint**, **API key**를 입력합니다.
3. **Save changes**로 저장합니다. 첫 프로필은 자동으로 활성화되며, 추가 프로필은 목록의 **Use**로 선택할 수 있습니다.
4. 채팅 화면에서 메시지를 보냅니다. 모델 용량·가격·메모리를 설정하지 않아도 기본 채팅을 시작할 수 있습니다.

Endpoint에는 리소스 주소 또는 `/openai/v1/`까지의 주소를 사용합니다. 개별 `/responses` 주소나 Foundry project 관리 주소를 입력하는 항목이 아닙니다.

```text
https://your-resource.cognitiveservices.azure.com/
https://your-resource.services.ai.azure.com/openai/v1/
```

**Deployment name은 모델 카탈로그 이름이 아니라 해당 리소스에 배포한 이름**입니다. 프로필을 저장하는 것은 연결 정보의 로컬 저장이며, 실제 API 호환성과 권한은 요청 시 확인됩니다.

[profiles.example.json](profiles.example.json)은 저장 형식 참고용입니다. 모델명·Deployment 이름·가격은 예시이므로 실제 배포 정보와 요금을 확인해 입력합니다. 새 연결은 UI에서 생성할 수 있습니다.

## API 지원 범위

| 항목 | 현재 구현 |
| --- | --- |
| 생성 API | `/openai/v1/responses`. 일반 답변·재생성·메모리 요약에 사용합니다. |
| 인증 | 리소스 API 키. Microsoft Entra ID 인증은 아직 구현하지 않았습니다. |
| 클라이언트 | Foundry endpoint를 지정한 OpenAI Python SDK. `model`에는 Deployment 이름을 보냅니다. |
| 모델·입력 | 해당 API와 전송하는 입력 형식·지시문을 지원하는 Deployment가 대상입니다. |
| 다른 API | Chat Completions·Anthropic Messages 호출이나 자동 API 전환은 제공하지 않습니다. |

Microsoft Foundry에는 OpenAI 이외의 모델도 있습니다. Helios는 모델명으로 제조사·용량·지원 기능을 판별하지 않으며, 위 호출 계약을 지원하는지를 기준으로 사용합니다. Foundry의 모든 모델과 입력 조합에 대한 실제 호환성을 검증한 것은 아닙니다.

Microsoft의 [v1 API와 모델 지원 안내](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle#model-support)는 Responses와 다른 v1 API의 지원을 구분합니다. 리소스·Deployment의 Responses 지원과 입력 조건은 [Microsoft Responses 안내](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses)를 확인합니다. Provider 문서의 모델별 조건을 앱 전체의 공통 제한으로 적용하지 않습니다.

## 입력·출력·메모리 설정

**Settings → Context & attachments**는 앱 기본값, **Connections → 프로필 → Context & memory**는 프로필별 설정입니다. 입력 예산과 Input check는 프로필에서 앱 기본값을 대신 선택할 수 있습니다. 출력 제한은 앱과 프로필에 명시한 값 중 작은 값이 적용됩니다. 저장한 변경은 새 요청에 적용됩니다.

### 입력과 출력

| 설정 | 새 설치의 기본값 | 의미 |
| --- | --- | --- |
| Input budget | No input limit | 로컬 입력 토큰 예산을 두지 않습니다. |
| Input check | Warn | 입력 예산이 적용될 때, 로컬 추정이 선택한 한도를 넘으면 경고하고 진행합니다. |
| Output limit | Provider default | `max_output_tokens`를 지정하지 않습니다. |
| Conversation memory | Off | 자동 요약 없이 원본 대화 이력을 사용합니다. |
| Deployment information | Unknown | 모델 용량을 자동으로 추측하거나 조회하지 않습니다. |

**Custom budget**은 입력 추정 토큰 값을 직접 지정합니다. **Warn**은 초과해도 진행하고, **Block**은 초과한 요청을 로컬에서 거절하며, **Off**는 입력 예산 검사를 건너뜁니다. Off에서도 명시적인 출력 제한·첨부 검사·메모리 설정은 적용됩니다.

출력 제한을 입력하면 답변과 요약의 출력에 적용되며 reasoning 토큰도 포함됩니다. **Declared maximum output**은 용량 정보로, 사용자 출력 제한과 별개입니다. Deployment 정보 입력만으로 일반 채팅의 입력 예산이나 출력 제한이 활성화되지는 않습니다.

이전 버전에서 가져온 정책은 **Review needed**로 표시될 수 있습니다. 검토 전에는 기존 입력 규칙을 보존하므로, 새 기본 정책을 사용하려면 해당 앱·프로필의 Input budget을 명시적으로 선택해 저장합니다.

### 대화 메모리

메모리는 오래된 대화를 요약해 이후 요청에서 원문 일부 대신 사용하는 선택 기능입니다. **추가 유료 API 호출**이 발생하며, 저장된 원본 메시지는 유지합니다.

1. 프로필의 **Deployment information**에 확인한 용량을 입력합니다. Declared maximum input 또는 Declared context window를 사용할 수 있습니다.
2. Context window를 사용하는 경우 출력 계획 여유량이 필요합니다. **Summary output headroom → 적용 중인 Output limit → Declared maximum output** 순으로 값을 선택합니다. 최대 입력만으로 계획할 때는 입력·출력 공유 용량을 확인하지 못한 상태로 표시합니다.
3. **Conversation memory → On**을 선택해 저장하고, **Saved effective policy**에서 준비 상태를 확인합니다. 정보가 부족하면 자동 요약 없이 원본 이력을 준비합니다.

앱의 메모리 기본값은 다음과 같습니다.

| 설정 | 기본값 | 의미 |
| --- | --- | --- |
| Summary work budget | 262,144 tokens | 요약 호출 한 번의 입력 추정 예산. 선언한 배포 용량도 함께 고려합니다. |
| Summary trigger | Automatic | 선언한 입력 허용량의 90%를 사용합니다. `0`은 요약을 일시 중지합니다. |
| Recent history target | 65,536 tokens | 요약할 때 유지할 최근 원문 목표량. 완전한 대화 턴을 보존하므로 초과할 수 있습니다. |
| Summary call limit | 8 | 한 번의 요약 작업에서 허용하는 최대 API 호출 수입니다. |

Context window와 최대 입력을 모두 선언하면 메모리는 더 작은 입력 허용량을 사용합니다. Summary output headroom은 계획용 여유량이며 API 출력 상한을 설정하지 않습니다.

### 토큰 추정

로컬 추정은 **모델 맞춤형 토크나이저를 선택하는 방식이 아닙니다.** 로컬 `o200k_base` 어휘 파일이 무결성 검사를 통과하면 이를 사용하고, 사용할 수 없으면 UTF-8 바이트 수 기반 휴리스틱을 사용합니다. 요청 형식과 이미지·PDF에도 근삿값이 포함되므로 Provider의 실제 토큰·과금 사용량과 다를 수 있습니다.

추정용 어휘 파일을 설치하려면 다음 명령을 별도로 실행합니다. 공개 파일을 내려받아 무결성을 확인하며, 모델 API를 호출하지 않습니다. 기본 채팅의 선행 조건은 아닙니다.

```powershell
.\.venv\Scripts\python.exe -m services.token_estimator --download
```

일반 실행 중에는 어휘 파일을 자동으로 내려받지 않습니다. 사용 중인 추정 방식은 프로필의 Saved effective policy에서 확인할 수 있습니다.

### 오류와 재요청

Provider 거절 뒤 입력 축소·API 전환·재요청은 자동으로 실행하지 않습니다. 오류 안내에서 질문·첨부·연결·사용량을 검토할 수 있으며, 알려진 사용량은 실패·미완료·취소 때도 기록합니다. 사용량을 받지 못한 요청은 비용이 0이라고 확정할 수 없습니다.

컨텍스트 초과 후 "Summarize once & retry…"가 제공되면 호출 범위와 비용 안내를 확인하고 명시적으로 실행할 수 있습니다. 이 요약은 해당 재시도에만 사용하며 자동 메모리 설정을 켜지 않습니다. 실패한 재생성이 기존 답변과 이후 대화 분기를 교체하지는 않습니다.

## Workspace와 첨부

처음 실행하거나 재시작하면 **Workspace 미지정**, **프로젝트 구조 전달 Off** 상태입니다. 폴더 없이 일반 채팅을 시작할 수 있습니다. 기본 Workspace 폴더는 제공하거나 자동으로 생성하지 않으며, 기존 작업 폴더와 파일을 자동으로 연결하거나 삭제하지 않습니다.

**Workspace**에서 앱이 실행되는 컴퓨터의 폴더를 선택하거나 연결을 해제합니다. Browse는 해당 컴퓨터에서 Tk 폴더 선택창을 열며, 사용할 수 없으면 절대 경로를 직접 입력할 수 있습니다. 선택 폴더는 한 서버 프로세스에 연결한 모든 브라우저에서 공유하며 재시작 후에는 유지하지 않습니다. 해당 폴더를 사용하는 작업이 진행 중이면 **Stop**으로 중지하고 정리가 끝난 뒤 변경하거나 해제합니다.

폴더를 연결할 때는 경로만 확인하고, 파일 목록은 **Attach**를 열거나 새로고침할 때 조회합니다. 하위 폴더는 펼칠 때 화면에 표시하며, 긴 목록은 **Show more entries**로 이어서 볼 수 있습니다. 검색은 전체 파일 목록을 대상으로 하며 표시 개수로 첨부 범위를 제한하지 않습니다. 목록 조회가 끝나기 전에 Attach를 닫으면 해당 요청을 취소합니다.

폴더 선택만으로 호스트의 절대 경로·폴더 구조·파일 본문이 모델에 전달되지는 않습니다. 구조 전달을 켜면 상대 경로로 범위·깊이·최대 항목 수를 선택하고 포함·생략 범위를 미리 확인할 수 있습니다. 절대 경로는 별도로 선택한 경우에만 포함합니다. 구조 전달을 꺼도 명시적으로 첨부한 파일은 전송하며, 이전 대화나 메모리에서 이미 언급한 경로를 삭제하지는 않습니다.

**Attach → Choose files**에서 브라우저가 실행 중인 기기의 파일을 선택합니다. Workspace가 없어도 사용할 수 있으며, 업로드된 원본은 **Uploaded files**에서 다시 선택할 수 있습니다. 프로젝트 파일은 같은 창의 **Workspace files**에서 고릅니다. **Check selection → Apply selection**으로 출처가 구분된 검사 결과를 확인하고 적용합니다. 선택 검사는 로컬 준비 상태를 확인하며 모델의 수락을 보장하지는 않습니다.

추가한 파일은 제거할 때까지 새 메시지에 포함됩니다. 폴더 변경·해제는 Workspace 첨부만 제거하며 업로드 첨부는 유지합니다. 대화를 다시 열면 마지막 사용자 메시지의 첨부를 복원합니다. 재생성은 해당 메시지의 원래 첨부를 사용하되, Attach에서 선택을 적용하거나 입력창의 첨부를 제거했다면 변경한 선택을 사용합니다. 선택을 바꿀 때는 다시 검사하며, 실패한 재생성은 이전 답변과 저장된 첨부를 유지합니다.

업로드 중 **Stop upload**를 누르거나 Attach를 닫으면 진행 중인 업로드를 취소합니다. 완료된 원본은 앱을 실행하는 컴퓨터에 보관하며, 선택 취소·입력창에서 제거·대화 삭제만으로 지우지 않습니다. 원본을 영구 삭제하려면 Uploaded files의 휴지통 버튼을 사용합니다. 삭제·유실된 원본은 **Unavailable**로 표시하며 다시 선택하거나 첨부에서 제거해야 합니다. Explore project UI는 아직 Planned입니다.

| 형식 | 모델에 보내는 내용 |
| --- | --- |
| UTF-8 텍스트 | 읽은 텍스트. UTF-8 BOM도 지원합니다. |
| PNG·JPEG·WebP·정적 GIF | 로컬 검사 후 원본 이미지 입력. 애니메이션은 지원하지 않습니다. |
| PDF | 로컬 구조·페이지 검사 후 원본 파일 입력. 로컬 텍스트 추출을 필수로 요구하지 않습니다. |
| DOCX | 본문 문단·표와 header/footer의 텍스트. |
| XLSX | 저장된 셀과 수식 텍스트. 빈 영역 전체를 셀 수로 세거나 수식을 실행하지 않습니다. |
| PPTX | 슬라이드의 텍스트·표·그룹 도형과 기존 발표자 노트. |

Office 첨부에는 내장 이미지·차트·서식이 포함되지 않습니다. 시각 자료가 필요하면 PDF나 이미지로 제공할 수 있습니다. 암호화된 문서와 지원하지 않는 바이너리 형식은 거절합니다. 이미지·PDF 입력 지원 여부는 실제 Deployment가 판단합니다.

### 로컬 파일 제한

**Context & attachments → Attachment resource limits**에서 조정합니다. 아래는 로컬 읽기·추출의 잠정 기본값이며 모델의 입력 용량이나 Provider의 공통 파일 상한이 아닙니다. 메모리 사용량·처리 시간 전체를 보장하지 않으며, 실행 환경과 디코더의 보호 한도가 더 낮게 적용될 수 있습니다.

| 항목 | 기본값 |
| --- | --- |
| 선택 파일 수 | 50개 |
| 파일별 원본 크기 | 49,999,999 bytes |
| 선택 전체 원본 크기 합계 | 49,999,999 bytes |
| 파일별 읽거나 추출한 UTF-8 텍스트 | 4,194,304 bytes (4MiB) |
| Office 파일별 ZIP 확장 크기 합계 | 134,217,728 bytes (128MiB) |
| Office 파일별 ZIP 엔트리 수 | 10,000개 |
| 이미지별 decode 픽셀 수 | 50,000,000 pixels |
| PDF별 검사 페이지 수 | 1,024페이지 |
| XLSX별 저장 셀 수 | 100,000개 |

초과한 파일 본문을 조용히 잘라 보내는 대신 선택을 오류로 알립니다. 여러 파일의 추출 결과·전송 직렬화·동시 작업에는 원본 크기보다 큰 자원이 필요할 수 있습니다. 중지는 협력적으로 처리하므로 실행 중인 개별 parser·파일 시스템 호출이 즉시 끝나는 것은 아닙니다.

## 사용량과 비용

**Settings → Usage & costs**에서 호출 기록과 기간별 집계를 조회합니다. 메시지별·대화별 표시도 선택할 수 있습니다. 사용량은 Provider가 보고한 토큰을 기준으로 하며 누락과 `0`을 구분합니다. reasoning 토큰을 output에 다시 더하지 않습니다.

**Connections → 프로필 → Token pricing**에는 USD / 1M tokens 단위의 요금을 입력합니다. 표준 요금, cache read/write 요금, 긴 컨텍스트 요금과 입력 토큰 분기점을 설정할 수 있습니다. 빈 표준 요금은 미설정, `0`은 무료를 뜻하며, 빈 긴 컨텍스트 요금은 대응하는 표준 요금을 사용합니다. 기본 분기점은 272,000 tokens이며 실제 모델의 과금 조건에 맞춰 변경합니다.

비용 표시는 호출 시점에 저장한 요금과 보고된 사용량에 따른 추정입니다. 필요한 값이 없으면 부분 추정 또는 확인 불가로 표시하며, Azure 청구액이나 계정의 남은 크레딧을 조회하는 기능은 아닙니다. 대화를 삭제해도 독립적인 사용량 원장은 유지합니다.

## 로컬 데이터와 설정

| 위치 | 내용 |
| --- | --- |
| `profiles.json` | 연결 프로필·API 키·가격·프로필별 정책과 활성 프로필. |
| `settings.json` | 앱의 입력·출력·요약·첨부 기본 정책. |
| `conversation/*.json` | 저장된 대화 원문·메모리 요약·응답 사용량. |
| `data/attachments/` | 독립 첨부 API의 재사용 원본과 SQLite manifest. 캐시·로그 회전과 별도로 보존합니다. |
| `data/work_runs.sqlite3` | Work tools 기반 저장소. 실행 상태·모델 응답·원본 도구 결과·호출별 사용량 참조를 보존합니다. Explore UI는 아직 Planned입니다. |
| `logs/usage_calls.sqlite3` | 호출별 사용량·상태·요금 스냅샷과 집계 원장. |
| `logs/summary_usage.sqlite3` | 요약 사용량 저장소. 호출 원장과 함께 사용합니다. |
| `logs/helios.log*` | 회전 보관하는 앱 로그. |
| `.cache/tokenizers/o200k_base.tiktoken` | 선택적으로 설치하는 추정용 어휘 파일. |
| 브라우저 localStorage | 글꼴·텍스트 배율·시간대·사용량 표시 등 브라우저별 환경 설정. |

기본 파일 위치는 [config.py](config.py)에 정의돼 있습니다. 런타임 데이터와 설정 파일은 Git에서 제외합니다. **API 키는 `profiles.json`에 평문으로 저장**하며, 저장된 키를 조회 응답으로 브라우저에 돌려주지는 않습니다. 프로필 백업도 원본과 같은 수준으로 관리해야 합니다.

앱과 대화 데이터가 로컬에 있어도 답변 생성은 외부 API에서 수행합니다. 전송할 질문·이력·선택한 프로젝트 구조·첨부 내용과 Provider 측 데이터 처리 조건을 고려해 사용합니다. 폰트와 프런트엔드 라이브러리는 로컬 파일로 제공하며 브라우저에서 외부 CDN을 불러오지 않습니다.

실행 기록은 자동 만료하지 않으며 대화 삭제 후에도 보존합니다. 실행 문맥이 있는 대화는 대화 JSON과 실행 저장소를 함께 백업해야 합니다. 사용량 원장은 별도로 유지하며 실행 집계값을 새 API 호출로 가져오지 않습니다.

## 버전과 호환성

앱 버전과 저장 스키마 버전은 독립적으로 관리합니다. 알파 기간에는 각각 `0.x.y`와 문자열 `"0.*"` 범위를 사용합니다.

### 버전을 올리는 시점

앱 버전은 개별 커밋마다 올리지 않고, 여러 커밋을 묶어 검증한 릴리스 단위로 올립니다. 개발 중 세부 코드는 Git 커밋 SHA로 구분하며, 한 번 릴리스한 버전 번호를 다른 코드에 다시 사용하지 않습니다.

| 변경 단위 | 알파 기간의 버전 처리 |
| --- | --- |
| 개발 중 개별 커밋, 문서·주석·테스트 정리 | 앱 버전을 유지합니다. |
| 오류 수정·작은 UI/UX 보완을 묶은 릴리스 | PATCH를 올립니다. 예: `0.2.0 → 0.2.1`. |
| 기능 추가·의미 있는 정책 변경·알파의 비호환 변경을 묶은 릴리스 | MINOR를 올리고 PATCH를 0으로 바꿉니다. 예: `0.2.x → 0.3.0`. |
| 정식 릴리스 | 앱 `1.0.0`부터 시작합니다. |

이 MINOR/PATCH 구분은 Helios의 알파 운영 규칙입니다. 릴리스 준비 커밋에서 `app.py`와 두 README의 버전을 맞추고, 검증한 커밋에 `v0.2.0`처럼 버전 태그를 붙입니다. 저장 스키마는 앱 기능 추가만으로 올리지 않고 저장 형식의 계약이 바뀔 때 별도로 갱신합니다.

### 저장 형식과 알파 종료

실행 저장소와 선택적 메시지 실행 참조는 각각 독립된 문자열 스키마 `"0.1"`을 사용합니다. 새 메시지 참조는 `run_schema_version`과 `run_id`를 기록합니다. 참조 없는 기존 대화는 그대로 읽으며 자동으로 다시 저장하지 않습니다. 지원하지 않는 버전의 원본은 덮어쓰지 않습니다.

- 현재 앱 버전은 **`0.2.0`**, 설정·프로필 저장 스키마는 **문자열 `"0.1"`**입니다. JSON 소수로 저장하지 않으므로 `"0.1"`과 `"0.10"`은 다른 식별자입니다.
- 독립 첨부 manifest와 메시지의 선택적 첨부 참조는 각각 문자열 `"0.1"` 스키마를 사용합니다. 첨부가 없는 기존 대화는 그대로 읽으며, 새 참조에는 `attachment_schema_version`과 `attachment_sources`를 기록합니다. 지원하지 않는 첨부 형식은 원본을 덮어쓰거나 자동 이행하지 않습니다.
- 버전 필드가 없는 기존 형식은 내부적으로 `0`으로 식별합니다. 최초 명시적 저장 전에 원본 바이트를 백업하며, 이행 기록의 `from_schema_version: 0`과 백업 이름의 `v0`에 출처를 표시합니다. 새 문서에 숫자 `schema_version: 0`을 쓰는 것은 아닙니다.
- 유효하지 않거나 지원하지 않는 버전의 설정·프로필은 원본을 덮어쓰지 않고 오류로 처리합니다. 먼저 해당 파일과 백업을 보존한 상태에서 원인을 확인합니다.
- 알파 기능과 호환 형식은 이후 단계의 영구 지원 계약이 아닙니다. **알파 종료 시점(베타 전환 포함)**에 알파 레거시 이행 코드, deprecated API 필드 alias, 검토 전 legacy 입력 정책의 지원을 종료합니다. 알파 종료 이후의 정상 실행 경로에서 알파 레거시를 자동으로 해석하거나 복구하지 않습니다.
- 최종 알파 데이터를 옮겨야 한다면 알파 종료 준비 단계에서 별도 절차나 도구를 정합니다. 호환성 종료가 원본·백업의 삭제를 뜻하지는 않습니다. 실제 호환 코드 제거도 알파 종료 작업에서 수행하며, 현재 알파의 보존·호환 처리는 유지합니다.
- 베타 전환 시 사용할 앱·스키마 버전과 대상 데이터 형식은 전환 전에 별도로 정합니다. 베타는 정식 릴리스보다 먼저 진행할 수 있으며, 정식 릴리스는 앱 **`1.0.0`**, 저장 스키마 **`"1.0"`**부터 시작합니다.

## 개발과 구성

| 경로 | 역할 |
| --- | --- |
| `app.py` | 웹 페이지·API와 서비스 연결, 요청 및 스트림 수명 관리. |
| `models/` | 프로필·앱 설정·대화·메시지 데이터 모델. |
| `services/` | Responses 호출, 입력 정책·요약·첨부·Workspace·저장·사용량 처리. |
| `templates/index.html` | 웹 UI 구조. |
| `static/` | 브라우저 모듈, 스타일, 폰트와 포함된 라이브러리. |

개발 규칙은 [AGENTS.md](AGENTS.md)를 따릅니다. 프런트엔드 자산·원본 라이선스·업데이트 방법은 [자산 목록](static/vendor/README.md)에 정리돼 있습니다.

개발 중 자동 reload가 필요하면 다음 명령을 사용합니다.

```powershell
.\.venv\Scripts\python.exe app.py
```

### 로컬 MCP 프로젝트 읽기 기반

`ProjectMCPClient`는 선택한 Workspace에 대해 앱에 포함된 stdio 서버의 `list_files`, `search_text`, `read_file`을 호출합니다. 공식 Python MCP SDK `2.3.0`과 프로토콜 `2026-07-28`을 고정합니다. 아래의 명시적 탐색 API에서 반복 실행을 지원하며 **Explore project UI는 Planned**입니다. 일반 채팅·Settings·Attach는 MCP 서버를 시작하지 않습니다.

목록은 해당 폴더의 직접 하위 항목만 반환하고, 검색은 대소문자를 구별하는 literal UTF-8 검색입니다. 부분 읽기는 상대 경로·줄·Unicode 열·원본 byte 구간과 메타데이터 기반 파일 revision을 반환합니다. 긴 줄은 조각으로 나누며 원문 개행을 보존합니다. `next_cursor`가 있으면 빈 진행 페이지도 이어서 읽어야 합니다. `complete`는 요청 범위의 순회를 마쳤다는 뜻이며, 제외한 경로나 읽지 못한 파일은 누락 집계에 남습니다. 전체 프로젝트를 읽었다는 보장은 아닙니다.

자동 읽기에는 상위·하위 `.gitignore` 규칙이 적용됩니다. 숨김 파일, 심볼릭 링크·junction·hard link, 호스트가 지정한 Helios 자격 증명·백업·런타임 저장소는 제외하며 모델 인자로 포함시킬 수 없습니다. Windows 8.3 별칭으로도 우회할 수 없습니다. 이는 명시적 첨부 선택과 별개인 앱 수준 경로 정책이며 OS sandbox가 아닙니다. 열린 파일·폴더와 ignore 정책의 변경을 감지하면 해당 이어 읽기를 거절합니다. 파일 revision은 내용 hash나 파일시스템 snapshot이 아닙니다.

ignore 패턴은 대소문자를 구별하며 Git 전역 설정·전역 ignore·`.git/info/exclude`는 읽지 않습니다.

로컬 잠정 자원 한도는 결과 64KiB, stdio 메시지 256KiB, 텍스트 조각 2KiB, 페이지 작업량 1MiB/2,048단계/협력적 5초입니다. 파일시스템 호출 하나의 지연까지 보장하지는 않으며 호스트 호출은 8초 후 세션을 중지합니다. 페이지당 목록 256개·검색 128개·읽기 조각 128개, 탐색 깊이 32, 세션당 cursor 16개/유휴 5분을 적용합니다. `.gitignore`는 파일당 64KiB의 UTF-8만 허용하며 읽을 수 없으면 실패합니다. 이 값은 모델 토큰 한도가 아니며 첨부 제한을 바꾸지 않습니다. 취소·시간 초과·프로토콜 오류·세션 종료 시 프로세스 트리를 정리하고 cursor를 폐기합니다. Windows Job Object를 확보할 수 없으면 시작을 거절하며 임의 명령·외부 MCP 서버는 받지 않습니다.

### 프로젝트 탐색 API

개발용 `POST /api/chat`의 `explore_project: true`로 읽기 탐색을 명시적으로 선택합니다. 먼저 `GET /api/workspace?include_entries=false`에서 얻은 현재 `workspace_revision`과 `workspace_session_id`를 함께 전달해야 합니다. Workspace가 필요하며 구조 전달은 별도 선택입니다. 파일 tree나 절대 root를 모델에 자동으로 넣지 않습니다.

```json
{"prompt":"이 프로젝트의 설정 로딩을 설명해 주세요.","explore_project":true,"workspace_revision":0,"workspace_session_id":"<current session>","exploration_limits":null}
```

모델에는 `helios_project_list_files`, `helios_project_search_text`, `helios_project_read_file`만 등록합니다. 여러 함수 호출은 원래 순서대로 직렬 실행하며 원본 output·opaque reasoning·해당 `call_id`의 원본 도구 결과를 후속 요청에 재전송합니다. 중간 응답의 텍스트는 최종 답변으로 합치지 않습니다. 저장된 결과와 참조 범위는 다음 턴과 완료한 실행의 메모리 요약에서도 사용하며, 현재 파일을 다시 읽어 과거 결과를 바꾸지 않습니다.

입력 추정과 명시적 Block에는 함수 정의·탐색 지침·누적 도구 결과도 포함합니다. 진행 중 실행은 요약하지 않으며 입력 초과·Provider 실패 뒤 자동 축소나 재요청을 하지 않습니다. 탐색은 `store: false`와 `reasoning.encrypted_content` 요청을 사용합니다. reasoning 항목이 반환됐는데 재사용할 암호화 상태가 없으면 호환성 오류로 중지합니다. 실제 Foundry Deployment의 지원은 별도 확인이 필요하며 모든 조합을 검증한 것은 아닙니다. 함수 계약은 [OpenAI function calling 문서](https://developers.openai.com/api/docs/guides/function-calling)를 참고하되 Foundry 지원 보장으로 해석하지 않습니다.

`exploration_limits`의 세 값은 모두 기본 `null`입니다. `max_model_calls`는 탐색 응답 호출 수를, `elapsed_time_target_seconds`와 `cost_goal_usd`는 작업 사이에서 확인할 시간·알려진 추정 비용 목표를 지정합니다. 시간·비용 목표는 진행 중인 API 호출의 강제 deadline이나 청구 상한이 아니며, 비용 정보가 불완전한 비용 목표 실행은 추가 호출 전에 중지합니다. 초기·백그라운드 대화 메모리 요약은 기존 동의·정책과 별도 사용량을 유지하며 이 탐색 응답 호출 목표에 포함하지 않습니다. 도구 자원 한도나 모델 용량을 이 값으로 바꾸지 않습니다.

SSE의 `start`/최종 `done`에는 `run_id`가, `work_progress`에는 단계·도구 호출 ID가 포함됩니다. `work_usage`는 호출별 사용량, `run_usage`는 별도 집계입니다. 실패 응답도 실행 참조와 알려진 집계를 제공하며 저장소를 확인할 수 없으면 unknown으로 남깁니다. 집계를 단일 호출의 사용량으로 메시지에 복제하지 않습니다. 연결·가격·정책·읽기 범위는 시작 시 고정하며, 대화 변경·삭제·Workspace 변경과 사용한 메모리 동의 철회는 후속 실행과 답변 채택을 중지합니다. 첫 제출 전 메모리가 철회되면 원본 이력으로 준비할 수 있지만, 이미 그 요약을 사용한 실행은 원본으로 갈아 끼워 계속하지 않습니다.

중지는 요청 취소·연결 종료로 처리하고 MCP 정리까지 Workspace 점유를 유지합니다. 재생성은 새 실행이며 성공한 최종 답변과 첨부 참조만 기존 분기를 교체합니다. 실패·취소는 이전 분기를 보존합니다. 종료 상태 저장도 실패하면 점유는 해제하고 저장 상태를 unavailable로 표시하며, 재시작 시 남은 미완료 실행은 interrupted로 복구합니다. 탐색의 컨텍스트 오류에는 불완전 실행을 재개하는 일회성 요약 receipt를 만들지 않습니다. 입력을 검토한 새 탐색이 필요합니다. 최종 대화 JSON 저장 실패로 채택되지 않은 completed 실행이 남을 수 있으며 자동 채택·재실행하지 않습니다.

현재 앱은 `0.2.0`을 유지합니다. `0.3.0-alpha`는 Explore project UI 구현과 기존 UI/UX 정렬·검증을 마친 뒤 준비합니다.

### 독립 첨부 API

브라우저 Attach와 같은 준비 경로를 사용하는 백엔드 API를 제공합니다. `POST /api/attachments/uploads?filename=notes.txt`에 원본 bytes를 본문으로 보내면 앱이 발급한 UUID와 `reference`를 반환합니다. 파일명은 표시용 basename이며 서버의 절대 경로나 디렉터리를 받지 않습니다. 수신·검사는 기존 첨부 자원 제한을 적용하고 실패·취소된 업로드를 정리합니다.

`POST /api/attachments/inspect`와 `POST /api/chat`의 `attachment_sources`에 반환된 참조를 전달합니다.

```json
{"attachment_sources":[{"source":"upload","attachment_id":"<app-issued UUID>"}]}
```

Workspace 참조는 `{"source":"workspace","path":"notes.txt"}`이며 검사 결과에는 해당 Workspace의 revision과 session ID도 포함됩니다. 두 출처는 파일 개수·합계 bytes·형식 처리 정책을 공유합니다. 기존 `paths` 검사와 `attachment_files`·`workspace_file`·`workspace_files` 채팅 필드도 유지하며, 이 필드에는 Workspace 선택이 필요합니다.

`GET /api/attachments/uploads`는 원본 목록과 존재 여부를, `GET /api/attachments/uploads/{id}`는 사용할 수 있는 원본의 메타데이터를 반환합니다. 원본은 자동 만료하지 않으며 선택 해제·대화 삭제 후에도 재사용할 수 있습니다. `DELETE /api/attachments/uploads/{id}`로 명시적으로 삭제합니다. 사용 중에는 `409`, 삭제·유실된 ID를 읽을 때는 재선택을 안내하는 `410`을 반환합니다. 원본 폴더와 manifest는 함께 백업합니다.

채팅은 새 참조를 사용자 메시지에 저장하고, 재생성에서 `attachment_sources`를 생략하면 그 참조를 복원합니다. `[]`는 선택 해제이며 변경 참조는 재생성 성공 시 답변과 함께 저장됩니다. 기존 경로 필드의 재생성은 현재 요청의 선택을 사용합니다. 일회성 복구는 실패 요청의 준비 자료를 재사용하며 원본 삭제 시 해당 복구 자료도 폐기합니다. 이미지·PDF bytes와 추출 본문은 대화 JSON에 복제하지 않습니다.

## 이후 검토할 기능

아래 항목은 아직 채팅 UI에서 제공하지 않는 기능이며, 구체적인 범위와 구현 순서는 별도로 정합니다.

- MCP 기반 Work tools: 필요한 프로젝트 파일 탐색, 변경 제안·검토·적용, 명령 실행.
- 웹 검색과 Sandbox/Docker 등을 활용한 코드 실행, 필요한 파일 구간을 읽는 도구와 실행 자원 관리.
- 선택적인 Microsoft Entra ID 인증, Foundry 배포·모델 조회와 배포 정보 자동 채움.

Entra ID나 모델 조회는 일반 채팅의 선행 조건으로 두지 않습니다. 모델 정보의 자동 채움과 메모리·토큰 제한을 켜는 사용자 선택도 구분합니다.

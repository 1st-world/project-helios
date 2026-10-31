언어: [English](README.md) | ***한국어***

# Helios

Microsoft Foundry에 배포한 모델을 자신의 API 키로 사용하는 로컬 웹 앱입니다. 대화를 저장하고, 로컬 파일을 첨부하거나 Workspace를 연결해 프로젝트에 관해 질문할 수 있습니다.

현재 버전은 **`0.4.0-alpha`**이며, 앱 UI는 영어로 제공됩니다.

## 주요 기능

- **대화별 모델 선택**: 여러 연결 프로필을 저장하고 대화마다 사용할 모델을 선택합니다.
- **스트리밍 대화**: Markdown·코드 강조, 메시지 편집, 답변 재생성과 대화 관리를 지원합니다.
- **추론 요약과 작업 과정**: 지원하는 모델의 공개 추론 요약과 도구 활동을 답변 앞에서 확인합니다.
- **Workspace와 파일 첨부**: 폴더 연결 없이도 텍스트·이미지·PDF·Office 파일을 첨부할 수 있습니다.
- **읽기 전용 프로젝트 탐색**: Explore project가 필요한 파일을 목록 조회·검색·부분 읽기하고 근거를 남깁니다.
- **작업 일시중지와 재개**: 안전한 단계 경계에서 멈추고, 같은 모델이나 선택한 다른 모델로 이어갑니다.
- **입력·메모리·비용 관리**: 선택적 입력 예산·출력 제한·대화 메모리와 호출별 사용량·비용 추정을 제공합니다.

## 시작하기

아래 명령은 **Windows PowerShell과 Python 3.12** 기준입니다. Responses API를 지원하는 Microsoft Foundry Deployment와 API 키, 브라우저가 필요합니다. Node.js나 프런트엔드 빌드는 필요하지 않습니다.

프로젝트 루트에서 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

브라우저에서 [http://127.0.0.1:8000](http://127.0.0.1:8000)을 엽니다. 종료할 때는 터미널에서 `Ctrl + C`를 누릅니다.

### 첫 연결

1. **Settings → Connections → New profile**을 엽니다.
2. **Profile name**, **Deployment name**, **Foundry endpoint**, **API key**를 입력합니다.
3. **Save changes**로 저장합니다. 첫 프로필은 새 채팅의 기본값이 되며, **Set as default**로 바꿀 수 있습니다.
4. 입력창의 **Conversation model**에서 모델을 확인하고 메시지를 보냅니다.

**Deployment name**은 모델 카탈로그 이름이 아니라 해당 리소스에 배포한 이름입니다. **Endpoint**에는 모델을 호출할 리소스 또는 Foundry 프로젝트의 API 기준 주소를 입력합니다. 끝에 `/openai/v1/`이 포함된 주소도 사용할 수 있습니다.

```text
https://your-resource.cognitiveservices.azure.com/
https://your-resource.services.ai.azure.com/openai/v1/
https://your-resource.services.ai.azure.com/api/projects/your-project
```

프로필 저장은 연결 정보의 로컬 저장이며 실제 API 호환성은 요청 시 확인됩니다. 모델 용량·가격·메모리를 설정하지 않아도 기본 채팅을 시작할 수 있습니다.

## 기본 사용법

### 대화와 모델

사이드바에서 새 대화를 만들거나 저장된 대화를 엽니다. 모델 선택은 대화별로 저장되고 다음 응답에 적용됩니다. 새 채팅의 기본 프로필을 바꿔도 기존 대화의 선택은 유지됩니다. 메시지를 편집하거나 답변을 재생성하면 해당 지점 이후의 대화 분기를 바꿀 수 있습니다.

### Workspace와 첨부

**Workspace**에서 Helios가 실행되는 컴퓨터의 폴더를 선택합니다. 연결은 대화별로 저장되며, 폴더 없이도 채팅할 수 있습니다. 연결된 폴더의 이름과 최상위 항목 개요는 메시지에 자동으로 포함됩니다. **Preview context**에서 확인하고 연결 해제로 이후 자동 문맥 제공을 중지할 수 있습니다.

숨김 경로와 `.gitignore` 일치 경로는 기본적으로 자동 제외하지 않습니다. **Automatic reading**에서 제외 옵션을 선택하거나 Workspace 루트에 `.heliosignore`를 둘 수 있습니다. 이 파일이 없으면 사용자 지정 제외 규칙이 없으며 앱이 자동 생성하지 않습니다.

**Attach → Add files → Check files → Attach files**로 파일을 선택·검사·적용합니다. 선택창은 Helios가 실행되는 컴퓨터에서 열리며 Workspace 안팎의 파일을 같은 방식으로 가져옵니다. 첨부는 사본이므로 나중에 원래 파일을 수정해도 이미 추가한 내용은 바뀌지 않습니다.

| 형식 | 전달 내용 |
| --- | --- |
| UTF-8 텍스트 | 파일 본문 |
| PNG·JPEG·WebP·정적 GIF, PDF | 원본 이미지 또는 PDF 입력. 모델이 해당 입력을 지원해야 합니다. |
| DOCX·XLSX·PPTX | 추출한 텍스트·표·셀 등의 내용. 내장 이미지·차트·서식은 포함하지 않습니다. |

파일 선택에는 네이티브 선택창이 필요합니다. Workspace는 경로 직접 입력도 지원합니다. 처리 형식과 로컬 자원 제한은 [상세 사용 가이드](docs/reference/usage.ko-kr.md)에 있습니다.

### Explore project와 진행 제어

Workspace를 연결한 뒤 **Tools → Explore project**를 켜고 질문을 보냅니다. 권한은 다음 응답 한 번에 적용되며, 필요한 경우 등록된 읽기 전용 도구를 사용합니다. 파일 수정·명령 실행은 지원하지 않습니다. **Work process**에서 진행과 저장된 결과를 확인할 수 있습니다.

| 동작 | 의미 |
| --- | --- |
| **Stop** | 현재 요청을 중단합니다. 이미 발생한 사용량과 저장된 작업 기록은 유지합니다. |
| **Pause after current step** | Work tools의 현재 모델 호출이나 도구 작업이 끝난 뒤 일시중지합니다. 멈추기 전까지 작업과 비용이 계속될 수 있습니다. |
| **Resume** | 저장된 지점에서 이어갑니다. 다른 모델을 선택하면 인계 내용을 확인한 뒤 재개합니다. |
| **Discard run** | 일시중지한 실행의 재개 지점을 폐기합니다. |

같은 대화에 새 메시지를 보내려면 일시중지한 실행을 재개해 요청이 끝날 때까지 기다리거나 실행을 폐기해야 합니다. 다른 대화는 사용할 수 있습니다. 저장된 일시중지 지점은 최대 24시간 유지되며 자동으로 유료 작업을 재개하지 않습니다.

## 선택적 설정과 비용

기본 채팅에는 앱의 입력 한도나 출력 상한을 두지 않으며, 모델의 실제 한도는 Provider가 판단합니다. 앱 기본값은 **Settings → Context & attachments**, 프로필별 설정은 **Connections → Context & memory**에서 조정합니다.

| 설정 | 용도 |
| --- | --- |
| **Input budget / Input check** | 로컬 추정 입력량에 대해 경고하거나 전송을 차단합니다. 추정치는 실제 과금 토큰과 다를 수 있습니다. |
| **Output limit** | 응답 출력 상한을 지정합니다. reasoning 토큰도 포함됩니다. |
| **Conversation memory** | 오래된 대화를 요약해 이후 입력에 사용합니다. 원본은 보존하지만 추가 유료 호출이 발생하며, 용량 정보와 명시적인 활성화가 필요합니다. |
| **Reasoning summary** | 지원하는 Deployment에 공개 추론 요약을 요청합니다. 기본값은 Off이며 내부 추론 원문을 보여 주는 기능은 아닙니다. |

**Settings → Usage & costs**에서 사용량을 확인합니다. 비용은 프로필에 입력한 가격과 Provider가 보고한 사용량에 따른 추정치이며 실제 청구서나 잔액이 아닙니다. 사용량을 받지 못한 호출은 `0`으로 간주하지 않습니다.

설정 우선순위, 메모리 준비, 오류 복구와 가격 입력은 [상세 사용 가이드](docs/reference/usage.ko-kr.md)를 참조하세요.

## 로컬 데이터와 주의사항

- API 키는 `profiles.json`에 **평문으로 저장**됩니다. 설정과 백업 파일을 함께 보호하세요.
- 대화는 `conversation/`, 첨부·작업 기록은 `data/`, 사용량과 앱 로그는 `logs/`에 저장됩니다. 대화를 삭제해도 원본 작업 기록과 별도 사용량 원장은 자동 삭제하지 않습니다.
- 앱은 로컬에서 실행하지만 질문·대화 이력·선택한 문맥과 첨부 내용은 답변 생성을 위해 외부 Provider로 전송됩니다.
- 기본 구성은 로그인·사용자별 권한 분리가 없는 **로컬 단일 사용자 앱**입니다. 서버는 한 프로세스로 실행합니다.
- 알파 버전의 기능과 저장 형식은 변경될 수 있습니다. 중요한 자료는 [저장 형식과 호환성 안내](docs/reference/architecture.ko-kr.md#버전과-호환성)에 따라 함께 백업하세요.

## 문서와 개발

- [상세 사용 가이드](docs/reference/usage.ko-kr.md): 설정·메모리·Workspace·첨부·작업 결과·비용.
- [아키텍처와 저장 형식](docs/reference/architecture.ko-kr.md): 모듈 역할·입력 구성·MCP·버전·호환성.
- [API 참조](docs/reference/api.ko-kr.md): 대화 모델 선택·탐색·Pause/Resume·모델 인계·첨부 계약.
- [공유 Provider 통신 기록](docs/SHARED-provider-records/README.md): 실제 요청·응답·사용량과 관찰 범위.
- [개발 지침](AGENTS.md) · [프런트엔드 자산과 라이선스](static/vendor/README.md).

현재 인증은 리소스 API 키 방식이며 Responses API를 지원하는 배포를 대상으로 합니다. Chat Completions·Anthropic Messages 호출, Entra ID 인증, 웹 검색·코드 실행과 `Planned` 도구는 아직 제공하지 않습니다.

# 1209chatapp backend

과학 탐구보고서 모둠협업 웹앱의 FastAPI 백엔드입니다. 학생 로그인, 모둠별 WebSocket 채팅, 파일 공유, 타이머, 교사 관리 기능과 AI 보조교사 API를 담당합니다.

## 로컬 실행

```powershell
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

## Docker 실행

```powershell
docker build -t 1209chatapp-backend .
docker run --rm -p 8000:8000 --env-file .env 1209chatapp-backend
```

## Google Cloud Run 배포 준비

이 저장소의 루트를 빌드 컨텍스트로 사용합니다. 운영 환경에는 다음 환경변수를 비밀 설정으로 등록해야 합니다.

- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `TEACHER_CODE`
- `ALLOWED_ORIGINS=https://1209chatapp.netlify.app`
- `DEPLOYMENT_MODE=full`

첫 공개 상태 확인 배포에서는 `DEPLOYMENT_MODE=health-only`를 사용합니다. 이 모드에서는 `/`와 `/api/health`만 열리고 학생·교사 API와 WebSocket은 HTTP 503 또는 연결 종료로 차단됩니다.

현재 SQLite와 로컬 업로드 저장소는 단일 인스턴스 시연용입니다. 여러 인스턴스로 확장하기 전 Firestore와 Cloud Storage로 전환해야 합니다.

# Proposal AI

AI 기반 제안서 자동 생성 웹 서비스.

## 로컬 실행

```bash
cp .env.example .env
# .env에 API 키 입력 후
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Railway 배포

### 1. GitHub 저장소 준비

```bash
git init
git add .
git commit -m "initial commit"
gh repo create proposal-ai --private --source=. --push
```

### 2. Railway 프로젝트 생성

1. [railway.app](https://railway.app) 접속 → **New Project** → **Deploy from GitHub repo**
2. 저장소 선택 후 배포 시작

### 3. 환경변수 설정

Railway 대시보드 → **Variables** 탭에서 아래 키 추가:

| 변수 | 설명 |
|------|------|
| `ANTHROPIC_API_KEY` | Claude API 키 |
| `TAVILY_API_KEY` | Tavily 웹서치 키 |
| `SERP_API_KEY` | SerpAPI 키 |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 알림 봇 토큰 (선택) |
| `SECRET_KEY` | Flask 세션 암호화 키 (랜덤 문자열) |
| `FLASK_ENV` | `production` |

### 4. 볼륨 마운트 (선택 — 데이터 영속성)

Railway 대시보드 → **Volumes** → **Add Volume**  
마운트 경로: `/app/database`  
이후 환경변수 추가: `DB_PATH=/app/database/rfp_cases.db`

> 볼륨 없이 배포하면 재시작 시 DB가 초기화됩니다.

### 5. 배포 확인

- **Deployments** 탭에서 빌드 로그 확인
- 제공된 URL로 접속 → `/login` 페이지 정상 표시 확인
# proposal-ai

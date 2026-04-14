# config.py
# 역할: 환경변수 로딩 및 전역 설정값 관리
# - .env 파일에서 API 키 등 민감정보 로딩
# - 모델명, 경로 등 공통 상수 정의

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

IS_PRODUCTION = os.getenv("FLASK_ENV", "production") == "production"

# 경로 — production(Railway)에서는 볼륨 마운트 경로 사용
BASE_DIR = Path(__file__).parent
DATABASE_DIR = Path("/app/database") if IS_PRODUCTION else BASE_DIR / "database"
OUTPUT_DIR   = Path("/tmp/proposals") if IS_PRODUCTION else BASE_DIR / "output" / "proposals"

# Claude API
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
DEFAULT_MODEL: str = "claude-sonnet-4-6"
MAX_TOKENS: int = 8192

# 웹서치 API
TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
SERP_API_KEY:   str = os.getenv("SERP_API_KEY", "")

# 텔레그램 알림
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# DB — DATABASE_URL > DB_PATH 환경변수 > 경로 기본값 순으로 적용
_default_db = str(DATABASE_DIR / "rfp_cases.db")
DB_PATH: str = os.getenv("DATABASE_URL") or os.getenv("DB_PATH") or _default_db

AGENCY_PROFILES_PATH: str = str(BASE_DIR / "database" / "agency_profiles.json")

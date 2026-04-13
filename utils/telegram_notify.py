# utils/telegram_notify.py
# 텔레그램 봇 알림 전송 유틸리티
# requests 기반 동기 구현 (python-telegram-bot 불필요)

import requests

from config import TELEGRAM_BOT_TOKEN

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(chat_id: "str | int", message: str) -> bool:
    """텔레그램 메시지 전송.

    Args:
        chat_id: 수신자 채팅 ID (숫자 또는 문자열)
        message: 전송할 텍스트 (Markdown 지원)

    Returns:
        성공 여부 (실패 시 로그만 출력, 예외 미전파)
    """
    print(f"  [Telegram] 전송 시도: chat_id={chat_id}", flush=True)

    if not TELEGRAM_BOT_TOKEN:
        print("  [Telegram] TELEGRAM_BOT_TOKEN이 비어 있음 — 전송 중단", flush=True)
        return False
    if not chat_id:
        print("  [Telegram] chat_id가 비어 있음 — 전송 중단", flush=True)
        return False

    url = _API_BASE.format(token=TELEGRAM_BOT_TOKEN)
    try:
        resp = requests.post(
            url,
            json={
                "chat_id":    str(chat_id),
                "text":       message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        print(f"  [Telegram] 응답 코드: {resp.status_code}", flush=True)
        print(f"  [Telegram] 응답 내용: {resp.text[:300]}", flush=True)
        if not resp.ok:
            return False
        return True
    except Exception as e:
        print(f"  [Telegram] 전송 오류: {e}", flush=True)
        return False

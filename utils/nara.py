# utils/nara.py
import os
import threading
import time
import urllib.request
import urllib.parse
import json
from datetime import datetime, timedelta

NARA_API_KEY = os.environ.get("NARA_API_KEY", "YOUR_NARA_API_KEY")
NARA_API_URL = "https://apis.data.go.kr/1230000/BidPublicInfoService04/getBidPblancListInfoServc"

_scheduler_started = False
_scheduler_lock = threading.Lock()

def fetch_bids(keyword: str, page: int = 1, rows: int = 20) -> list:
    today     = datetime.now()
    from_date = (today - timedelta(days=1)).strftime("%Y%m%d%H%M%S")
    to_date   = today.strftime("%Y%m%d%H%M%S")
    params = {
        "serviceKey": NARA_API_KEY,
        "numOfRows":  str(rows),
        "pageNo":     str(page),
        "inqryDiv":   "1",
        "inqryBgnDt": from_date,
        "inqryEndDt": to_date,
        "bidNtceNm":  keyword,
        "type":       "json",
    }
    url = NARA_API_URL + "?" + urllib.parse.urlencode(params, encoding="utf-8")
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        data  = json.loads(raw)
        items = data.get("response", {}).get("body", {}).get("items", {})
        if not items:
            return []
        items = items.get("item", [])
        if isinstance(items, dict):
            items = [items]
        return [_normalize(i) for i in items]
    except Exception as e:
        print(f"[nara] API 오류 ({keyword}): {e}")
        return []

def _normalize(item: dict) -> dict:
    return {
        "bid_ntce_no":   item.get("bidNtceNo", ""),
        "bid_ntce_nm":   item.get("bidNtceNm", ""),
        "ntce_instt_nm": item.get("ntceInsttNm", ""),
        "dmnd_instt_nm": item.get("dmndInsttNm", ""),
        "bid_mtd_nm":    item.get("bidMthdNm", ""),
        "presmpt_prce":  item.get("presmptPrce", ""),
        "bid_ntce_dt":   item.get("bidNtceDt", ""),
        "bid_clse_dt":   item.get("bidClseDt", ""),
        "ntce_url":      item.get("ntceURL", ""),
    }

def start_scheduler(app):
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    def _loop():
        print("[nara] 스케줄러 시작 (1시간 주기)")
        while True:
            try:
                with app.app_context():
                    _run_scan()
            except Exception as e:
                print(f"[nara] 스캔 오류: {e}")
            time.sleep(3600)
    threading.Thread(target=_loop, daemon=True).start()

def _run_scan():
    from database.db import get_nara_keywords, save_nara_bid, is_nara_bid_seen, get_admin_telegram_ids
    from utils.telegram_notify import send_telegram
    keywords = get_nara_keywords()
    if not keywords:
        return
    print(f"[nara] 스캔 시작 — 키워드 {len(keywords)}개")
    new_count = 0
    for kw_row in keywords:
        keyword = kw_row["keyword"]
        bids    = fetch_bids(keyword)
        for bid in bids:
            bid_no = bid["bid_ntce_no"]
            if not bid_no or is_nara_bid_seen(bid_no):
                continue
            save_nara_bid(bid, matched_keyword=keyword)
            new_count += 1
            msg = _format_msg(bid, keyword)
            for chat_id in get_admin_telegram_ids():
                send_telegram(chat_id, msg)
    print(f"[nara] 스캔 완료 — 신규 {new_count}건")

def _format_msg(bid: dict, keyword: str) -> str:
    prce = bid.get("presmpt_prce", "")
    prce_str = f"{int(prce):,}원" if prce and str(prce).isdigit() else "미공개"
    return (
        f"📋 *새 입찰공고 알림*\n"
        f"키워드: `{keyword}`\n"
        f"━━━━━━━━━━━━━━\n"
        f"📌 {bid['bid_ntce_nm']}\n"
        f"🏛 {bid['ntce_instt_nm']}\n"
        f"💰 추정가격: {prce_str}\n"
        f"📅 마감: {bid.get('bid_clse_dt', '-')}\n"
        f"🔗 {bid.get('ntce_url', '')}"
    )

def manual_scan():
    _run_scan()

# utils/nara.py
import os
import threading
import time
import urllib.request
import urllib.parse
import json
from datetime import datetime, timedelta

NARA_API_KEY = os.environ.get("NARA_API_KEY", "")
NARA_API_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch"

print(f"[nara] NARA_API_KEY: {'SET' if os.environ.get('NARA_API_KEY') else 'NOT SET'}")

REGION_CODES = {
    '전국': '',
    '서울': '11',
    '경기': '41',
    '인천': '28',
    '부산': '26',
    '대구': '27',
    '광주': '29',
    '대전': '30',
    '울산': '31',
    '세종': '36',
    '강원': '42',
    '충북': '43',
    '충남': '44',
    '전북': '45',
    '전남': '46',
    '경북': '47',
    '경남': '48',
    '제주': '50',
}

_scheduler_started = False
_scheduler_lock = threading.Lock()

def fetch_bids(keyword: str, page: int = 1, rows: int = 20,
               min_budget: int = 0, max_budget: int = 999,
               period_days: int = 30, region: str = "전국") -> list:
    key = os.environ.get("NARA_API_KEY", "")
    print(f"[nara] 사용 키: {key[:10]}...")
    if not key:
        print(f"[nara] NARA_API_KEY 미설정 — 검색 생략 ({keyword})")
        return []

    today     = datetime.now()
    from_date = (today - timedelta(days=period_days)).strftime("%Y%m%d%H%M")
    to_date   = today.strftime("%Y%m%d%H%M")

    params: dict = {
        "numOfRows":  str(rows),
        "pageNo":     str(page),
        "inqryDiv":   "1",
        "inqryBgnDt": from_date,
        "inqryEndDt": to_date,
        "bidNtceNm":  keyword,
        "type":       "json",
    }

    # 예산 범위 (억원 → 원 단위)
    if min_budget > 0:
        params["presmptPrceMin"] = str(min_budget * 100_000_000)
    if max_budget < 999:
        params["presmptPrceMax"] = str(max_budget * 100_000_000)

    # 지역 코드
    region_code = REGION_CODES.get(region, "")
    if region_code:
        params["dminsttOffrMrktplcRegion"] = region_code

    # serviceKey는 urlencode에서 분리해 이중 인코딩 방지
    # (data.go.kr API 키는 +, = 포함 가능 → urlencode 시 손상됨)
    other_params = urllib.parse.urlencode(params, encoding="utf-8")
    decoded_key = urllib.parse.unquote(key)
    url = (NARA_API_URL
           + "?serviceKey=" + urllib.parse.quote(decoded_key, safe='')
           + "&" + other_params)
    print(f"[nara 요청 URL] {url}")
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        print(f"[nara 응답] {resp.status}: {raw[:200]}")
        data  = json.loads(raw)
        # 오류 응답 감지
        err_msg = data.get("response", {}).get("header", {}).get("returnReasonCode", "")
        err_auth = data.get("response", {}).get("header", {}).get("returnAuthMsg", "")
        if err_auth:
            print(f"[nara] API 인증 오류: {err_auth} (코드: {err_msg})")
            return []
        items = data.get("response", {}).get("body", {}).get("items", [])
        if isinstance(items, dict):
            items = items.get("item", [])
            if isinstance(items, dict):
                items = [items]
        elif isinstance(items, list):
            pass  # 이미 리스트
        else:
            items = []
        if not items:
            return []
        return [_normalize(i) for i in items]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[nara] HTTP {e.code} 오류 전문: {body}")
        return []
    except Exception as e:
        body = ""
        if hasattr(e, 'read'):
            try: body = e.read().decode("utf-8")[:300]
            except: pass
        print(f"[nara] API 오류 ({keyword}): {type(e).__name__}: {e}"
              + (f"\n  응답 body: {body}" if body else ""))
        return []

def _normalize(item: dict) -> dict:
    ntce_url = item.get("ntceURL", "")
    if not ntce_url:
        bid_no  = item.get("bidNtceNo", "")
        bid_ord = item.get("bidNtceOrd", "000")
        if bid_no:
            ntce_url = (
                "https://www.g2b.go.kr/pt/menu/selectSubFrame.do"
                "?framesrc=/pt/menu/frameBidPblancDtl.do"
                f"?bidno={bid_no}&bidseq={bid_ord}"
            )
    return {
        "bid_ntce_no":   item.get("bidNtceNo", ""),
        "bid_ntce_nm":   item.get("bidNtceNm", ""),
        "ntce_instt_nm": item.get("ntceInsttNm", ""),
        "dmnd_instt_nm": item.get("dmndInsttNm", ""),
        "bid_mtd_nm":    item.get("bidMthdNm", ""),
        "presmpt_prce":  item.get("presmptPrce", ""),
        "bid_ntce_dt":   item.get("bidNtceDt", ""),
        "bid_clse_dt":   item.get("bidClseDt", ""),
        "ntce_url":      ntce_url,
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
            time.sleep(86400)
    threading.Thread(target=_loop, daemon=True).start()

def _run_scan():
    from database.db import (get_nara_keywords, save_nara_bid, is_nara_bid_seen,
                              get_admin_telegram_ids, get_nara_settings)
    from utils.telegram_notify import send_telegram
    keywords = get_nara_keywords()
    if not keywords:
        return
    settings    = get_nara_settings()
    min_budget  = settings.get("min_budget",  0)
    max_budget  = settings.get("max_budget",  999)
    period_days = settings.get("period_days", 30)
    region      = settings.get("regions",     "전국")
    print(f"[nara] 스캔 시작 — 키워드 {len(keywords)}개 "
          f"(예산 {min_budget}~{max_budget}억, {period_days}일, {region})")
    new_count = 0
    for kw_row in keywords:
        keyword = kw_row["keyword"]
        bids    = fetch_bids(keyword, min_budget=min_budget, max_budget=max_budget,
                             period_days=period_days, region=region)
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

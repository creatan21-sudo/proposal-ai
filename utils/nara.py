# utils/nara.py
import os
import re
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

_ROWS_PER_PAGE = 100
_MAX_PAGES     = 5
_CHUNK_DAYS    = 30


# ─────────────────────────────────────────────
# 저수준: 단일 API 호출
# ─────────────────────────────────────────────

def fetch_bids_single(keyword: str, from_date: str, to_date: str,
                      page: int = 1, rows: int = _ROWS_PER_PAGE,
                      min_budget: int = 0, max_budget: int = 999,
                      region: str = "전국") -> tuple:
    """단일 날짜 범위 + 단일 페이지 API 호출.

    Returns:
        (items: list, total_count: int)
    """
    key = os.environ.get("NARA_API_KEY", "")
    if not key:
        print(f"[nara] NARA_API_KEY 미설정 — 검색 생략 ({keyword})")
        return [], 0

    params: dict = {
        "numOfRows":  str(rows),
        "pageNo":     str(page),
        "inqryDiv":   "1",
        "inqryBgnDt": from_date,
        "inqryEndDt": to_date,
        "bidNtceNm":  keyword,
        "type":       "json",
    }
    if min_budget > 0:
        params["presmptPrceMin"] = str(min_budget * 100_000_000)
    if max_budget < 999:
        params["presmptPrceMax"] = str(max_budget * 100_000_000)
    region_code = REGION_CODES.get(region, "")
    if region_code:
        params["dminsttOffrMrktplcRegion"] = region_code

    other_params = urllib.parse.urlencode(params, encoding="utf-8")
    decoded_key  = urllib.parse.unquote(key)
    url = (NARA_API_URL
           + "?serviceKey=" + urllib.parse.quote(decoded_key, safe='')
           + "&" + other_params)
    print(f"[nara 요청 URL] {url}")
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
        print(f"[nara 응답] {resp.status}: {raw[:200]}")
        data = json.loads(raw)

        err_auth = data.get("response", {}).get("header", {}).get("returnAuthMsg", "")
        err_code = data.get("response", {}).get("header", {}).get("returnReasonCode", "")
        if err_auth:
            print(f"[nara] API 인증 오류: {err_auth} (코드: {err_code})")
            return [], 0

        body  = data.get("response", {}).get("body", {})
        total = int(body.get("totalCount", 0) or 0)
        items = body.get("items", [])
        if isinstance(items, dict):
            items = items.get("item", [])
            if isinstance(items, dict):
                items = [items]
        elif not isinstance(items, list):
            items = []
        return [_normalize(i) for i in items], total

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[nara] HTTP {e.code} 오류 전문: {body}")
        return [], 0
    except Exception as e:
        print(f"[nara] API 오류 ({keyword}): {type(e).__name__}: {e}")
        return [], 0


# ─────────────────────────────────────────────
# 중간: 특정 기간 전체 페이지 조회
# ─────────────────────────────────────────────

def fetch_bids_all_pages(keyword: str, from_date: str, to_date: str,
                         min_budget: int = 0, max_budget: int = 999,
                         region: str = "전국") -> list:
    """특정 날짜 범위에서 모든 페이지 조회 (최대 _MAX_PAGES 페이지)."""
    all_items = []
    for page in range(1, _MAX_PAGES + 1):
        items, total = fetch_bids_single(
            keyword, from_date, to_date,
            page=page, rows=_ROWS_PER_PAGE,
            min_budget=min_budget, max_budget=max_budget, region=region,
        )
        all_items.extend(items)
        print(f"  [nara] {keyword!r} 페이지 {page}: {len(items)}건 (전체 {total}건)")
        if not items or len(all_items) >= total:
            break
    return all_items


# ─────────────────────────────────────────────
# 최상위: 기간 분할 + 전체 페이지 조회
# ─────────────────────────────────────────────

def fetch_bids_range(keyword: str, period_days: int = 30,
                     min_budget: int = 0, max_budget: int = 999,
                     region: str = "전국") -> list:
    """period_days를 _CHUNK_DAYS 단위로 분할해 전체 조회.

    bid_ntce_no 기준 중복 제거 후 반환.
    """
    today      = datetime.now()
    all_items  : list = []
    seen_nos   : set  = set()
    remaining  = period_days
    end        = today

    while remaining > 0:
        chunk = min(remaining, _CHUNK_DAYS)
        start     = end - timedelta(days=chunk)
        from_date = start.strftime("%Y%m%d%H%M")
        to_date   = end.strftime("%Y%m%d%H%M")
        print(f"  [nara 기간분할] {keyword!r}: {from_date} ~ {to_date} ({chunk}일)")
        items = fetch_bids_all_pages(keyword, from_date, to_date,
                                     min_budget=min_budget, max_budget=max_budget,
                                     region=region)
        for item in items:
            no = item.get("bid_ntce_no", "")
            if no and no not in seen_nos:
                seen_nos.add(no)
                all_items.append(item)
        remaining -= chunk
        end        = start

    return all_items


# ─────────────────────────────────────────────
# 하위 호환: 기존 단순 호출 유지
# ─────────────────────────────────────────────

def fetch_bids(keyword: str, page: int = 1, rows: int = 100,
               min_budget: int = 0, max_budget: int = 999,
               period_days: int = 30, region: str = "전국") -> list:
    """기존 단일 호출 방식 (UI 직접 호출 등 하위 호환용)."""
    today     = datetime.now()
    from_date = (today - timedelta(days=period_days)).strftime("%Y%m%d%H%M")
    to_date   = today.strftime("%Y%m%d%H%M")
    items, _ = fetch_bids_single(keyword, from_date, to_date,
                                  page=page, rows=rows,
                                  min_budget=min_budget, max_budget=max_budget,
                                  region=region)
    return items


# ─────────────────────────────────────────────
# 정규화
# ─────────────────────────────────────────────

def _normalize(item: dict) -> dict:
    ntce_url = item.get("ntceURL", "")
    if not ntce_url:
        bid_no  = item.get("bidNtceNo", "")
        bid_ord = item.get("bidNtceOrd", "000")
        if bid_no:
            ntce_url = f"https://www.g2b.go.kr/link/PNPE027_01/?bidPbancNo={bid_no}&bidPbancOrd={bid_ord}"
        else:
            bid_nm_enc = urllib.parse.quote(item.get("bidNtceNm", ""), safe='')
            ntce_url = f"https://www.g2b.go.kr/index.jsp?search={bid_nm_enc}"
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


# ─────────────────────────────────────────────
# 스케줄러 / 스캔
# ─────────────────────────────────────────────

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

        # 공백 포함 키워드는 공백 제거 버전도 함께 검색
        kw_variants = [keyword]
        kw_no_space = keyword.replace(" ", "")
        if kw_no_space != keyword:
            kw_variants.append(kw_no_space)
            print(f"  [nara] 공백 제거 변형 추가: {kw_no_space!r}")

        # 모든 변형 검색 후 bid_ntce_no 중복 제거
        seen_in_kw: set = set()
        merged_bids: list = []
        for kw_v in kw_variants:
            bids = fetch_bids_range(kw_v, period_days=period_days,
                                    min_budget=min_budget, max_budget=max_budget,
                                    region=region)
            for bid in bids:
                no = bid.get("bid_ntce_no", "")
                if no and no not in seen_in_kw:
                    seen_in_kw.add(no)
                    merged_bids.append(bid)

        raw_count = len(merged_bids)
        # Claude AI 2차 필터링
        if merged_bids:
            merged_bids = filter_bids_with_ai(merged_bids)
        print(f"[nara AI필터] {keyword!r}: {raw_count}건 → {len(merged_bids)}건")

        for bid in merged_bids:
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


# ─────────────────────────────────────────────
# Claude AI 2차 필터링
# ─────────────────────────────────────────────

_COMPANY_PROFILE = """
인터즈(주)는 영상 콘텐츠 제작 전문 회사입니다.
주요 사업 영역:
- 홍보영상, 다큐멘터리, 교육영상 제작
- 유튜브/SNS 콘텐츠 기획 및 제작
- 정부/공공기관 홍보 프로그램 기획·제작
- 방송 콘텐츠 제작
- 디지털 마케팅 및 미디어 운영
"""

def filter_bids_with_ai(bids: list) -> list:
    """Claude API로 영상/콘텐츠/홍보 제작 관련 공고만 필터링."""
    if not bids:
        return []

    bid_list = "\n".join([
        f"{i+1}. [{b['bid_ntce_no']}] {b['bid_ntce_nm']} / {b['ntce_instt_nm']} / 예산: {b['presmpt_prce']}원"
        for i, b in enumerate(bids)
    ])

    prompt = f"""당신은 입찰 공고 분류 전문가입니다.

[회사 프로필]
{_COMPANY_PROFILE}

[입찰 공고 목록]
{bid_list}

위 공고 중 이 회사가 입찰할 수 있는 관련 공고 번호만 골라주세요.
관련 기준: 영상 제작, 콘텐츠 제작, 홍보 프로그램, 방송 제작, SNS/유튜브 운영, 미디어 제작, 홍보 대행 등

반드시 아래 JSON 형식으로만 응답하세요:
{{"relevant": [1, 3, 5]}}
번호는 위 목록의 순서 번호입니다."""

    try:
        from core.claude_client import call_json
        result = call_json(prompt, max_tokens=512)
        relevant_indices = result.get("relevant", [])
        filtered = [bids[i - 1] for i in relevant_indices if 1 <= i <= len(bids)]
        return filtered
    except Exception as e:
        print(f"[nara AI필터] 오류 — 전체 반환: {e}")
        return bids


# ─────────────────────────────────────────────
# 공고번호 직접 조회
# ─────────────────────────────────────────────

def fetch_bid_by_no(bid_ntce_no: str) -> dict | None:
    """공고번호로 단일 공고를 탐색해 반환. 없으면 None.

    공고번호(예: R26BK01523856)에서 일련번호를 추출해 등록일을 역추산.
    기준점: R26BK01480000 ≈ 2026-04-22, 하루 약 1500건 증가.
    추정 등록일 ±3일 범위로 API 검색, 페이지당 100건 최대 10페이지.
    """
    key = os.environ.get("NARA_API_KEY", "")
    if not key:
        return None

    # ── 일련번호 추출 및 등록일 추정 ─────────────────────────
    _BASE_SERIAL = 1_480_000
    _BASE_DATE   = datetime(2026, 4, 22)
    _DAILY_RATE  = 1_500

    try:
        serial       = int(bid_ntce_no[5:])           # R26BK 5자리 제거 → 01523856
        days_diff    = (serial - _BASE_SERIAL) / _DAILY_RATE
        estimated    = _BASE_DATE + timedelta(days=days_diff)
    except (ValueError, IndexError):
        estimated = datetime.now()

    MARGIN    = 7   # ±7일
    ROWS      = 100
    MAX_PAGES = 10

    search_start = estimated - timedelta(days=MARGIN)
    search_end   = estimated + timedelta(days=MARGIN)
    from_date    = search_start.strftime("%Y%m%d%H%M")
    to_date      = search_end.strftime("%Y%m%d%H%M")

    print(f"[nara 번호검색] 추정 등록일: {estimated.strftime('%Y-%m-%d')} (±3일 범위 검색)")
    print(f"[nara 번호검색] 검색 범위: {from_date[:8]} ~ {to_date[:8]}")

    for page in range(1, MAX_PAGES + 1):
        params = {
            "numOfRows":  str(ROWS),
            "pageNo":     str(page),
            "inqryDiv":   "1",
            "inqryBgnDt": from_date,
            "inqryEndDt": to_date,
            "type":       "json",
        }
        other_params = urllib.parse.urlencode(params, encoding="utf-8")
        decoded_key  = urllib.parse.unquote(key)
        url = (NARA_API_URL
               + "?serviceKey=" + urllib.parse.quote(decoded_key, safe='')
               + "&" + other_params)
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8")
            data  = json.loads(raw)
            body  = data.get("response", {}).get("body", {})
            total = int(body.get("totalCount", 0) or 0)
            items = body.get("items", [])
            if isinstance(items, dict):
                items = items.get("item", [])
                if isinstance(items, dict):
                    items = [items]
            elif not isinstance(items, list):
                items = []

            print(f"  [nara 번호검색] 페이지{page}: {len(items)}건 (전체 {total})")

            for item in items:
                if item.get("bidNtceNo", "") == bid_ntce_no:
                    print(f"  [nara 번호검색] ✓ 발견: {bid_ntce_no}")
                    return _normalize(item)

            if not items or page * ROWS >= total:
                break
        except Exception as e:
            print(f"  [nara 번호검색 오류] 페이지{page}: {e}")
            break

    print(f"[nara 번호검색] {bid_ntce_no} — ±3일 범위 내 결과 없음")
    return None

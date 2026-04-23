# agents/researcher.py
# STEP 1: 발주처 리서치 에이전트
#
# [검색 전략]
#   SerpAPI (google.co.kr, hl=ko, gl=kr) — 한국 정보 7개 쿼리
#     ① 발주처 정책/사업 계획  → agency_policy
#     ② 기관장 메시지/현안     → leadership_message
#     ③ 기존 홍보 콘텐츠       → existing_content
#     ④ 최근 이슈/뉴스         → recent_issues_news
#     ⑤ 공공영상 우수사례       → best_cases
#     ⑥ 유사 과업 분석          → task_patterns
#     ⑦ 관련 정책/법령 변화    → (platform_patterns, competitor_patterns 참고)
#   Tavily — 글로벌 트렌드 2개 쿼리
#     ⑧⑨ 타겟·플랫폼 트렌드   → target_media_habits, target_content_preference, platform_patterns
#   우선순위: SerpAPI → Tavily → Claude 지식
#
# 출력: 10개 항목, 항목당 최소 500자 (max_tokens=30000)

import json
import concurrent.futures as _cf
from pathlib import Path

from serpapi import GoogleSearch
from tavily import TavilyClient

from config import SERP_API_KEY, TAVILY_API_KEY
from core import claude_client
from core.dna import ConceptDNA, update_dna, dna_to_context_string
from database.db import (find_similar_analyses, find_past_research, save_research,
                          get_learning_cases_for_researcher,
                          get_research_cache, save_research_cache)

_SONNET_MODEL = "claude-sonnet-4-6"

_AGENCY_PROFILES_PATH = Path(__file__).parent.parent / "database" / "agency_profiles.json"

# 클라이언트 싱글톤
_tavily: TavilyClient | None = None


def _get_tavily() -> TavilyClient | None:
    global _tavily
    if not TAVILY_API_KEY:
        return None
    if _tavily is None:
        _tavily = TavilyClient(api_key=TAVILY_API_KEY)
    return _tavily


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────

_MIN_ITEM_CHARS = 500  # 항목별 최소 글자 수 — 미달 시 재생성


def run(dna: ConceptDNA) -> dict:
    """발주처 리서치 실행.

    Returns:
        10개 항목 dict (항목당 최소 500자, max_tokens=30000):
        발주처: agency_policy, leadership_message, existing_content, recent_issues_news
        시장:   best_cases, task_patterns, competitor_patterns
        타겟:   target_media_habits, target_content_preference
        트렌드: platform_patterns
    """
    # ── 캐시 확인 (7일 이내 동일 발주처+과업) ────────
    try:
        cached = get_research_cache(dna.client_name, dna.project_name)
        if cached:
            print(f"  [캐시] '{dna.client_name} / {dna.project_name}' 리서치 캐시 재사용 (7일 이내)")
            update_dna(dna, {"agency_characteristics": cached.get("agency_policy", "")})
            return cached
    except Exception as e:
        print(f"  [경고] 캐시 조회 실패 (계속 진행): {e}")

    profile    = _load_agency_profile(dna.agency_type)
    past_cases = _query_past_cases(dna.client_name, dna.agency_type)
    if past_cases:
        print(f"  DB 참조: {len(past_cases)}건 이력 발견")

    # 학습 데이터 참조 (발주처·영상유형 유사도 정렬)
    learning_cases = get_learning_cases_for_researcher(
        dna.client_name,
        agency_type=getattr(dna, "agency_type", "") or "",
        video_type=getattr(dna, "video_type", "") or "",
        limit=5,
    )
    print(f"[학습데이터] {len(learning_cases)}건 참조")

    # ── SerpAPI + Tavily 병렬 검색 ────────────
    serp_results: dict[str, list] = {}
    tavily_results: list[dict] = []
    tavily = _get_tavily()

    def _run_serp():
        if not SERP_API_KEY:
            print("  [경고] SERP_API_KEY 없음 — SerpAPI 검색 생략")
            return {}
        print("  SerpAPI 한국 검색 중...")
        r = _serp_search(dna.client_name, dna.project_name, dna.agency_type)
        total = sum(len(v) for v in r.values())
        print(f"  SerpAPI 완료: {total}건")
        return r

    def _run_tavily():
        if not tavily:
            print("  [경고] TAVILY_API_KEY 없음 — Tavily 검색 생략")
            return []
        print("  Tavily 글로벌 트렌드 검색 중...")
        r = _tavily_search(tavily, dna.agency_type)
        print(f"  Tavily 완료: {len(r)}건")
        return r

    with _cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_serp   = ex.submit(_run_serp)
        f_tavily = ex.submit(_run_tavily)
        serp_results   = f_serp.result()
        tavily_results = f_tavily.result()

    if not serp_results and not tavily_results:
        print("  웹서치 결과 없음 — Claude 지식 활용")

    # ── Claude 분석 (Sonnet, 고품질) ──────────
    print("  Claude 10개 항목 분석 중... (항목당 최소 500자, max_tokens=30000)")
    result = _analyze(dna, profile, past_cases, serp_results, tavily_results, learning_cases)

    # ── 500자 미만 항목 재생성 ────────────────
    short_keys = [k for k, v in result.items() if isinstance(v, str) and 0 < len(v) < _MIN_ITEM_CHARS]
    if short_keys:
        print(f"  [품질검사] 500자 미만 항목: {short_keys} — 재생성 시도")
        result = _regenerate_short_items(result, dna, short_keys)

    update_dna(dna, {
        "agency_characteristics": result.get("agency_policy", ""),
        "recent_issues": [],
    })

    # ── DB 저장 (백그라운드 — 타임아웃에 영향 없음) ──
    _data_size = len(json.dumps(result, ensure_ascii=False))
    print(f"  저장 데이터 크기: {_data_size:,}자")

    _client_name = dna.client_name
    _project_name = dna.project_name
    _case_id = getattr(dna, "case_id", 0) or 0
    _result_snapshot = dict(result)  # 스레드에 전달할 사본

    def _save_to_db():
        try:
            save_research(_client_name, _project_name, _result_snapshot, case_id=_case_id)
            print(f"  [백그라운드] 리서치 DB 저장 완료")
        except Exception as e:
            print(f"  [백그라운드] DB 저장 실패 (파이프라인 영향 없음): {e}")
        try:
            save_research_cache(_client_name, _project_name, _result_snapshot)
            print(f"  [백그라운드] 리서치 캐시 저장 완료")
        except Exception as e:
            print(f"  [백그라운드] 캐시 저장 실패 (계속 진행): {e}")

    import threading
    threading.Thread(target=_save_to_db, daemon=True).start()

    return result


# ─────────────────────────────────────────────
# SerpAPI 한국 검색
# ─────────────────────────────────────────────

def _serp_search(client_name: str, project_name: str, agency_type: str) -> dict[str, list]:
    """6개 쿼리로 한국 정보 수집. ThreadPoolExecutor(max_workers=3)로 병렬 실행."""

    queries = [
        ("agency",   f"{client_name} 2025 주요 정책 사업 계획"),
        ("leader",   f"{client_name} 기관장 메시지 현안 2025"),
        ("content",  f"{client_name} 유튜브 홍보 영상 콘텐츠"),
        ("news",     f"{client_name} 최근 이슈 뉴스 논란 보도 2024 2025"),
        ("cases",    f"{agency_type} 공공기관 홍보영상 우수사례 조회수 2024 2025"),
        ("pricing",  f"공공기관 영상 제작 입찰 낙찰 업체 패턴 {agency_type} 2024 2025"),
        ("policy",   f"{client_name} 관련 정책 법령 변화 2024 2025"),
    ]

    results: dict[str, list] = {k: [] for k, _ in queries}

    _SERP_MAX_ATTEMPTS = 2

    def _fetch_one(category: str, query: str) -> tuple[str, list]:
        """단일 쿼리 실행 후 (category, items) 반환."""
        print(f"  [SerpAPI] 검색 중: {query}")
        for attempt in range(_SERP_MAX_ATTEMPTS):
            try:
                params = {
                    "q":             query,
                    "api_key":       SERP_API_KEY,
                    "google_domain": "google.co.kr",
                    "hl":            "ko",
                    "gl":            "kr",
                    "num":           10,
                }
                data = GoogleSearch(params).get_dict()
                hits = data.get("organic_results", [])
                print(f"  [SerpAPI] {category}: {len(hits)}건 수신")
                items = [
                    {"title": h.get("title", ""), "url": h.get("link", ""),
                     "content": h.get("snippet", "")}
                    for h in hits if h.get("link")
                ]
                return category, items
            except Exception as e:
                if attempt < _SERP_MAX_ATTEMPTS - 1:
                    print(f"  [SerpAPI] {category} 실패, 재시도 ({attempt+1}): {e}")
                else:
                    print(f"  [SerpAPI] {category} 최종 실패: {e}")
        return category, []

    with _cf.ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(_fetch_one, cat, q) for cat, q in queries]
        for future in _cf.as_completed(futures):
            try:
                cat, items = future.result()
                results[cat] = items
            except Exception as e:
                print(f"  [SerpAPI] 결과 수집 오류: {e}")

    return results


# ─────────────────────────────────────────────
# Tavily 글로벌 트렌드 검색
# ─────────────────────────────────────────────

def _tavily_search(tavily: TavilyClient, agency_type: str) -> list[dict]:
    """2개 글로벌 쿼리로 알고리즘/콘텐츠 트렌드 수집. 병렬 실행."""

    queries = [
        "youtube algorithm changes 2025 public sector short-form content",
        "government public sector video content marketing trends 2025 platform",
    ]

    _TAVILY_MAX_ATTEMPTS = 2

    def _fetch_one(query: str) -> list[dict]:
        print(f"  [Tavily] 검색 중: {query[:60]}...")
        for attempt in range(_TAVILY_MAX_ATTEMPTS):
            try:
                resp = tavily.search(query, max_results=5)
                hits = resp.get("results", [])
                print(f"  [Tavily] {len(hits)}건 수신")
                return [{"title": h.get("title", ""), "url": h.get("url", ""),
                         "content": h.get("content", "")} for h in hits if h.get("url")]
            except Exception as e:
                if attempt < _TAVILY_MAX_ATTEMPTS - 1:
                    print(f"  [Tavily] 실패, 재시도 ({attempt+1}): {e}")
                else:
                    print(f"  [Tavily] 최종 실패: {e}")
        return []

    collected: list[dict] = []
    seen_urls: set[str]   = set()

    with _cf.ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(_fetch_one, q) for q in queries]
        for f in futures:
            try:
                for item in f.result():
                    if item["url"] not in seen_urls:
                        seen_urls.add(item["url"])
                        collected.append(item)
            except Exception as e:
                print(f"  [Tavily] 결과 수집 오류: {e}")

    return collected


# ─────────────────────────────────────────────
# Claude 분석
# ─────────────────────────────────────────────

_RESEARCH_DEFAULTS = {
    "agency_policy":             "",   # ① 기관 특성/정책
    "leadership_message":        "",   # ② 기관장 메시지 및 현안
    "existing_content":          "",   # ③ 기존 콘텐츠 현황
    "recent_issues_news":        "",   # ④ 최근 이슈/뉴스
    "best_cases":                "",   # ⑤ 유사 기관 홍보전략
    "task_patterns":             "",   # ⑥ 유사 과업 분석 (유사 사업 규모·범위·요구사항)
    "target_media_habits":       "",   # ⑦ 타겟 미디어 소비 행태 (플랫폼별 이용 시간·통계)
    "target_content_preference": "",   # ⑧ 타겟 반응 콘텐츠 선호도 (포맷·주제·톤 선호)
    "platform_patterns":         "",   # ⑨ 플랫폼 최적화 패턴 (알고리즘·SEO·업로드 전략)
    "competitor_patterns":       "",   # ⑩ 경쟁사 패턴 (낙찰 업체·입찰 전략·차별화 포인트)
}


def _fmt_serp_block(items: list[dict], max_chars: int = 500) -> str:
    if not items:
        return "(검색 결과 없음)"
    lines = []
    for r in items:
        lines.append(f"[{r['title']}]\n{r['content'][:max_chars]}\nURL: {r['url']}")
    return "\n\n".join(lines)


def _fmt_tavily_block(items: list[dict], max_chars: int = 600) -> str:
    if not items:
        return "(검색 결과 없음)"
    lines = []
    for r in items:
        lines.append(f"[{r['title']}]\n{r['content'][:max_chars]}\nURL: {r['url']}")
    return "\n\n".join(lines)


# ─────────────────────────────────────────────
# 항목 정의: (key, label, search_keys, instructions)
# search_keys는 _analyze_per_item 내 ctx dict 키를 참조
# ─────────────────────────────────────────────
_ITEM_DEFS = [
    ("agency_policy",             "① 기관 특성/정책",
     ["agency", "policy"],
     "## 기관 소개 — 공식명·설립 목적·주요 역할\n"
     "## 미션·비전 — 공식 미션/비전 문장·핵심 가치\n"
     "## 조직 규모 — 직원 수·예산 규모(원)·산하기관\n"
     "## 최근 정책 우선순위 — 2024-2025 핵심 사업 3개 이상·예산 배분 현황\n"
     "## 관련 법령·제도 변화 — 최신 법령·고시·가이드라인 변화\n"
     "## 홍보 성향 — 기존 홍보 방식(보수적/혁신적)·선호 포맷·국민 인지도"),
    ("leadership_message",        "② 기관장 메시지",
     ["leader"],
     "## 기관장 정보 — 성명·취임 시기·경력\n"
     "## 올해 핵심 메시지 — 신년사·공식 발언 반복 키워드 5개 이상\n"
     "## 최우선 과제 — 올해 기관 최우선 과제 3가지 이상 (구체적 내용)\n"
     "## 예산·전략 방향 — 예산 배분 우선순위·대외 이미지 전략 방향"),
    ("existing_content",          "③ 기존 콘텐츠 현황",
     ["content"],
     "## 유튜브·SNS 채널 현황 — 구독자 수·평균 조회수·게시물 수\n"
     "## 기존 홍보영상 스타일 — 최근 2년 제작 영상 포맷/톤/주제\n"
     "## 조회수·반응 분석 — 가장 높은 조회수 사례·댓글·공유 반응\n"
     "## 성공작·실패작 사례 — 구체적 사례 2개 이상·성공/실패 요인 분석\n"
     "## 콘텐츠 개선 여지 — 현재 문제점과 개선 방향"),
    ("recent_issues_news",        "④ 최근 이슈/뉴스",
     ["news"],
     "## 최근 6개월 주요 뉴스 — 2024.10~2025.04 주요 보도 3건 이상\n"
     "## 긍정 이슈 — 기관 성과·포상·정책 성공 사례\n"
     "## 부정 이슈 — 논란·비판·문제 제기 사안 (없으면 '없음' 명시)\n"
     "## 언론 보도 경향 — 어떤 언론이 어떤 각도로 보도하는지 패턴 분석"),
    ("best_cases",                "⑤ 유사기관 홍보전략",
     ["cases"],
     "## 동종 기관 우수사례 2개 이상 (2024-2025) — 기관명·영상명·조회수·핵심 전략\n"
     "## 성공 요인 분석 — 공통 성공 패턴·차별화 포인트\n"
     "## 우리 제안서 적용 포인트 — 이 사례에서 벤치마킹할 전략"),
    ("task_patterns",             "⑥ 유사과업 분석",
     ["pricing", "policy"],
     "## 유사 과업 규모 분석 — 최근 유사 사업 예산 범위·납품 수량·기간 패턴\n"
     "## 요구사항 공통점 — 유사 RFP에서 반복 등장하는 필수 항목·스펙\n"
     "## 심사 기준 패턴 — 높이 평가받는 요소·필수 제출 서류\n"
     "## 피해야 할 클리셰 — 심사에서 부정적 평가받는 진부한 표현·구성"),
    ("target_media_habits",       "⑦ 타겟 미디어 소비행태",
     ["tavily", "cases"],
     "## 주요 타겟층 정의 — 연령·직업·관심사·지역 (이 과업의 실제 시청 대상)\n"
     "## 플랫폼별 이용 시간 — 유튜브·인스타·틱톡·네이버TV 일평균 이용 시간 (통계 포함)\n"
     "## TV vs 디지털 소비 비율 — 연령대별 미디어 이용 채널 분포\n"
     "## 공공기관 콘텐츠 접촉 패턴 — 공공 콘텐츠를 어디서 어떻게 소비하는지"),
    ("target_content_preference", "⑧ 타겟 반응 콘텐츠 선호도",
     ["tavily", "cases"],
     "## 선호 포맷 — 숏폼(15~60초) vs 롱폼(5분 이상) 선호도 및 시청 완료율\n"
     "## 선호 주제·톤 — 타겟이 즐겨 보는 주제·선호하는 톤앤매너\n"
     "## 바이럴 유발 패턴 — 공공 콘텐츠 중 공유·댓글 많이 받은 공통 요소\n"
     "## 기피 요소 — 타겟이 즉시 이탈하는 콘텐츠 패턴"),
    ("platform_patterns",         "⑨ 플랫폼 최적화 패턴",
     ["tavily"],
     "## 유튜브 알고리즘 최적화 — 2024-2025 최신 랭킹 요인·CTR·AVD 기준\n"
     "## 숏폼 플랫폼 패턴 — 인스타그램 릴스·틱톡 노출 알고리즘 변화\n"
     "## 최적 업로드 전략 — 영상 길이·업로드 시간대·썸네일·제목 패턴\n"
     "## 공공기관 채널 성장 패턴 — 팔로워 증가 속도·인게이지먼트 벤치마크"),
    ("competitor_patterns",       "⑩ 경쟁사 패턴",
     ["pricing"],
     "## 주요 낙찰 업체 분석 — 반복 수주 업체명·수주 집중도(%)\n"
     "## 낙찰 업체 제안 전략 — 공통 제안 패턴·차별화 포인트\n"
     "## 낙찰가 분석 — 예정가 대비 낙찰 비율(%)·가격 경쟁 강도\n"
     "## 우리의 경쟁 우위 전략 — 기존 강자들 대비 차별화할 포인트"),
]


def _analyze(dna: ConceptDNA, profile: dict, past_cases: list,
             serp_results: dict[str, list], tavily_results: list[dict],
             learning_cases: list = None) -> dict:
    """10개 항목을 개별 Claude 호출로 분석 (ThreadPoolExecutor max_workers=3).

    각 항목은 claude_client.call()로 일반 텍스트를 받아 저장 — JSON 파싱 없음.
    500자 미만 항목은 자동 재시도.
    """
    s = serp_results or {}
    ctx = {
        "agency":   _fmt_serp_block(s.get("agency",  [])),
        "leader":   _fmt_serp_block(s.get("leader",  [])),
        "content":  _fmt_serp_block(s.get("content", [])),
        "news":     _fmt_serp_block(s.get("news",    [])),
        "cases":    _fmt_serp_block(s.get("cases",   [])),
        "pricing":  _fmt_serp_block(s.get("pricing", [])),
        "policy":   _fmt_serp_block(s.get("policy",  [])),
        "tavily":   _fmt_tavily_block(tavily_results or []),
    }

    dna_ctx   = dna_to_context_string(dna)
    past_str  = "\n".join(
        f"- {c.get('client_name','')} / {c.get('project_name','')} / {c.get('agency_type','')}"
        for c in past_cases[:3]
    ) or "(없음)"

    # 학습 데이터 → 프롬프트 주입용 블록 구성
    _lc_list = learning_cases or []
    if _lc_list:
        _lc_parts = []
        for lc in _lc_list:
            score_str  = f" (평가점수 {lc['eval_score']:.0f}점)" if lc.get("eval_score") else ""
            result_str = f" [{lc['bid_result']}]" if lc.get("bid_result") and lc["bid_result"] != "미정" else ""
            header     = f"[{lc.get('data_type','')}] {lc.get('client_name','')} / {lc.get('project_name','')}{result_str}{score_str}"
            content    = (lc.get("content") or "")[:600]
            _lc_parts.append(f"{header}\n{content}")
        learning_block = (
            "\n\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            "[INTERZ 우수 제안 사례 참고]\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "아래는 인터즈의 실제 우수 제안 사례입니다. "
            "이 사례들의 전략·컨셉·표현 방식을 참고하여 작성하세요.\n\n"
            + "\n\n".join(_lc_parts)
        )
    else:
        learning_block = ""

    def _call_one(key: str, label: str, search_keys: list, instructions: str) -> tuple[str, str]:
        search_block = "\n\n".join(
            f"▶ {sk}\n{ctx.get(sk, '(없음)')}" for sk in search_keys
        )
        prompt = (
            f"당신은 대한민국 정부 입찰 전략 전문가이자 공공 홍보 분야 수석 리서처입니다.\n\n"
            f"아래 정보를 바탕으로 [{label}]을 최소 500자 이상 마크다운 텍스트로 작성하십시오.\n\n"
            f"【절대 원칙】\n"
            f"1. 최소 500자 이상 작성. 짧은 답변은 품질 기준 미달.\n"
            f"2. ## 소제목으로 반드시 구분 (아래 지침의 ## 항목 모두 포함).\n"
            f"3. 수치와 구체적 사례 반드시 포함. '증가했다'(X) → '23% 증가 (출처, 2024)'(O)\n"
            f"4. 모든 수치에 (기관명, 연도) 형식으로 출처 표기.\n"
            f"5. JSON 형식이 아닌 순수 마크다운 텍스트로만 작성.\n\n"
            f"[프로젝트 정보]\n{dna_ctx}\n\n"
            f"[DB 유사 케이스]\n{past_str}\n"
            f"{learning_block}\n\n"
            f"[검색 결과]\n{search_block}\n\n"
            f"[작성 지침]\n{instructions}\n\n"
            f"위 지침에 따라 {label} 내용을 지금 바로 작성하십시오:"
        )
        for retry in range(2):
            try:
                text = claude_client.call(prompt, model=_SONNET_MODEL,
                                          max_tokens=3000, max_retries=2)
                text = text.strip()
                if len(text) >= 400:
                    print(f"  [{key}] 완료 ({len(text)}자)")
                    return key, text
                print(f"  [{key}] 너무 짧음 ({len(text)}자) — 재시도 {retry + 1}/2")
            except claude_client.OverloadError:
                print(f"  [{key}] API 과부하 — 빈값으로 대체")
                return key, ""
            except Exception as e:
                print(f"  [{key}] 오류: {e}")
        return key, ""

    result = dict(_RESEARCH_DEFAULTS)
    try:
        with _cf.ThreadPoolExecutor(max_workers=3) as ex:
            futures = {
                ex.submit(_call_one, key, label, sk, instr): key
                for key, label, sk, instr in _ITEM_DEFS
            }
            for f in _cf.as_completed(futures):
                try:
                    key, text = f.result()
                    result[key] = text
                except Exception as e:
                    key = futures[f]
                    print(f"  [{key}] 예외: {e}")
    except Exception as e:
        print(f"  [리서처] 병렬 실행 실패: {e}")

    return result


# ─── 아래는 레거시 코드 (삭제됨 — _build_prompt는 _analyze 내 _ITEM_DEFS로 대체) ───

def _build_prompt_legacy(*args, **kwargs) -> str:
    """레거시: 더 이상 사용하지 않음. _analyze()의 per-item 방식으로 대체됨."""
    return ""


# ─────────────────────────────────────────────
# 보조 함수
# ─────────────────────────────────────────────

def _load_agency_profile(agency_type: str) -> dict:
    if not _AGENCY_PROFILES_PATH.exists():
        return {}
    try:
        with open(_AGENCY_PROFILES_PATH, encoding="utf-8") as f:
            profiles = json.load(f)
        if agency_type in profiles:
            return profiles[agency_type]
        for key in profiles:
            if key in agency_type or agency_type in key:
                return profiles[key]
    except Exception:
        pass
    return {}


def _query_past_cases(client_name: str, agency_type: str) -> list:
    results = []
    try:
        results.extend(find_similar_analyses(client_name, agency_type, limit=2))
    except Exception:
        pass
    try:
        results.extend(find_past_research(client_name, agency_type, limit=2))
    except Exception:
        pass
    return results

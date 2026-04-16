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
    # ── 캐시 확인 (7일 이내 동일 발주처) ────────
    try:
        cached = get_research_cache(dna.client_name)
        if cached:
            print(f"  [캐시] '{dna.client_name}' 리서치 캐시 재사용 (7일 이내)")
            update_dna(dna, {"agency_characteristics": cached.get("agency_policy", "")})
            return cached
    except Exception as e:
        print(f"  [경고] 캐시 조회 실패 (계속 진행): {e}")

    profile    = _load_agency_profile(dna.agency_type)
    past_cases = _query_past_cases(dna.client_name, dna.agency_type)
    if past_cases:
        print(f"  DB 참조: {len(past_cases)}건 이력 발견")

    # 학습 데이터 참조
    learning_cases = get_learning_cases_for_researcher(dna.client_name, limit=5)
    if learning_cases:
        print(f"  학습 데이터 참조: {len(learning_cases)}건")

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

    try:
        save_research(dna.client_name, dna.project_name, result,
                      case_id=getattr(dna, "case_id", 0) or 0)
    except Exception as e:
        print(f"  [경고] DB 저장 실패: {e}")

    # ── 캐시 저장 ────────────────────────────────
    try:
        save_research_cache(dna.client_name, result)
    except Exception as e:
        print(f"  [경고] 캐시 저장 실패 (계속 진행): {e}")

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


def _regenerate_short_items(result: dict, dna: ConceptDNA, short_keys: list) -> dict:
    """500자 미만 항목 개별 재생성."""
    items_desc = "\n".join(
        f"- {k}: 현재 {len(result.get(k, ''))}자" for k in short_keys
    )
    current_content = json.dumps(
        {k: result.get(k, "") for k in short_keys},
        ensure_ascii=False, indent=2
    )
    prompt = (
        f"다음 리서치 항목들이 500자 미만으로 품질 기준에 미달합니다.\n"
        f"각 항목을 최소 500자 이상으로 재작성하십시오.\n\n"
        f"[재작성 대상]\n{items_desc}\n\n"
        f"[현재 내용]\n{current_content}\n\n"
        f"[프로젝트 정보]\n"
        f"발주처: {dna.client_name}\n"
        f"사업명: {dna.project_name}\n"
        f"영상 종류: {dna.video_type}\n\n"
        "각 항목을 최소 500자 이상으로 확장하여 JSON으로만 출력하십시오.\n"
        "출력 형식 (다른 텍스트 없이 순수 JSON):\n"
        "{" + ", ".join(f'"{k}": "내용"' for k in short_keys) + "}"
    )
    try:
        new_result = claude_client.call_json(
            prompt, model=_SONNET_MODEL, max_tokens=15000, max_retries=1
        )
        for k in short_keys:
            if k in new_result and isinstance(new_result[k], str) and len(new_result[k]) >= 400:
                result[k] = new_result[k]
                print(f"  [품질검사] {k} 재생성 완료 ({len(new_result[k])}자)")
            else:
                print(f"  [품질검사] {k} 재생성 실패 또는 여전히 짧음")
    except Exception as e:
        print(f"  [품질검사] 재생성 오류: {e}")
    return result


def _analyze(dna: ConceptDNA, profile: dict, past_cases: list,
             serp_results: dict[str, list], tavily_results: list[dict],
             learning_cases: list = None) -> dict:
    """Claude Sonnet으로 10개 항목 분석 (고품질). 실패 시 빈 defaults 반환.

    max_tokens=30000 (항목당 3000 × 10개) — 각 항목 최소 500자 이상 보장.
    """
    prompt = _build_prompt(dna, profile, past_cases, serp_results, tavily_results, learning_cases or [])

    try:
        result = claude_client.call_json(prompt, model=_SONNET_MODEL,
                                         max_tokens=30000, max_retries=2)
        defaults = dict(_RESEARCH_DEFAULTS)
        for k, v in defaults.items():
            result.setdefault(k, v)
        return result

    except claude_client.OverloadError:
        print("  [리서처] Claude API 과부하 (2회 재시도 소진) — 기본값으로 대체, 계속 진행")
        return dict(_RESEARCH_DEFAULTS)

    except Exception as e:
        print(f"  [리서처] Claude 분석 실패: {e} — 기본값으로 대체, 계속 진행")
        return dict(_RESEARCH_DEFAULTS)


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


def _build_prompt(dna: ConceptDNA, profile: dict, past_cases: list,
                  serp_results: dict[str, list],
                  tavily_results: list[dict],
                  learning_cases: list = None) -> str:

    dna_ctx       = dna_to_context_string(dna)
    profile_block = json.dumps(profile, ensure_ascii=False, indent=2) if profile else "(없음)"
    rfp_excerpt   = (dna.rfp_text or "")[:3000]

    past_block = "\n".join(
        f"- {c.get('client_name','')} / {c.get('project_name','')} / {c.get('agency_type','')}"
        for c in past_cases[:3]
    ) or "(없음)"

    # 학습 데이터 블록
    learning_block = "(없음)"
    if learning_cases:
        lines = []
        for lc in learning_cases[:5]:
            bid = lc.get("bid_result", "미정")
            score = f"{lc.get('eval_score',0):.1f}점" if lc.get("eval_score") else ""
            lines.append(
                f"[{lc.get('data_type','')}] {lc.get('client_name','')} / {lc.get('project_name','')} "
                f"— {bid}{' / ' + score if score else ''}\n  {(lc.get('content') or '')[:300]}"
            )
        learning_block = "\n\n".join(lines)

    # SerpAPI 블록 — 용도별
    s = serp_results
    serp_agency  = _fmt_serp_block(s.get("agency",  []))
    serp_leader  = _fmt_serp_block(s.get("leader",  []))
    serp_content = _fmt_serp_block(s.get("content", []))
    serp_news    = _fmt_serp_block(s.get("news",    []))
    serp_cases   = _fmt_serp_block(s.get("cases",   []))
    serp_pricing = _fmt_serp_block(s.get("pricing", []))
    serp_policy  = _fmt_serp_block(s.get("policy",  []))

    # Tavily 블록 — 글로벌 트렌드
    tavily_block = _fmt_tavily_block(tavily_results)

    return f"""당신은 대한민국 정부 입찰 전략 전문가이자 공공 홍보 분야 수석 리서처입니다.
아래 [SerpAPI 한국 검색 결과]와 [Tavily 글로벌 트렌드]를 최우선 근거로 활용하고,
검색 결과가 부족한 항목은 Claude 학습 지식으로 보완하여
10개 항목을 JSON으로 작성하십시오.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【절대 원칙 — 반드시 준수】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 모든 항목은 최소 500자 이상 작성한다. 500자 미만은 품질 기준 미달이다.
2. ## 소제목으로 반드시 구분하여 작성한다. 소제목 없는 서술은 금지.
3. 수치(예산액, 조회수, %, 명 수)와 구체적 사례를 반드시 포함한다.
   - '증가했다' (X) → '2024년 대비 23% 증가했다' (O)
   - '낮다' (X) → '전체의 7%에 불과하다' (O)
4. 출처를 반드시 명시한다. 형식: (출처명, 연도)
   - '조회수 300만 회 (행정안전부 공식 발표, 2024)'
   - 출처 불명 시 '추정치' 또는 '자체 분석'으로 표기
5. 모호한 표현 절대 금지: "~로 알려져 있다", "~할 것으로 예상된다"
6. 내용 없는 빈 항목 절대 금지. 검색 결과가 없으면 Claude 지식으로 보완할 것.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【데이터 품질 규칙 — 수치 중심 작성 필수】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 숫자 우선 원칙:
   모든 주장은 수치로 뒷받침하라.
   - '유튜브 이용률이 높다' (X)
     '20대 유튜브 월간 이용률 92.3% (방통위, 2024)' (O)
   - '최근 증가했다' (X)
     '전년 대비 23% 증가 (출처명, 연도)' (O)

2. 출처 명기 규칙:
   모든 수치에 (기관명, 연도) 형식으로 출처를 표기하라.
   - 웹서치 결과 기반 → 실제 출처명 표기: (기상청, 2024)
   - Claude 자체 지식 기반 → (추정: 출처기관명 기준)으로 표기
   - 출처 확인 불가 → (출처 미확인, 검증 필요)로 표기
   - '자체 분석' 단독 표기 절대 금지

3. 데이터 우선순위 (높은 순):
   ① 조회수·구독자수·참여율 등 플랫폼 실측 지표
   ② 예산 규모·입찰 금액·낙찰가
   ③ 설문·조사 통계 (방통위, 문화체육관광부, 한국갤럽 등)
   ④ 시장 규모·성장률
   ⑤ 연령별·성별 이용 통계

4. 항목별 필수 수치:
   ① agency_policy             → 예산 규모(원), 조직 인원(명), 운영 기간(년)
   ② leadership_message        → 올해 목표 수치(예산 증감%, KPI 목표)
   ③ existing_content          → 조회수(회), 구독자(명), 게시물 수(건)
   ④ recent_issues_news        → 보도 건수, 관련 수치(있는 경우)
   ⑤ best_cases                → 조회수(회), 참여율(%), 제작 예산(원)
   ⑥ task_patterns             → 유사 사업 예산 규모(원), 납품 수량(편), 기간(월)
   ⑦ target_media_habits       → 연령별 플랫폼 이용률(%), 일평균 이용 시간(분)
   ⑧ target_content_preference → 포맷별 선호도(%), 평균 시청 완료율(%)
   ⑨ platform_patterns         → 알고리즘 가중치, 최적 영상 길이(초), 업로드 주기
   ⑩ competitor_patterns       → 낙찰 금액(원), 참가 업체 수(개사), 수주 집중도(%)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[SerpAPI — 한국 검색 결과 (google.co.kr)]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

▶ 발주처 정책/사업 계획 (→ agency_policy 근거)
{serp_agency}

▶ 기관장 메시지/현안 (→ leadership_message 근거)
{serp_leader}

▶ 기존 홍보 콘텐츠 (→ existing_content 근거)
{serp_content}

▶ 최근 이슈/뉴스 (→ recent_issues_news 근거)
{serp_news}

▶ 공공기관 홍보 우수사례 (→ best_cases 근거)
{serp_cases}

▶ 유사 입찰/낙찰 패턴 (→ competitor_patterns 근거)
{serp_pricing}

▶ 관련 정책/법령 변화 (→ recent_trends 참고)
{serp_policy}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[Tavily — 글로벌 트렌드 (→ target_analysis, recent_trends 근거)]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{tavily_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[프로젝트 정보]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{dna_ctx}

[RFP 원문 발췌]
{rfp_excerpt or "(없음)"}

[기관 유형 프로필]
{profile_block}

[DB 유사 케이스]
{past_block}

[학습 데이터 — 이 발주처 관련 과거 RFP/제안서/낙찰 사례 (있으면 우선 참조)]
{learning_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[10개 항목 작성 지침]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【발주처 분석】 ← SerpAPI 한국 검색 결과 우선 활용

① agency_policy (최소 500자)
   ## 기관 소개 — 기관 공식명, 설립 목적, 법적 근거, 주요 역할
   ## 미션·비전 — 공식 미션/비전 문장, 핵심 가치
   ## 조직 규모 — 직원 수, 예산 규모, 산하기관, 관련 기관
   ## 최근 정책 우선순위 — 2024-2025 핵심 사업 3개 이상, 예산 배분 현황
   ## 관련 법령·제도 변화 — 기관 운영에 영향을 주는 법령·고시·가이드라인 최신 변화
   ## 홍보 성향 — 기존 홍보 방식(보수적/혁신적), 선호 포맷, 국민 인지도

② leadership_message (최소 500자)
   ## 기관장 정보 — 성명, 취임 시기, 경력
   ## 올해 핵심 메시지 — 신년사·공식 발언에서 반복되는 핵심 키워드 5개 이상
   ## 최우선 과제 — 올해 기관의 최우선 과제 3가지 이상 (구체적 내용 포함)
   ## 예산·전략 방향 — 예산 배분 우선순위, 대외 이미지 전략 방향

③ existing_content (최소 500자)
   ## 유튜브·SNS 채널 현황 — 구독자 수, 평균 조회수, 게시물 수
   ## 기존 홍보영상 스타일 — 최근 2년 제작 영상 포맷/톤/주제
   ## 조회수·반응 분석 — 가장 높은 조회수 사례, 댓글·공유 반응
   ## 성공작·실패작 사례 — 구체적 사례 2개 이상, 성공/실패 요인 분석
   ## 콘텐츠 개선 여지 — 현재 문제점과 개선 방향

④ recent_issues_news (최소 500자) ← SerpAPI 최근 이슈 검색 결과 우선 활용
   ## 최근 6개월 주요 뉴스 — 2024.10~2025.04 기준 주요 보도 3건 이상
   ## 긍정 이슈 — 기관 성과, 포상, 정책 성공 사례
   ## 부정 이슈 — 논란, 비판, 문제 제기 사안 (없으면 '없음'으로 명시)
   ## 언론 보도 경향 — 어떤 언론이 어떤 각도로 보도하는지 패턴 분석

【과업 시장 분석】 ← SerpAPI 한국 검색 결과 우선 활용

⑤ best_cases (최소 500자)
   ## 동종 기관 우수사례 2개 이상 (2024-2025)
   각 사례: 기관명, 영상/캠페인명, 조회수·참여율 등 수치, 핵심 전략
   ## 성공 요인 분석 — 공통 성공 패턴, 차별화 포인트
   ## 우리 제안서 적용 포인트 — 이 사례에서 벤치마킹할 전략

⑥ task_patterns (최소 500자)
   ## 유사 과업 규모 분석 — 최근 유사 사업 예산 범위, 납품 수량·기간 패턴
   ## 요구사항 공통점 — 유사 RFP에서 반복 등장하는 필수 항목·스펙
   ## 심사 기준 패턴 — 높이 평가받는 요소, 필수 제출 서류
   ## 피해야 할 클리셰 — 심사에서 부정적 평가받는 진부한 표현·구성

【타겟 심층 분석】 ← Tavily 글로벌 트렌드 + Claude 지식 활용

⑦ target_media_habits (최소 500자)
   ## 주요 타겟층 정의 — 연령·직업·관심사·지역 (이 과업의 실제 시청 대상)
   ## 플랫폼별 이용 시간 — 유튜브·인스타·틱톡·네이버TV 일평균 이용 시간 (통계 포함)
   ## TV vs 디지털 소비 비율 — 연령대별 미디어 이용 채널 분포
   ## 공공기관 콘텐츠 접촉 패턴 — 공공 콘텐츠를 어디서 어떻게 소비하는지

⑧ target_content_preference (최소 500자)
   ## 선호 포맷 — 숏폼(15~60초) vs 롱폼(5분 이상) 선호도 및 시청 완료율
   ## 선호 주제·톤 — 타겟이 즐겨 보는 주제, 선호하는 톤앤매너
   ## 바이럴 유발 패턴 — 공공 콘텐츠 중 공유·댓글 많이 받은 공통 요소
   ## 기피 요소 — 타겟이 즉시 이탈하는 콘텐츠 패턴

⑨ platform_patterns (최소 500자)
   ## 유튜브 알고리즘 최적화 — 2024-2025 최신 랭킹 요인, CTR·AVD 기준
   ## 숏폼 플랫폼 패턴 — 인스타그램 릴스·틱톡 노출 알고리즘 변화
   ## 최적 업로드 전략 — 영상 길이, 업로드 시간대, 썸네일·제목 패턴
   ## 공공기관 채널 성장 패턴 — 팔로워 증가 속도, 인게이지먼트 벤치마크

⑩ competitor_patterns (최소 500자) ← SerpAPI 입찰/낙찰 패턴 검색 결과 우선 활용
   ## 주요 낙찰 업체 분석 — 반복 수주 업체명, 수주 집중도(%)
   ## 낙찰 업체 제안 전략 — 공통 제안 패턴, 차별화 포인트
   ## 낙찰가 분석 — 예정가 대비 낙찰 비율(%), 가격 경쟁 강도
   ## 우리의 경쟁 우위 전략 — 기존 강자들 대비 차별화할 포인트

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[텍스트 필드 형식 규칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
모든 문자열 필드는 아래 마크다운 서식으로 작성하십시오.
• ## 소제목  — 섹션 구분 (예: ## 핵심 현황)
• ### 소제목 — 세부 소제목 (예: ### 주요 수치)
• **키워드** — 핵심 개념·용어 강조
• 수치·통계 — 별도 줄에 작성 / 출처 필수 (기관명, 연도)
• 섹션 사이 — 빈 줄 하나

예시:
## 기관 소개

**행정안전부**는 2025년 예산 4조 2천억 원 규모의 중앙부처이다. (행안부 예산서, 2025)

### 핵심 수치
- 직원 수 4,200명 (2024 기준, 행안부 발표)
- 국민 신뢰도 67% (한국갤럽, 2024)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[출력 형식 — 순수 JSON만, 다른 텍스트 절대 금지]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{{
  "agency_policy":             "최소 500자. ## 기관 소개 / ## 미션·비전 / ## 조직 규모 / ## 최근 정책 우선순위 / ## 관련 법령·제도 변화 포함.",
  "leadership_message":        "최소 500자. ## 기관장 정보 / ## 올해 핵심 메시지 / ## 최우선 과제 3가지 이상 / ## 예산·전략 방향 포함.",
  "existing_content":          "최소 500자. ## 유튜브·SNS 채널 현황 / ## 기존 홍보영상 스타일 / ## 조회수·반응 분석 / ## 성공작·실패작 사례 포함.",
  "recent_issues_news":        "최소 500자. ## 최근 6개월 주요 뉴스 / ## 긍정 이슈 / ## 부정 이슈 / ## 언론 보도 경향 포함.",
  "best_cases":                "최소 500자. ## 동종 기관 우수사례 2개 이상(수치 포함) / ## 성공 요인 분석 / ## 우리 제안서 적용 포인트 포함.",
  "task_patterns":             "최소 500자. ## 유사 과업 규모 분석 / ## 요구사항 공통점 / ## 심사 기준 패턴 / ## 피해야 할 클리셰 포함.",
  "target_media_habits":       "최소 500자. ## 주요 타겟층 정의 / ## 플랫폼별 이용 시간(통계) / ## TV vs 디지털 소비 비율 / ## 공공 콘텐츠 접촉 패턴 포함.",
  "target_content_preference": "최소 500자. ## 선호 포맷(숏폼/롱폼) / ## 선호 주제·톤 / ## 바이럴 유발 패턴 / ## 기피 요소 포함.",
  "platform_patterns":         "최소 500자. ## 유튜브 알고리즘 최적화 / ## 숏폼 플랫폼 패턴 / ## 최적 업로드 전략 / ## 공공기관 채널 성장 패턴 포함.",
  "competitor_patterns":       "최소 500자. ## 주요 낙찰 업체 분석 / ## 낙찰 업체 제안 전략 / ## 낙찰가 분석 / ## 우리의 경쟁 우위 전략 포함."
}}"""


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

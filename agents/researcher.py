# agents/researcher.py
# STEP 1: 발주처 리서치 에이전트
#
# [검색 전략]
#   SerpAPI (google.co.kr, hl=ko, gl=kr) — 한국 정보 6개 쿼리
#     ① 발주처 정책/사업 계획  → agency_policy
#     ② 기관장 메시지/현안     → leadership_message
#     ③ 기존 홍보 콘텐츠       → existing_content
#     ④ 공공영상 우수사례       → best_cases
#     ⑤ 시장 단가/입찰         → market_pricing
#     ⑥ 관련 정책/법령 변화    → policy_changes
#   Tavily — 글로벌 트렌드 2개 쿼리
#     ⑦ 유튜브 알고리즘        → algorithm_trends
#     ⑧ 공공 콘텐츠 트렌드     → content_trends / platform_patterns
#   우선순위: SerpAPI → Tavily → Claude 지식
#
# 출력: 12개 항목, 각 최소 500자

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

def run(dna: ConceptDNA) -> dict:
    """발주처 리서치 실행.

    Returns:
        12개 항목 dict:
        발주처: agency_policy, leadership_message, existing_content
        시장:   best_cases, content_trends, competitor_patterns
        타겟:   target_media_habits, target_content_preference, platform_patterns
        환경:   policy_changes, algorithm_trends, market_pricing
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

    # ── Claude 분석 (Haiku 모델, 고속) ──────────
    print("  Claude 12개 항목 분석 중...")
    result = _analyze(dna, profile, past_cases, serp_results, tavily_results, learning_cases)

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
        ("cases",    f"공공기관 홍보영상 우수사례 2024 2025"),
        ("pricing",  f"공공기관 영상 제작 입찰 단가 {agency_type} 2025"),
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
    "agency_policy":              "",
    "leadership_message":         "",
    "existing_content":           "",
    "best_cases":                 "",
    "content_trends":             "",
    "competitor_patterns":        "",
    "target_media_habits":        "",
    "target_content_preference":  "",
    "platform_patterns":          "",
    "policy_changes":             "",
    "algorithm_trends":           "",
    "market_pricing":             "",
}


def _analyze(dna: ConceptDNA, profile: dict, past_cases: list,
             serp_results: dict[str, list], tavily_results: list[dict],
             learning_cases: list = None) -> dict:
    """Claude Sonnet으로 12개 항목 분석 (고품질). 실패 시 빈 defaults 반환."""
    prompt = _build_prompt(dna, profile, past_cases, serp_results, tavily_results, learning_cases or [])

    try:
        result = claude_client.call_json(prompt, model=_SONNET_MODEL,
                                         max_tokens=8000, max_retries=2)
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
    serp_cases   = _fmt_serp_block(s.get("cases",   []))
    serp_pricing = _fmt_serp_block(s.get("pricing", []))
    serp_policy  = _fmt_serp_block(s.get("policy",  []))

    # Tavily 블록 — 글로벌 트렌드
    tavily_block = _fmt_tavily_block(tavily_results)

    return f"""당신은 대한민국 정부 입찰 전략 전문가이자 공공 홍보 분야 수석 리서처입니다.
아래 [SerpAPI 한국 검색 결과]와 [Tavily 글로벌 트렌드]를 최우선 근거로 활용하고,
검색 결과가 부족한 항목은 Claude 학습 지식으로 보완하여
12개 항목을 JSON으로 작성하십시오.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【절대 원칙】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【품질 기준 — 최우선 준수】
각 항목은 최소 500자 이상 작성한다. 출처와 수치 데이터를 반드시 포함하라.
소제목(## 형식)으로 반드시 구분하여 작성. 절대 요약하지 말 것.
짧은 답변은 품질 기준 미달로 재작성된다. 내용 없는 빈 항목은 절대 금지.

1. 모든 텍스트 필드는 각각 최소 500자 이상 작성한다.
2. 수치(예산액, 조회수, 비율, %, 명 수)와 구체적 사례를 반드시 포함한다.
3. "~로 알려져 있다", "~할 것으로 예상된다" 같은 모호한 표현은 절대 금지.
4. 검색 결과에 날짜·수치가 있으면 우선 인용. 출처 URL 명시.
5. 【수치 의무】 모든 주장과 분석에는 반드시 구체적인 수치 데이터를 포함해야 한다.
   '증가했다' (X) → '2024년 대비 23% 증가했다' (O)
   '낮다' (X) → '전체의 7%에 불과하다' (O)
   수치 없는 문장은 작성하지 마라.
6. 【출처 의무】 모든 통계·수치·사실에는 반드시 출처를 표기해야 한다.
   형식: (출처명, 발행연도)
   예: '조회수 300만 회를 기록했다 (아리랑TV 공식 발표, 2024)'
   예: '국내 숏폼 소비율 65% (YouTube Creator Insider, 2024)'
   출처 불명확한 수치는 '추정치' 또는 '자체 분석'으로 명시.
   출처 없는 수치는 절대 제시하지 마라.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[SerpAPI — 한국 검색 결과 (google.co.kr)]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

▶ 발주처 정책/사업 계획 (→ agency_policy 근거)
{serp_agency}

▶ 기관장 메시지/현안 (→ leadership_message 근거)
{serp_leader}

▶ 기존 홍보 콘텐츠 (→ existing_content 근거)
{serp_content}

▶ 공공영상 우수사례 (→ best_cases 근거)
{serp_cases}

▶ 시장 단가/입찰 (→ market_pricing 근거)
{serp_pricing}

▶ 관련 정책/법령 변화 (→ policy_changes 근거)
{serp_policy}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[Tavily — 글로벌 트렌드 (→ algorithm_trends, content_trends, platform_patterns 근거)]
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
[12개 항목 작성 지침]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【발주처 분석】 ← SerpAPI 한국 검색 결과 우선 활용
① agency_policy (최소 500자)
   기관의 법정 미션·비전, 조직 규모(직원 수·예산·산하기관), 최근 2년 핵심 정책·주요 사업,
   기관 특유의 홍보 성향(보수적/혁신적, 선호 포맷), 국민 인지도·신뢰도 현황(통계 포함).

② leadership_message (최소 500자)
   기관장 성명·취임 시기, 신년사·공식 발언에서 반복되는 핵심 키워드,
   올해 기관의 최우선 과제 3가지, 예산 배분 우선순위, 대외 이미지 전략 방향.

③ existing_content (최소 500자)
   이 기관이 최근 2년간 제작한 홍보 콘텐츠 현황(유튜브 구독자·평균 조회수),
   콘텐츠 형식 분포(롱폼/숏폼/카드뉴스 비율), 주요 성공작과 실패작 사례,
   현재 콘텐츠의 문제점과 개선 여지.

【과업 시장 분석】 ← SerpAPI 한국 검색 결과 우선 활용
④ best_cases (최소 500자)
   최근 2년(2024-2025) 국내외 공공기관 홍보영상 우수사례 4개 이상.
   각 사례: 기관명, 영상/캠페인명, 조회수·공유수 등 수치, 핵심 전략, 우리 제안서 적용 포인트.

⑤ content_trends (최소 500자)
   동일 분야 콘텐츠 트렌드. Tavily 글로벌 트렌드 + SerpAPI 국내 사례 통합.
   숏폼 vs 롱폼 비율 변화, 리얼 다큐·1인칭 내러티브·인터랙티브 포맷 채택 현황,
   플랫폼별 공공 콘텐츠 인게이지먼트 변화(수치 포함).

⑥ competitor_patterns (최소 500자)
   공공기관 영상 입찰에서 반복 수주하는 업체들의 제안 패턴.
   제안서에서 자주 쓰는 차별화 포인트, 심사위원이 높이 평가하는 요소,
   최근 낙찰 사례의 공통 전략, 우리가 피해야 할 클리셰.

【타겟 오디언스 분석】 ← Claude 지식 활용
⑦ target_media_habits (최소 500자)
   주요 타겟층(연령·직업·관심사) 미디어 소비 행태.
   플랫폼별 일평균 이용 시간, 공공기관 콘텐츠 소비 계층 특성(통계 기반),
   TV vs 디지털 접촉 빈도, 광고 회피율·완주율 데이터.

⑧ target_content_preference (최소 500자)
   타겟층이 높은 반응을 보이는 콘텐츠 형식·주제·톤앤매너.
   공공 콘텐츠 중 바이럴된 사례 분석, 감성적/정보전달형/엔터테인먼트형 선호도,
   클릭을 유발하는 썸네일·제목 패턴(실제 사례 포함).

⑨ platform_patterns (최소 500자)
   Tavily 글로벌 트렌드 기반으로 유튜브·인스타그램·틱톡·페이스북·네이버 블로그
   각 플랫폼별 공공 콘텐츠 인기 패턴, 최적 영상 길이, 게시 시간대, 해시태그 전략,
   알고리즘이 밀어주는 콘텐츠 특성(2024-2025 기준 수치 포함).

【환경 분석】
⑩ policy_changes (최소 500자) ← SerpAPI 정책 검색 결과 우선 활용
   이 과업과 직접 관련된 정책·법령·행정 규칙 최신 변화(2024-2025).
   시행령·고시·가이드라인 개정 사항, 예산 편성 지침 변화,
   이 기관 또는 동일 분야에 영향을 주는 규제 환경.

⑪ algorithm_trends (최소 500자) ← Tavily 글로벌 트렌드 우선 활용
   유튜브·인스타그램·틱톡 알고리즘 2024-2025 최신 변화.
   공공기관 계정에 불리하게 작용하는 요소, 조회수 늘리기 위한 최적 전략,
   숏폼 vs 롱폼 알고리즘 차이, 실제 수치 기반 인사이트.

⑫ market_pricing (최소 500자) ← SerpAPI 단가 검색 결과 우선 활용
   이 과업 예산 규모({dna.budget or "미정"}) 대비 시장 단가 현황.
   유사 규모 공공기관 영상 제작비 평균, 편당 단가 범위(최저~최고),
   예산 내 품질 극대화 전략, 외주 vs 직제작 비용 구조.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[텍스트 필드 형식 규칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
모든 문자열 필드는 아래 마크다운 서식으로 작성하십시오.
• ## 소제목  — 섹션 구분 (예: ## 핵심 현황)
• ### 소제목 — 세부 소제목 (예: ### 주요 수치)
• **키워드** — 핵심 개념·용어 강조
• 수치·통계 — 별도 줄에 작성
• 섹션 사이 — 빈 줄 하나

예시:
## 핵심 현황

기관은 **위험기상 대응**을 핵심 과제로 설정하고 있다.

### 주요 수치
- 2024년 예산 전년 대비 15% 증가 (기상청, 2024)
- 국민 신뢰도 67% (한국갤럽, 2024)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[출력 형식 — 순수 JSON만, 다른 텍스트 절대 금지]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{{
  "agency_policy":             "최소 500자. 미션·예산·조직 규모·홍보 성향 포함.",
  "leadership_message":        "최소 500자. 기관장 발언·키워드·올해 과제 포함.",
  "existing_content":          "최소 500자. 유튜브 현황·성공/실패 사례·문제점 포함.",
  "best_cases":                "최소 500자. 4개 이상 사례·수치·적용 포인트 포함.",
  "content_trends":            "최소 500자. 트렌드·포맷 변화·수치 포함.",
  "competitor_patterns":       "최소 500자. 낙찰 패턴·심사 기준·클리셰 포함.",
  "target_media_habits":       "최소 500자. 플랫폼별 소비 시간·완주율 포함.",
  "target_content_preference": "최소 500자. 반응 형식·바이럴 사례·썸네일 패턴 포함.",
  "platform_patterns":         "최소 500자. 플랫폼별 최적 길이·게시 시간·수치 포함.",
  "policy_changes":            "최소 500자. 관련 법령·고시·예산 지침 변화 포함.",
  "algorithm_trends":          "최소 500자. 2024-2025 알고리즘 변화·수치 포함.",
  "market_pricing":            "최소 500자. 편당 단가 범위·예산 전략·비용 구조 포함."
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

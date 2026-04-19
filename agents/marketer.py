# agents/marketer.py
# STEP 9: 플랫폼 운영전략 / STEP 10: 마케팅·홍보 전략 에이전트
# 역할: 완성된 영상의 채널별 유통·확산 전략 수립
#
# 출력:
#   1. 유튜브 SEO (제목공식·태그·설명글·썸네일·업로드시간)
#   2. 숏폼 플랫폼 (Shorts/Reels/TikTok 플랫폼별 톤 조정)
#   3. SNS 채널별 운영 (인스타/페이스북/X/블로그 주기·포맷)
#   4. 인플루언서/미디어 협업 방향
#   5. KPI 지표 + 월별 목표치 (RFP 요구 KPI 자동 반영)
#   6. 성과 측정·보고 체계
#   7. 마케팅 예산 배분 (광고비/운영비/제작비)
#
# 핵심 원칙:
#   - 기관 유형과 타겟 오디언스에 맞는 플랫폼 자동 선택
#   - RFP evaluation_keywords에서 KPI 힌트 자동 추출
#   - 롱폼·숏폼 보유 여부에 따라 재편집 계획 자동 포함

import re
import concurrent.futures as _cf

from core import claude_client
from core.dna import ConceptDNA, update_dna, dna_to_context_string
from database.db import save_marketing, save_platform

_SONNET_MODEL = "claude-sonnet-4-6"


# ─────────────────────────────────────────────
# 플랫폼 적합성 매트릭스
# ─────────────────────────────────────────────

# 기관 유형별 권장 플랫폼 (우선순위 순)
_PLATFORM_MAP: dict[str, list[str]] = {
    "중앙부처":  ["유튜브", "인스타그램", "페이스북", "블로그", "X(트위터)"],
    "지자체":   ["유튜브", "인스타그램", "페이스북", "카카오채널", "블로그"],
    "의회":     ["유튜브", "페이스북", "X(트위터)", "블로그", "인스타그램"],
    "공공기관": ["유튜브", "인스타그램", "블로그", "페이스북", "X(트위터)"],
    "기타":     ["유튜브", "인스타그램", "페이스북", "블로그"],
}

# KPI 키워드 → 지표명 매핑
_KPI_KEYWORD_MAP: dict[str, str] = {
    "조회수":    "총 조회수 (누적)",
    "도달":      "도달 인원수 (Reach)",
    "구독":      "구독자 증가수",
    "참여":      "참여율 (Engagement Rate)",
    "공유":      "공유·바이럴 수",
    "댓글":      "댓글·반응 수",
    "인지도":    "브랜드 인지도 향상율",
    "홍보효과":  "홍보 효과 지수",
    "노출":      "노출(Impression) 수",
    "클릭":      "클릭률 (CTR)",
    "시청":      "평균 시청 지속 시간",
    "완주율":    "영상 완주율",
}

# 마케팅 예산 배분 기준 (총 예산 대비 마케팅 예산 권장 비율)
_MARKETING_BUDGET_RATIO = 0.20   # 전체 예산의 20%를 마케팅에 권장

_MARKETING_BREAKDOWN = [
    ("디지털 광고비 (유튜브·메타 등)",  0.45),
    ("SNS 채널 운영비",                  0.20),
    ("인플루언서 협업비",                0.15),
    ("콘텐츠 재편집비 (숏폼 등)",        0.12),
    ("모니터링·분석 도구",               0.08),
]


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────

_FUTURE_TIMEOUT = 80   # 섹션 1개 최대 대기 시간 (초)


def run(dna: ConceptDNA, progress_fn=None) -> dict:
    """유통/마케팅 전략 수립."""

    def _progress(msg: str):
        if progress_fn:
            try:
                progress_fn({"type": "step_progress", "step": "marketing", "message": msg})
            except Exception:
                pass

    platforms     = _select_platforms(dna)
    rfp_kpis      = _extract_rfp_kpi(dna)
    edit_versions = _build_edit_versions(dna)
    mkt_budget    = _calc_marketing_budget(dna)

    print(f"  선정 플랫폼: {', '.join(platforms)}")
    if rfp_kpis:
        print(f"  RFP KPI 힌트: {', '.join(rfp_kpis)}")

    # DNA 컨텍스트 한 번만 생성 — 4개 섹션이 공유
    dna_ctx = _compact_dna_ctx(dna)
    script_summary = _summarize_scripts(dna)

    print("  유튜브·SNS·KPI 전략 텍스트 병렬 생성 중... (핵심 3개 섹션)")
    _progress(f"마케팅 전략 생성 중... ({', '.join(platforms[:3])})")

    youtube_text = sns_text = influencer_text = kpi_text = ""

    with _cf.ThreadPoolExecutor(max_workers=3) as ex:
        f1 = ex.submit(_gen_youtube_strategy_text, dna_ctx, platforms, edit_versions, script_summary)
        f2 = ex.submit(_gen_sns_strategy_text,     dna_ctx, platforms)
        f3 = ex.submit(_gen_kpi_targets_text,      dna_ctx, platforms, rfp_kpis, mkt_budget)
        # 인플루언서 전략은 선택 실행 (기본 제외 — 속도 우선)

        for label, future in [
            ("youtube_strategy", f1),
            ("sns_strategy",     f2),
            ("kpi_targets",      f3),
        ]:
            try:
                val = future.result(timeout=_FUTURE_TIMEOUT)
                if label == "youtube_strategy":
                    youtube_text = val
                elif label == "sns_strategy":
                    sns_text = val
                elif label == "kpi_targets":
                    kpi_text = val
                _progress(f"{label} 완료")
                print(f"  {label}: {len(val)}자")
            except _cf.TimeoutError:
                print(f"  [타임아웃] {label} {_FUTURE_TIMEOUT}초 초과 — 빈 값으로 대체")
                future.cancel()
            except Exception as e:
                print(f"  [오류] {label} 생성 실패 ({type(e).__name__}): {e}")

    result = {
        "platforms":           platforms,
        "edit_versions":       edit_versions,
        "marketing_budget":    mkt_budget,
        "youtube_strategy":    youtube_text,
        "sns_strategy":        sns_text,
        "influencer_strategy": influencer_text,
        "kpi_targets":         kpi_text,
        "reporting_system":    "",
    }

    update_dna(dna, {
        "distribution_channels":  platforms,
        "youtube_strategy":       youtube_text,
        "sns_strategy":           sns_text,
        "influencer_strategy":    influencer_text,
        "kpi_targets":            [kpi_text] if kpi_text else [],
        "reporting_system":       "",
        "marketing_budget":       mkt_budget,
        "distribution_strategy":  youtube_text[:200] if youtube_text else "",
    })

    try:
        save_marketing(dna.client_name, dna.project_name, result,
                       case_id=getattr(dna, "case_id", 0) or 0)
        print("  마케팅 전략 DB 저장 완료")
    except Exception as e:
        print(f"  [경고] DB 저장 실패 (계속 진행): {e}")

    return result


# ─────────────────────────────────────────────
# 플랫폼 선정
# ─────────────────────────────────────────────

def _select_platforms(dna: ConceptDNA) -> list[str]:
    """기관 유형·타겟·숏폼 보유 여부 기반으로 운영 플랫폼 자동 선정.

    Args:
        dna: 현재 ConceptDNA

    Returns:
        플랫폼명 목록 (우선순위 순)
    """
    base = list(_PLATFORM_MAP.get(dna.agency_type, _PLATFORM_MAP["기타"]))

    # 숏폼 보유 시 TikTok 추가 고려 (젊은 타겟 키워드 있을 때)
    target = (dna.agency_characteristics + " ".join(dna.recent_issues
                                                      and [str(i) for i in dna.recent_issues]
                                                      or [])).lower()
    youth_signals = ["청년", "청소년", "10대", "20대", "대학", "학생"]
    if dna.has_shortform and any(sig in target for sig in youth_signals):
        if "TikTok" not in base:
            base.append("TikTok")

    return base


# ─────────────────────────────────────────────
# RFP KPI 힌트 추출
# ─────────────────────────────────────────────

def _extract_rfp_kpi(dna: ConceptDNA) -> list[str]:
    """evaluation_keywords + rfp_requirements에서 KPI 관련 항목 추출.

    Args:
        dna: 현재 ConceptDNA

    Returns:
        RFP가 명시·암시한 KPI 지표명 목록
    """
    sources = dna.evaluation_keywords + [
        str(r) for r in dna.rfp_requirements
    ]
    combined = " ".join(sources).lower()

    found = []
    for keyword, metric in _KPI_KEYWORD_MAP.items():
        if keyword in combined and metric not in found:
            found.append(metric)

    # 예산·일정 항목은 KPI가 아니므로 제거
    return found[:8]


# ─────────────────────────────────────────────
# 재편집 버전 목록
# ─────────────────────────────────────────────

def _build_edit_versions(dna: ConceptDNA) -> list[dict]:
    """보유 영상(롱폼/숏폼) 기반으로 재편집 버전 계획 생성.

    Args:
        dna: 현재 ConceptDNA

    Returns:
        [{"version": str, "duration": str, "platform": str, "purpose": str}]
    """
    versions = []
    qty = max(dna.quantity, 1)

    if not dna.has_shortform:
        # 롱폼만 있으면 숏폼 파생 버전 추가
        versions.extend([
            {"version": "숏폼 60초",  "duration": "60초",  "platform": "유튜브 Shorts / 인스타 Reels",
             "purpose": "롱폼 핵심 요약, 신규 유입"},
            {"version": "숏폼 30초",  "duration": "30초",  "platform": "인스타그램 / TikTok",
             "purpose": "후킹 클립, 바이럴 확산"},
            {"version": "숏폼 15초",  "duration": "15초",  "platform": "인스타 스토리 / 광고",
             "purpose": "광고 소재, 브랜드 리마인더"},
        ])
    else:
        # 숏폼이 있으면 롱폼 파생
        versions.append(
            {"version": "풀버전",     "duration": dna.duration, "platform": "유튜브 메인",
             "purpose": "정식 공개, 채널 콘텐츠"}
        )

    # 편수가 여럿이면 편집본 예고 클립 추가
    if qty >= 2:
        versions.append(
            {"version": f"{qty}편 하이라이트 편집",
             "duration": "2~3분",
             "platform": "유튜브 / SNS",
             "purpose": "시리즈 총정리, 재공유 유도"}
        )

    # 자막 없는 B-roll 무음 버전 (인스타 릴스 등 자동재생 환경)
    versions.append(
        {"version": "무음 캡션 최적화버전", "duration": dna.duration,
         "platform": "인스타그램 / 페이스북",
         "purpose": "자동재생 환경 최적화, 무음 시청자 커버"}
    )

    return versions


# ─────────────────────────────────────────────
# 마케팅 예산 계산
# ─────────────────────────────────────────────

def _calc_marketing_budget(dna: ConceptDNA) -> dict:
    """제작 예산 기반으로 마케팅 예산 배분 계산.

    Args:
        dna: 현재 ConceptDNA

    Returns:
        {"total_ratio": str, "recommended_amount": str, "breakdown": [...]}
    """
    from agents.planner import _parse_budget_won, _format_won

    total_won = _parse_budget_won(dna.budget)
    if total_won:
        mkt_won  = int(total_won * _MARKETING_BUDGET_RATIO)
        mkt_str  = _format_won(mkt_won)
        total_label = dna.budget
    else:
        mkt_won  = None
        mkt_str  = "협의"
        total_label = "협의"

    breakdown = []
    for category, ratio in _MARKETING_BREAKDOWN:
        if mkt_won:
            amount = _format_won(int(mkt_won * ratio))
        else:
            amount = "협의"
        breakdown.append({
            "category": category,
            "ratio":    f"{int(ratio * 100)}%",
            "amount":   amount,
        })

    return {
        "production_budget":   total_label,
        "total_ratio":         f"제작비의 {int(_MARKETING_BUDGET_RATIO * 100)}% 권장",
        "recommended_amount":  mkt_str,
        "breakdown":           breakdown,
    }


# ─────────────────────────────────────────────
# 텍스트 전략 생성 함수 (JSON 파싱 없음)
# ─────────────────────────────────────────────

def _compact_dna_ctx(dna: ConceptDNA) -> str:
    """마케팅 프롬프트용 간결 컨텍스트 (dna_to_context_string 대비 절반 이하 분량)."""
    lines = [
        f"발주처: {dna.client_name}",
        f"사업명: {dna.project_name}",
        f"기관유형: {dna.agency_type or '공공기관'}",
        f"영상종류: {dna.video_type}",
        f"수량/길이: {dna.quantity}편 / {dna.duration}",
        f"예산: {dna.budget or '미지정'}",
        f"기관특성: {(dna.agency_characteristics or '')[:200]}",
    ]
    if dna.concept:
        lines.append(f"핵심컨셉: {dna.concept}")
    if dna.slogan:
        lines.append(f"슬로건: {dna.slogan}")
    if dna.evaluation_keywords:
        lines.append(f"평가키워드: {', '.join(dna.evaluation_keywords[:8])}")
    if dna.core_tasks:
        lines.append(f"핵심과업: {', '.join(str(t) for t in dna.core_tasks[:5])}")
    return "\n".join(lines)


def _gen_platform_ops_text(
    dna_ctx: str,
    platforms: list[str],
    edit_versions: list[dict],
    script_summary: str,
) -> str:
    """【플랫폼 운영전략】 — 채널 운영·배포·알고리즘 전략만 작성. 광고/홍보 금지."""
    edit_block = ", ".join(
        f"{v['version']}({v['duration']})"
        for v in edit_versions[:3]
    )
    prompt = (
        f"【플랫폼 운영전략】\n"
        f"아래 내용만 작성. 마케팅/광고/홍보 내용 절대 포함 금지.\n\n"
        f"[프로젝트]\n{dna_ctx}\n"
        f"[대본요약] {script_summary}\n"
        f"[운영플랫폼] {', '.join(platforms[:5])}\n"
        f"[재편집버전] {edit_block}\n\n"
        f"다음 5개 항목을 마크다운으로 작성하라 (각 3~5줄):\n"
        f"## 유튜브 채널 운영 계획 (업로드 주기, 제목/썸네일 전략, SEO 키워드)\n"
        f"## 각 SNS 플랫폼별 운영 방식 (인스타그램/페이스북/TikTok/블로그)\n"
        f"## 플랫폼별 콘텐츠 포맷 최적화 (세로형/가로형, 길이, 자막)\n"
        f"## 알고리즘 활용 전략 (해시태그, 게시 시간, 인게이지먼트)\n"
        f"## 콘텐츠 배포 캘린더 및 운영 일정\n\n"
        f"핵심: 만든 콘텐츠를 플랫폼에서 지속 운영하는 방법"
    )
    text = claude_client.call(prompt, model=_SONNET_MODEL, max_tokens=1200)
    return text.strip()


def _gen_marketing_promo_text(
    dna_ctx: str,
    platforms: list[str],
    rfp_kpis: list[str],
    mkt_budget: dict,
) -> str:
    """【마케팅/홍보 전략】 — 광고·바이럴·인플루언서·KPI만 작성. 플랫폼 운영 금지."""
    rfp_kpi_block = ", ".join(rfp_kpis) if rfp_kpis else "별도 미명시"
    campaign_months = _estimate_campaign_months_from_ctx(mkt_budget)
    prompt = (
        f"【마케팅/홍보 전략】\n"
        f"아래 내용만 작성. 플랫폼 운영/채널 관리 내용 절대 포함 금지.\n\n"
        f"[프로젝트]\n{dna_ctx}\n"
        f"[RFP KPI] {rfp_kpi_block}\n"
        f"[운영플랫폼] {', '.join(platforms[:4])}\n"
        f"[마케팅예산] 권장 {mkt_budget['recommended_amount']} ({mkt_budget['total_ratio']})\n\n"
        f"다음 6개 항목을 마크다운으로 작성하라 (각 3~5줄):\n"
        f"## 유료 광고 집행 계획 (유튜브 광고, SNS 광고, 타겟팅 전략)\n"
        f"## 바이럴 마케팅 전략 (공유 유도, 챌린지, 이벤트)\n"
        f"## 인플루언서/협업 채널 활용 계획\n"
        f"## 보도자료/언론 홍보 방안\n"
        f"## KPI 목표 수치 (조회수, 구독자, 참여율, 전환율) — {campaign_months}개월 기준\n"
        f"## 성과 측정 및 보고 방법\n\n"
        f"핵심: 콘텐츠와 채널을 외부에 알리고 확산시키는 방법"
    )
    text = claude_client.call(prompt, model=_SONNET_MODEL, max_tokens=1200)
    return text.strip()


def _gen_youtube_strategy_text(
    dna_ctx: str,
    platforms: list[str],
    edit_versions: list[dict],
    script_summary: str,
) -> str:
    """레거시 — _gen_platform_ops_text 로 대체."""
    return _gen_platform_ops_text(dna_ctx, platforms, edit_versions, script_summary)


def _gen_sns_strategy_text(dna_ctx: str, platforms: list[str]) -> str:
    """레거시 — _gen_platform_ops_text 로 대체."""
    return ""


def _gen_influencer_strategy_text(dna_ctx: str, platforms: list[str]) -> str:
    """레거시 — _gen_marketing_promo_text 로 대체."""
    return ""


def _gen_kpi_targets_text(
    dna_ctx: str,
    platforms: list[str],
    rfp_kpis: list[str],
    mkt_budget: dict,
) -> str:
    """KPI 목표 + 성과 보고 체계."""
    rfp_kpi_block = (
        ", ".join(rfp_kpis) if rfp_kpis else "별도 미명시"
    )
    campaign_months = _estimate_campaign_months_from_ctx(mkt_budget)

    prompt = (
        f"공공기관 캠페인 KPI 목표와 성과 보고 체계를 작성하라.\n\n"
        f"[프로젝트]\n{dna_ctx}\n"
        f"[RFP KPI] {rfp_kpi_block}\n"
        f"[운영플랫폼] {', '.join(platforms[:4])}\n"
        f"[마케팅예산] 권장 {mkt_budget['recommended_amount']} ({mkt_budget['total_ratio']})\n\n"
        f"다음 3개 항목을 마크다운으로 작성하라 (각 3~5줄):\n"
        f"## 핵심 KPI 지표 (4~5개, 측정방법 포함)\n"
        f"## {campaign_months}개월 목표치 (런칭/중간/최종)\n"
        f"## 성과 보고 체계 (주간·월간·최종)\n"
    )
    text = claude_client.call(prompt, model=_SONNET_MODEL, max_tokens=900)
    return text.strip()


def _estimate_campaign_months_from_ctx(mkt_budget: dict) -> int:
    """마케팅 예산 dict에서 캠페인 개월 수 추정 (기본 3)."""
    return 3


# ─────────────────────────────────────────────
# Claude 호출 – PART 1 (레거시 JSON 방식, 미사용)
# ─────────────────────────────────────────────

def _generate_distribution_strategy(
    dna: ConceptDNA,
    platforms: list[str],
    edit_versions: list[dict],
) -> dict:
    """레거시: _gen_youtube_strategy_text 로 대체됨."""
    return {}


def _generate_sns_strategy(dna: ConceptDNA, platforms: list[str]) -> dict:
    """SNS 채널별 운영 계획 전용 호출."""
    dna_ctx = dna_to_context_string(dna)
    platform_block = ", ".join(platforms)

    prompt = f"""공공 캠페인 SNS 채널 운영 계획을 수립하라.

[프로젝트]
{dna_ctx}

[운영 플랫폼]
{platform_block}

[출력 — JSON만, 다른 텍스트 금지]
{{
  "sns_channels": {{
    "instagram": {{
      "posting_frequency": "주 N회 (피드/스토리/릴스 구분)",
      "content_mix": "피드 N% / 스토리 N% / 릴스 N%",
      "posting_example": "실제 피드 포스팅 초안 (이모지+본문+해시태그 20개 이상)",
      "hashtag_strategy": "캠페인·이슈·일반 해시태그 구성"
    }},
    "facebook": {{
      "posting_frequency": "주 N회",
      "content_format": "영상/링크/이벤트 비율",
      "posting_example": "실제 포스팅 텍스트 초안 (150~200자)"
    }},
    "x_twitter": {{
      "posting_frequency": "일 N회",
      "posting_example": "실제 X 포스팅 초안 (140자 이내, 해시태그 2~3개)",
      "hashtag_campaign": "캠페인 전용 해시태그 + 확산 전략"
    }},
    "blog": {{
      "posting_frequency": "월 N회",
      "seo_title_example": "블로그 SEO 제목 예시 2개",
      "keyword_focus": "주력 검색 키워드 5개"
    }}
  }}
}}

빈 객체/빈 문자열 금지. 모든 채널에 구체적 내용 포함."""

    result = claude_client.call_json(prompt, model=_SONNET_MODEL, max_tokens=2000)
    if not result.get("sns_channels"):
        print("  [경고] marketer: sns_channels 비어있음!")
    return result


def _build_distribution_prompt(
    dna: ConceptDNA,
    platforms: list[str],
    edit_versions: list[dict],
) -> str:
    dna_ctx = dna_to_context_string(dna)

    platform_block = ", ".join(platforms)

    edit_block = "\n".join(
        f"  - {v['version']} ({v['duration']}) → {v['platform']}: {v['purpose']}"
        for v in edit_versions
    )

    script_summary = _summarize_scripts(dna)
    has_short_note = "숏폼 버전 보유 (15/30/60초)" if dna.has_shortform else "롱폼만 보유 → 숏폼 재편집 필요"

    return f"""당신은 대한민국 공공 캠페인 전문 디지털 마케터입니다.
아래 정보를 바탕으로 영상 유통·마케팅 전략을 실제 집행 가능한 수준으로 수립해주세요.

【절대 원칙】
- 개요나 방향만 제시하는 것은 금지. 실제로 집행할 수 있는 구체적 내용을 작성하라.
- 유튜브 제목 3개는 실제 업로드할 수 있는 완성된 문구로 작성하라.
- SNS 포스팅 예시는 실제 게시할 수 있는 텍스트 초안으로 작성하라.
- 수치가 들어갈 수 있는 모든 곳에 구체적 숫자를 넣어라.
- "효과적인 전략", "적극 활용" 같은 공허한 표현은 금지.
- 【수치 의무】 모든 주장과 분석에는 반드시 구체적인 수치 데이터를 포함해야 한다.
  '증가했다' (X) → '2024년 대비 23% 증가했다' (O) / '낮다' (X) → '전체의 7%에 불과하다' (O)
  수치 없는 문장은 작성하지 마라.
- 【출처 의무】 모든 통계·수치·사실에는 반드시 출처를 표기해야 한다.
  형식: (출처명, 발행연도) — 예: '국내 숏폼 소비율 65% (YouTube Creator Insider, 2024)'
  출처 불명확한 수치는 '추정치' 또는 '자체 분석'으로 명시. 출처 없는 수치는 제시 금지.

━━━━━━━━━━━━━━━━━━━━━━━
[프로젝트 컨텍스트]
━━━━━━━━━━━━━━━━━━━━━━━
{dna_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[대본·기획 요약]
━━━━━━━━━━━━━━━━━━━━━━━
{script_summary}

━━━━━━━━━━━━━━━━━━━━━━━
[운영 플랫폼 (선정됨)]
━━━━━━━━━━━━━━━━━━━━━━━
{platform_block}

━━━━━━━━━━━━━━━━━━━━━━━
[재편집 버전 계획]
━━━━━━━━━━━━━━━━━━━━━━━
영상 포맷: {has_short_note}
{edit_block}

━━━━━━━━━━━━━━━━━━━━━━━
[전략 수립 세부 지침]
━━━━━━━━━━━━━━━━━━━━━━━
① 유튜브 SEO
   - 제목 3개: 각각 다른 공식 사용
     · 감성형: [감정유발 단어] + 핵심키워드 + 공감 상황
     · 정보형: 숫자 + 구체적 정보 + 키워드
     · 행동유도형: 동사(하세요/해보세요/확인하세요) + 혜택
   - 설명글: 실제 업로드할 수 있는 전체 텍스트 초안 (타임스탬프·링크·해시태그 포함)
   - 태그 15개: 광역(3개) → 중간(7개) → 장문(5개) 계층 구조
   - 썸네일: 글자 수·위치·인물 표정·배경색까지 구체적으로

② 인플루언서 전략
   - 구체적 카테고리와 팔로워 규모 명시
   - 협업 포스팅 예시 문구 포함
   - 공공기관 계약 특성(사전 승인 절차 등) 고려


━━━━━━━━━━━━━━━━━━━━━━━
[텍스트 필드 형식 규칙]
━━━━━━━━━━━━━━━━━━━━━━━
모든 문자열 필드는 아래 마크다운 서식으로 작성하십시오.
• ## 소제목  — 섹션 구분 (예: ## 핵심 현황)
• ### 소제목 — 세부 소제목 (예: ### 주요 수치)
• **키워드** — 핵심 개념·용어 강조
• 수치·통계 — 별도 줄에 작성
• 섹션 사이 — 빈 줄 하나

예시:
## 현황 진단

발주처는 **디지털 전환**을 핵심 과제로 설정하고 있다.

### 주요 수치
- 2024년 홍보 예산 전년 대비 15% 증가 (기관 발표, 2024)
- 국민 신뢰도 67% (한국갤럽, 2024)

━━━━━━━━━━━━━━━━━━━━━━━
[출력 형식 — JSON만 출력, 다른 텍스트 절대 금지]
━━━━━━━━━━━━━━━━━━━━━━━

{{
  "youtube_seo": {{
    "title_formula": "감성형/정보형/행동유도형 공식 설명 (각 공식의 구조를 명시)",
    "titles": [
      "실제 업로드할 유튜브 제목 1 (감성형, 30자 내외)",
      "실제 업로드할 유튜브 제목 2 (정보형, 숫자 포함, 30자 내외)",
      "실제 업로드할 유튜브 제목 3 (행동유도형, 동사 시작, 30자 내외)"
    ],
    "description_template": "실제 업로드할 설명글 초안. 핵심 키워드 포함. 타임스탬프. 해시태그 10개. (200자 이상)",
    "tags": ["태그1", "태그2", "태그3", "태그4", "태그5", "태그6", "태그7", "태그8", "태그9", "태그10"],
    "thumbnail_direction": "글자·인물·배경 구성 방향 (디자이너에게 전달할 수준)",
    "upload_time": "최적 업로드 요일·시간대 + 근거"
  }},

  "influencer_strategy": {{
    "direction": "공공기관 특성(사전 승인·중립성)을 고려한 협업 방향 (3문장 이상)",
    "types": [
      {{
        "category": "구체적 인플루언서 카테고리",
        "scale": "나노/마이크로/매크로 중 선택",
        "count": "N명",
        "collaboration_format": "공동제작/게재의뢰/리뷰 중 선택 + 방식",
        "rationale": "이 카테고리 선택 이유"
      }}
    ],
    "media_pr": "보도자료 배포 매체 + 배포 시점 + 주요 앵글"
  }},

  "campaign_linkage": "오프라인 행사·타 매체(TV/라디오/옥외광고)·타부서 사업과의 연계 방안. 구체적 접점과 시너지 효과. (3문장 이상)",

  "shortform_strategy": {{
    "youtube_shorts": {{
      "tone": "채널 타겟에 맞는 톤 설명",
      "hook_rule": "첫 2초 훅 예시 문구 2개",
      "posting_frequency": "주 N회 (요일 명시)",
      "optimization_tips": ["팁1", "팁2", "팁3"]
    }},
    "instagram_reels": {{
      "tone": "릴스 톤 (유튜브와 차이점)",
      "music_direction": "장르·BPM 방향",
      "posting_frequency": "주 N회",
      "posting_example": "실제 포스팅 텍스트 초안 (이모지+본문+해시태그)"
    }},
    "tiktok": {{
      "tone": "해당 없음 또는 구체적 전략",
      "trend_linkage": "트렌드 연계 방안 또는 해당 없음 이유",
      "posting_frequency": "해당 없음 또는 주 N회"
    }}
  }}
}}

위 JSON만 출력하라. influencer_strategy와 campaign_linkage는 반드시 채울 것.
빈 객체/빈 문자열 금지. 모든 항목에 구체적 내용 포함."""


# ─────────────────────────────────────────────
# Claude 호출 – PART 2: KPI + 보고체계
# ─────────────────────────────────────────────

def _generate_kpi_strategy(
    dna: ConceptDNA,
    platforms: list[str],
    rfp_kpis: list[str],
    mkt_budget: dict,
) -> dict:
    """KPI 지표 + 월별 목표 + 보고체계 생성."""
    prompt = _build_kpi_prompt(dna, platforms, rfp_kpis, mkt_budget)
    result = claude_client.call_json(prompt, model=_SONNET_MODEL, max_tokens=2000)
    return result


def _build_kpi_prompt(
    dna: ConceptDNA,
    platforms: list[str],
    rfp_kpis: list[str],
    mkt_budget: dict,
) -> str:
    dna_ctx = dna_to_context_string(dna)

    rfp_kpi_block = (
        "\n".join(f"  - {k}" for k in rfp_kpis)
        if rfp_kpis else "  (RFP에서 별도 KPI 미명시)"
    )

    budget_block = "\n".join(
        f"  {b['category']}: {b['ratio']} ({b['amount']})"
        for b in mkt_budget["breakdown"]
    )

    # 운영 기간 추정 (deadline 기반)
    campaign_months = _estimate_campaign_months(dna)

    return f"""당신은 공공 캠페인 전문 퍼포먼스 마케터입니다.
아래 정보를 바탕으로 KPI 지표와 월별 목표, 성과 보고 체계를 수립해주세요.

━━━━━━━━━━━━━━━━━━━━━━━
[프로젝트 컨텍스트]
━━━━━━━━━━━━━━━━━━━━━━━
{dna_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[RFP에서 요구하거나 암시한 KPI — 반드시 반영]
━━━━━━━━━━━━━━━━━━━━━━━
{rfp_kpi_block}

━━━━━━━━━━━━━━━━━━━━━━━
[운영 플랫폼]
━━━━━━━━━━━━━━━━━━━━━━━
{', '.join(platforms)}

━━━━━━━━━━━━━━━━━━━━━━━
[마케팅 예산 배분]
━━━━━━━━━━━━━━━━━━━━━━━
총 권장 마케팅 예산: {mkt_budget['recommended_amount']} ({mkt_budget['total_ratio']})
{budget_block}

━━━━━━━━━━━━━━━━━━━━━━━
[KPI 수립 지침]
━━━━━━━━━━━━━━━━━━━━━━━
- 캠페인 운영 기간: 약 {campaign_months}개월 기준
- primary_kpi: RFP 요구 KPI 우선 포함, 총 4~6개 지표
- monthly_targets: 1개월차(런칭) → 중간 → 최종 3단계 목표 제시
- 목표치는 유사 공공기관 캠페인 벤치마크 기준으로 현실적으로 설정
- reporting_system: 주간·월간·최종 보고서 체계와 대시보드 구성


━━━━━━━━━━━━━━━━━━━━━━━
[텍스트 필드 형식 규칙]
━━━━━━━━━━━━━━━━━━━━━━━
모든 문자열 필드는 아래 마크다운 서식으로 작성하십시오.
• ## 소제목  — 섹션 구분 (예: ## 핵심 현황)
• ### 소제목 — 세부 소제목 (예: ### 주요 수치)
• **키워드** — 핵심 개념·용어 강조
• 수치·통계 — 별도 줄에 작성
• 섹션 사이 — 빈 줄 하나

예시:
## 현황 진단

발주처는 **디지털 전환**을 핵심 과제로 설정하고 있다.

### 주요 수치
- 2024년 홍보 예산 전년 대비 15% 증가 (기관 발표, 2024)
- 국민 신뢰도 67% (한국갤럽, 2024)

━━━━━━━━━━━━━━━━━━━━━━━
[출력 형식]
━━━━━━━━━━━━━━━━━━━━━━━
반드시 아래 JSON으로만 출력하세요.

{{
  "kpi": {{
    "primary_kpi": [
      {{
        "metric":      "지표명",
        "definition":  "측정 방법 설명",
        "tool":        "측정 도구 (유튜브 스튜디오/GA4/인스타 인사이트 등)",
        "rfp_linked":  true
      }}
    ],
    "monthly_targets": [
      {{
        "period":       "1개월차 (런칭)",
        "views":        "누적 조회수 목표",
        "reach":        "도달 인원 목표",
        "engagement":   "참여율 목표 (%)",
        "subscribers":  "구독자 증가 목표 (해당 시)",
        "key_action":   "이 기간 핵심 액션"
      }},
      {{
        "period":       "{campaign_months // 2}개월차 (중간)",
        "views":        "...",
        "reach":        "...",
        "engagement":   "...",
        "subscribers":  "...",
        "key_action":   "..."
      }},
      {{
        "period":       "{campaign_months}개월차 (최종)",
        "views":        "...",
        "reach":        "...",
        "engagement":   "...",
        "subscribers":  "...",
        "key_action":   "최종 성과 정리 및 결과 보고"
      }}
    ]
  }},

  "reporting_system": "주간·월간·최종 보고서 구성과 대시보드 체계 설명 (3~4문장)",

  "marketing_budget": {{
    "production_budget":   "{mkt_budget['production_budget']}",
    "total_ratio":         "{mkt_budget['total_ratio']}",
    "recommended_amount":  "{mkt_budget['recommended_amount']}",
    "breakdown":           "(위 배분 골격 그대로 유지하되 사업 특성에 맞게 조정)",
    "budget_notes": "예산 집행 시 주의사항 또는 유연성 확보 방안"
  }}
}}

위 JSON 구조만 출력하라. kpi.primary_kpi는 4개 이상, monthly_targets는 3개(런칭·중간·최종) 포함.
reporting_system은 주간/월간/최종 보고 체계를 3문장 이상 구체적으로 작성할 것."""


# ─────────────────────────────────────────────
# 유틸리티
# ─────────────────────────────────────────────

def _summarize_scripts(dna: ConceptDNA) -> str:
    """대본 개요를 마케팅 프롬프트용으로 간략 요약."""
    if not dna.script_outline:
        return f"총 {dna.quantity}편 / {dna.duration} / {dna.video_type}"

    lines = [f"총 {len(dna.script_outline)}편"]
    for item in dna.script_outline[:3]:
        ep    = item.get("episode", "")
        title = item.get("title", "")
        fmt   = item.get("format", "")
        lines.append(f"  {ep}편 《{title}》 [{fmt}]")
    return "\n".join(lines)


def _estimate_campaign_months(dna: ConceptDNA) -> int:
    """납품기한 기반 캠페인 운영 기간(개월) 추정. 기본 3개월."""
    if not dna.deadline:
        return 3

    from agents.planner import _parse_deadline
    from datetime import datetime

    end = _parse_deadline(dna.deadline)
    if not end:
        return 3

    months = max(1, round((end - datetime.today()).days / 30))
    return min(months, 12)


# ─────────────────────────────────────────────
# STEP 9: 플랫폼 운영전략 진입점
# ─────────────────────────────────────────────

def run_platform(dna: ConceptDNA, progress_fn=None) -> dict:
    """STEP 9 — 유튜브/SNS 채널 운영 및 배포 전략."""

    def _progress(msg: str):
        if progress_fn:
            try:
                progress_fn({"type": "step_progress", "step": "platform", "message": msg})
            except Exception:
                pass

    platforms     = _select_platforms(dna)
    edit_versions = _build_edit_versions(dna)
    dna_ctx       = _compact_dna_ctx(dna)
    script_summary = _summarize_scripts(dna)

    print(f"  [플랫폼 운영전략] 선정 플랫폼: {', '.join(platforms)}")
    _progress(f"플랫폼 운영전략 수립 중... ({', '.join(platforms[:3])})")

    platform_text = ""
    try:
        platform_text = _gen_platform_ops_text(dna_ctx, platforms, edit_versions, script_summary)
        _progress("플랫폼 운영전략 완료")
        print(f"  platform_ops: {len(platform_text)}자")
    except _cf.TimeoutError:
        print(f"  [타임아웃] platform_ops {_FUTURE_TIMEOUT}초 초과")
    except Exception as e:
        print(f"  [오류] platform_ops: {type(e).__name__}: {e}")

    result = {
        "platforms":        platforms,
        "edit_versions":    edit_versions,
        "youtube_strategy": platform_text,
        "sns_strategy":     "",
    }

    update_dna(dna, {
        "distribution_channels":  platforms,
        "youtube_strategy":       platform_text,
        "sns_strategy":           "",
        "distribution_strategy":  platform_text[:200] if platform_text else "",
    })

    try:
        save_platform(dna.client_name, dna.project_name, result,
                      case_id=getattr(dna, "case_id", 0) or 0)
        print("  플랫폼 운영전략 DB 저장 완료")
    except Exception as e:
        print(f"  [경고] DB 저장 실패 (계속 진행): {e}")

    return result


# ─────────────────────────────────────────────
# STEP 10: 마케팅/홍보 전략 진입점
# ─────────────────────────────────────────────

def run_marketing(dna: ConceptDNA, progress_fn=None) -> dict:
    """STEP 10 — 광고/바이럴/인플루언서/KPI/성과 측정 전략."""

    def _progress(msg: str):
        if progress_fn:
            try:
                progress_fn({"type": "step_progress", "step": "marketing", "message": msg})
            except Exception:
                pass

    platforms  = dna.distribution_channels or _select_platforms(dna)
    rfp_kpis   = _extract_rfp_kpi(dna)
    mkt_budget = _calc_marketing_budget(dna)
    dna_ctx    = _compact_dna_ctx(dna)

    if rfp_kpis:
        print(f"  [마케팅/홍보] RFP KPI 힌트: {', '.join(rfp_kpis)}")
    _progress(f"마케팅·홍보 전략 수립 중...")

    marketing_text = ""
    try:
        marketing_text = _gen_marketing_promo_text(dna_ctx, platforms, rfp_kpis, mkt_budget)
        _progress("마케팅/홍보 전략 완료")
        print(f"  marketing_promo: {len(marketing_text)}자")
    except _cf.TimeoutError:
        print(f"  [타임아웃] marketing_promo {_FUTURE_TIMEOUT}초 초과")
    except Exception as e:
        print(f"  [오류] marketing_promo: {type(e).__name__}: {e}")

    result = {
        "platforms":           platforms,
        "marketing_budget":    mkt_budget,
        "influencer_strategy": marketing_text,
        "kpi_targets":         marketing_text,
        "reporting_system":    "",
    }

    update_dna(dna, {
        "influencer_strategy": marketing_text,
        "kpi_targets":         [marketing_text] if marketing_text else [],
        "marketing_budget":    mkt_budget,
    })

    try:
        save_marketing(dna.client_name, dna.project_name, result,
                       case_id=getattr(dna, "case_id", 0) or 0)
        print("  마케팅/홍보 전략 DB 저장 완료")
    except Exception as e:
        print(f"  [경고] DB 저장 실패 (계속 진행): {e}")

    return result

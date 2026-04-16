# agents/orchestrator.py
# STEP 7: 일관성 검수 에이전트 (오케스트레이터)
# 역할: STEP 0~6 전체 산출물을 검수하고 최종 제안서를 확정
#
# 처리 순서:
#   PASS 1 (규칙 기반, 빠름): 컨셉 흐름·평가항목 커버리지·키워드 통합 점수 계산
#   PASS 2 (Claude): 서사 완성도 심층 분석 + 미흡 섹션 자동 보완
#   PASS 3 (Claude): 인터즈 회사소개 RFP 맞춤 재구성
#   PASS 4 (Claude): PT 원고 초안 + 심사위원 예상 질의응답 5개
#   FINAL:           최종 ConceptDNA 확정 + DB 저장

import json
import dataclasses
from dataclasses import asdict

from core import claude_client
from core.dna import ConceptDNA, update_dna, dna_to_context_string
from database.db import save_final_proposal, get_winning_patterns

_OPUS_MODEL = "claude-opus-4-6"   # orchestrator 전용 고성능 모델


# ─────────────────────────────────────────────
# 인터즈 기본 실적 데이터베이스
# (실제 운영 시 DB 또는 별도 파일로 관리 권장)
# ─────────────────────────────────────────────

_INTERZ_ACHIEVEMENTS = [
    {"year": "2024", "client": "행정안전부",     "project": "국민 재난안전 홍보영상 3편 제작",           "keywords": ["안전", "재난", "예방", "홍보", "중앙부처"]},
    {"year": "2024", "client": "환경부",         "project": "탄소중립 2050 캠페인 영상 시리즈 5편",      "keywords": ["환경", "캠페인", "시리즈", "기후", "탄소"]},
    {"year": "2024", "client": "문화체육관광부", "project": "K-컬처 해외 홍보 다큐멘터리 10편",          "keywords": ["문화", "다큐멘터리", "홍보", "창의성", "중앙부처"]},
    {"year": "2023", "client": "교육부",         "project": "미래교육 홍보 숏폼 콘텐츠 20편",            "keywords": ["교육", "숏폼", "콘텐츠", "청소년", "시리즈"]},
    {"year": "2023", "client": "보건복지부",     "project": "건강생활 실천 캠페인 영상 7편",              "keywords": ["건강", "예방", "캠페인", "생활", "공감"]},
    {"year": "2023", "client": "경기도",         "project": "지역 브랜드 다큐멘터리 및 SNS 콘텐츠 제작", "keywords": ["지역", "브랜드", "지자체", "SNS", "공감"]},
    {"year": "2022", "client": "한국관광공사",   "project": "Visit Korea 인바운드 홍보 영상 12편",        "keywords": ["홍보", "창의성", "완성도", "공공기관", "시리즈"]},
    {"year": "2022", "client": "병무청",         "project": "국방 홍보영상 및 유튜브 채널 운영 대행",     "keywords": ["홍보", "유튜브", "채널", "SNS", "시리즈"]},
    {"year": "2022", "client": "서울특별시",     "project": "서울안전 캠페인 멀티포맷 영상 제작",         "keywords": ["안전", "캠페인", "지자체", "숏폼", "도달"]},
    {"year": "2021", "client": "국민권익위원회", "project": "청렴 문화 홍보 영상 시리즈 6편",             "keywords": ["소통", "투명성", "의회", "캠페인", "공감"]},
]

_INTERZ_COMPETENCIES = [
    "정부·공공기관 영상콘텐츠 전문 제작 (연간 30건 이상)",
    "기획-촬영-편집-납품 원스톱 인하우스 시스템",
    "숏폼(Shorts/Reels/TikTok) 특화 전담팀 운영",
    "멀티채널 동시 배포 및 성과 분석 체계 보유",
    "데이터 기반 KPI 관리 및 월별 성과 보고서 제공",
    "수어·자막·화면해설 접근성 제작 100% 준수",
]


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────

def run(dna: ConceptDNA, pipeline_results: dict = None) -> dict:
    """일관성 검수 및 최종 제안서 확정.

    Args:
        dna: STEP 0~6 결과가 모두 반영된 ConceptDNA
        pipeline_results: 각 에이전트 원본 출력물 (없으면 DNA에서 재구성)

    Returns:
        {
            "consistency_score":   float,  # 0.0~1.0
            "evaluation_coverage": dict,   # 평가항목 커버리지
            "issues":              list,   # 발견된 문제·개선사항
            "company_profile":     dict,   # 인터즈 맞춤 회사소개
            "pt_script":           dict,   # PT 원고 초안
            "qa_prep":             list,   # 심사위원 예상 질의응답 5개
            "final_proposal":      dict,   # 최종 확정 제안서 구조
            "dna_snapshot":        dict,   # 최종 DNA 스냅샷
        }
    """
    # ── PASS 1: 규칙 기반 사전 검수 ──────────────
    print("  [PASS 1] 규칙 기반 사전 검수...")
    try:
        pre_checks = _rule_based_checks(dna)
        pre_score  = _calc_pre_score(pre_checks)
        print(f"           사전 검수 점수: {pre_score:.0%}  "
              f"(컨셉흐름 {pre_checks['concept_flow']['score']:.0%} / "
              f"평가항목 {pre_checks['evaluation_coverage']['score']:.0%} / "
              f"키워드통합 {pre_checks['keyword_integration']['score']:.0%})")
    except Exception as e:
        raise RuntimeError(f"PASS 1 (규칙 기반 사전 검수) 실패: {type(e).__name__}: {e}") from e

    # ── PASS 2: Claude 심층 일관성 분석 ──────────
    print("  [PASS 2] 서사 완성도 심층 분석 및 자동 보완...")
    try:
        winning_patterns = get_winning_patterns(limit=5)
        if winning_patterns:
            print(f"           낙찰 패턴 {len(winning_patterns)}건 참조")
        consistency = _deep_consistency_check(dna, pre_checks, winning_patterns)
        final_score = (pre_score * 0.4 + consistency.get("narrative_score", 0.5) * 0.6)
        print(f"           최종 일관성 점수: {final_score:.0%}")
    except Exception as e:
        raise RuntimeError(f"PASS 2 (서사 완성도 심층 분석) 실패: {type(e).__name__}: {e}") from e

    # ── PASS 3: 회사소개 맞춤 생성 ───────────────
    print("  [PASS 3] 인터즈 회사소개 맞춤 재구성...")
    try:
        company_profile = _generate_company_profile(dna)
    except Exception as e:
        raise RuntimeError(f"PASS 3 (회사소개 맞춤 재구성) 실패: {type(e).__name__}: {e}") from e

    # ── PASS 4: PT 원고 + 심사위원 Q&A ───────────
    print("  [PASS 4] PT 원고 초안 및 예상 질의응답 생성...")
    try:
        pt_qa = _generate_pt_and_qa(dna, consistency, company_profile)
    except Exception as e:
        raise RuntimeError(f"PASS 4 (PT 원고·예상 Q&A 생성) 실패: {type(e).__name__}: {e}") from e

    # ── FINAL: 최종 제안서 조립 ──────────────────
    print("  [FINAL] 최종 제안서 조립 및 저장...")
    try:
        final_proposal = _assemble_final_proposal(dna, consistency, company_profile, pt_qa)
    except Exception as e:
        raise RuntimeError(f"FINAL (최종 제안서 조립) 실패: {type(e).__name__}: {e}") from e
    dna_snapshot   = _snapshot_dna(dna)

    result = {
        "consistency_score":   round(final_score, 3),
        "evaluation_coverage": pre_checks["evaluation_coverage"],
        "issues":              consistency.get("issues", []),
        "revised_sections":    consistency.get("revised_sections", {}),
        "company_profile":     company_profile,
        "pt_script":           pt_qa.get("pt_script", {}),
        "qa_prep":             pt_qa.get("qa_prep", []),
        "final_proposal":      final_proposal,
        "dna_snapshot":        dna_snapshot,
    }

    # DNA 업데이트 (검수 통과 상태 기록)
    update_dna(dna, {
        "distribution_strategy": f"검수완료 | 일관성점수 {final_score:.0%} | {dna.project_name}",
    })

    # DB 저장
    try:
        row_id = save_final_proposal(dna.client_name, dna.project_name, result,
                                     case_id=getattr(dna, "case_id", 0) or 0)
        print(f"  최종 제안서 DB 저장 완료 (id={row_id})")
    except Exception as e:
        print(f"  [경고] DB 저장 실패 (계속 진행): {e}")

    return result


# ─────────────────────────────────────────────
# PASS 1: 규칙 기반 사전 검수
# ─────────────────────────────────────────────

def _rule_based_checks(dna: ConceptDNA) -> dict:
    """DNA 필드를 직접 순회하며 3개 영역 점수 계산.

    API 호출 없이 즉시 실행. 각 항목은 0.0~1.0 점수와 issues 리스트 반환.
    """
    return {
        "concept_flow":        _check_concept_flow(dna),
        "evaluation_coverage": _check_evaluation_coverage(dna),
        "keyword_integration": _check_keyword_integration(dna),
    }


def _check_concept_flow(dna: ConceptDNA) -> dict:
    """컨셉·슬로건이 전략→대본→마케팅까지 일관되게 흐르는지 확인.

    Returns:
        {"score": float, "passed": [...], "issues": [...]}
    """
    passed, issues = [], []
    checks = [
        (bool(dna.concept),              "핵심 컨셉 확정",            "STEP 3 크리에이티브 미실행"),
        (bool(dna.slogan),               "슬로건 확정",               "슬로건 미생성"),
        (bool(dna.persuasion_structure), "4단계 설득 구조 완성",       "STEP 2 전략가 미실행"),
        (bool(dna.script_outline),       "대본 개요 완성",             "STEP 5 스크립터 미실행"),
        (bool(dna.kpi_targets),          "KPI 지표 설정",              "STEP 6 마케터 미실행"),
        (bool(dna.tone_keywords),        "톤앤매너 감성 키워드 확정",  "감성 키워드 미생성"),
    ]
    for ok, label, err in checks:
        if ok:
            passed.append(label)
        else:
            issues.append({"severity": "warning", "field": label, "message": err})

    # 슬로건이 대본 어딘가에 반영됐는지 확인
    if dna.slogan and dna.script_outline:
        script_text = json.dumps(dna.script_outline, ensure_ascii=False)
        slogan_words = [w for w in dna.slogan.split() if len(w) > 1]
        if slogan_words and not any(w in script_text for w in slogan_words):
            issues.append({"severity": "info",
                           "field": "슬로건-대본 연계",
                           "message": f"슬로건 '{dna.slogan}'의 핵심 단어가 대본 개요에 반영되지 않음"})
        else:
            passed.append("슬로건-대본 연계")

    score = len(passed) / max(len(checks) + 1, 1)
    return {"score": round(score, 2), "passed": passed, "issues": issues}


def _check_evaluation_coverage(dna: ConceptDNA) -> dict:
    """평가항목이 제안서 산출물 내에 커버됐는지 확인.

    Returns:
        {"score": float, "covered": [...], "missing": [...]}
    """
    if not dna.evaluation_items:
        return {"score": 0.5, "covered": [], "missing": [], "note": "평가항목 정보 없음"}

    # 검색 대상: 전략·컨셉·대본·마케팅 관련 DNA 텍스트 합산
    haystack = " ".join(filter(None, [
        dna.concept, dna.slogan, dna.tone_and_manner,
        dna.core_problem, dna.crisis_statement, dna.solution_direction,
        " ".join(str(e) for e in dna.expected_effects),
        " ".join(str(s) for s in dna.script_outline),
        " ".join(str(p) for p in dna.persuasion_structure),
        " ".join(str(k) for k in dna.kpi_targets),
        dna.distribution_strategy,
    ])).lower()

    covered, missing = [], []
    for item in dna.evaluation_items:
        item_name = item.get("item", "")
        # 항목명의 핵심 단어 중 하나라도 포함되면 커버된 것으로 판정
        words = [w for w in item_name.replace("·", " ").replace("/", " ").split() if len(w) >= 2]
        if any(w.lower() in haystack for w in words):
            covered.append(item_name)
        else:
            missing.append({"item": item_name, "score": item.get("score", "")})

    total = len(dna.evaluation_items)
    score = len(covered) / total if total else 0.5
    return {"score": round(score, 2), "covered": covered, "missing": missing}


def _check_keyword_integration(dna: ConceptDNA) -> dict:
    """발주처 evaluation_keywords가 주요 산출물에 자연스럽게 반영됐는지 확인.

    Returns:
        {"score": float, "found": [...], "missing": [...]}
    """
    if not dna.evaluation_keywords:
        return {"score": 0.5, "found": [], "missing": [], "note": "평가 키워드 정보 없음"}

    haystack = " ".join(filter(None, [
        dna.concept, dna.slogan, dna.core_problem,
        dna.crisis_statement, dna.solution_direction,
        " ".join(dna.tone_keywords),
        " ".join(str(p) for p in dna.persuasion_structure[:2]),
    ])).lower()

    found, missing = [], []
    for kw in dna.evaluation_keywords:
        if kw.lower() in haystack:
            found.append(kw)
        else:
            missing.append(kw)

    score = len(found) / len(dna.evaluation_keywords)
    return {"score": round(score, 2), "found": found, "missing": missing}


def _calc_pre_score(pre_checks: dict) -> float:
    """3개 영역 점수의 가중 평균 계산."""
    weights = {"concept_flow": 0.4, "evaluation_coverage": 0.4, "keyword_integration": 0.2}
    return sum(pre_checks[k]["score"] * w for k, w in weights.items())


# ─────────────────────────────────────────────
# PASS 2: Claude 심층 일관성 분석
# ─────────────────────────────────────────────

def _deep_consistency_check(dna: ConceptDNA, pre_checks: dict, winning_patterns: list = None) -> dict:
    """서사 완성도·톤 일관성·미흡 섹션 자동 보완 (Claude)."""
    prompt  = _build_consistency_prompt(dna, pre_checks, winning_patterns or [])
    result  = claude_client.call_json(prompt, model=_OPUS_MODEL, max_tokens=4000)
    result.setdefault("narrative_score", 0.7)
    result.setdefault("issues", [])
    result.setdefault("revised_sections", {})
    result.setdefault("strengths", [])
    return result


def _build_consistency_prompt(dna: ConceptDNA, pre_checks: dict, winning_patterns: list = None) -> str:
    dna_ctx = dna_to_context_string(dna)

    # 설득 구조 요약
    persuasion_block = ""
    for step in dna.persuasion_structure:
        if isinstance(step, dict):
            persuasion_block += f"  [{step.get('stage','')}] {step.get('headline','')}\n"

    # 사전 검수 미흡 항목
    pre_issues = []
    for area, check in pre_checks.items():
        for issue in check.get("issues", []) + check.get("missing", []):
            item = issue if isinstance(issue, str) else issue.get("item", str(issue))
            pre_issues.append(f"  - [{area}] {item}")
    pre_issues_block = "\n".join(pre_issues) or "  (사전 검수 이상 없음)"

    # 대본 개요
    script_block = "\n".join(
        f"  {s.get('episode','')}편 《{s.get('title','')}》 [{s.get('format','')}]"
        for s in dna.script_outline[:3]
    ) or "  (대본 정보 없음)"

    return f"""당신은 정부 입찰 제안서 전문 심사 컨설턴트입니다.
아래 제안서 산출물의 일관성과 완성도를 심층 분석하고, 미흡한 부분을 자동 보완해주세요.

━━━━━━━━━━━━━━━━━━━━━━━
[프로젝트 컨텍스트]
━━━━━━━━━━━━━━━━━━━━━━━
{dna_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[설득 구조 (STEP 2)]
━━━━━━━━━━━━━━━━━━━━━━━
{persuasion_block or '  (미작성)'}

━━━━━━━━━━━━━━━━━━━━━━━
[크리에이티브 (STEP 3)]
━━━━━━━━━━━━━━━━━━━━━━━
- 컨셉: {dna.concept}
- 슬로건: {dna.slogan}
- 톤앤매너: {dna.tone_and_manner}

━━━━━━━━━━━━━━━━━━━━━━━
[대본 개요 (STEP 5)]
━━━━━━━━━━━━━━━━━━━━━━━
{script_block}

━━━━━━━━━━━━━━━━━━━━━━━
[KPI 지표 (STEP 6)]
━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(dna.kpi_targets[:3], ensure_ascii=False) if dna.kpi_targets else '  (미설정)'}

━━━━━━━━━━━━━━━━━━━━━━━
[사전 검수에서 발견된 미흡 항목]
━━━━━━━━━━━━━━━━━━━━━━━
{pre_issues_block}

━━━━━━━━━━━━━━━━━━━━━━━
[낙찰 케이스 패턴 — 이 패턴을 참조하여 개선 포인트 제안]
━━━━━━━━━━━━━━━━━━━━━━━
{chr(10).join(f"[{p.get('data_type','')}] {p.get('client_name','')} / {p.get('project_name','')} (점수 {p.get('eval_score',0):.1f}){chr(10)}  {(p.get('content') or '')[:200]}" for p in (winning_patterns or [])) or "  (낙찰 케이스 없음 — 학습 데이터 메뉴에서 추가하세요)"}

━━━━━━━━━━━━━━━━━━━━━━━
[분석 지침]
━━━━━━━━━━━━━━━━━━━━━━━
1. narrative_score: 위기→진단→해결→효과 4단계 서사 흐름의 완성도 (0.0~1.0)
2. issues: 발견된 불일치·빈틈·개선 사항 목록 (심각도별 분류)
3. revised_sections: 미흡 섹션의 보완 내용 (없으면 빈 dict)
4. strengths: 이 제안서의 강점 3~5개

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


반드시 아래 JSON으로만 출력하세요:
{{
  "narrative_score": 0.0,
  "issues": [
    {{
      "severity":    "critical|warning|info",
      "section":     "해당 섹션명",
      "description": "문제 설명",
      "suggestion":  "개선 방안"
    }}
  ],
  "revised_sections": {{
    "섹션명": "보완된 내용 (필요한 경우만)"
  }},
  "strengths": [
    "강점 1",
    "강점 2"
  ],
  "overall_assessment": "전체 제안서 완성도에 대한 종합 의견 (3~4문장)"
}}"""


# ─────────────────────────────────────────────
# PASS 3: 회사소개 맞춤 생성
# ─────────────────────────────────────────────

def _generate_company_profile(dna: ConceptDNA) -> dict:
    """RFP 키워드 기준으로 인터즈 실적 선별 + 역량 맞춤 재구성 (Claude)."""
    selected = _select_achievements(dna)
    prompt   = _build_company_profile_prompt(dna, selected)
    result   = claude_client.call_json(prompt, model=_OPUS_MODEL, max_tokens=2500)
    result["selected_raw_achievements"] = selected
    return result


def _select_achievements(dna: ConceptDNA) -> list:
    """RFP 키워드와 기관 유형 기준으로 관련성 높은 실적 상위 5개 선별.

    각 실적의 keywords와 DNA의 evaluation_keywords/agency_type을 비교해
    겹치는 키워드가 많은 순으로 정렬.
    """
    target_kws = set(dna.evaluation_keywords + [dna.agency_type] +
                     dna.core_tasks + dna.video_type.split())
    target_kws = {k.lower() for k in target_kws if k}

    scored = []
    for ach in _INTERZ_ACHIEVEMENTS:
        ach_kws  = {k.lower() for k in ach["keywords"]}
        overlap  = len(ach_kws & target_kws)
        scored.append((overlap, ach))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [ach for _, ach in scored[:5]]


def _build_company_profile_prompt(dna: ConceptDNA, selected: list) -> str:
    kw_list = ", ".join(f"'{k}'" for k in dna.evaluation_keywords[:8])
    core_tasks = "\n".join(f"  - {t}" for t in dna.core_tasks[:5]) or "  (없음)"

    achievement_block = "\n".join(
        f"  {a['year']} | {a['client']} | {a['project']}"
        for a in selected
    )
    competency_block = "\n".join(f"  - {c}" for c in _INTERZ_COMPETENCIES)

    return f"""당신은 영상 제작사 인터즈의 수석 제안 전략가입니다.
아래 발주처 요구사항을 분석해서 인터즈 회사소개를 맞춤 재구성해주세요.

【절대 원칙】
- 회사소개 섹션은 반드시 완성된 내용으로 생성하라. 빈 필드나 null은 허용하지 않는다.
- intro_paragraph는 심사위원이 읽고 "이 회사가 이 사업에 딱 맞다"고 느낄 수 있도록 작성하라.
- 실적은 단순 나열이 아니라 "왜 이 RFP와 관련 있는가"를 설득력 있게 연결하라.
- 차별화 포인트는 구체적 수치(제작 편수, 조회수 달성, 경력 연수 등)로 증명하라.
- 역량 설명은 추상적 선언 금지. 실제 경험과 시스템으로 뒷받침하라.

━━━━━━━━━━━━━━━━━━━━━━━
[발주처 요구 키워드]
━━━━━━━━━━━━━━━━━━━━━━━
{kw_list}

━━━━━━━━━━━━━━━━━━━━━━━
[핵심 과업]
━━━━━━━━━━━━━━━━━━━━━━━
{core_tasks}

━━━━━━━━━━━━━━━━━━━━━━━
[선별된 관련 실적 (관련성 높은 순)]
━━━━━━━━━━━━━━━━━━━━━━━
{achievement_block}

━━━━━━━━━━━━━━━━━━━━━━━
[인터즈 핵심 역량]
━━━━━━━━━━━━━━━━━━━━━━━
{competency_block}

━━━━━━━━━━━━━━━━━━━━━━━
[작성 세부 지침]
━━━━━━━━━━━━━━━━━━━━━━━
① headline
   - 이 RFP의 핵심 키워드를 자연스럽게 포함한 임팩트 있는 한 줄 헤드라인.
   - 예: "공공 영상 200편의 신뢰, {dna.client_name if hasattr(dna, 'client_name') else '○○'} 캠페인을 완성합니다"

② intro_paragraph (반드시 4문장 이상)
   - 1문장: 인터즈의 핵심 정체성 (공공기관 영상 전문 + 연차)
   - 2문장: 이 사업과의 직접적 연관성 (경험·역량)
   - 3문장: 차별화 방법론 또는 접근 철학
   - 4문장: 이 사업에서 인터즈가 만들어낼 성과 예고

③ selected_achievements (3~5개)
   - relevance: 단순 유사성이 아니라 이 RFP에서 직접 활용 가능한 노하우·성과를 설명.
   - 예: "이 사업에서 ○○ 방식을 동일하게 적용해 ○○ 효과를 낼 수 있습니다"

④ key_competencies (3~4개)
   - description: "○년간 ○편 제작, 평균 조회수 ○만" 같은 수치 기반 설명.
   - eval_linkage: 이 역량이 어떤 평가항목 배점에 직결되는지 명시.

⑤ differentiation (3문장 이상)
   - 타 제작사와 비교해 인터즈만이 가진 것을 구체적 수치로 증명.
   - 시스템·방법론·인력·실적 등 다각도에서 차별화.

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


반드시 아래 JSON으로만 출력하세요:
{{
  "headline": "임팩트 있는 헤드라인 한 줄 (RFP 키워드 포함, 30자 내외)",
  "intro_paragraph": "4문장 이상의 완성된 회사 소개 문단. 이 사업과의 연관성, 차별화 방법론, 성과 예고 포함. 최소 200자.",
  "selected_achievements": [
    {{
      "year":      "연도",
      "client":    "발주처",
      "project":   "사업명",
      "relevance": "이 RFP에서 직접 활용 가능한 노하우·성과 설명 (2문장 이상)"
    }}
  ],
  "key_competencies": [
    {{
      "competency":   "역량명 (구체적으로)",
      "description":  "수치 기반 구체적 설명 (제작 편수·조회수·경력 연수 등 포함)",
      "eval_linkage": "연결되는 평가항목명 + 배점에 미치는 영향"
    }}
  ],
  "differentiation": "타 제작사 대비 인터즈만의 차별점 3문장 이상. 수치·시스템·방법론으로 구체적으로 증명."
}}"""


# ─────────────────────────────────────────────
# PASS 4: PT 원고 + 심사위원 Q&A
# ─────────────────────────────────────────────

def _generate_pt_and_qa(
    dna: ConceptDNA,
    consistency: dict,
    company_profile: dict,
) -> dict:
    """PT 발표 원고 초안 + 심사위원 예상 질의응답 5개 생성 (Claude)."""
    prompt = _build_pt_qa_prompt(dna, consistency, company_profile)
    result = claude_client.call_json(prompt, model=_OPUS_MODEL, max_tokens=4000)
    result.setdefault("pt_script", {})
    result.setdefault("qa_prep", [])
    return result


def _build_pt_qa_prompt(
    dna: ConceptDNA,
    consistency: dict,
    company_profile: dict,
) -> str:
    dna_ctx   = dna_to_context_string(dna)
    strengths = "\n".join(f"  - {s}" for s in consistency.get("strengths", []))
    headline  = company_profile.get("headline", "인터즈")
    diff      = company_profile.get("differentiation", "")

    # 설득 구조 헤드라인 요약
    ps_headlines = " → ".join(
        step.get("headline", "")
        for step in dna.persuasion_structure
        if isinstance(step, dict) and step.get("headline")
    ) or f"{dna.crisis_statement} → {dna.solution_direction}"

    # KPI 목표 요약
    kpi_summary = ""
    if dna.kpi_targets:
        first = dna.kpi_targets[0]
        if isinstance(first, dict):
            kpi_summary = first.get("metric", "")

    return f"""당신은 정부 입찰 PT 전문 발표 코치입니다.
아래 제안서 내용을 바탕으로 실제 발표자가 그대로 읽을 수 있는 PT 원고와
심사위원이 실제로 물어볼 질문 5개에 대한 모범 답변을 작성해주세요.

【절대 원칙】
- PT 원고는 발표자가 그대로 읽을 수 있는 완성 문장으로 작성하라.
- "○○에 대해 설명드리겠습니다" 같은 메타 안내 문장은 금지.
- 각 key_point의 script는 최소 4문장 이상, 실제 발표 언어(구어체 경어)로.
- Q&A 답변은 "네, 좋은 질문입니다" 같은 전형적 답변 패턴은 금지.
  구체적 수치·사례·대안으로 시작하는 자신감 있는 답변을 작성하라.
- 심사위원이 가장 날카롭게 물어볼 질문 (예산 초과, 일정 지연, 역량 검증)에도
  정면으로 답할 수 있는 모범 답변을 준비하라.

━━━━━━━━━━━━━━━━━━━━━━━
[프로젝트 컨텍스트]
━━━━━━━━━━━━━━━━━━━━━━━
{dna_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[제안서 핵심 포인트]
━━━━━━━━━━━━━━━━━━━━━━━
- 핵심 컨셉: {dna.concept}
- 확정 슬로건: {dna.slogan}
- 설득 흐름: {ps_headlines}
- 인터즈 차별화: {diff}
- 대표 KPI: {kpi_summary}

━━━━━━━━━━━━━━━━━━━━━━━
[인터즈 강점 (회사소개 기반)]
━━━━━━━━━━━━━━━━━━━━━━━
{headline}
{strengths or '  (강점 분석 결과 없음)'}

━━━━━━━━━━━━━━━━━━━━━━━
[PT 원고 세부 지침]
━━━━━━━━━━━━━━━━━━━━━━━
총 발표 시간: 15분 기준

① 오프닝 (2분)
   - 심사위원의 시선을 즉시 집중시키는 충격적 사실 또는 질문으로 시작.
   - 이 제안서가 왜 이 사업에 최적인지 30초 안에 각인시켜라.
   - 마지막 문장에 슬로건을 자연스럽게 녹여라.
   - 실제 발표 언어(구어체 경어: "~습니다", "~드리겠습니다")로 작성.

② 본론 key_points 5~7개 (총 10분)
   - 각 포인트: 주제문(1) + 근거/사례(2~3) + 연결(1) 구조
   - script는 발표자가 그대로 읽을 수 있는 완성 문장 5문장 이상.
   - slide_hint는 해당 슬라이드에서 클릭/전환 타이밍까지 포함.
   - 수치가 나오는 부분은 강조 표현 포함 ("바로 이 숫자입니다", "주목해 주십시오").

③ 클로징 (3분)
   - 이 제안서의 핵심 가치 한 줄 요약.
   - 선정 후 인터즈가 만들어낼 변화를 생생하게 묘사.
   - 슬로건을 다시 한번 활용한 강렬한 마무리.

━━━━━━━━━━━━━━━━━━━━━━━
[Q&A 세부 지침]
━━━━━━━━━━━━━━━━━━━━━━━
- 반드시 5개: 예산(1) / 일정(1) / 역량(1) / 창의성(1) / 효과측정(1)
- 각 질문은 심사위원이 실제로 까다롭게 물어볼 날카로운 질문이어야 함.
- answer는 최소 3문장: ① 핵심 답변 (구체적 수치 포함) ② 보완 근거 ③ 대안/리스크 관리
- answer_strategy는 이 질문이 실제로 나왔을 때 발표자가 취해야 할 태도와 순서.

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


반드시 아래 JSON으로만 출력하세요:
{{
  "pt_script": {{
    "opening": "실제 발표자가 그대로 읽을 오프닝 멘트. 충격적 사실 또는 질문으로 시작. 슬로건 포함. 최소 4문장, 150자 이상. 구어체 경어.",
    "time_allocation": [
      {{"part": "오프닝", "duration": "2분", "content": "핵심 임팩트 + 슬로건 소개"}},
      {{"part": "본론", "duration": "10분", "content": "전략 → 컨셉 → 실행 → 대본 → 마케팅 → 회사소개"}},
      {{"part": "클로징", "duration": "3분", "content": "가치 요약 + 비전 + 슬로건 마무리"}}
    ],
    "key_points": [
      {{
        "order":      1,
        "title":      "발표 포인트 제목 (청중에게 전달할 핵심 메시지)",
        "script":     "발표자가 그대로 읽을 완성 문장. 최소 5문장. 수치 포함. 구어체 경어. 강조 포인트 명시.",
        "duration":   "약 N분",
        "slide_hint": "이 슬라이드에서 클릭 타이밍, 가리킬 항목, 전환 시점까지 포함"
      }}
    ],
    "closing": "실제 발표자가 그대로 읽을 클로징 멘트. 핵심 가치 요약 + 변화 비전 + 슬로건 마무리. 최소 4문장. 구어체 경어."
  }},

  "qa_prep": [
    {{
      "category":        "예산",
      "question":        "심사위원이 실제로 날카롭게 물어볼 구체적 질문 (예: '제시된 예산으로 이 품질이 가능한가요?')",
      "answer":          "① 핵심 답변 (구체적 수치 포함) ② 보완 근거 (유사 사례·시스템) ③ 리스크 관리 방안. 최소 3문장, 구어체 경어.",
      "answer_strategy": "이 질문이 나왔을 때 발표자의 태도, 답변 순서, 강조할 포인트"
    }},
    {{
      "category":        "일정",
      "question":        "일정 관련 날카로운 질문",
      "answer":          "① + ② + ③ 구조로 최소 3문장",
      "answer_strategy": "전략적 포인트"
    }},
    {{
      "category":        "역량",
      "question":        "역량 검증 관련 질문",
      "answer":          "① + ② + ③ 구조로 최소 3문장",
      "answer_strategy": "전략적 포인트"
    }},
    {{
      "category":        "창의성",
      "question":        "컨셉·아이디어 관련 질문",
      "answer":          "① + ② + ③ 구조로 최소 3문장",
      "answer_strategy": "전략적 포인트"
    }},
    {{
      "category":        "효과측정",
      "question":        "성과 측정·KPI 관련 질문",
      "answer":          "① + ② + ③ 구조로 최소 3문장",
      "answer_strategy": "전략적 포인트"
    }}
  ]
}}"""


# ─────────────────────────────────────────────
# FINAL: 최종 제안서 조립
# ─────────────────────────────────────────────

def _assemble_final_proposal(
    dna: ConceptDNA,
    consistency: dict,
    company_profile: dict,
    pt_qa: dict,
) -> dict:
    """모든 파트를 하나의 최종 제안서 구조로 조립.

    PPTX 빌더가 이 구조를 직접 사용할 수 있도록 섹션 단위로 정렬.
    """
    sections = [
        {
            "section_id":  "01_cover",
            "title":       "표지",
            "content":     {
                "client":       dna.client_name,
                "project":      dna.project_name,
                "video_type":   dna.video_type,
                "quantity":     f"{dna.quantity}편",
                "duration":     dna.duration,
                "slogan":       dna.slogan,
            },
        },
        {
            "section_id":  "02_strategy",
            "title":       "제안 전략",
            "content":     {
                "core_problem":       dna.core_problem,
                "persuasion_stages":  dna.persuasion_structure,
                "expected_effects":   dna.expected_effects,
            },
        },
        {
            "section_id":  "03_concept",
            "title":       "크리에이티브 컨셉",
            "content":     {
                "concept":       dna.concept,
                "description":   dna.concept_description,
                "slogans":       dna.slogans,
                "tone_keywords": dna.tone_keywords,
                "visual":        dna.visual_direction,
            },
        },
        {
            "section_id":  "04_episodes",
            "title":       "편별 제작 계획",
            "content":     {
                "episodes":  dna.episodes,
                "schedule":  dna.production_schedule,
                "team":      dna.team_composition,
                "budget":    dna.budget_plan,
            },
        },
        {
            "section_id":  "05_script",
            "title":       "대본 및 기획안",
            "content":     {
                "outline":      dna.script_outline,
                "series_hooks": [
                    {"episode": s.get("episode"), "hook": s.get("series_hook", {})}
                    for s in dna.scripts if s.get("series_hook")
                ],
            },
        },
        {
            "section_id":  "06_marketing",
            "title":       "유통 및 마케팅 전략",
            "content":     {
                "channels":    dna.distribution_channels,
                "youtube_seo": dna.youtube_strategy,
                "sns":         dna.sns_strategy,
                "kpi":         dna.kpi_targets,
                "reporting":   dna.reporting_system,
            },
        },
        {
            "section_id":  "07_company",
            "title":       "인터즈 수행 역량",
            "content":     company_profile,
        },
        {
            "section_id":  "08_qa",
            "title":       "예상 질의응답",
            "content":     {"qa_list": pt_qa.get("qa_prep", [])},
        },
    ]

    return {
        "sections":          sections,
        "overall_assessment": consistency.get("overall_assessment", ""),
        "strengths":         consistency.get("strengths", []),
        "pt_script":         pt_qa.get("pt_script", {}),
    }


# ─────────────────────────────────────────────
# DNA 스냅샷 (직렬화)
# ─────────────────────────────────────────────

def _snapshot_dna(dna: ConceptDNA) -> dict:
    """최종 ConceptDNA를 JSON 직렬화 가능한 dict로 변환.

    스크립트·마케팅 등 대용량 필드는 길이만 기록해 용량 절감.
    """
    d = asdict(dna)
    # 대용량 필드 요약
    for big_field in ("scripts", "rfp_text"):
        if big_field in d and d[big_field]:
            val = d[big_field]
            d[big_field] = f"[{len(val)}항목]" if isinstance(val, list) else f"[{len(val)}자]"
    return d


# ─────────────────────────────────────────────
# 공개 헬퍼 (pipeline.py에서 호출)
# ─────────────────────────────────────────────

def _check_consistency(dna: ConceptDNA, results: dict) -> list:
    """외부 호출용 래퍼: 규칙 기반 이슈 목록 반환."""
    checks = _rule_based_checks(dna)
    issues = []
    for area, check in checks.items():
        issues.extend(check.get("issues", []))
        for m in check.get("missing", []):
            issues.append({"severity": "warning", "field": area,
                           "message": str(m)})
    return issues


def _check_evaluation_keywords(dna: ConceptDNA, results: dict) -> dict:
    """외부 호출용 래퍼: 키워드 반영 현황 반환."""
    return _check_keyword_integration(dna)

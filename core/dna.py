# core/dna.py
# 역할: 컨셉 DNA 관리
# - RFP 분석 결과에서 추출한 핵심 요소를 DNA 구조체로 저장
# - 모든 에이전트가 DNA를 주입받아 일관된 방향성 유지
# - 에이전트 실행 중 DNA가 점진적으로 업데이트됨

from dataclasses import dataclass, field


@dataclass
class ConceptDNA:
    """제안서 전체를 관통하는 핵심 컨셉 DNA."""

    # STEP 0: RFP 파서가 추출
    client_name: str = ""             # 발주처명
    project_name: str = ""            # 사업명
    video_type: str = ""              # 영상 종류
    quantity: int = 0                 # 납품 수량
    duration: str = ""                # 편당 러닝타임
    budget: str = ""                  # 예산
    deadline: str = ""                # 납품기한
    rfp_text: str = ""                # 추출된 RFP 원문 (일부)
    core_tasks: list = field(default_factory=list)            # 핵심 과업 목록
    evaluation_items: list = field(default_factory=list)      # 평가항목 + 배점 (raw list)
    evaluation_criteria: str = ""                             # 평가 배점표 (프롬프트 주입용 포맷)
    top_criteria: list = field(default_factory=list)          # 배점 상위 3개 항목명 (전략 집중용)
    quantitative_requirements: list = field(default_factory=list)  # 정량 평가 항목 (실적·인력·등급 등)
    evaluation_strategy: dict = field(default_factory=dict)        # 배점표 전략 분석 (총점/핵심항목/체크리스트/집중공략)
    evaluation_keywords: list = field(default_factory=list)   # 평가 핵심 키워드 top10
    rfp_requirements: list = field(default_factory=list)      # RFP 요구사항 목록
    forbidden_notes: list = field(default_factory=list)       # 금지/주의사항

    # STEP 1: 리서처가 추가
    agency_type: str = ""             # 기관 유형 (중앙부처/지자체/의회/공공기관/기타)
    agency_characteristics: str = ""  # 기관 특성 요약
    recent_issues: list = field(default_factory=list)         # 발주처 최근 이슈

    # STEP 2: 전략가가 추가
    core_problem: str = ""                                         # 핵심 문제 정의 (한 문장)
    crisis_statement: str = ""                                     # 위기 제시 문장 (수치 포함)
    current_situation: str = ""                                    # 현황 진단
    solution_direction: str = ""                                   # 해결책 방향
    expected_effects: list = field(default_factory=list)           # 기대 효과 목록
    persuasion_structure: list = field(default_factory=list)       # 4단계 설득 구조 상세
    high_priority_eval_items: list = field(default_factory=list)   # 배점 높은 평가항목 순위

    # STEP 3: 크리에이티브 디렉터가 추가
    concept: str = ""                                           # 핵심 컨셉 (빅아이디어 한 줄)
    concept_description: str = ""                              # 컨셉 상세 설명
    slogan: str = ""                                           # 확정 슬로건 (1순위)
    slogans: list = field(default_factory=list)                # 슬로건 후보 3개 (text + rationale)
    tone_and_manner: str = ""                                  # 톤앤매너 요약
    tone_keywords: list = field(default_factory=list)          # 감성 키워드 5개
    forbidden_expressions: list = field(default_factory=list)  # 금지 표현/이미지 방향
    visual_direction: str = ""                                 # 비주얼 레퍼런스 방향

    # STEP 4: 플래너가 추가
    is_youtube_channel: bool = False                          # 유튜브 채널 포함 사업 여부
    episodes: list = field(default_factory=list)              # 편별 제작 계획
    production_schedule: list = field(default_factory=list)   # 단계별 제작 일정
    team_composition: dict = field(default_factory=dict)      # 투입 인력 구성
    budget_plan: dict = field(default_factory=dict)           # 예산 배분 계획
    series_plan: dict = field(default_factory=dict)           # 유튜브 시리즈 기획 (해당 시)
    execution_plan: dict = field(default_factory=dict)        # 실행 계획 전체 (하위 호환)

    # STEP 5: 스크립터가 추가
    scripts: list = field(default_factory=list)               # 편별 완성 대본 (전체)
    script_outline: list = field(default_factory=list)        # 대본 개요 (요약본)
    has_shortform: bool = False                               # 숏폼 버전 포함 여부

    # STEP 0.5: 내러티브 에이전트가 추가
    narrative: str = ""                                        # 20줄 전략 내러티브 전문

    # 사용자 사전 지시
    user_direction: str = ""                                   # 제안 방향 및 특별 요청 (입력 폼에서 주입)

    # 참고 제안서 구조 분석 결과
    reference_structure: str = ""                              # parse_reference_proposal() 결과

    # 파이프라인 인터랙티브 제어용 (에이전트 재실행 시 주입, 완료 후 초기화)
    user_feedback: str = ""                                    # 사용자 수정 지시 (임시)
    step_instruction: str = ""                                 # 스텝별 사전 지시 (confirm 시 주입, 스텝 실행 후 초기화)
    pages: int = 30                                            # 목표 제안서 페이지 수

    # 케이스 추적용 (파이프라인 시작 전 app.py에서 주입)
    case_id: int = 0                                           # rfp_cases.id (결과 테이블 연결용)

    # 재활용 참고 케이스 (이전 제안서 구조 참고용)
    reference_case_id: int = 0                                 # 참고할 이전 케이스 ID
    reference_case_context: str = ""                           # 이전 케이스 설득구조·컨셉·문체 요약

    # STEP 6: 마케터가 추가
    distribution_strategy: str = ""                            # 유통/마케팅 전략 요약
    distribution_channels: list = field(default_factory=list)  # 채널별 배포 전략
    youtube_strategy: dict = field(default_factory=dict)       # 유튜브 SEO 전략
    shortform_strategy: dict = field(default_factory=dict)     # 숏폼 플랫폼 전략
    sns_strategy: dict = field(default_factory=dict)           # SNS 채널별 운영 계획
    influencer_strategy: dict = field(default_factory=dict)    # 인플루언서/미디어 협업
    kpi_targets: list = field(default_factory=list)            # KPI 지표 + 월별 목표
    reporting_system: str = ""                                 # 성과 측정·보고 체계
    marketing_budget: dict = field(default_factory=dict)       # 마케팅 예산 배분


def create_dna(raw_input: dict) -> ConceptDNA:
    """사용자 입력값으로 초기 DNA 생성.

    Args:
        raw_input: main.py에서 받은 사용자 입력 dict

    Returns:
        초기화된 ConceptDNA
    """
    return ConceptDNA(
        client_name=raw_input.get("client_name", ""),
        project_name=raw_input.get("project_name", ""),
        video_type=raw_input.get("video_type", ""),
        quantity=int(raw_input.get("quantity", 0)),
        duration=raw_input.get("duration", ""),
        budget=raw_input.get("budget", ""),
        deadline=raw_input.get("deadline", ""),
        rfp_text=raw_input.get("rfp_text", ""),
        user_direction=raw_input.get("user_direction", ""),
    )


def update_dna(dna: ConceptDNA, updates: dict) -> ConceptDNA:
    """에이전트 실행 결과를 DNA에 반영.

    Args:
        dna: 현재 DNA
        updates: 업데이트할 필드 dict

    Returns:
        업데이트된 ConceptDNA (동일 인스턴스)
    """
    for key, value in updates.items():
        if hasattr(dna, key) and value not in (None, "", [], {}):
            setattr(dna, key, value)
    return dna


def dna_to_context_string(dna: ConceptDNA) -> str:
    """DNA를 프롬프트에 주입할 컨텍스트 문자열로 변환.

    Args:
        dna: 현재 ConceptDNA

    Returns:
        프롬프트 삽입용 요약 문자열
    """
    lines = [
        f"- 발주처: {dna.client_name}",
        f"- 사업명: {dna.project_name}",
        f"- 영상 종류: {dna.video_type}",
        f"- 납품 수량: {dna.quantity}편 / 편당 {dna.duration}",
        f"- 예산: {dna.budget or '미지정'}",
        f"- 납품기한: {dna.deadline or '미지정'}",
        f"- 기관 유형: {dna.agency_type or '미분류'}",
        f"- 기관 특성: {dna.agency_characteristics or '미분석'}",
    ]
    if dna.evaluation_criteria:
        criteria_block = f"\n【평가 배점표】\n{dna.evaluation_criteria}\n"
        if dna.top_criteria:
            top_names = " / ".join(dna.top_criteria)
            criteria_block += (
                f"\n⚠️ 최우선 집중 항목 (배점 TOP 3): {top_names}\n"
                "이 항목들이 전체 배점에서 가장 큰 비중을 차지한다. "
                "반드시 이 항목을 중심으로 내용을 구성하라."
            )
        else:
            criteria_block += "위 배점표 기준으로 높은 점수 항목에 집중해서 작성하라."
        lines.append(criteria_block)
    if dna.quantitative_requirements:
        quant_lines = []
        for it in dna.quantitative_requirements:
            if not isinstance(it, dict):
                continue
            name   = it.get("item", "")
            score  = it.get("score", "")
            detail = it.get("detail_criteria", "")
            hint   = it.get("strategic_hint", "")
            warn   = it.get("warning", "")
            line   = f"- {name}: {score}"
            if detail:
                line += f" — {detail}"
            if hint:
                line += f"\n  → {hint}"
            if warn:
                line += f" (⚠️ {warn})"
            quant_lines.append(line)
        if quant_lines:
            lines.append(
                "\n【정량 평가 필수 대응 — 미대응 시 감점·실격】\n"
                + "\n".join(quant_lines)
                + "\n위 정량 항목은 수치·실적·증빙 자료가 명확히 제시되어야 한다. "
                "제출 기준 미달 시 최저점 처리."
            )
    if dna.evaluation_strategy:
        es = dna.evaluation_strategy
        checklist = es.get("정량항목_체크리스트", [])
        focus = es.get("집중공략", "")
        core_items = es.get("핵심항목", [])
        if checklist or focus or core_items:
            strat_lines = []
            if core_items:
                strat_lines.append(f"핵심 배점 항목: {', '.join(str(x) for x in core_items)}")
            if checklist:
                strat_lines.append("정량 평가 체크리스트:")
                strat_lines.extend(f"  ✓ {c}" for c in checklist)
            if focus:
                strat_lines.append(f"집중공략: {focus}")
            lines.append(
                "\n【배점표 전략】\n"
                + "\n".join(strat_lines)
            )
    if dna.evaluation_keywords:
        lines.append(f"- 평가 키워드: {', '.join(dna.evaluation_keywords)}")
    if dna.core_tasks:
        tasks_str = "\n".join(f"  • {t}" for t in dna.core_tasks)
        lines.append(f"\n【핵심 과업 — 반드시 모두 다뤄야 함】\n{tasks_str}")
    if dna.forbidden_notes:
        notes_str = "\n".join(f"  ⚠️ {n}" for n in dna.forbidden_notes)
        lines.append(
            f"\n【금지·주의 사항 — 위반 시 감점·실격】\n{notes_str}\n"
            "위 사항은 제안서 전체에서 절대 위반하지 마라."
        )
    if dna.concept:
        lines.append(f"- 핵심 컨셉: {dna.concept}")
    if dna.slogan:
        lines.append(f"- 슬로건: {dna.slogan}")
    if dna.tone_and_manner:
        lines.append(f"- 톤앤매너: {dna.tone_and_manner}")
    if dna.pages:
        lines.append(f"- 목표 제안서 페이지: {dna.pages}페이지")
    if dna.user_direction:
        lines.append(f"\n【사용자 사전 지시】\n{dna.user_direction}")
    if dna.step_instruction:
        lines.append(f"\n【이 스텝 특별 지시】\n{dna.step_instruction}")
    if dna.reference_structure:
        lines.append(
            f"\n【참고 제안서 구조】\n{dna.reference_structure}\n"
            "위 구조와 흐름을 참고하되, 내용은 현재 발주처에 맞게 새로 작성하라.\n"
            "문체와 설득 방식은 참고 제안서를 따른다."
        )
    if dna.narrative:
        lines.append(f"\n[전략 내러티브]\n{dna.narrative}")
    if dna.user_feedback:
        lines.append(f"\n[사용자 수정 지시]\n{dna.user_feedback}")
    if dna.reference_case_context:
        lines.append(
            f"\n【참고 케이스 — 구조 참고, 내용은 현재 발주처에 맞게 새로 작성】\n"
            f"{dna.reference_case_context}\n"
            "위 케이스의 설득 구조·컨셉 방향·문체를 참고하되, "
            "현재 발주처({dna.client_name})에 맞게 내용은 완전히 새로 작성하라."
            .format(dna=dna)
        )
    return "\n".join(lines)

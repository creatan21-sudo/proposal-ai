# agents/ppt_narrator.py
# PPT 설계 에이전트: 전체 제안서 내용을 N장 슬라이드 설계안으로 압축

from core.claude_client import call_json


def _measure_content(case_detail: dict) -> int:
    """전체 스텝 결과 텍스트 분량 측정 (자 수)."""
    steps = case_detail.get("steps", {})
    total = 0
    for val in steps.values():
        if isinstance(val, dict):
            total += len(str(val))
        elif isinstance(val, list):
            total += sum(len(str(v)) for v in val)
    return total


def _build_context(case_detail: dict) -> str:
    """Claude 프롬프트용 전체 컨텍스트 구성."""
    case  = case_detail.get("case", {})
    dna   = case.get("dna", {})
    steps = case_detail.get("steps", {})
    rfp   = steps.get("rfp_analysis", {})
    cr    = steps.get("creative", {})

    lines = [
        f"# {case.get('client_name','')} / {case.get('project_name','')}",
        f"영상종류: {case.get('video_type','')} | 예산: {case.get('budget','')} | 납품기한: {case.get('deadline','')}",
        "",
    ]

    # 컨셉 & 슬로건
    concept = cr.get("concept", "") or dna.get("concept", "")
    slogan  = cr.get("confirmed_slogan", "") or dna.get("slogan", "")
    if concept: lines += [f"핵심 컨셉: {concept}", f"컨셉 설명: {cr.get('concept_description','') or dna.get('concept_description','')}"]
    if slogan:  lines.append(f"슬로건: {slogan}")
    lines.append("")

    # 평가 배점표
    eval_crit = dna.get("evaluation_criteria", "") or rfp.get("evaluation_criteria", "")
    if not eval_crit:
        eval_items = rfp.get("evaluation_items", []) or dna.get("evaluation_items", [])
        if eval_items:
            eval_crit = "\n".join(
                f"  - {it.get('item','') if isinstance(it, dict) else str(it)}"
                + (f" ({it['score']}점)" if isinstance(it, dict) and it.get("score") else "")
                for it in eval_items[:15]
            )
    if eval_crit:
        lines += ["## 평가 배점표 (배점 높은 순)", str(eval_crit)[:1500], ""]

    # RFP 핵심 과업
    core_tasks = rfp.get("core_tasks", []) or dna.get("core_tasks", [])
    if core_tasks:
        lines.append("## RFP 핵심 과업 (반드시 슬라이드에 포함)")
        for i, t in enumerate(core_tasks[:15], 1):
            lines.append(f"  {i}. {str(t)[:150]}")
        lines.append("")

    # 특이사항
    special = rfp.get("forbidden_notes", []) or dna.get("forbidden_notes", [])
    if special:
        lines += ["## RFP 특이사항/주의사항"] + [f"  - {str(s)[:150]}" for s in special[:5]] + [""]

    # 전략
    strat = steps.get("strategy", {})
    if strat:
        lines.append("## 전략")
        for k, lbl in [("core_problem","핵심문제"), ("crisis_statement","위기제시"),
                       ("solution_direction","해결방향")]:
            if strat.get(k): lines.append(f"{lbl}: {str(strat[k])[:200]}")
        effects = strat.get("expected_effects", [])
        if effects:
            lines.append("기대효과: " + " / ".join(str(e)[:80] for e in effects[:5]))
        ps = strat.get("persuasion_structure", [])
        if ps:
            lines.append("설득구조:")
            for s in ps[:5]:
                if isinstance(s, dict):
                    lines.append(f"  [{s.get('stage','')}] {str(s.get('body',''))[:150]}")
        hi = strat.get("high_priority_eval", []) or strat.get("high_priority_eval_items", [])
        if hi:
            lines.append("배점 상위 항목: " + " / ".join(
                (it.get("item","") if isinstance(it, dict) else str(it))[:60]
                for it in hi[:5]
            ))
        lines.append("")

    # 제작 계획
    plan = steps.get("plan", {})
    if plan:
        lines.append("## 제작 계획")
        for ep in plan.get("episodes", [])[:6]:
            if isinstance(ep, dict):
                lines.append(f"  {ep.get('episode_number','')}편: {ep.get('title','')} — {str(ep.get('core_message',''))[:120]}")
        sched = plan.get("production_schedule", [])
        if sched:
            lines.append("제작 일정:")
            for ph in sched[:4]:
                if isinstance(ph, dict):
                    lines.append(f"  [{ph.get('phase','')}] {str(ph.get('tasks',''))[:120]}")
        lines.append("")

    # 마케팅
    mkt = steps.get("marketing", {})
    if mkt:
        lines.append("## 마케팅 전략")
        for k, lbl in [("target_audience","타겟"), ("kpi","KPI"), ("key_strategy","핵심전략")]:
            if mkt.get(k): lines.append(f"{lbl}: {str(mkt[k])[:150]}")
        pl = mkt.get("platforms", [])
        if pl: lines.append("채널: " + ", ".join(str(p)[:40] for p in pl[:6]))
        lines.append("")

    # 리서치 인사이트
    research = steps.get("research", {})
    if research:
        lines.append("## 리서치 인사이트")
        issues = research.get("recent_issues", [])
        if issues:
            for iss in issues[:3]:
                if isinstance(iss, dict):
                    lines.append(f"  - {str(iss.get('title','') or iss.get('issue',''))[:150]}")
                elif isinstance(iss, str):
                    lines.append(f"  - {iss[:150]}")
        lines.append("")

    return "\n".join(lines)


def run(case_detail: dict, target_slides: int = 30) -> dict:
    """PPT 슬라이드 설계안 생성.

    Returns:
        {
            "slides": [
                {
                    "number": int,
                    "section": str,         # 섹션명
                    "head_copy": str,       # 헤드카피 (주장 문장)
                    "key_message": str,     # 핵심 메시지 1~2줄
                    "evidence": str,        # 데이터/근거/출처
                    "rfp_tags": list[str],  # 해당 RFP 요구항목
                    "slide_type": str,      # cover/toc/content/process/compare/number/message
                },
                ...
            ],
            "total_slides": int,
            "content_chars": int,
            "rfp_coverage": {"covered": [...], "missing": [...]},
        }
    """
    target_slides = max(10, min(60, target_slides))
    context       = _build_context(case_detail)
    content_chars = _measure_content(case_detail)

    case   = case_detail.get("case", {})
    dna    = case.get("dna", {})
    steps  = case_detail.get("steps", {})
    rfp    = steps.get("rfp_analysis", {})
    cr     = steps.get("creative", {})

    core_tasks = rfp.get("core_tasks", []) or dna.get("core_tasks", [])
    concept    = cr.get("concept", "")       or dna.get("concept", "")
    slogan     = cr.get("confirmed_slogan","") or dna.get("slogan", "")

    body_slides = target_slides - 3  # 표지(1) + 목차(1) + 마무리(1) 고정

    prompt = f"""당신은 정부 제안서 PT 전문 편집장입니다.
아래 제안서 전체 내용({content_chars:,}자)을 분석해 정확히 {target_slides}장 PPT 설계안을 출력하세요.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[제안서 전체 내용]
{context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【핵심 관통 메시지 — 표지·목차·마지막 슬라이드에 반드시 반영】
컨셉: {concept or "(데이터 참조)"}
슬로건: {slogan or "(데이터 참조)"}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【설계 원칙 — 반드시 준수】

1. RFP 요구항목 전체 커버 (누락 제로)
   · core_tasks의 모든 항목이 최소 1개 슬라이드에 포함
   · rfp_tags에 해당 RFP 항목명 정확히 태깅

2. 평가 배점 기반 장수 배분
   · 배점 높은 항목 = 더 많은 슬라이드 (배점에 비례)
   · 표지(1) + 목차(1) + 마무리(1) 고정 → 나머지 {body_slides}장을 내용에 배분

3. 헤드카피 원칙 (가장 중요)
   · "섹션명"이 아닌 "직관적 주장 문장" — 동사 포함, 15자 이내
   · 슬라이드를 보는 순간 핵심을 파악할 수 있어야 함
   · ✗ "전략 방향"  ✓ "선택과 집중으로 예산 효율 30% 확보"
   · ✗ "기대효과"   ✓ "6개월 내 조회수 300만 달성 가능"

4. 내러티브 연속성
   · 헤드카피를 순서대로 읽으면 하나의 완결된 스토리
   · 흐름: 현황 문제 → 전략 방향 → 크리에이티브 컨셉 → INTERZ 차별화 → 실행 계획 → 기대효과 → 마무리

5. INTERZ 차별화 3가지 (별도 슬라이드 필수)
   · compare 타입: 일반 접근 vs INTERZ만의 방식
   · 크리에이티브 역량 + 전략적 사고 + 데이터 기반 실행력

6. 컨셉/슬로건 관통
   · 표지 head_copy에 슬로건 반영
   · 목차 슬라이드 key_message 마지막 줄에 슬로건
   · 마지막 슬라이드 head_copy = 슬로건 전문

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
슬라이드 타입 7종:
  cover / toc / content / process / compare / number / message

반드시 아래 JSON만 출력하세요 (설명·마크다운 없이 순수 JSON만):
{{
  "slides": [
    {{
      "number": 1,
      "section": "표지",
      "head_copy": "슬로건 반영 제안서 타이틀 (15자 이내)",
      "key_message": "제안사: INTERZ(인터즈) | 제안일: {__import__('datetime').date.today().strftime('%Y.%m')}",
      "evidence": "",
      "rfp_tags": [],
      "slide_type": "cover"
    }},
    {{
      "number": 2,
      "section": "목차",
      "head_copy": "오늘 제안서의 핵심 흐름",
      "key_message": "01. 현황 진단\\n02. 전략 방향\\n03. 크리에이티브 컨셉\\n04. INTERZ 차별화\\n05. 실행 계획\\n06. 기대효과\\n—— {slogan or '슬로건'}",
      "evidence": "",
      "rfp_tags": [],
      "slide_type": "toc"
    }},
    {{
      "number": 3,
      "section": "현황 진단",
      "head_copy": "현황 핵심 주장 문장",
      "key_message": "핵심 메시지 (불릿 형식 가능)",
      "evidence": "데이터·수치·출처",
      "rfp_tags": ["해당 RFP 요구항목"],
      "slide_type": "compare"
    }}
  ],
  "rfp_coverage": {{
    "covered": ["커버된 RFP 항목"],
    "missing": ["누락 항목 (없으면 빈 배열)"]
  }}
}}

【최종 검증 — 출력 전 반드시 확인】
□ slides 배열이 정확히 {target_slides}개인가?
□ 모든 core_tasks 항목이 rfp_tags에 최소 1번 태깅되었는가?
□ rfp_coverage.missing이 비어있는가?
□ 표지(1) + 목차(1) + INTERZ차별화(1이상) + 마무리(1) 포함인가?
□ 모든 head_copy가 "주장 문장"인가 (섹션명이 아닌가)?"""

    result = call_json(prompt, max_tokens=16000)
    slides = result.get("slides", [])

    print(f"  [PPT설계] {len(slides)}/{target_slides}장 설계안 생성 완료")
    missing = result.get("rfp_coverage", {}).get("missing", [])
    if missing:
        print(f"  [PPT설계] 경고: 미커버 RFP 항목 {len(missing)}개 — {missing[:3]}")

    return {
        "slides":        slides,
        "total_slides":  len(slides),
        "content_chars": content_chars,
        "rfp_coverage":  result.get("rfp_coverage", {"covered": [], "missing": []}),
    }

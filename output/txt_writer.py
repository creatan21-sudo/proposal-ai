# output/txt_writer.py
# 역할: 파이프라인 결과를 TXT 파일로 출력
# - output/proposals/{사업명}_{날짜}.txt 저장
# - === 구분선 + 전체 내용 (요약 없이)

import json
from datetime import datetime
from pathlib import Path

from core.dna import ConceptDNA


def write_txt(dna: ConceptDNA, results: dict, output_dir: str = None) -> str:
    """파이프라인 결과를 TXT 파일로 저장.

    Args:
        dna: 완성된 ConceptDNA
        results: 파이프라인 결과 dict
        output_dir: 저장 경로 (None이면 output/proposals/)

    Returns:
        저장된 파일의 절대 경로
    """
    if output_dir:
        save_dir = Path(output_dir)
    else:
        save_dir = Path(__file__).parent / "proposals"
    save_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    safe_name = _safe_filename(dna.project_name or "제안서")
    filename = f"{safe_name}_{date_str}.txt"
    filepath = save_dir / filename

    lines = _build_content(dna, results)
    filepath.write_text("\n".join(lines), encoding="utf-8")
    return str(filepath)


# ─────────────────────────────────────────────
# 전체 내용 빌더
# ─────────────────────────────────────────────

def _build_content(dna: ConceptDNA, results: dict) -> list:
    lines = []

    # ── 표지 ──────────────────────────────────
    lines += _section_header("제안서 표지")
    lines += [
        f"사업명:    {dna.project_name}",
        f"발주처:    {dna.client_name}",
        f"영상 종류: {dna.video_type}",
        f"납품 수량: {dna.quantity}편  /  편당 {dna.duration}",
        f"예산:      {dna.budget or '미지정'}",
        f"납품기한:  {dna.deadline or '미지정'}",
        f"생성일:    {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"목표 페이지: {dna.pages}페이지",
    ]

    # ── STEP 0: RFP 분석 ──────────────────────
    lines += _section_header("STEP 0  RFP 분석")
    lines += _rfp_section(dna, results.get("rfp_analysis", {}))

    # ── STEP 0.5: 전략 내러티브 ───────────────
    if dna.narrative:
        lines += _section_header("STEP 0.5  전략 내러티브")
        lines.append(dna.narrative)

    # ── STEP 1: 발주처 리서치 ─────────────────
    lines += _section_header("STEP 1  발주처 리서치")
    lines += _research_section(dna, results.get("research", {}))

    # ── STEP 2: 전략 수립 ─────────────────────
    lines += _section_header("STEP 2  전략 수립")
    lines += _strategy_section(dna, results.get("strategy", {}))

    # ── STEP 3: 컨셉 개발 ─────────────────────
    lines += _section_header("STEP 3  컨셉 개발")
    lines += _creative_section(dna, results.get("creative", {}))

    # ── STEP 4: 실행 기획 ─────────────────────
    lines += _section_header("STEP 4  실행 기획")
    lines += _plan_section(dna, results.get("plan", {}))

    # ── STEP 5: 대본 제작 ─────────────────────
    lines += _section_header("STEP 5  대본 제작")
    lines += _script_section(dna, results.get("script", {}))

    # ── STEP 6: 마케팅 전략 ───────────────────
    lines += _section_header("STEP 6  마케팅 전략")
    lines += _marketing_section(dna, results.get("marketing", {}))

    # ── STEP 7: 최종 제안서 ───────────────────
    lines += _section_header("STEP 7  최종 제안서")
    lines += _final_section(dna, results.get("final_proposal", {}))

    # 마무리
    lines += ["", "=" * 70, "  END OF PROPOSAL", "=" * 70]
    return lines


# ─────────────────────────────────────────────
# 섹션별 내용 포맷터
# ─────────────────────────────────────────────

def _rfp_section(dna: ConceptDNA, r: dict) -> list:
    lines = []
    lines += _field("기관 유형", dna.agency_type or r.get("agency_type", "-"))
    if dna.evaluation_items:
        lines += _list_block("평가항목", [
            f"{it.get('item', it)} [{it.get('score', '')}점]"
            if isinstance(it, dict) else str(it)
            for it in dna.evaluation_items
        ])
    if dna.evaluation_keywords:
        lines += _field("핵심 키워드", " / ".join(dna.evaluation_keywords))
    if dna.core_tasks:
        lines += _list_block("핵심 과업", dna.core_tasks)
    if dna.rfp_requirements:
        lines += _list_block("RFP 요구사항", dna.rfp_requirements)
    if dna.forbidden_notes:
        lines += _list_block("주의·금지사항", dna.forbidden_notes)
    if dna.rfp_text:
        lines += _subheader("RFP 원문 (발췌)")
        lines.append(dna.rfp_text[:2000] + ("..." if len(dna.rfp_text) > 2000 else ""))
    return lines


def _research_section(dna: ConceptDNA, r: dict) -> list:
    lines = []
    lines += _field("기관 특성", dna.agency_characteristics or r.get("agency_characteristics", "-"))
    if dna.recent_issues:
        lines += _list_block("최근 이슈", [str(i) for i in dna.recent_issues])
    for key in ("similar_cases", "target_audience", "preferred_message_style",
                "real_needs", "top_three_wants", "attack_points"):
        val = r.get(key) or getattr(dna, key, None)
        if not val:
            continue
        label = {
            "similar_cases": "유사 사례",
            "target_audience": "타겟 오디언스",
            "preferred_message_style": "선호 메시지 스타일",
            "real_needs": "발주처 진짜 니즈",
            "top_three_wants": "Top 3 원하는 것",
            "attack_points": "공략 포인트",
        }.get(key, key)
        if isinstance(val, list):
            lines += _list_block(label, [str(v) for v in val])
        else:
            lines += _field(label, str(val))
    return lines


def _strategy_section(dna: ConceptDNA, r: dict) -> list:
    lines = []
    lines += _field("핵심 문제",   dna.core_problem or r.get("core_problem", "-"))
    lines += _field("위기 제시",   dna.crisis_statement or r.get("crisis_statement", "-"))
    lines += _field("현황 진단",   dna.current_situation or r.get("current_situation", "-"))
    lines += _field("해결책 방향", dna.solution_direction or r.get("solution_direction", "-"))
    if dna.expected_effects:
        lines += _list_block("기대 효과", [str(e) for e in dna.expected_effects])
    if dna.persuasion_structure:
        lines += _subheader("4단계 설득 구조")
        for i, step in enumerate(dna.persuasion_structure, 1):
            if isinstance(step, dict):
                title = step.get("stage", step.get("title", f"단계 {i}"))
                body  = step.get("body", step.get("description", ""))
                lines.append(f"  {i}. {title}")
                if body:
                    lines.append(f"     {body}")
            else:
                lines.append(f"  {i}. {step}")
    if dna.high_priority_eval_items:
        lines += _list_block("고배점 평가항목", [str(e) for e in dna.high_priority_eval_items])
    return lines


def _creative_section(dna: ConceptDNA, r: dict) -> list:
    lines = []
    lines += _field("핵심 컨셉",   dna.concept or r.get("concept", "-"))
    lines += _field("컨셉 설명",   dna.concept_description or r.get("concept_description", "-"))
    lines += _field("확정 슬로건", dna.slogan or r.get("confirmed_slogan", "-"))
    lines += _field("톤앤매너",    dna.tone_and_manner or r.get("tone_description", "-"))
    if dna.tone_keywords:
        lines += _field("감성 키워드", " / ".join(dna.tone_keywords))
    if dna.slogans:
        lines += _subheader("슬로건 후보")
        for i, s in enumerate(dna.slogans, 1):
            text = s.get("text", s) if isinstance(s, dict) else str(s)
            rationale = s.get("rationale", "") if isinstance(s, dict) else ""
            lines.append(f"  {i}. {text}")
            if rationale:
                lines.append(f"     {rationale}")
    tone_examples = r.get("tone_examples", [])
    if tone_examples:
        lines += _list_block("톤 예시 (나레이션/캡션)", [str(e) for e in tone_examples])
    lines += _field("비주얼 방향", dna.visual_direction or r.get("visual_direction", "-"))
    if dna.forbidden_expressions:
        lines += _list_block("금지 표현·이미지", dna.forbidden_expressions)
    return lines


def _plan_section(dna: ConceptDNA, r: dict) -> list:
    lines = []
    lines += _field("유튜브 채널", "포함" if dna.is_youtube_channel else "해당 없음")
    if dna.episodes:
        lines += _subheader("편별 제작 계획")
        for ep in dna.episodes:
            if isinstance(ep, dict):
                num   = ep.get("ep_num", ep.get("num", ""))
                title = ep.get("title", "")
                msg   = ep.get("key_message", ep.get("message", ""))
                dur   = ep.get("duration", "")
                lines.append(f"  {num}편. {title}" + (f"  [{dur}]" if dur else ""))
                if msg:
                    lines.append(f"     메시지: {msg}")
                concept_ep = ep.get("concept", "")
                if concept_ep:
                    lines.append(f"     컨셉: {concept_ep}")
            else:
                lines.append(f"  • {ep}")
    if dna.production_schedule:
        lines += _subheader("제작 일정")
        for phase in dna.production_schedule:
            if isinstance(phase, dict):
                name = phase.get("phase", phase.get("stage", ""))
                period = phase.get("period", phase.get("duration", ""))
                tasks = phase.get("tasks", phase.get("deliverables", []))
                lines.append(f"  {name}" + (f"  ({period})" if period else ""))
                if isinstance(tasks, list):
                    for t in tasks:
                        lines.append(f"    - {t}")
            else:
                lines.append(f"  • {phase}")
    if dna.team_composition:
        lines += _subheader("팀 구성")
        lines += _dict_block(dna.team_composition)
    if dna.budget_plan:
        lines += _subheader("예산 계획")
        lines += _dict_block(dna.budget_plan)
    if dna.series_plan:
        lines += _subheader("유튜브 시리즈 기획")
        lines += _dict_block(dna.series_plan)
    return lines


def _script_section(dna: ConceptDNA, r: dict) -> list:
    lines = []
    scripts = dna.scripts or r.get("scripts", [])
    lines += _field("총 대본 수", f"{len(scripts)}편")
    for sc in scripts:
        if not isinstance(sc, dict):
            continue
        num    = sc.get("ep_num", sc.get("episode", ""))
        title  = sc.get("title", "")
        fmt    = sc.get("format", "longform")
        dur    = sc.get("duration", "")
        lines += _subheader(f"{num}편 [{title}]  {fmt}  {dur}")
        scenes = sc.get("scenes", [])
        for j, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                lines.append(f"  씬 {j}. {scene}")
                continue
            scene_title = scene.get("scene_title", scene.get("title", f"씬 {j}"))
            lines.append(f"\n  ── {scene_title} ──")
            narr = scene.get("narration", "")
            if narr:
                lines.append(f"  [나레이션]\n  {narr}")
            dialogue = scene.get("dialogue", "")
            if dialogue:
                lines.append(f"  [대화]\n  {dialogue}")
            caption = scene.get("caption", "")
            if caption:
                lines.append(f"  [자막]\n  {caption}")
            visual = scene.get("visual_direction", scene.get("visual", ""))
            if visual:
                lines.append(f"  [비주얼 연출]\n  {visual}")
            interview_q = scene.get("interview_question", "")
            if interview_q:
                lines.append(f"  [인터뷰 질문]\n  {interview_q}")
        versions = sc.get("versions", {})
        for ver_key, ver_data in sorted(versions.items()):
            if not isinstance(ver_data, dict):
                continue
            sf_scenes = ver_data.get("scenes", [])
            if not sf_scenes:
                continue
            lines += _subheader(f"{num}편 숏폼 {ver_key} 버전")
            for sf in sf_scenes:
                if isinstance(sf, dict):
                    timecode = sf.get("timecode", sf.get("time", ""))
                    visual   = sf.get("visual", "")
                    audio    = sf.get("audio", sf.get("narration", sf.get("content", "")))
                    caption  = sf.get("caption", "")
                    if timecode:
                        lines.append(f"  [{timecode}]")
                    if visual:
                        lines.append(f"  [비주얼] {visual}")
                    if audio:
                        lines.append(f"  [나레이션/대사] {audio}")
                    if caption:
                        lines.append(f"  [자막] {caption}")
                else:
                    lines.append(f"  {sf}")
    return lines


def _marketing_section(dna: ConceptDNA, r: dict) -> list:
    lines = []
    lines += _field("전략 요약", dna.distribution_strategy or r.get("distribution_strategy", "-"))
    yt = dna.youtube_strategy or r.get("youtube_seo", {})
    if yt:
        lines += _subheader("유튜브 SEO 전략")
        lines += _dict_block(yt)
    sf = dna.shortform_strategy or r.get("shortform_strategy", {})
    if sf:
        lines += _subheader("숏폼 전략")
        lines += _dict_block(sf)
    sns = dna.sns_strategy or r.get("sns_channels", {})
    if sns:
        lines += _subheader("SNS 채널별 전략")
        lines += _dict_block(sns)
    inf = dna.influencer_strategy or r.get("influencer_strategy", {})
    if inf:
        lines += _subheader("인플루언서 협업")
        lines += _dict_block(inf)
    kpis = dna.kpi_targets or r.get("kpi", {})
    if kpis:
        lines += _subheader("KPI 목표")
        if isinstance(kpis, list):
            lines += _list_block("", [str(k) for k in kpis])
        else:
            lines += _dict_block(kpis)
    budget = dna.marketing_budget or r.get("marketing_budget", {})
    if budget:
        lines += _subheader("마케팅 예산")
        lines += _dict_block(budget)
    return lines


def _final_section(dna: ConceptDNA, r: dict) -> list:
    lines = []
    score = r.get("consistency_score", 0)
    lines += _field("일관성 점수", f"{score:.2f} / 1.00" if isinstance(score, float) else str(score))
    coverage = r.get("evaluation_coverage", {})
    if coverage:
        lines += _subheader("평가항목 커버 현황")
        for item, covered in coverage.items():
            mark = "✓" if covered else "✗"
            lines.append(f"  {mark} {item}")
    issues = r.get("issues", [])
    if issues:
        lines += _list_block("개선 포인트", [str(i) for i in issues])
    profile = r.get("company_profile", {})
    if profile:
        lines += _subheader("회사 소개")
        intro = profile.get("intro_paragraph", "")
        if intro:
            lines.append(intro)
        for key in ("strengths", "references", "differentiators"):
            val = profile.get(key, [])
            if val:
                label = {"strengths": "강점", "references": "실적", "differentiators": "차별화"}[key]
                lines += _list_block(label, [str(v) for v in val] if isinstance(val, list) else [str(val)])
    pt = r.get("pt_script", {})
    if pt:
        lines += _subheader("PT 발표 스크립트")
        slides = pt.get("slides", [])
        for slide in slides:
            if isinstance(slide, dict):
                sl_num = slide.get("slide_num", "")
                sl_title = slide.get("title", "")
                key_pts = slide.get("key_points", [])
                narr = slide.get("narration", "")
                lines.append(f"\n  슬라이드 {sl_num}: {sl_title}")
                for kp in key_pts:
                    lines.append(f"    • {kp}")
                if narr:
                    lines.append(f"  [발표 멘트]\n  {narr}")
    qa = r.get("qa_prep", [])
    if qa:
        lines += _subheader("예상 Q&A")
        for item in qa:
            if isinstance(item, dict):
                q = item.get("question", "")
                a = item.get("answer", "")
                cat = item.get("category", "")
                lines.append(f"\n  [{cat}] Q: {q}")
                lines.append(f"  A: {a}")
            else:
                lines.append(f"  • {item}")
    return lines


# ─────────────────────────────────────────────
# 공통 포맷 헬퍼
# ─────────────────────────────────────────────

def _section_header(title: str) -> list:
    sep = "=" * 70
    return ["", sep, f"  {title}", sep, ""]


def _subheader(title: str) -> list:
    if not title.strip():
        return []
    return ["", f"  ▶ {title}", "  " + "─" * 50]


def _field(label: str, value: str) -> list:
    if not value or value == "-":
        return []
    return [f"  {label}:", f"    {value}", ""]


def _list_block(label: str, items: list) -> list:
    if not items:
        return []
    lines = []
    if label:
        lines.append(f"  {label}:")
    for item in items:
        lines.append(f"    • {item}")
    lines.append("")
    return lines


def _dict_block(d: dict, indent: int = 4) -> list:
    if not d:
        return []
    pad = " " * indent
    lines = []
    for k, v in d.items():
        if isinstance(v, list):
            lines.append(f"{pad}{k}:")
            for item in v:
                lines.append(f"{pad}  - {item}")
        elif isinstance(v, dict):
            lines.append(f"{pad}{k}:")
            for kk, vv in v.items():
                lines.append(f"{pad}  {kk}: {vv}")
        else:
            lines.append(f"{pad}{k}: {v}")
    lines.append("")
    return lines


def _safe_filename(name: str) -> str:
    """파일명에 쓸 수 없는 문자 제거."""
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name[:60]

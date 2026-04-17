# output/pptx_builder.py
# 역할: 최종 제안서 데이터를 PPTX 파일로 출력
# - 오케스트레이터의 final_proposal 데이터를 슬라이드로 변환
# - 슬라이드 구성: 표지 → 전략 → 컨셉 → 실행계획 → 대본 → 유통전략 → 회사소개 → Q&A
# - 색상/폰트/레이아웃 일관성 유지

import re
from datetime import datetime
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Cm, Pt, Emu

from core.dna import ConceptDNA


# ─────────────────────────────────────────────
# 디자인 토큰 (모노크롬 — 흰 배경 / 검정 텍스트 / 컬러 없음)
# ─────────────────────────────────────────────

_C_NAVY   = RGBColor(0x00, 0x00, 0x00)   # 헤더 배경 → 검정
_C_GOLD   = RGBColor(0x33, 0x33, 0x33)   # 강조 → 진한 회색
_C_WHITE  = RGBColor(0xFF, 0xFF, 0xFF)
_C_LIGHT  = RGBColor(0xF8, 0xF8, 0xF8)   # 본문 배경 → 거의 흰색
_C_TEXT   = RGBColor(0x00, 0x00, 0x00)   # 본문 텍스트 → 검정
_C_GRAY   = RGBColor(0x77, 0x77, 0x77)   # 보조 텍스트 → 회색
_C_ACCENT = RGBColor(0x44, 0x44, 0x44)   # 강조 포인트 → 어두운 회색

# 폰트 (Windows/Mac 공통 한글 지원)
_FONT_KO = "맑은 고딕"
_FONT_EN = "Calibri"

# 슬라이드 크기 (16:9 와이드)
_SLIDE_W = Cm(33.867)
_SLIDE_H = Cm(19.05)

# 공통 여백
_MARGIN_L = Cm(2.0)
_MARGIN_T = Cm(3.8)
_CONTENT_W = Cm(29.867)


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────

def build_pptx(dna: ConceptDNA, final_proposal: dict, output_dir: str = None) -> Path:
    """제안서 전체를 PPTX로 생성.

    Args:
        dna: 최종 ConceptDNA (표지/메타 정보 사용)
        final_proposal: orchestrator의 final_proposal dict
        output_dir: 저장 경로 (None이면 output/proposals/ 사용)

    Returns:
        생성된 PPTX 파일 경로
    """
    prs = Presentation()
    prs.slide_width  = _SLIDE_W
    prs.slide_height = _SLIDE_H

    # 섹션별 슬라이드 추가
    _add_cover_slide(prs, dna, final_proposal.get("cover", {}))
    _add_toc_slide(prs, dna)
    _add_strategy_slide(prs, final_proposal.get("strategy", {}), dna)
    _add_concept_slide(prs, final_proposal.get("concept", {}), dna)
    _add_execution_slide(prs, final_proposal.get("episodes", {}), dna)
    _add_script_slide(prs, final_proposal.get("script", {}))
    _add_distribution_slide(prs, final_proposal.get("marketing", {}))
    _add_company_slide(prs, final_proposal.get("company", {}))
    _add_qa_slide(prs, final_proposal.get("qa", {}))

    # 파일 저장
    out_dir = Path(output_dir) if output_dir else Path(__file__).parent / "proposals"
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w가-힣\-]", "_", dna.project_name)[:40]
    filename = f"{safe_name}_{timestamp}.pptx"
    file_path = out_dir / filename

    prs.save(str(file_path))
    return file_path


# ─────────────────────────────────────────────
# 슬라이드별 구현
# ─────────────────────────────────────────────

def _add_cover_slide(prs: Presentation, dna: ConceptDNA, cover: dict) -> None:
    """표지 슬라이드: 발주처, 사업명, 컨셉, 슬로건."""
    slide = _blank_slide(prs)

    # 전체 배경 → 네이비
    _fill_background(slide, _C_NAVY)

    # 금색 상단 라인 (장식)
    _add_rect(slide, Cm(0), Cm(0), _SLIDE_W, Cm(0.5), _C_GOLD)

    # 발주처 (소제목)
    _add_text_box(
        slide, dna.client_name,
        Cm(2.5), Cm(2.5), Cm(28), Cm(1.2),
        font_size=18, bold=False, color=_C_GOLD, align=PP_ALIGN.LEFT,
    )

    # 사업명 (메인 타이틀)
    _add_text_box(
        slide, dna.project_name,
        Cm(2.5), Cm(4.0), Cm(28), Cm(3.5),
        font_size=32, bold=True, color=_C_WHITE, align=PP_ALIGN.LEFT,
        wrap=True,
    )

    # 슬로건
    slogan = dna.slogan or cover.get("slogan", "")
    if slogan:
        _add_text_box(
            slide, f'"{slogan}"',
            Cm(2.5), Cm(8.0), Cm(28), Cm(1.5),
            font_size=20, bold=False, color=_C_GOLD, align=PP_ALIGN.LEFT,
        )

    # 컨셉 한 줄
    concept = dna.concept or cover.get("concept", "")
    if concept:
        _add_text_box(
            slide, concept,
            Cm(2.5), Cm(9.8), Cm(28), Cm(1.2),
            font_size=14, bold=False, color=_C_GRAY, align=PP_ALIGN.LEFT,
        )

    # 하단 구분선
    _add_rect(slide, Cm(2.5), Cm(14.5), Cm(28.867), Cm(0.08), _C_GOLD)

    # 하단 정보 (영상 종류 / 수량 / 기간)
    meta_parts = [dna.video_type, f"{dna.quantity}편 / 편당 {dna.duration}"]
    if dna.deadline:
        meta_parts.append(f"납품기한: {dna.deadline}")
    _add_text_box(
        slide, "   |   ".join(p for p in meta_parts if p),
        Cm(2.5), Cm(14.8), Cm(24), Cm(0.9),
        font_size=11, bold=False, color=_C_GRAY, align=PP_ALIGN.LEFT,
    )

    # 제출 날짜
    today = datetime.now().strftime("%Y년 %m월")
    _add_text_box(
        slide, today,
        Cm(26), Cm(14.8), Cm(5.5), Cm(0.9),
        font_size=11, bold=False, color=_C_GRAY, align=PP_ALIGN.RIGHT,
    )


def _add_toc_slide(prs: Presentation, dna: ConceptDNA) -> None:
    """목차 슬라이드."""
    slide = _blank_slide(prs)
    _fill_background(slide, _C_LIGHT)
    _add_header_bar(slide, "목차  /  Contents")

    toc_items = [
        ("01", "현황 분석 & 전략",  "Crisis Statement · 해결책 방향"),
        ("02", "핵심 컨셉 & 슬로건", "Big Idea · 톤앤매너 · 비주얼"),
        ("03", "편별 실행 계획",    "제작 방향 · 일정 · 예산"),
        ("04", "대본 & 장면 구성",  "씬 테이블 · 내레이션"),
        ("05", "유통 & 마케팅 전략", "채널 전략 · KPI"),
        ("06", "회사 소개",         "포트폴리오 · 인력 구성"),
        ("07", "예상 Q&A",          "심사위원 예상 질문 & 답변"),
    ]

    col_w = Cm(13.5)
    for idx, (num, title, sub) in enumerate(toc_items):
        row   = idx // 2
        col   = idx % 2
        x     = _MARGIN_L + col * (col_w + Cm(1.0))
        y     = Cm(4.2) + row * Cm(2.5)

        # 번호 박스
        _add_rect(slide, x, y, Cm(1.4), Cm(1.4), _C_NAVY)
        _add_text_box(slide, num, x, y, Cm(1.4), Cm(1.4),
                      font_size=12, bold=True, color=_C_GOLD, align=PP_ALIGN.CENTER)

        # 타이틀
        _add_text_box(slide, title, x + Cm(1.6), y, col_w - Cm(1.8), Cm(0.8),
                      font_size=13, bold=True, color=_C_TEXT, align=PP_ALIGN.LEFT)
        # 서브타이틀
        _add_text_box(slide, sub, x + Cm(1.6), y + Cm(0.85), col_w - Cm(1.8), Cm(0.7),
                      font_size=10, bold=False, color=_C_GRAY, align=PP_ALIGN.LEFT)


def _add_strategy_slide(prs: Presentation, strategy: dict, dna: ConceptDNA) -> None:
    """전략 슬라이드: 위기제시 → 현황진단 → 해결책."""
    # ── 슬라이드 1: 위기·현황 진단 ──
    slide = _blank_slide(prs)
    _fill_background(slide, _C_LIGHT)
    _add_header_bar(slide, "01  현황 분석 & 전략")

    blocks = [
        ("위기 제시", dna.crisis_statement or strategy.get("crisis_statement", ""), _C_ACCENT),
        ("현황 진단", dna.current_situation or strategy.get("current_situation", ""), _C_NAVY),
        ("해결책 방향", dna.solution_direction or strategy.get("solution_direction", ""), _C_GOLD),
    ]

    for i, (label, text, color) in enumerate(blocks):
        x = _MARGIN_L + i * Cm(9.8)
        y = Cm(4.0)
        # 색상 박스 헤더
        _add_rect(slide, x, y, Cm(9.2), Cm(0.9), color)
        _add_text_box(slide, label, x, y, Cm(9.2), Cm(0.9),
                      font_size=12, bold=True, color=_C_WHITE, align=PP_ALIGN.CENTER)
        # 내용 박스
        _add_rect(slide, x, y + Cm(0.9), Cm(9.2), Cm(9.5), _C_WHITE)
        _add_text_box(slide, text or "(내용 없음)",
                      x + Cm(0.3), y + Cm(1.2), Cm(8.6), Cm(8.8),
                      font_size=11, bold=False, color=_C_TEXT, align=PP_ALIGN.LEFT,
                      wrap=True)

    # ── 슬라이드 2: 평가항목 배점 분석 ──
    eval_items = dna.evaluation_items or strategy.get("evaluation_items", [])
    if eval_items:
        slide2 = _blank_slide(prs)
        _fill_background(slide2, _C_LIGHT)
        _add_header_bar(slide2, "01  평가항목 분석")

        headers = ["평가항목", "배점", "대응 전략 포인트"]
        rows = []
        for item in eval_items[:8]:
            if isinstance(item, dict):
                rows.append([
                    item.get("item", ""),
                    str(item.get("score", "")),
                    item.get("strategy", "") or item.get("description", ""),
                ])
            else:
                rows.append([str(item), "", ""])

        _add_table(slide2, headers, rows,
                   Cm(2.0), Cm(3.8), Cm(29.867), Cm(0.75))


def _add_concept_slide(prs: Presentation, concept: dict, dna: ConceptDNA) -> None:
    """컨셉 슬라이드: 핵심 컨셉, 슬로건, 톤앤매너."""
    slide = _blank_slide(prs)
    _fill_background(slide, _C_NAVY)
    _add_header_bar(slide, "02  핵심 컨셉 & 슬로건", header_bg=_C_GOLD, text_color=_C_NAVY)

    # 빅아이디어 박스
    big_idea = dna.concept or concept.get("concept", "")
    _add_text_box(
        slide, big_idea,
        Cm(2.5), Cm(4.0), Cm(28.0), Cm(2.5),
        font_size=28, bold=True, color=_C_WHITE, align=PP_ALIGN.CENTER, wrap=True,
    )

    # 슬로건 후보 3개
    slogans = dna.slogans or concept.get("slogans", [])
    if slogans:
        for i, s in enumerate(slogans[:3]):
            y = Cm(7.0) + i * Cm(2.2)
            text = s.get("text", s) if isinstance(s, dict) else str(s)
            rationale = s.get("rationale", "") if isinstance(s, dict) else ""
            star = "★" if i == 0 else "☆"
            _add_rect(slide, Cm(2.5), y, Cm(1.2), Cm(1.6), _C_GOLD if i == 0 else _C_ACCENT)
            _add_text_box(slide, star, Cm(2.5), y, Cm(1.2), Cm(1.6),
                          font_size=14, bold=True, color=_C_WHITE, align=PP_ALIGN.CENTER)
            _add_text_box(slide, text, Cm(4.0), y, Cm(25.5), Cm(0.85),
                          font_size=14 if i == 0 else 12, bold=(i == 0),
                          color=_C_GOLD if i == 0 else _C_GRAY,
                          align=PP_ALIGN.LEFT)
            if rationale:
                _add_text_box(slide, rationale, Cm(4.0), y + Cm(0.9), Cm(25.5), Cm(0.7),
                              font_size=10, bold=False, color=_C_GRAY, align=PP_ALIGN.LEFT)

    # 톤앤매너 + 감성 키워드
    slide2 = _blank_slide(prs)
    _fill_background(slide2, _C_LIGHT)
    _add_header_bar(slide2, "02  톤앤매너 & 비주얼 방향")

    tone = dna.tone_and_manner or concept.get("tone_and_manner", "")
    keywords = dna.tone_keywords or concept.get("tone_keywords", [])
    visual = dna.visual_direction or concept.get("visual_direction", "")

    _add_text_box(slide2, "[ 톤앤매너 ]",
                  Cm(2.0), Cm(3.8), Cm(10), Cm(0.8),
                  font_size=12, bold=True, color=_C_NAVY, align=PP_ALIGN.LEFT)
    _add_text_box(slide2, tone or "-",
                  Cm(2.0), Cm(4.7), Cm(14.5), Cm(4.0),
                  font_size=12, bold=False, color=_C_TEXT, align=PP_ALIGN.LEFT, wrap=True)

    # 감성 키워드 칩
    _add_text_box(slide2, "[ 감성 키워드 ]",
                  Cm(17.5), Cm(3.8), Cm(10), Cm(0.8),
                  font_size=12, bold=True, color=_C_NAVY, align=PP_ALIGN.LEFT)
    for i, kw in enumerate(keywords[:5]):
        kx = Cm(17.5) + (i % 3) * Cm(4.0)
        ky = Cm(4.7) + (i // 3) * Cm(1.5)
        _add_rect(slide2, kx, ky, Cm(3.6), Cm(1.1), _C_NAVY)
        _add_text_box(slide2, str(kw), kx, ky, Cm(3.6), Cm(1.1),
                      font_size=12, bold=False, color=_C_WHITE, align=PP_ALIGN.CENTER)

    # 비주얼 방향
    if visual:
        _add_text_box(slide2, "[ 비주얼 방향 ]",
                      Cm(2.0), Cm(9.2), Cm(10), Cm(0.8),
                      font_size=12, bold=True, color=_C_NAVY, align=PP_ALIGN.LEFT)
        _add_text_box(slide2, visual,
                      Cm(2.0), Cm(10.1), Cm(29.5), Cm(4.0),
                      font_size=11, bold=False, color=_C_TEXT, align=PP_ALIGN.LEFT, wrap=True)


def _add_execution_slide(prs: Presentation, episodes_data: dict, dna: ConceptDNA) -> None:
    """실행계획 슬라이드: 편별 제작 방향, 일정, 예산."""
    # ── 편별 제작 기획 ──
    slide = _blank_slide(prs)
    _fill_background(slide, _C_LIGHT)
    _add_header_bar(slide, "03  편별 실행 계획")

    episodes = dna.episodes or episodes_data.get("episodes", [])
    if episodes:
        headers = ["편수", "제목", "핵심 메시지", "주요 장면"]
        rows = []
        for ep in episodes[:6]:
            if isinstance(ep, dict):
                rows.append([
                    f"{ep.get('ep_num', '')}편",
                    ep.get("title", ""),
                    ep.get("key_message", ep.get("message", "")),
                    ep.get("key_scene", ep.get("scene", "")),
                ])
            else:
                rows.append([str(ep), "", "", ""])
        _add_table(slide, headers, rows,
                   Cm(2.0), Cm(3.8), Cm(29.867), Cm(0.75))

    # ── 제작 일정 ──
    schedule = dna.production_schedule or episodes_data.get("production_schedule", [])
    if schedule:
        slide2 = _blank_slide(prs)
        _fill_background(slide2, _C_LIGHT)
        _add_header_bar(slide2, "03  제작 일정")

        headers2 = ["단계", "기간", "주요 작업", "산출물"]
        rows2 = []
        for phase in schedule:
            if isinstance(phase, dict):
                rows2.append([
                    phase.get("phase", phase.get("stage", "")),
                    phase.get("period", phase.get("duration", "")),
                    phase.get("tasks", phase.get("work", "")),
                    phase.get("deliverable", phase.get("output", "")),
                ])
            else:
                rows2.append([str(phase), "", "", ""])
        _add_table(slide2, headers2, rows2,
                   Cm(2.0), Cm(3.8), Cm(29.867), Cm(0.75))

    # ── 예산 계획 ──
    budget_plan = dna.budget_plan or episodes_data.get("budget_plan", {})
    if budget_plan:
        slide3 = _blank_slide(prs)
        _fill_background(slide3, _C_LIGHT)
        _add_header_bar(slide3, "03  예산 배분 계획")

        items_raw = budget_plan.get("items", budget_plan.get("breakdown", []))
        if isinstance(items_raw, list) and items_raw:
            headers3 = ["항목", "금액", "비율", "비고"]
            rows3 = []
            for item in items_raw:
                if isinstance(item, dict):
                    rows3.append([
                        item.get("category", item.get("name", "")),
                        item.get("amount", ""),
                        item.get("ratio", item.get("percent", "")),
                        item.get("note", ""),
                    ])
                else:
                    rows3.append([str(item), "", "", ""])
            total = budget_plan.get("total", dna.budget or "")
            if total:
                rows3.append(["합계", str(total), "100%", ""])
            _add_table(slide3, headers3, rows3,
                       Cm(2.0), Cm(3.8), Cm(29.867), Cm(0.75))


def _add_script_slide(prs: Presentation, script_data: dict) -> None:
    """대본/장면구성 슬라이드."""
    slide = _blank_slide(prs)
    _fill_background(slide, _C_LIGHT)
    _add_header_bar(slide, "04  대본 & 장면 구성")

    scripts = script_data.get("scripts", [])
    if not scripts:
        _add_text_box(slide, "대본이 생성되지 않았습니다.",
                      _MARGIN_L, Cm(5.0), Cm(28), Cm(2.0),
                      font_size=13, bold=False, color=_C_GRAY, align=PP_ALIGN.CENTER)
        return

    for ep_script in scripts[:3]:   # 최대 3편까지
        ep_slide = _blank_slide(prs)
        _fill_background(ep_slide, _C_LIGHT)

        ep_num   = ep_script.get("ep_num", ep_script.get("episode", ""))
        ep_title = ep_script.get("title", "")
        header_label = f"04  대본 — {ep_num}편: {ep_title}" if ep_title else f"04  대본 — {ep_num}편"
        _add_header_bar(ep_slide, header_label)

        scenes = ep_script.get("scenes", [])
        if scenes:
            headers = ["씬", "장소/상황", "영상 구성", "나레이션/대사"]
            rows = []
            for sc in scenes[:12]:
                if isinstance(sc, dict):
                    rows.append([
                        str(sc.get("scene_no", sc.get("num", ""))),
                        sc.get("location", sc.get("place", sc.get("setting", ""))),
                        sc.get("video", sc.get("visual", "")),
                        sc.get("narration", sc.get("dialogue", sc.get("script", ""))),
                    ])
                else:
                    rows.append(["", "", "", str(sc)])
            _add_table(ep_slide, headers, rows,
                       Cm(1.5), Cm(3.8), Cm(30.867), Cm(0.65), font_size=9)
        else:
            full_text = ep_script.get("full_script", ep_script.get("content", ""))
            if full_text:
                _add_text_box(ep_slide, str(full_text)[:800],
                              _MARGIN_L, Cm(4.0), Cm(29.0), Cm(12.0),
                              font_size=10, bold=False, color=_C_TEXT,
                              align=PP_ALIGN.LEFT, wrap=True)


def _add_distribution_slide(prs: Presentation, marketing: dict) -> None:
    """유통/마케팅 슬라이드: 채널 전략, KPI."""
    # ── 채널 전략 ──
    slide = _blank_slide(prs)
    _fill_background(slide, _C_LIGHT)
    _add_header_bar(slide, "05  유통 & 마케팅 전략")

    strategy_text = marketing.get("distribution_strategy", "")
    channels = marketing.get("distribution_channels", [])

    if strategy_text:
        _add_text_box(slide, strategy_text,
                      _MARGIN_L, Cm(4.0), _CONTENT_W, Cm(2.5),
                      font_size=12, bold=False, color=_C_TEXT,
                      align=PP_ALIGN.LEFT, wrap=True)

    if channels:
        headers = ["채널", "주요 전략", "목표 KPI"]
        rows = []
        for ch in channels[:7]:
            if isinstance(ch, dict):
                rows.append([
                    ch.get("channel", ch.get("platform", "")),
                    ch.get("strategy", ch.get("content", "")),
                    ch.get("kpi", ch.get("target", "")),
                ])
            else:
                rows.append([str(ch), "", ""])
        y_offset = Cm(7.0) if strategy_text else Cm(4.0)
        _add_table(slide, headers, rows,
                   Cm(2.0), y_offset, Cm(29.867), Cm(0.7))

    # ── KPI 목표 ──
    kpi_targets = marketing.get("kpi_targets", [])
    if kpi_targets:
        slide2 = _blank_slide(prs)
        _fill_background(slide2, _C_LIGHT)
        _add_header_bar(slide2, "05  KPI 목표 & 성과 측정")

        headers2 = ["지표", "목표값", "측정 주기", "비고"]
        rows2 = []
        for kpi in kpi_targets[:8]:
            if isinstance(kpi, dict):
                rows2.append([
                    kpi.get("metric", kpi.get("name", "")),
                    kpi.get("target", kpi.get("value", "")),
                    kpi.get("period", kpi.get("cycle", "월별")),
                    kpi.get("note", ""),
                ])
            else:
                rows2.append([str(kpi), "", "", ""])
        _add_table(slide2, headers2, rows2,
                   Cm(2.0), Cm(3.8), Cm(29.867), Cm(0.75))

        reporting = marketing.get("reporting_system", "")
        if reporting:
            _add_text_box(slide2, f"▶ 성과 보고 체계:  {reporting}",
                          _MARGIN_L, Cm(14.0), _CONTENT_W, Cm(1.2),
                          font_size=11, bold=False, color=_C_ACCENT, align=PP_ALIGN.LEFT)


def _add_company_slide(prs: Presentation, company: dict) -> None:
    """회사 소개 슬라이드."""
    slide = _blank_slide(prs)
    _fill_background(slide, _C_LIGHT)
    _add_header_bar(slide, "06  회사 소개 & 포트폴리오")

    intro = company.get("intro", "")
    if intro:
        _add_text_box(slide, intro,
                      _MARGIN_L, Cm(4.0), _CONTENT_W, Cm(3.0),
                      font_size=12, bold=False, color=_C_TEXT,
                      align=PP_ALIGN.LEFT, wrap=True)

    # 실적 표
    achievements = company.get("achievements", [])
    if achievements:
        headers = ["연도", "사업명", "발주처", "영상 종류"]
        rows = []
        for a in achievements[:8]:
            if isinstance(a, dict):
                rows.append([
                    str(a.get("year", "")),
                    a.get("project", a.get("name", "")),
                    a.get("client", ""),
                    a.get("type", a.get("video_type", "")),
                ])
            else:
                rows.append(["", str(a), "", ""])
        y_off = Cm(7.2) if intro else Cm(4.0)
        _add_table(slide, headers, rows,
                   Cm(2.0), y_off, Cm(29.867), Cm(0.7))

    # 팀 구성
    team = company.get("team_composition", {})
    if team:
        slide2 = _blank_slide(prs)
        _fill_background(slide2, _C_LIGHT)
        _add_header_bar(slide2, "06  투입 인력 구성")

        headers2 = ["역할", "담당자", "주요 경력"]
        rows2 = []
        for role, info in team.items():
            if isinstance(info, dict):
                rows2.append([role, info.get("name", ""), info.get("career", "")])
            else:
                rows2.append([role, str(info), ""])
        _add_table(slide2, headers2, rows2,
                   Cm(2.0), Cm(3.8), Cm(29.867), Cm(0.8))


def _add_qa_slide(prs: Presentation, qa: dict) -> None:
    """Q&A 슬라이드: 심사위원 예상 질문 & 답변."""
    qa_list = qa.get("qa_list", qa.get("pairs", []))
    if not qa_list:
        return

    slide = _blank_slide(prs)
    _fill_background(slide, _C_NAVY)
    _add_header_bar(slide, "07  예상 Q&A", header_bg=_C_GOLD, text_color=_C_NAVY)

    for i, pair in enumerate(qa_list[:5]):
        y_base = Cm(3.8) + i * Cm(2.8)
        q_text = pair.get("question", pair.get("q", "")) if isinstance(pair, dict) else str(pair)
        a_text = pair.get("answer", pair.get("a", "")) if isinstance(pair, dict) else ""

        # Q 레이블
        _add_rect(slide, Cm(1.5), y_base, Cm(1.0), Cm(0.9), _C_GOLD)
        _add_text_box(slide, "Q", Cm(1.5), y_base, Cm(1.0), Cm(0.9),
                      font_size=12, bold=True, color=_C_NAVY, align=PP_ALIGN.CENTER)
        _add_text_box(slide, q_text, Cm(2.7), y_base, Cm(29.0), Cm(0.9),
                      font_size=11, bold=True, color=_C_WHITE, align=PP_ALIGN.LEFT)

        # A 레이블
        _add_rect(slide, Cm(1.5), y_base + Cm(1.0), Cm(1.0), Cm(0.9), _C_ACCENT)
        _add_text_box(slide, "A", Cm(1.5), y_base + Cm(1.0), Cm(1.0), Cm(0.9),
                      font_size=12, bold=True, color=_C_WHITE, align=PP_ALIGN.CENTER)
        _add_text_box(slide, a_text, Cm(2.7), y_base + Cm(1.0), Cm(29.0), Cm(0.9),
                      font_size=11, bold=False, color=_C_WHITE,
                      align=PP_ALIGN.LEFT, wrap=True)


# ─────────────────────────────────────────────
# 공통 UI 헬퍼
# ─────────────────────────────────────────────

def _blank_slide(prs: Presentation):
    """빈 레이아웃 슬라이드 추가."""
    blank_layout = prs.slide_layouts[6]   # index 6 = blank
    return prs.slides.add_slide(blank_layout)


def _fill_background(slide, color: RGBColor) -> None:
    """슬라이드 배경색 채우기."""
    background = slide.background
    fill = background.fill
    fill.solid()
    fill.fore_color.rgb = color


def _add_header_bar(
    slide,
    text: str,
    header_bg: RGBColor = None,
    text_color: RGBColor = None,
) -> None:
    """슬라이드 상단 헤더 바 추가."""
    bg = header_bg or _C_NAVY
    tc = text_color or _C_WHITE
    _add_rect(slide, Cm(0), Cm(0), _SLIDE_W, Cm(3.2), bg)
    _add_text_box(slide, text,
                  Cm(2.0), Cm(0.8), Cm(28), Cm(1.8),
                  font_size=16, bold=True, color=tc, align=PP_ALIGN.LEFT)
    # 골드 하단 라인
    _add_rect(slide, Cm(0), Cm(3.2), _SLIDE_W, Cm(0.12), _C_GOLD)


def _add_rect(slide, x, y, w, h, color: RGBColor):
    """단색 직사각형 도형 추가."""
    shape = slide.shapes.add_shape(
        1,  # MSO_SHAPE_TYPE.RECTANGLE
        x, y, w, h,
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()   # 테두리 없음
    return shape


def _add_text_box(
    slide,
    text: str,
    x, y, w, h,
    font_size: int = 12,
    bold: bool = False,
    color: RGBColor = None,
    align: PP_ALIGN = PP_ALIGN.LEFT,
    wrap: bool = False,
) -> None:
    """텍스트 박스 추가 (투명 배경)."""
    txBox = slide.shapes.add_textbox(x, y, w, h)
    tf = txBox.text_frame
    tf.word_wrap = wrap

    p = tf.paragraphs[0]
    p.alignment = align

    run = p.add_run()
    run.text = str(text) if text else ""

    run.font.size     = Pt(font_size)
    run.font.bold     = bold
    run.font.color.rgb = color or _C_TEXT
    run.font.name     = _FONT_KO

    return txBox


def _add_table(
    slide,
    headers: list,
    rows: list,
    x, y, w,
    row_height,
    font_size: int = 10,
) -> None:
    """데이터 테이블 추가."""
    if not rows:
        return

    total_rows = len(rows) + 1   # 헤더 포함
    cols       = len(headers)

    table = slide.shapes.add_table(total_rows, cols, x, y, w, row_height * total_rows).table
    table.first_row = True

    # 컬럼 너비 균등 배분 (첫 열 좁게)
    col_widths = _calc_col_widths(headers, w, cols)
    for ci, cw in enumerate(col_widths):
        table.columns[ci].width = int(cw)

    # 헤더 행
    for ci, header in enumerate(headers):
        cell = table.cell(0, ci)
        cell.fill.solid()
        cell.fill.fore_color.rgb = _C_NAVY
        _set_cell_text(cell, header, font_size=font_size, bold=True, color=_C_WHITE, align=PP_ALIGN.CENTER)

    # 데이터 행
    for ri, row in enumerate(rows):
        bg = _C_WHITE if ri % 2 == 0 else _C_LIGHT
        for ci, value in enumerate(row[:cols]):
            cell = table.cell(ri + 1, ci)
            cell.fill.solid()
            cell.fill.fore_color.rgb = bg
            _set_cell_text(cell, str(value) if value else "",
                           font_size=font_size, bold=False, color=_C_TEXT,
                           align=PP_ALIGN.LEFT)


def _calc_col_widths(headers: list, total_w, n_cols: int) -> list:
    """헤더 이름 기준 컬럼 너비 비율 계산."""
    # 첫 컬럼이 번호/편수/단계 등 짧은 경우 좁게
    short_first = headers[0] in ("편수", "씬", "연도", "단계", "역할", "채널")
    if short_first and n_cols >= 3:
        ratios = [0.1] + [(0.9 / (n_cols - 1))] * (n_cols - 1)
    elif n_cols == 4:
        ratios = [0.15, 0.3, 0.3, 0.25]
    elif n_cols == 3:
        ratios = [0.25, 0.45, 0.30]
    else:
        ratios = [1.0 / n_cols] * n_cols

    return [total_w * r for r in ratios]


def _set_cell_text(
    cell,
    text: str,
    font_size: int = 10,
    bold: bool = False,
    color: RGBColor = None,
    align: PP_ALIGN = PP_ALIGN.LEFT,
) -> None:
    """테이블 셀 텍스트 설정."""
    cell.text = ""
    tf = cell.text_frame
    tf.word_wrap = True

    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text

    run.font.size      = Pt(font_size)
    run.font.bold      = bold
    run.font.color.rgb = color or _C_TEXT
    run.font.name      = _FONT_KO


# ─────────────────────────────────────────────
# Gamma API 연동
# ─────────────────────────────────────────────

def generate_with_gamma(content: str, pages: int) -> dict:
    """Gamma API로 프레젠테이션 생성.

    사전 조건:
        - Gamma Pro 플랜 가입 (gamma.app)
        - Settings → API Keys → Generate 에서 키 발급
        - Railway Variables에 GAMMA_API_KEY=발급받은키 설정

    Args:
        content: 제안서 전체 텍스트 (마크다운 형식 권장)
        pages:   목표 슬라이드 수 (1~50)

    Returns:
        {
            "url":      str,        # Gamma 프레젠테이션 웹 URL
            "pptx_url": str | None, # PPTX 익스포트 URL (있을 경우)
        }

    Raises:
        RuntimeError: API 키 미설정 또는 API 오류 시
    """
    import os
    import requests

    api_key = os.environ.get("GAMMA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GAMMA_API_KEY 환경변수가 설정되지 않았습니다.\n"
            "Gamma Pro 가입 후 Settings → API Keys에서 키를 발급받아 "
            "Railway Variables에 GAMMA_API_KEY=발급받은키 로 추가하세요."
        )

    num_slides = max(5, min(50, pages))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Gamma Generate API
    # 문서: https://gamma.app/docs/api  (API 키 발급 후 확인 가능)
    resp = requests.post(
        "https://api.gamma.app/v1/generate",
        json={
            "text":        content[:8000],   # Gamma API 입력 길이 제한
            "num_cards":   num_slides,
            "mode":        "presentation",
        },
        headers=headers,
        timeout=180,
    )

    if resp.status_code == 401:
        raise RuntimeError("Gamma API 인증 실패 — API 키를 확인하세요.")
    if resp.status_code == 402:
        raise RuntimeError("Gamma Pro 플랜이 필요합니다. gamma.app에서 업그레이드하세요.")
    if not resp.ok:
        raise RuntimeError(
            f"Gamma API 오류 ({resp.status_code}): {resp.text[:300]}"
        )

    data = resp.json()

    presentation_url = (
        data.get("url") or
        data.get("share_url") or
        data.get("view_url") or
        ""
    )
    pptx_url = (
        data.get("pptx_url") or
        data.get("export_url") or
        data.get("download_url") or
        None
    )

    if not presentation_url:
        raise RuntimeError(
            f"Gamma API 응답에 URL이 없습니다. 응답: {str(data)[:300]}"
        )

    return {"url": presentation_url, "pptx_url": pptx_url}

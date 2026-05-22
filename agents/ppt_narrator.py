# agents/ppt_narrator.py
# PPT 설계 에이전트: 전체 제안서 내용을 N장 슬라이드 설계안으로 압축

import math
import math
import anthropic
import requests as _requests

from core.claude_client import call_json
from core.dna import wrap_prompt_with_instruction as _wrap_instruction

_DATA_RELIABILITY_BLOCK = """
========================================
🚨 데이터 신뢰성 절대 원칙 (반드시 준수)
========================================
1. 모든 수치/통계/데이터는 실제 존재하는 자료만 사용
2. 출처 없는 수치 사용 절대 금지
3. 상상하거나 추정한 수치 사용 절대 금지
4. 유사한 주제의 데이터로 대체 절대 금지
   (데이트폭력 주제에 가정폭력 통계 사용 금지 등)
5. 확실하지 않으면 데이터 없이 서술
6. 출처 표기 형식: (출처: 기관명, 연도, 자료명)
7. Perplexity 검색으로 확인된 데이터만 수치로 인용
8. AI가 생성한 추정값은 반드시 '추정' 명시

위 원칙 위반 시 해당 내용 삭제 후 재작성하세요.
========================================
"""

_CHUNK_SIZE = 15  # 청크당 최대 슬라이드 수 (타임아웃 방지)


def _build_toc_prompt(context: str, target_slides: int, concept: str, slogan: str,
                      rfp_raw_section: str = "", ppx_supplement: str = "") -> str:
    """전체 목차(번호·섹션·헤드카피·slide_type) 생성 프롬프트."""
    body_slides = target_slides - 3
    ppx_block = (
        f"\n🌐 Perplexity 실시간 통계 (참고):\n{ppx_supplement[:800]}\n"
        if ppx_supplement else ""
    )
    rfp_block = rfp_raw_section[:2000] if rfp_raw_section else ""
    return f"""당신은 PPT 스토리 디렉터입니다.
아래 제안서 데이터를 바탕으로 정확히 {target_slides}장의 슬라이드 목차(뼈대)를 만드세요.
{rfp_block}{ppx_block}
[제안서 핵심 데이터]
{context[:4000]}

컨셉: {concept or "(제안서에서 추출)"}
슬로건: {slogan or "(제안서에서 추출)"}

【목차 설계 원칙】
- 표지(1) + 목차(1) + 본문({body_slides}장) + 마무리 message(1) = 총 {target_slides}장
- 배점 높은 RFP 항목 → 더 많은 슬라이드 배분
- 마지막 슬라이드: head_copy = 슬로건 전문, slide_type = message
- 각 head_copy는 15자 이내 주장 문장

반드시 {target_slides}개 항목을 아래 JSON으로만 출력 (설명 없이):
{{"toc": [
  {{"number": 1, "section": "표지", "head_copy": "슬로건 전문", "slide_type": "cover"}},
  {{"number": 2, "section": "목차", "head_copy": "오늘 이 자리에서 말씀드릴 것들", "slide_type": "toc"}},
  {{"number": 3, "section": "섹션명", "head_copy": "주장 문장", "slide_type": "content"}},
  ...
  {{"number": {target_slides}, "section": "마무리", "head_copy": "슬로건 전문", "slide_type": "message"}}
]}}"""


def _build_chunk_fill_prompt(chunk_toc: list, all_toc_text: str, context: str,
                              chunk_idx: int, total_chunks: int) -> str:
    """목차의 특정 청크 슬라이드 내용 채우기 프롬프트."""
    import json as _j
    start_num = chunk_toc[0]["number"]
    end_num   = chunk_toc[-1]["number"]
    n         = len(chunk_toc)
    toc_snip  = _j.dumps(chunk_toc, ensure_ascii=False)
    return f"""PPT 슬라이드 내용을 작성합니다. [{chunk_idx + 1}/{total_chunks} 청크, {n}장]

[전체 목차 개요]
{all_toc_text}

[제안서 핵심 데이터]
{context[:2500]}

{start_num}번~{end_num}번 슬라이드({n}장)의 세부 내용을 아래 목차 뼈대를 기반으로 작성하세요:
{toc_snip}

{_DATA_RELIABILITY_BLOCK}

【작성 원칙】
1. key_message: 2~4줄 핵심 내용 (수치 포함, 줄바꿈은 \n)
2. evidence: 구체적 수치·근거 (출처: 기관명, 연도)
3. rfp_tags: 해당 RFP 평가항목 1~3개
4. head_copy·section·slide_type은 목차에서 그대로 사용

아래 JSON만 출력 (설명 없이):
{{"slides": [
  {{"number": {start_num}, "section": "...", "head_copy": "...",
    "key_message": "...", "evidence": "...",
    "rfp_tags": [], "slide_type": "..."}},
  ...
]}}"""


_CHUNK_SIZE = 15  # 청크당 최대 슬라이드 수 (타임아웃 방지)


def _build_toc_prompt(context: str, target_slides: int, concept: str, slogan: str,
                      rfp_raw_section: str = "", ppx_supplement: str = "") -> str:
    """전체 목차(번호·섹션·헤드카피·slide_type) 생성 프롬프트."""
    body_slides = target_slides - 3
    ppx_block = (
        f"\n🌐 Perplexity 실시간 통계 (참고):\n{ppx_supplement[:800]}\n"
        if ppx_supplement else ""
    )
    rfp_block = rfp_raw_section[:2000] if rfp_raw_section else ""
    return f"""당신은 PPT 스토리 디렉터입니다.
아래 제안서 데이터를 바탕으로 정확히 {target_slides}장의 슬라이드 목차(뼈대)를 만드세요.
{rfp_block}{ppx_block}
[제안서 핵심 데이터]
{context[:4000]}

컨셉: {concept or "(제안서에서 추출)"}
슬로건: {slogan or "(제안서에서 추출)"}

【목차 설계 원칙】
- 표지(1) + 목차(1) + 본문({body_slides}장) + 마무리 message(1) = 총 {target_slides}장
- 배점 높은 RFP 항목 → 더 많은 슬라이드 배분
- 마지막 슬라이드: head_copy = 슬로건 전문, slide_type = message
- 각 head_copy는 15자 이내 주장 문장

반드시 {target_slides}개 항목을 아래 JSON으로만 출력 (설명 없이):
{{"toc": [
  {{"number": 1, "section": "표지", "head_copy": "슬로건 전문", "slide_type": "cover"}},
  {{"number": 2, "section": "목차", "head_copy": "오늘 이 자리에서 말씀드릴 것들", "slide_type": "toc"}},
  {{"number": 3, "section": "섹션명", "head_copy": "주장 문장", "slide_type": "content"}},
  ...
  {{"number": {target_slides}, "section": "마무리", "head_copy": "슬로건 전문", "slide_type": "message"}}
]}}"""


def _build_chunk_fill_prompt(chunk_toc: list, all_toc_text: str, context: str,
                              chunk_idx: int, total_chunks: int) -> str:
    """목차의 특정 청크 슬라이드 내용 채우기 프롬프트."""
    import json as _j
    start_num = chunk_toc[0]["number"]
    end_num   = chunk_toc[-1]["number"]
    n         = len(chunk_toc)
    toc_snip  = _j.dumps(chunk_toc, ensure_ascii=False)
    return f"""PPT 슬라이드 내용을 작성합니다. [{chunk_idx + 1}/{total_chunks} 청크, {n}장]

[전체 목차 개요]
{all_toc_text}

[제안서 핵심 데이터]
{context[:2500]}

{start_num}번~{end_num}번 슬라이드({n}장)의 세부 내용을 아래 목차 뼈대를 기반으로 작성하세요:
{toc_snip}

{_DATA_RELIABILITY_BLOCK}

【작성 원칙】
1. key_message: 2~4줄 핵심 내용 (수치 포함, 줄바꿈은 \n)
2. evidence: 구체적 수치·근거 (출처: 기관명, 연도)
3. rfp_tags: 해당 RFP 평가항목 1~3개
4. head_copy·section·slide_type은 목차에서 그대로 사용

아래 JSON만 출력 (설명 없이):
{{"slides": [
  {{"number": {start_num}, "section": "...", "head_copy": "...",
    "key_message": "...", "evidence": "...",
    "rfp_tags": [], "slide_type": "..."}},
  ...
]}}"""


_PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"


def _lookup_perplexity(query: str, api_key: str) -> str:
    """슬라이드 데이터·수치 필요 시 Perplexity로 실시간 검색.

    Returns:
        answer + citations 문자열 (API 키 없거나 실패 시 빈 문자열)
    """
    try:
        payload = {
            "model": "sonar-pro",
            "messages": [
                {"role": "system",
                 "content": "한국어로 답변하세요. 수치와 출처(기관명, 연도)를 반드시 포함하세요."},
                {"role": "user", "content": query},
            ],
            "max_tokens": 768,
        }
        resp = _requests.post(
            _PERPLEXITY_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=25,
        )
        resp.raise_for_status()
        data      = resp.json()
        content   = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        citations = data.get("citations", [])
        n = len(citations)
        print(f"  [Perplexity PPT] {n}건 출처 포함 결과 수신")
        if citations:
            content += "\n출처: " + " | ".join(citations[:5])
        return content
    except Exception as e:
        print(f"  [Perplexity PPT] 검색 실패: {e}")
        return ""


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


def _build_essential_block(case_detail: dict) -> str:
    """필수 블록만 구성: 컨셉/슬로건, 평가 배점표, 핵심 과업."""
    case  = case_detail.get("case", {})
    dna   = case.get("dna", {})
    steps = case_detail.get("steps", {})
    rfp   = steps.get("rfp_analysis", {})
    cr    = steps.get("creative", {})

    lines = []

    concept = cr.get("concept", "") or dna.get("concept", "")
    slogan  = cr.get("confirmed_slogan", "") or dna.get("slogan", "")
    if concept:
        lines += [f"핵심 컨셉: {concept}",
                  f"컨셉 설명: {cr.get('concept_description','') or dna.get('concept_description','')}"]
    if slogan:
        lines.append(f"슬로건: {slogan}")
    lines.append("")

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

    core_tasks = rfp.get("core_tasks", []) or dna.get("core_tasks", [])
    if core_tasks:
        lines.append("## RFP 핵심 과업 (반드시 슬라이드에 포함)")
        for i, t in enumerate(core_tasks[:15], 1):
            lines.append(f"  {i}. {str(t)[:150]}")
        lines.append("")

    return "\n".join(lines)


def _build_important_block(case_detail: dict, step_limit: int) -> str:
    """중요 블록: 전략, 크리에이티브, 제작 계획."""
    steps = case_detail.get("steps", {})
    lines = []

    strat = steps.get("strategy", {})
    if strat:
        lines.append("## 전략")
        for k, lbl in [("core_problem", "핵심문제"), ("crisis_statement", "위기제시"),
                        ("solution_direction", "해결방향")]:
            if strat.get(k):
                lines.append(f"{lbl}: {str(strat[k])[:200]}")
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
                (it.get("item", "") if isinstance(it, dict) else str(it))[:60]
                for it in hi[:5]
            ))
        lines.append("")

    plan = steps.get("plan", {})
    if plan:
        lines.append("## 제작 계획")
        for ep in plan.get("episodes", [])[:6]:
            if isinstance(ep, dict):
                lines.append(
                    f"  {ep.get('episode_number','')}편: {ep.get('title','')} — "
                    f"{str(ep.get('core_message',''))[:120]}"
                )
        sched = plan.get("production_schedule", [])
        if sched:
            lines.append("제작 일정:")
            for ph in sched[:4]:
                if isinstance(ph, dict):
                    lines.append(f"  [{ph.get('phase','')}] {str(ph.get('tasks',''))[:120]}")
        lines.append("")

    return "\n".join(lines)[:step_limit]


def _build_summary_block(case_detail: dict, step_limit: int) -> str:
    """요약 블록: 리서치 핵심 5줄, 마케팅 핵심 3줄."""
    steps = case_detail.get("steps", {})
    lines = []

    research = steps.get("research", {})
    if research:
        lines.append("## 리서치 데이터 (출처 명시 — 슬라이드에 우선 활용)")
        issues = research.get("recent_issues", [])
        if issues:
            lines.append("### 주요 이슈")
            for iss in issues[:5]:
                if isinstance(iss, dict):
                    title  = iss.get("title", "") or iss.get("issue", "")
                    source = iss.get("source", "") or iss.get("출처", "")
                    year   = iss.get("year", "") or iss.get("date", "")
                    cite   = f" (출처: {source}{', ' + str(year) if year else ''})" if source else " (출처: 리서치 결과)"
                    lines.append(f"  - {str(title)[:150]}{cite}")
                elif isinstance(iss, str):
                    lines.append(f"  - {iss[:150]} (출처: 리서치 결과)")
        stats = research.get("statistics", []) or research.get("key_stats", []) or research.get("data_points", [])
        if stats:
            lines.append("### 핵심 통계/수치")
            for st in stats[:5]:
                if isinstance(st, dict):
                    val    = st.get("value", "") or st.get("stat", "") or st.get("content", "")
                    source = st.get("source", "") or st.get("출처", "")
                    cite   = f" (출처: {source})" if source else " (출처: 리서치 결과)"
                    lines.append(f"  - {str(val)[:150]}{cite}")
                elif isinstance(st, str):
                    lines.append(f"  - {st[:150]} (출처: 리서치 결과)")
        trends = research.get("trends", []) or research.get("trend_keywords", [])
        if trends:
            lines.append("### 트렌드")
            for tr in trends[:4]:
                if isinstance(tr, dict):
                    kw     = tr.get("keyword", "") or tr.get("trend", "") or tr.get("name", "")
                    source = tr.get("source", "")
                    cite   = f" (출처: {source})" if source else ""
                    lines.append(f"  - {str(kw)[:120]}{cite}")
                elif isinstance(tr, str):
                    lines.append(f"  - {tr[:120]}")
        lines.append("")

    mkt = steps.get("marketing", {})
    if mkt:
        lines.append("## 마케팅 전략")
        for k, lbl in [("target_audience", "타겟"), ("kpi", "KPI"), ("key_strategy", "핵심전략")]:
            if mkt.get(k):
                lines.append(f"{lbl}: {str(mkt[k])[:150]}")
        pl = mkt.get("platforms", [])
        if pl:
            lines.append("채널: " + ", ".join(str(p)[:40] for p in pl[:6]))
        lines.append("")

    return "\n".join(lines)[:step_limit]


def _build_omittable_block(case_detail: dict, step_limit: int) -> str:
    """생략 가능 블록: 특이사항, platform_ops."""
    case  = case_detail.get("case", {})
    dna   = case.get("dna", {})
    steps = case_detail.get("steps", {})
    rfp   = steps.get("rfp_analysis", {})
    lines = []

    special = rfp.get("forbidden_notes", []) or dna.get("forbidden_notes", [])
    if special:
        lines += ["## RFP 특이사항/주의사항"] + \
                 [f"  - {str(s)[:150]}" for s in special[:5]] + [""]

    platform_ops = steps.get("platform_ops", {})
    if platform_ops:
        lines.append("## 플랫폼 운영 전략")
        lines.append(str(platform_ops)[:500])
        lines.append("")

    return "\n".join(lines)[:step_limit]


def _build_context(case_detail: dict, step_limit: int = 3_000, total_limit: int = 80_000) -> str:
    """Claude 프롬프트용 전체 컨텍스트 구성 (우선순위 기반 트런케이션).

    우선순위: essential > important > summary > omittable
    total_limit 초과 시 낮은 우선순위 블록부터 제거.
    """
    case = case_detail.get("case", {})

    header = "\n".join([
        f"# {case.get('client_name','')} / {case.get('project_name','')}",
        f"영상종류: {case.get('video_type','')} | 예산: {case.get('budget','')} | 납품기한: {case.get('deadline','')}",
        "",
    ])

    essential  = _build_essential_block(case_detail)[:step_limit]
    important  = _build_important_block(case_detail, step_limit)
    summary    = _build_summary_block(case_detail, step_limit)
    omittable  = _build_omittable_block(case_detail, step_limit)

    # 우선순위 순으로 total_limit 내에서 블록 조립
    blocks_by_priority = [
        ("essential",  essential),
        ("important",  important),
        ("summary",    summary),
        ("omittable",  omittable),
    ]

    used = len(header)
    included = []
    for name, block in blocks_by_priority:
        if not block.strip():
            continue
        needed = len(block) + 2
        if used + needed <= total_limit:
            included.append(block)
            used += needed
        else:
            remaining = total_limit - used - 2
            if remaining > 200 and name in ("essential", "important"):
                # 필수/중요 블록은 잘라서라도 포함
                included.append(block[:remaining])
            # summary/omittable은 공간 부족 시 드롭
            break

    return header + "\n\n".join(included)


def _is_context_overflow(exc: Exception) -> bool:
    """422 또는 context_length_exceeded 오류인지 판별."""
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 422:
            return True
        if exc.status_code == 400:
            body = getattr(exc, "body", None) or {}
            msg  = body.get("error", {}).get("message", "") if isinstance(body, dict) else str(exc)
            return "context_length" in msg.lower() or "too large" in msg.lower()
    msg = str(exc).lower()
    return "context_length" in msg or "too large" in msg or "422" in msg


def _case_detail_from_dna(dna, results: dict) -> dict:
    """파이프라인 실행 중 dna + results에서 case_detail 유사 구조를 조립."""
    import dataclasses as _dc
    dna_dict = {f.name: getattr(dna, f.name) for f in _dc.fields(dna)}
    case = {
        "client_name":  dna.client_name,
        "project_name": dna.project_name,
        "video_type":   dna.video_type,
        "budget":       dna.budget,
        "deadline":     dna.deadline,
        "dna":          dna_dict,
    }
    steps = {
        "rfp_analysis": {
            "evaluation_criteria": dna.evaluation_criteria,
            "evaluation_items":    dna.evaluation_items,
            "core_tasks":          dna.core_tasks,
            "top_keywords":        dna.evaluation_keywords,
            "forbidden_notes":     dna.forbidden_notes,
            "raw_text":            getattr(dna, "rfp_raw_text", "") or getattr(dna, "rfp_text", ""),
        },
        "research":  results.get("research", {}),
        "strategy":  results.get("strategy", {}),
        "creative": {
            "concept":             dna.concept,
            "confirmed_slogan":    dna.slogan,
            "concept_description": dna.concept_description,
            "tone_description":    dna.tone_and_manner,
        },
        "plan":      results.get("plan", {}),
        "marketing": results.get("marketing", {}),
    }
    return {"case": case, "steps": steps}


def run_from_dna(dna, results: dict, target_slides: int = 50, push_event=None) -> dict:
    """파이프라인 실행 중 dna + results에서 직접 설계안 생성."""
    case_detail = _case_detail_from_dna(dna, results)
    return run(case_detail, target_slides, push_event=push_event)


def run(case_detail: dict, target_slides: int = 50, push_event=None) -> dict:
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
    print(f"[PPT설계] 목표 슬라이드 수: {target_slides}")
    content_chars = _measure_content(case_detail)

    case  = case_detail.get("case", {})
    dna   = case.get("dna", {})

    # ── 사용자 수정 요청 추출 (재실행 시 최우선 반영) ──────────────
    _step_instruction = (dna.get("step_instruction") or "").strip()

    def _inject_instruction(prompt: str) -> str:
        """수정 요청이 있으면 프롬프트 최상단에 강하게 주입."""
        if not _step_instruction:
            return prompt
        header = (
            "========================================\n"
            "## 사용자 수정 요청 (최우선 반영)\n"
            "========================================\n"
            f"{_step_instruction}\n\n"
            "위 요청을 중심으로 처음부터 새로 설계하세요.\n"
            "기존 설계안은 참고만 하고 그대로 답습하지 마세요.\n"
            "========================================\n\n"
        )
        return header + prompt
    steps = case_detail.get("steps", {})
    rfp   = steps.get("rfp_analysis", {})
    cr    = steps.get("creative", {})

    case_id      = (case.get("id") or case.get("case_id") or dna.get("case_id"))
    case_id      = (case.get("id") or case.get("case_id") or dna.get("case_id"))
    core_tasks   = rfp.get("core_tasks", []) or dna.get("core_tasks", [])
    concept      = cr.get("concept", "")        or dna.get("concept", "")
    slogan       = cr.get("confirmed_slogan", "") or dna.get("slogan", "")
    rfp_raw_text = (rfp.get("raw_text", "")
                    or dna.get("rfp_raw_text", "")
                    or dna.get("rfp_text", ""))

    print(f"[PPT설계] RFP 원문 길이: {len(rfp_raw_text)}자")

    # 원문이 없으면 rfp_analyses 테이블에서 직접 조회
    if not rfp_raw_text:
        _cid = (case.get("id")
                or case.get("case_id")
                or case.get("dna", {}).get("case_id"))
        if _cid:
            print(f"  [PPT설계] rfp_raw_text 없음 — DB 직접 조회 (case_id={_cid})")
            try:
                from database.db import get_connection as _get_conn
                with _get_conn() as _conn:
                    _row = _conn.execute(
                        "SELECT raw_text FROM rfp_analyses"
                        " WHERE case_id=? AND raw_text != ''"
                        " ORDER BY created_at DESC LIMIT 1",
                        (_cid,),
                    ).fetchone()
                if _row and _row[0]:
                    rfp_raw_text = _row[0]
                    print(f"  [PPT설계] DB에서 RFP 원문 {len(rfp_raw_text)}자 로드 성공")
                else:
                    print(f"  [PPT설계] DB에도 RFP 원문 없음 — 원문 없이 진행")
            except Exception as _db_err:
                print(f"  [PPT설계] DB 조회 오류: {_db_err}")
        else:
            print(f"  [PPT설계] case_id 없음 — DB 조회 불가 (파이프라인 실행 중)")

    # ── Perplexity 실시간 통계 보강 ──────────────────────────────
    _perplexity_supplement = ""
    try:
        from config import PERPLEXITY_API_KEY as _PPX_KEY
        if _PPX_KEY and case.get("client_name"):
            _q = (f"{case['client_name']} {case.get('project_name','')} "
                  f"관련 최신 통계·수치·현황 2024 2025")
            print(f"  [PPT설계] Perplexity 최신 통계 조회 중...")
            _perplexity_supplement = _lookup_perplexity(_q, _PPX_KEY)
            if _perplexity_supplement:
                print(f"  [PPT설계] Perplexity 보강 데이터 {len(_perplexity_supplement)}자 수신")
    except Exception as _ppx_err:
        print(f"  [PPT설계] Perplexity 보강 생략: {_ppx_err}")

    body_slides = target_slides - 3  # 표지(1) + 목차(1) + 마무리(1) 고정

    def _build_rfp_raw_section() -> str:
        if not rfp_raw_text:
            return ""
        truncated = rfp_raw_text[:25000]
        suffix = "\n...(이하 생략)" if len(rfp_raw_text) > 25000 else ""
        return (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ RFP 원본 전문 — 배점 역설계의 핵심 근거\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "아래는 RFP 원본 전문입니다.\n"
            "평가배점표를 직접 읽고 배점이 높은 항목일수록 슬라이드를 더 많이 배정하세요.\n"
            "배점 역설계가 이 제안서의 핵심입니다.\n\n"
            + truncated + suffix
            + "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        )

    def _build_prompt(context: str) -> str:
        _ppx_section = ""
        if _perplexity_supplement:
            _ppx_section = (
                "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🌐 Perplexity 실시간 통계 (슬라이드 수치 근거로 활용)\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                + _perplexity_supplement
                + "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            )
        return f"""당신은 영상 제작 제안서 PT의 스토리 디렉터입니다.
아래 제안서 데이터({content_chars:,}자)를 바탕으로 정확히 {target_slides}장의 PPT 설계안을 만드세요.
반드시 {target_slides}장의 슬라이드를 설계하세요. {target_slides}장 미만이면 실패입니다.
절대 임의로 줄이지 마세요. 부족하면 배점 낮은 항목을 세분화하거나 슬라이드를 추가해서 정확히 {target_slides}장을 채우세요.
{_build_rfp_raw_section()}{_ppx_section}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[제안서 전체 데이터]
{context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【핵심 컨셉 & 슬로건 — 전체 슬라이드를 관통해야 함】
컨셉: {concept or "(데이터에서 추출)"}
슬로건: {slogan or "(데이터에서 추출)"}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【9단계 스토리텔링 구조 — 이 순서로 슬라이드를 설계하라】

이 제안서는 하나의 이야기입니다.
심사위원이 표지를 넘기는 순간부터 마지막 장에 도달할 때까지,
"이 팀이 우리를 가장 잘 이해하고 있구나"라는 확신을 심어야 합니다.

각 헤드카피는 '장면'처럼 써야 합니다 — 읽는 사람이 다음 장면이 궁금해지도록.
섹션명을 쓰지 말고, 그 슬라이드의 핵심 주장을 한 문장으로 압축하세요.

STEP 1 ─ 표지 (1장 고정)
  · 슬로건을 head_copy로 사용
  · 제안사명·날짜·프로젝트명 포함
  · slide_type: cover

STEP 2 ─ 목차 (1장 고정)
  · head_copy: "오늘 이 제안서가 말하는 것" 같은 호기심 유발 문장
  · key_message에 9단계 섹션명 나열 + 마지막 줄에 슬로건
  · slide_type: toc

STEP 3 ─ 발주처 이해 (1~3장)
  목적: "우리는 당신들을 깊이 공부했습니다"를 보여준다
  · 발주처의 핵심 사업, 과거 성공, 현재 위치를 정확히 짚는다
  · head_copy 예: "○○부는 이미 한 번 성공을 만든 조직입니다"
  · 단순 기관 소개가 아닌, 발주처의 자랑거리를 먼저 인정하는 어조
  · slide_type: content 또는 compare

STEP 4 ─ 문제 → 기회 재정의 (2~4장)
  목적: 위기를 기회로 바꿔 제시 — "문제가 있다"가 아니라 "기회가 있다"
  · 현황의 갭(gap)을 데이터로 보여주고, 그것을 뒤집어 기회로 재정의
  · head_copy 예: "콘텐츠가 넘치는 시대, 기억되는 영상은 3%뿐입니다"
  · 평가 배점이 높은 항목 → 더 많은 장수 배분
  · slide_type: compare 또는 number (수치 강조 시)

STEP 5 ─ 전략 (2~4장)
  목적: "어떻게 이 기회를 잡을 것인가"의 명쾌한 답
  · 핵심 전략 방향 + 설득 구조 + 배점 상위 평가항목 대응
  · head_copy 예: "선택과 집중 — 핵심 타겟 3개 채널에 화력을 집중합니다"
  · slide_type: content 또는 process

STEP 6 ─ 크리에이티브 컨셉 (2~3장)
  목적: 전략을 집약하는 창의적 아이디어 제시
  · 컨셉명 + 슬로건 + 비주얼 방향 + 왜 이 컨셉인가
  · head_copy에 슬로건 또는 컨셉 문구 직접 활용
  · slide_type: message (슬로건 강조) 또는 content

STEP 7 ─ 실행 계획 (3~6장)
  목적: "말뿐이 아니라 구체적으로 어떻게 만들 것인가"
  · 편성 계획(편 수·분량·주제), 제작 프로세스, 촬영 방식
  · 배점 높은 과업 = 더 많은 슬라이드
  · head_copy 예: "총 6편, 2주에 1편씩 — 12주 완주 로드맵"
  · slide_type: process 또는 content

STEP 8 ─ 출연·캐스팅 & 운영 (1~2장)
  목적: "전문가가 운영한다"는 신뢰 제공
  · 출연진 전략, 협력사, 크루 구성, 스튜디오 등
  · head_copy 예: "기획부터 편집까지 — 인하우스 원스톱 제작"
  · slide_type: content

STEP 9 ─ 홍보·마케팅 전략 (2~3장)
  목적: 영상을 만드는 것에서 끝나지 않고 "퍼뜨리는 방법"까지 제시
  · 플랫폼별 전략, KPI, 유통 방식, 바이럴 시나리오
  · head_copy 예: "유튜브·인스타·TikTok — 채널별 맞춤 콘텐츠 전략"
  · slide_type: content 또는 compare

STEP 10 ─ INTERZ 차별화 (2~3장, 필수)
  목적: "다른 업체와 왜 다른가" — compare 타입으로 시각적 대비
  · 반드시 compare 타입: 일반 제작사 vs INTERZ 방식
  · 크리에이티브 역량 / 전략적 사고 / 데이터 실행력 3가지 축
  · head_copy 예: "기획이 먼저입니다 — 촬영 전 3단계 전략 검증"
  · slide_type: compare

STEP 11 ─ 기대효과 & 마무리 (2~3장)
  목적: 숫자로 보여주고, 슬로건으로 닫는다
  · KPI 수치를 number 타입으로 강조 + 마지막 장은 슬로건 전문
  · head_copy 예: "6개월 후 당신의 채널은 달라질 것입니다"
  · 마지막 슬라이드 head_copy = 슬로건 전문, slide_type: message

{_DATA_RELIABILITY_BLOCK}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【데이터 출처 및 신뢰도 원칙 — 반드시 준수】

▌원칙 1 · 출처 필수 표기
  · 모든 수치·통계·데이터에 반드시 출처 명시
  · 표기 형식:  (출처: 기관명, 연도)  또는  (출처: 리서치 결과)
  · 출처 없는 수치는 슬라이드에 사용 금지
  · 출처 불명확한 경우 → "업계 추정" 표기 또는 해당 수치 삭제
  · evidence 필드에 수치와 출처를 함께 기재:
    예) "유튜브 쇼핑 클릭률 전년 대비 34% 상승 (출처: 유튜브 코리아, 2024)"

▌원칙 2 · 신뢰도 우선순위
  1순위: 정부·공공기관 공식 발표 자료 (문화체육관광부, 행정안전부 등)
  2순위: 언론 보도 (주요 일간지, 공영방송, 연합뉴스 등)
  3순위: 학술 연구·공식 보고서 (학회, 연구기관)
  4순위: 업계 리포트 (닐슨, 오픈서베이, 대행사 리포트 등)
  5순위: 인터즈 학습 데이터 사례
  사용 불가: 출처 불명 블로그, 커뮤니티 게시글, 익명 정보

▌원칙 3 · 리서치 데이터 우선 활용
  · 위 [리서치 데이터] 섹션의 실제 수집 데이터를 최우선으로 사용
  · 리서치 데이터에 출처가 명시된 경우 그대로 인용
  · 리서치에 없는 수치가 필요한 경우 → evidence에 "추가 조사 필요" 표시
  · 스스로 수치를 만들어내거나 추정치를 확정 수치처럼 쓰지 않는다
  · AI가 생성하거나 확인되지 않은 추정값: ⚠️ AI 추정값 — 제출 전 직접 확인 필요 표시

▌원칙 4 · 슬라이드별 출처 표기 방법
  · evidence 필드 = "수치/데이터 내용 (출처: 기관명, 연도)" 형식으로 작성
  · number·compare 타입 슬라이드: evidence에 수치 출처 필수
  · content 타입: 핵심 근거가 있으면 evidence에 기재
  · 출처가 없는 슬라이드(커버·목차·메시지 타입)는 evidence를 빈 문자열로

▌원칙 5 · 데이터 적합성
  · 인용 데이터는 반드시 현재 슬라이드 주제와 직접 관련된 것이어야 한다
  · 유사하지만 다른 주제의 통계·사례 사용 금지
    예: 데이트 폭력 주제 슬라이드 → 가정 폭력 통계 (X)
    예: 청소년 도박 주제 → 성인 도박 통계 (X)
  · 주제와 100% 일치하는 데이터만 사용. 일치 데이터 없으면 evidence에 "관련 데이터 없음" 표기
  · 절대 유사 데이터로 대체하지 말 것

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【헤드카피 작성 원칙 — 가장 중요】

헤드카피는 '장면'이어야 합니다.
독자가 읽는 순간 "다음이 궁금하다"는 감각을 만들어야 합니다.

✗ 나쁜 예 (섹션명, 정보 나열):
  "전략 방향" / "기대효과" / "제작 일정" / "마케팅 전략"

✓ 좋은 예 (주장 문장, 장면):
  "세 채널, 하나의 메시지 — 통일된 세계관을 만듭니다"
  "먼저 알아봤습니다 — ○○의 팬은 이미 존재합니다"
  "6개월 뒤 ○○부 유튜브 구독자 10만을 제안합니다"
  "이야기가 없으면 조회수도 없습니다"

규칙:
  · 동사 포함 필수
  · 20자 이내 (짧을수록 강력)
  · 수치가 있으면 수치로 말하라
  · 발주처 입장에서 읽었을 때 "맞아!" 하는 공감이 와야 함

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【슬라이드 배분 원칙】
  · 표지(1) + 목차(1) + 마무리(1) 고정 → 나머지 {body_slides}장을 내용에 배분
  · 평가 배점이 높은 항목 → 더 많은 슬라이드 (배점 비례 배분)
  · RFP core_tasks 전체 항목이 rfp_tags에 최소 1회 태깅

슬라이드 타입 7종:
  cover / toc / content / process / compare / number / message

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
반드시 아래 JSON만 출력하세요 (설명·마크다운 없이 순수 JSON만):
{{
  "slides": [
    {{
      "number": 1,
      "section": "표지",
      "head_copy": "{slogan or '슬로건 전문'}",
      "key_message": "제안사: INTERZ(인터즈) | 제안일: {__import__('datetime').date.today().strftime('%Y.%m')}",
      "evidence": "",
      "rfp_tags": [],
      "slide_type": "cover"
    }},
    {{
      "number": 2,
      "section": "목차",
      "head_copy": "오늘 이 자리에서 말씀드릴 것들",
      "key_message": "01. 발주처 이해\\n02. 문제와 기회\\n03. 전략\\n04. 크리에이티브 컨셉\\n05. 실행 계획\\n06. 운영·마케팅\\n07. INTERZ 차별화\\n08. 기대효과\\n—— {slogan or '슬로건'}",
      "evidence": "",
      "rfp_tags": [],
      "slide_type": "toc"
    }},
    {{
      "number": 3,
      "section": "발주처 이해",
      "head_copy": "발주처의 강점을 주장하는 15자 이내 문장",
      "key_message": "발주처 핵심 사업·성과·현재 위치 (불릿 형식)",
      "evidence": "데이터·출처",
      "rfp_tags": [],
      "slide_type": "content"
    }}
  ],
  "rfp_coverage": {{
    "covered": ["커버된 RFP 항목들"],
    "missing": ["누락 항목 (없으면 빈 배열)"]
  }}
}}

【최종 검증 — 출력 전 반드시 확인】
□ slides 배열이 정확히 {target_slides}개인가?
□ 모든 core_tasks 항목이 rfp_tags에 최소 1번 태깅되었는가?
□ rfp_coverage.missing이 비어있는가?
□ 표지(1) + 목차(1) + INTERZ차별화(compare, 2장 이상) + 마무리 message(1) 포함인가?
□ 모든 head_copy가 "주장 문장 or 장면"인가? (섹션명이면 다시 쓰라)
□ 마지막 슬라이드 head_copy가 슬로건 전문인가?
□ number·compare 타입 슬라이드의 evidence에 출처가 명시되어 있는가?
□ 출처 없는 수치를 확정 사실처럼 쓴 슬라이드가 없는가?
□ 리서치 데이터의 수치는 "(출처: 리서치 결과)" 또는 원본 출처로 표기했는가?"""


    # ── 청크 분할 생성 모드 (CHUNK_SIZE 초과 시 타임아웃 방지) ──────────
    if target_slides > _CHUNK_SIZE:
        _context_c  = _build_context(case_detail, 3_000, 80_000)
        _rfp_raw_s  = _build_rfp_raw_section()
        _n_chunks   = math.ceil(target_slides / _CHUNK_SIZE)

        # ── 재시작 대비: DB에서 기존 진행분 확인 ──────────────────────
        # rerun 모드(코멘트 있음)일 때는 기존 완료 체크를 건너뛰고 처음부터 재생성
        _is_rerun       = bool(_step_instruction)
        _resumed_slides = []
        _saved_count    = 0
        if case_id and not _is_rerun:
            try:
                from database.db import get_ppt_narrative as _get_ppt_narr
                _db_state = _get_ppt_narr(case_id)
                if _db_state and _db_state.get("slides"):
                    _resumed_slides = list(_db_state["slides"])
                    _saved_count    = len(_resumed_slides)
                    if _saved_count >= target_slides:
                        print(f"[PPT설계] 기존 {_saved_count}장 이미 완료 — 재생성 불필요")
                        return {
                            "slides":        _resumed_slides,
                            "total_slides":  _saved_count,
                            "content_chars": content_chars,
                            "rfp_coverage":  _db_state.get("rfp_coverage",
                                                           {"covered": [], "missing": []}),
                        }
                    print(f"[PPT설계] 기존 {_saved_count}장 발견 → {_saved_count + 1}장부터 이어서 생성")
            except Exception as _re:
                print(f"  [PPT설계] 기존 슬라이드 확인 오류: {_re}")
        elif _is_rerun:
            print(f"[PPT설계] rerun 모드 — 기존 완료 여부 무시하고 처음부터 재생성")

        _skip_chunks = _saved_count // _CHUNK_SIZE
        _all_slides  = list(_resumed_slides)
        _rfp_cov     = {"covered": [], "missing": []}

        print(f"  [PPT설계] 청크 분할 생성: {target_slides}장 → {_n_chunks}개 청크 "
              f"({_CHUNK_SIZE}장/청크, 시작={_skip_chunks + 1}번째 청크)")

        # ① 전체 목차 생성
        if push_event:
            push_event({"type": "log", "message": "✦ PPT 전체 목차 생성 중..."})
        try:
            _toc_p = _inject_instruction(
                _build_toc_prompt(_context_c, target_slides, concept, slogan,
                                  _rfp_raw_s, _perplexity_supplement)
            )
            _toc   = call_json(_toc_p, max_tokens=4000).get("toc", [])
            print(f"  [PPT설계] 목차 {len(_toc)}장 생성")
        except Exception as _te:
            print(f"  [PPT설계] 목차 생성 오류: {_te} — 단일 호출로 폴백")
            _toc = []

        if len(_toc) < target_slides:
            for _n in range(len(_toc) + 1, target_slides + 1):
                _toc.append({"number": _n, "section": "본문",
                              "head_copy": f"슬라이드 {_n}", "slide_type": "content"})
        _toc = _toc[:target_slides]
        for _idx, _t in enumerate(_toc):
            _t["number"] = _idx + 1

        # 기존 슬라이드 데이터로 TOC 앞부분 교체 (구조 일관성 유지)
        for _ri, _rs in enumerate(_resumed_slides):
            if _ri < len(_toc):
                _toc[_ri] = {
                    "number":     _rs["number"],
                    "section":    _rs.get("section",    _toc[_ri].get("section", "")),
                    "head_copy":  _rs.get("head_copy",  _toc[_ri].get("head_copy", "")),
                    "slide_type": _rs.get("slide_type", _toc[_ri].get("slide_type", "content")),
                }

        _all_toc_text = "\n".join(
            f"  {t['number']}번: [{t.get('section','')}] {t.get('head_copy','')}"
            + (" ✓완료" if t["number"] <= _saved_count else "")
            for t in _toc
        )

        # ② 청크별 내용 채우기 (완료 청크 스킵)
        _chunks = [_toc[i:i+_CHUNK_SIZE] for i in range(0, len(_toc), _CHUNK_SIZE)]

        for _ci, _chunk in enumerate(_chunks):
            _c_start = _chunk[0]["number"]
            _c_end   = _chunk[-1]["number"]

            if _ci < _skip_chunks:
                print(f"  [PPT설계] 청크 {_ci+1}/{_n_chunks}: {_c_start}~{_c_end}번 기존 데이터 있음 — 스킵")
                continue

            print(f"  [PPT설계] 청크 {_ci+1}/{_n_chunks}: {_c_start}~{_c_end}번 생성 중...")
            if push_event:
                push_event({"type": "log",
                            "message": f"✦ PPT 청크 {_ci+1}/{_n_chunks} 생성 중 ({_c_start}~{_c_end}번)..."})
            try:
                _fp = _inject_instruction(
                    _build_chunk_fill_prompt(_chunk, _all_toc_text, _context_c, _ci, _n_chunks)
                )
                _cr = call_json(_fp, max_tokens=6000)
                _chunk_slides = _cr.get("slides", [])
            except Exception as _ce:
                print(f"  [PPT설계] 청크 {_ci+1} 오류: {_ce} — TOC 항목으로 대체")
                _chunk_slides = []

            if len(_chunk_slides) < len(_chunk):
                _existing_nums = {s.get("number") for s in _chunk_slides}
                for _t in _chunk:
                    if _t["number"] not in _existing_nums:
                        _chunk_slides.append({
                            "number": _t["number"], "section": _t.get("section",""),
                            "head_copy": _t.get("head_copy",""), "key_message": "",
                            "evidence": "", "rfp_tags": [],
                            "slide_type": _t.get("slide_type","content"),
                        })
            _chunk_slides.sort(key=lambda s: s.get("number", 0))
            for _ji, _s in enumerate(_chunk_slides[:len(_chunk)]):
                _s["number"] = _chunk[_ji]["number"]
            _all_slides.extend(_chunk_slides[:len(_chunk)])

            print(f"  [PPT설계] 청크 {_ci+1}/{_n_chunks} 완료 → 누적 {len(_all_slides)}장")

            # ── 중간 저장: 슬라이드 수가 늘었을 때만 (절대 감소 불가) ──
            if case_id and len(_all_slides) > _saved_count:
                try:
                    from database.db import save_ppt_narrative
                    save_ppt_narrative(
                        case_id=case_id, slides=list(_all_slides),
                        rfp_coverage=_rfp_cov,
                        target_slides=target_slides,
                        content_chars=content_chars,
                    )
                    _saved_count = len(_all_slides)
                    print(f"  [PPT설계] 중간 저장 완료 ({_saved_count}장, 청크 {_ci+1}/{_n_chunks})")
                except Exception as _se:
                    print(f"  [PPT설계] 중간 저장 오류: {_se}")
            elif case_id:
                print(f"  [PPT설계] 기존 {_saved_count}장 보존 — 덮어쓰기 방지")

            if push_event:
                push_event({"type": "log",
                            "message": f"[PPT설계] {_ci+1}/{_n_chunks} 완료 ({len(_all_slides)}장)"})

        _all_slides.sort(key=lambda s: s.get("number", 0))
        print(f"[PPT설계] 목표: {target_slides}장 / 실제: {len(_all_slides)}장")
        print(f"  [PPT설계] 최종 {len(_all_slides)}/{target_slides}장 확정")
        return {
            "slides":        _all_slides,
            "total_slides":  len(_all_slides),
            "content_chars": content_chars,
            "rfp_coverage":  _rfp_cov,
        }


    # ── 청크 분할 생성 모드 (CHUNK_SIZE 초과 시 타임아웃 방지) ──────────
    if target_slides > _CHUNK_SIZE:
        _context_c  = _build_context(case_detail, 3_000, 80_000)
        _rfp_raw_s  = _build_rfp_raw_section()  # closure 호출
        _n_chunks   = math.ceil(target_slides / _CHUNK_SIZE)
        print(f"  [PPT설계] 청크 분할 생성: {target_slides}장 → {_n_chunks}개 청크 ({_CHUNK_SIZE}장/청크)")

        # ① 전체 목차 생성
        if push_event:
            push_event({"type": "log", "message": "✦ PPT 전체 목차 생성 중..."})
        try:
            _toc_p  = _inject_instruction(
                _build_toc_prompt(_context_c, target_slides, concept, slogan,
                                  _rfp_raw_s, _perplexity_supplement)
            )
            _toc    = call_json(_toc_p, max_tokens=4000).get("toc", [])
            print(f"  [PPT설계] 목차 {len(_toc)}장 생성")
        except Exception as _te:
            print(f"  [PPT설계] 목차 생성 오류: {_te} — 단일 호출로 폴백")
            _toc = []

        # 목차 수 보완
        if len(_toc) < target_slides:
            for _n in range(len(_toc) + 1, target_slides + 1):
                _toc.append({"number": _n, "section": "본문",
                              "head_copy": f"슬라이드 {_n}", "slide_type": "content"})
        _toc = _toc[:target_slides]
        for _idx, _t in enumerate(_toc):
            _t["number"] = _idx + 1  # 번호 재정렬

        _all_toc_text = "\n".join(
            f"  {t['number']}번: [{t.get('section','')}] {t.get('head_copy','')}"
            for t in _toc
        )

        # ② 청크별 내용 채우기
        _chunks       = [_toc[i:i+_CHUNK_SIZE] for i in range(0, len(_toc), _CHUNK_SIZE)]
        _all_slides   = []
        _rfp_cov      = {"covered": [], "missing": []}

        for _ci, _chunk in enumerate(_chunks):
            _c_start = _chunk[0]["number"]
            _c_end   = _chunk[-1]["number"]
            print(f"  [PPT설계] 청크 {_ci+1}/{_n_chunks}: {_c_start}~{_c_end}번 생성 중...")
            if push_event:
                push_event({"type": "log",
                            "message": f"✦ PPT 청크 {_ci+1}/{_n_chunks} 생성 중 ({_c_start}~{_c_end}번)..."})
            try:
                _fp = _inject_instruction(
                    _build_chunk_fill_prompt(_chunk, _all_toc_text, _context_c, _ci, _n_chunks)
                )
                _cr = call_json(_fp, max_tokens=6000)
                _chunk_slides = _cr.get("slides", [])
            except Exception as _ce:
                print(f"  [PPT설계] 청크 {_ci+1} 오류: {_ce} — TOC 항목으로 대체")
                _chunk_slides = []

            # 빈 슬라이드 보완 — TOC 항목으로 채움
            if len(_chunk_slides) < len(_chunk):
                _existing_nums = {s.get("number") for s in _chunk_slides}
                for _t in _chunk:
                    if _t["number"] not in _existing_nums:
                        _chunk_slides.append({
                            "number": _t["number"], "section": _t.get("section",""),
                            "head_copy": _t.get("head_copy",""), "key_message": "",
                            "evidence": "", "rfp_tags": [],
                            "slide_type": _t.get("slide_type","content"),
                        })
            # 번호 강제 보정
            _chunk_slides.sort(key=lambda s: s.get("number", 0))
            for _ji, _s in enumerate(_chunk_slides[:len(_chunk)]):
                _s["number"] = _chunk[_ji]["number"]
            _all_slides.extend(_chunk_slides[:len(_chunk)])

            print(f"  [PPT설계] 청크 {_ci+1}/{_n_chunks} 완료 → 누적 {len(_all_slides)}장")

            # 청크 완료 후 즉시 중간 DB 저장
            if case_id:
                try:
                    from database.db import save_ppt_narrative
                    save_ppt_narrative(
                        case_id=case_id, slides=list(_all_slides),
                        rfp_coverage=_rfp_cov,
                        target_slides=target_slides,
                        content_chars=content_chars,
                    )
                    print(f"  [PPT설계] 중간 저장 완료 ({len(_all_slides)}장, 청크 {_ci+1}/{_n_chunks})")
                except Exception as _se:
                    print(f"  [PPT설계] 중간 저장 오류: {_se}")

            if push_event:
                push_event({"type": "log",
                            "message": f"[PPT설계] {_ci+1}/{_n_chunks} 완료 ({len(_all_slides)}장)"})

        _all_slides.sort(key=lambda s: s.get("number", 0))
        print(f"[PPT설계] 목표: {target_slides}장 / 실제: {len(_all_slides)}장")
        print(f"  [PPT설계] 최종 {len(_all_slides)}/{target_slides}장 확정")
        return {
            "slides":        _all_slides,
            "total_slides":  len(_all_slides),
            "content_chars": content_chars,
            "rfp_coverage":  _rfp_cov,
        }

    # ── 3단계 재시도: 3000자 → 1500자 → 필수만 ──────────────────────────
    attempts = [
        {"step_limit": 3_000, "total_limit": 80_000, "label": "3000자/스텝"},
        {"step_limit": 1_500, "total_limit": 80_000, "label": "1500자/스텝"},
        {"step_limit": None,  "total_limit": None,   "label": "필수항목만"},
    ]

    result = None
    for i, cfg in enumerate(attempts):
        if cfg["step_limit"] is None:
            context = _build_essential_block(case_detail)
        else:
            context = _build_context(case_detail, cfg["step_limit"], cfg["total_limit"])

        print(f"  [PPT설계] 컨텍스트 {len(context):,}자 ({cfg['label']}) — API 호출 중...")
        try:
            _raw_prompt = _build_prompt(context)
            _step_instr = case_detail.get("case", {}).get("dna", {}).get("step_instruction", "")
            if _step_instr:
                _prev = case_detail.get("case", {}).get("dna", {}).get("step_prev_content", "")

                class _FakeDNA:
                    step_instruction = _step_instr
                    step_prev_content = _prev

                _raw_prompt = _wrap_instruction(_raw_prompt, _FakeDNA())
            result = call_json(_raw_prompt, max_tokens=16000)
            break
        except Exception as exc:
            if _is_context_overflow(exc) and i < len(attempts) - 1:
                print(f"  [PPT설계] 컨텍스트 초과 — {attempts[i+1]['label']}으로 재시도")
                continue
            raise

    if result is None:
        result = {}

    slides = result.get("slides", [])
    print(f"  [PPT설계] {len(slides)}/{target_slides}장 설계안 생성 완료")

    # ── 슬라이드 수 부족 시 자동 재시도 (최대 2회) ──────────────
    for _retry in range(2):
        if len(slides) >= target_slides:
            break
        n_existing = len(slides)
        shortage   = target_slides - n_existing
        print(f"  [PPT설계] 슬라이드 부족 ({n_existing}/{target_slides}) — 추가 생성 재시도 {_retry + 1}/2")
        _tail_ctx = "\n".join(
            f"  {s.get('number', n_existing - len(slides) + idx + 1)}번: "
            f"[{s.get('section', '')}] {s.get('head_copy', '')}"
            for idx, s in enumerate(slides[-5:])
        )
        _retry_prompt = (
            f"앞서 {n_existing}장만 생성됨. "
            f"{n_existing + 1}번부터 {target_slides}번까지 나머지 {shortage}장을 추가로 생성해줘.\n\n"
            f"기존 마지막 슬라이드 (참고):\n{_tail_ctx}\n\n"
            f"{n_existing + 1}번부터 {target_slides}번까지 {shortage}장을 아래 JSON 형식으로만 출력:\n"
            '{{"slides": ['
            f'{{"number": {n_existing + 1}, "section": "...", "head_copy": "...", '
            '"key_message": "...", "evidence": "...", "rfp_tags": [], "slide_type": "content"}},'
            ' ...]}'
        )
        try:
            _extra = call_json(_retry_prompt, max_tokens=8000)
            _extra_slides = _extra.get("slides", [])
            if _extra_slides:
                for _j, _s in enumerate(_extra_slides):
                    _s["number"] = n_existing + _j + 1
                slides = slides + _extra_slides
                print(f"  [PPT설계] 추가 {len(_extra_slides)}장 생성 — 현재 {len(slides)}장")
        except Exception as _re:
            print(f"  [PPT설계] 추가 생성 오류: {_re}")
            break

    missing = result.get("rfp_coverage", {}).get("missing", [])
    if missing:
        print(f"  [PPT설계] 경고: 미커버 RFP 항목 {len(missing)}개 — {missing[:3]}")
    print(f"[PPT설계] 목표: {target_slides}장 / 실제: {len(slides)}장")
    print(f"  [PPT설계] 최종 {len(slides)}/{target_slides}장 확정")

    return {
        "slides":        slides,
        "total_slides":  len(slides),
        "content_chars": content_chars,
        "rfp_coverage":  result.get("rfp_coverage", {"covered": [], "missing": []}),
    }

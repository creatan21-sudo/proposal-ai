# core/pipeline.py
# 역할: 에이전트 순차 실행 파이프라인 (인터랙티브 모드)
# - STEP 0 → 0.5 → 1~7 에이전트 순서 실행
# - 각 스텝 완료 후 핵심 결과 출력 → 사용자 컨펌 대기
# - 수정 지시 입력 시 해당 스텝 재실행
# - STEP 3 컨셉 선택/수정 후 STEP 4~7 자동 재실행
# - --concept: STEP 3 스킵 (컨셉 외부 주입)
# - --start-step: 지정 스텝부터 재실행 (prior_results 재사용)

import time
import textwrap
import traceback

_STEP_DELAY_SEC = 3   # 에이전트 간 호출 딜레이 (rate limit 완화)

from core.dna import ConceptDNA
from agents import (
    rfp_parser, narrator, researcher, strategist,
    creative, planner, scripter, marketer, orchestrator,
)


# ─────────────────────────────────────────────
# 스텝 정의
# ─────────────────────────────────────────────

# (step_key, display_name, step_num_label, agent_module, critical)
_STEPS = [
    ("rfp_analysis",   "STEP 0    RFP 분석",          "0",   rfp_parser,   True),
    ("narrative",      "STEP 0.5  전략 내러티브",      "0.5", narrator,     False),
    ("research",       "STEP 1    발주처 리서치",       "1",   researcher,   False),
    ("strategy",       "STEP 2    전략 수립",           "2",   strategist,   True),
    ("creative",       "STEP 3    컨셉 개발",           "3",   creative,     True),
    ("plan",           "STEP 4    실행 기획",           "4",   planner,      True),
    ("script",         "STEP 5    대본 제작",           "5",   scripter,     True),
    ("marketing",      "STEP 6    마케팅 전략",         "6",   marketer,     False),
    ("final_proposal", "STEP 7    최종 검수·완성",      "7",   orchestrator, True),
]

# step_key → 인덱스 매핑
_STEP_INDEX = {entry[0]: idx for idx, entry in enumerate(_STEPS)}

# 스텝 번호 라벨 → step_key 매핑 (재실행 메뉴용)
STEP_NUM_TO_KEY = {entry[2]: entry[0] for entry in _STEPS}

# STEP 3 이후 downstream 스텝 키
_DOWNSTREAM_OF_CREATIVE = ["plan", "script", "marketing", "final_proposal"]


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────

def run_pipeline(
    dna: ConceptDNA,
    rfp_file: str = None,
    concept: str = None,
    start_step_key: str = None,
    pages: int = 30,
    prior_results: dict = None,
) -> dict:
    """전체 멀티에이전트 파이프라인 실행 (인터랙티브).

    Args:
        dna: 초기 ConceptDNA (사용자 입력 포함)
        rfp_file: RFP 파일 경로 (HWP/HWPX/PDF/TXT, 선택)
        concept: 미리 정해진 컨셉 문자열 → STEP 3 스킵
        start_step_key: 이 스텝 키부터 시작 (재실행용)
        pages: 목표 제안서 페이지 수 (DNA에 주입)
        prior_results: 이전 파이프라인 결과 (재실행 시 앞 스텝 결과 재사용)

    Returns:
        각 에이전트 출력을 담은 결과 dict
    """
    # pages → DNA 주입
    dna.pages = pages

    # 컨셉 외부 주입 → STEP 3 스킵
    if concept:
        dna.concept = concept
        _print_info(f"컨셉 외부 주입: [{concept}] — STEP 3 스킵")

    _print_header(dna)

    # 이전 결과 재사용 (재실행 시)
    results: dict = dict(prior_results) if prior_results else {}

    # 시작 인덱스 결정
    start_idx = 0
    if start_step_key and start_step_key in _STEP_INDEX:
        start_idx = _STEP_INDEX[start_step_key]
        _print_info(f"{_STEPS[start_idx][1]} 부터 재실행합니다.")

    pipeline_start = time.time()
    i = start_idx

    while i < len(_STEPS):
        step_key, display_name, _, agent_mod, critical = _STEPS[i]

        # concept 외부 주입 시 STEP 3 스킵
        if step_key == "creative" and concept:
            _print_info("STEP 3  컨셉 개발 — 외부 컨셉 사용으로 스킵")
            results["creative"] = {"concept": concept}
            i += 1
            continue

        # ── 스텝 실행 ──────────────────────────
        result = _execute_step(
            step_key=step_key,
            display_name=display_name,
            agent_mod=agent_mod,
            dna=dna,
            critical=critical,
            rfp_file=rfp_file if step_key == "rfp_analysis" else None,
            prior_results=results,
        )

        # 실행 실패 처리
        if result is None:
            if critical:
                _print_abort(display_name)
                results["__aborted_at__"] = step_key
                return results
            results[step_key] = {}
            i += 1
            continue

        results[step_key] = result

        # ── 인터랙티브 컨펌 ──────────────────────
        try:
            if step_key == "narrative":
                rerun = _confirm_narrative(dna, result)
                if rerun:
                    continue

            elif step_key == "creative" and not concept:
                creative_modified, jump_back = _confirm_creative(dna, result)
                if jump_back:
                    continue
                if creative_modified:
                    _print_info("컨셉이 변경되었습니다. STEP 4~7을 재실행합니다.")
                    for k in _DOWNSTREAM_OF_CREATIVE:
                        results.pop(k, None)

            else:
                rerun = _confirm_step(step_key, display_name, dna, result)
                if rerun:
                    continue

        except KeyboardInterrupt:
            print("\n\n[중단] 사용자가 파이프라인을 종료했습니다.")
            results["__aborted_at__"] = step_key
            return results

        # 다음 스텝으로
        dna.user_feedback = ""
        if step_key != _STEPS[-1][0]:
            time.sleep(_STEP_DELAY_SEC)
        i += 1

    elapsed = time.time() - pipeline_start
    _print_footer(elapsed)

    # 최종 제안서 키 정규화
    orchestrator_out = results.get("final_proposal", {})
    if isinstance(orchestrator_out, dict) and "final_proposal" in orchestrator_out:
        results["final_proposal"] = orchestrator_out["final_proposal"]

    return results


# ─────────────────────────────────────────────
# 스텝 실행
# ─────────────────────────────────────────────

def _execute_step(
    step_key: str,
    display_name: str,
    agent_mod,
    dna: ConceptDNA,
    critical: bool,
    rfp_file: str = None,
    prior_results: dict = None,
) -> dict | None:
    _print_step_start(display_name)
    step_start = time.time()
    try:
        if step_key == "rfp_analysis":
            result = agent_mod.run(dna, file_path=rfp_file)
        elif step_key == "final_proposal":
            result = agent_mod.run(dna, pipeline_results=prior_results)
        else:
            result = agent_mod.run(dna)

        elapsed = time.time() - step_start
        _print_step_done(display_name, elapsed)
        return result

    except KeyboardInterrupt:
        raise

    except Exception as e:
        elapsed = time.time() - step_start
        _print_step_error(display_name, e, elapsed, critical)
        if critical:
            traceback.print_exc()
        return None


# ─────────────────────────────────────────────
# 인터랙티브 컨펌
# ─────────────────────────────────────────────

def _confirm_step(step_key: str, display_name: str, dna: ConceptDNA, result: dict) -> bool:
    """일반 스텝 결과 요약 출력 후 사용자 컨펌.

    Returns:
        True  → 수정 지시 입력됨, 동일 스텝 재실행 필요
        False → 'y' 입력, 다음 스텝으로 진행
    """
    _print_summary(step_key, dna, result)

    print(f"\n{'─' * 60}")
    print("  계속 진행할까요?")
    print("  [y] 다음 스텝  /  [수정 내용 입력] 이 스텝 재실행")
    print(f"{'─' * 60}")

    while True:
        try:
            user_input = input("  > ").strip()
        except EOFError:
            return False

        if user_input.lower() in ("y", "yes", "예", "ㅇ", ""):
            return False
        if user_input:
            dna.user_feedback = user_input
            _print_info(f"수정 지시 반영 후 [{display_name}] 재실행합니다...")
            return True


def _confirm_narrative(dna: ConceptDNA, result: dict) -> bool:
    """STEP 0.5 전략 내러티브 확인 및 재생성.

    Returns:
        True  → 수정 지시 입력됨, 재생성 필요
        False → 'y' 또는 Enter, 다음 스텝으로 진행
    """
    narrative = result.get("narrative", "")

    print(f"\n{'═' * 60}")
    print("  ◆ STEP 0.5  전략 내러티브")
    print(f"{'═' * 60}")
    if narrative:
        print()
        print(narrative)
    print()

    # DNA에 내러티브 저장
    dna.narrative = narrative

    print(f"\n{'─' * 60}")
    print("  이 전략 방향으로 진행할까요?")
    print("  [y] 다음 스텝  /  [수정 방향 입력] 내러티브 재생성")
    print(f"{'─' * 60}")

    while True:
        try:
            user_input = input("  > ").strip()
        except EOFError:
            return False

        if user_input.lower() in ("y", "yes", "예", "ㅇ", ""):
            return False
        if user_input:
            dna.user_feedback = user_input
            _print_info("수정 지시 반영 후 [STEP 0.5  전략 내러티브] 재생성합니다...")
            return True


def _confirm_creative(dna: ConceptDNA, result: dict) -> tuple[bool, bool]:
    """STEP 3 전용: 슬로건 선택 또는 수정 방향 입력.

    Returns:
        (creative_modified, jump_back)
        creative_modified: 슬로건/컨셉이 원본에서 변경됐는지
        jump_back: True → 수정 지시 입력, 같은 스텝 재실행 필요
    """
    _print_creative_summary(dna, result)

    slogans = dna.slogans or []
    n = len(slogans)

    print(f"\n{'─' * 60}")
    print("  컨셉을 확정하세요.")
    if n > 0:
        print(f"  [{'/'.join(str(i+1) for i in range(n))}] 슬로건 번호 선택")
    print("  [y] 현재 1순위 유지")
    print("  [수정 방향 입력] 컨셉 재생성 (STEP 4~7 재실행)")
    print(f"{'─' * 60}")

    original_concept = dna.concept
    original_slogan  = dna.slogan

    while True:
        try:
            user_input = input("  > ").strip()
        except EOFError:
            return False, False

        if user_input.lower() in ("y", "yes", "예", "ㅇ", ""):
            return False, False

        if user_input.isdigit():
            idx = int(user_input) - 1
            if 0 <= idx < n:
                chosen = slogans[idx]
                new_slogan = chosen.get("text", chosen) if isinstance(chosen, dict) else str(chosen)
                dna.slogan = new_slogan
                _print_info(f"슬로건 {idx+1}번 선택: {new_slogan}")
                modified = (dna.slogan != original_slogan or dna.concept != original_concept)
                return modified, False
            else:
                print(f"  1~{n} 사이의 숫자를 입력하세요.")
                continue

        if user_input:
            dna.user_feedback = user_input
            _print_info("수정 지시 반영 후 [STEP 3  컨셉 개발] 재실행합니다...")
            return False, True


# ─────────────────────────────────────────────
# 스텝별 결과 요약 출력
# ─────────────────────────────────────────────

def _print_summary(step_key: str, dna: ConceptDNA, result: dict) -> None:
    _fn = {
        "rfp_analysis":   _summary_rfp,
        "research":       _summary_research,
        "strategy":       _summary_strategy,
        "plan":           _summary_plan,
        "script":         _summary_script,
        "marketing":      _summary_marketing,
        "final_proposal": _summary_final,
    }.get(step_key)
    if _fn:
        _fn(dna, result)


def _summary_rfp(dna: ConceptDNA, result: dict) -> None:
    _box("STEP 0  RFP 분석 결과")
    _kv("발주처",    dna.client_name)
    _kv("사업명",    dna.project_name)
    _kv("기관 유형", dna.agency_type or result.get("agency_type", "-"))
    _kv("예산",      dna.budget or "-")
    _kv("납품기한",  dna.deadline or "-")
    items = dna.evaluation_items or result.get("evaluation_items", [])
    if items:
        print("  ┌ 평가항목")
        for it in items[:6]:
            name  = it.get("item", it) if isinstance(it, dict) else str(it)
            score = it.get("score", "") if isinstance(it, dict) else ""
            print(f"  │  • {name}" + (f"  [{score}점]" if score else ""))
        print("  └")
    kws = dna.evaluation_keywords or result.get("top_keywords", [])
    if kws:
        _kv("핵심 키워드", "  /  ".join(kws[:8]))


def _summary_research(dna: ConceptDNA, result: dict) -> None:
    _box("STEP 1  발주처 리서치 결과")
    _kv("기관 특성", _truncate(dna.agency_characteristics or "-", 80))
    issues = dna.recent_issues or result.get("recent_issues", [])
    if issues:
        print("  ┌ 최근 이슈")
        for iss in issues[:4]:
            print(f"  │  • {_truncate(str(iss), 70)}")
        print("  └")
    for key in ("real_needs", "top_three_wants", "attack_points"):
        val = result.get(key)
        if val:
            label = {"real_needs": "진짜 니즈", "top_three_wants": "Top 3 원하는 것",
                     "attack_points": "공략 포인트"}[key]
            _kv(label, _truncate(str(val) if not isinstance(val, list) else " / ".join(str(v) for v in val[:3]), 80))


def _summary_strategy(dna: ConceptDNA, result: dict) -> None:
    _box("STEP 2  전략 수립 결과")
    _kv("핵심 문제",   _truncate(dna.core_problem or "-", 80))
    _kv("위기 제시",   _truncate(dna.crisis_statement or "-", 80))
    _kv("현황 진단",   _truncate(dna.current_situation or "-", 80))
    _kv("해결책 방향", _truncate(dna.solution_direction or "-", 80))
    effects = dna.expected_effects or result.get("expected_effects", [])
    if effects:
        print("  ┌ 기대 효과")
        for ef in effects[:3]:
            print(f"  │  • {_truncate(str(ef), 70)}")
        print("  └")


def _print_creative_summary(dna: ConceptDNA, result: dict) -> None:
    _box("STEP 3  컨셉 개발 결과")
    _kv("핵심 컨셉",   dna.concept or "-")
    _kv("컨셉 설명",   _truncate(dna.concept_description or "-", 80))
    _kv("톤앤매너",    dna.tone_and_manner or "-")
    kws = dna.tone_keywords or []
    if kws:
        _kv("감성 키워드", "  /  ".join(kws))

    slogans = dna.slogans or []
    if slogans:
        print()
        print("  ┌ 슬로건 후보")
        for i, s in enumerate(slogans, 1):
            text      = s.get("text", s) if isinstance(s, dict) else str(s)
            rationale = s.get("rationale", "") if isinstance(s, dict) else ""
            marker = "★" if i == 1 else "☆"
            print(f"  │  {i}. {marker}  {text}")
            if rationale:
                print(f"  │      └ {_truncate(rationale, 65)}")
        print("  └")


def _summary_plan(dna: ConceptDNA, result: dict) -> None:
    _box("STEP 4  실행 기획 결과")
    episodes = dna.episodes or result.get("episodes", [])
    if episodes:
        print("  ┌ 편별 제작 계획")
        for ep in episodes[:5]:
            if isinstance(ep, dict):
                num   = ep.get("ep_num", ep.get("num", ""))
                title = ep.get("title", "")
                msg   = ep.get("key_message", ep.get("message", ""))
                print(f"  │  {num}편. {title}")
                if msg:
                    print(f"  │      └ {_truncate(msg, 65)}")
            else:
                print(f"  │  • {ep}")
        print("  └")
    schedule = dna.production_schedule or result.get("production_schedule", [])
    if schedule:
        phases = [p.get("phase", p.get("stage", str(p))) if isinstance(p, dict) else str(p)
                  for p in schedule[:4]]
        _kv("제작 단계", " → ".join(phases))
    if dna.budget_plan:
        total = dna.budget_plan.get("total", "")
        if total:
            _kv("총 예산", str(total))


def _summary_script(dna: ConceptDNA, result: dict) -> None:
    _box("STEP 5  대본 제작 결과")
    scripts = dna.scripts or result.get("scripts", [])
    _kv("총 대본 수", f"{len(scripts)}편")
    for sc in scripts[:3]:
        if not isinstance(sc, dict):
            continue
        num    = sc.get("ep_num", sc.get("episode", ""))
        title  = sc.get("title", "")
        scenes = sc.get("scenes", [])
        print(f"  │  {num}편 [{title}]  — {len(scenes)}씬")
        if scenes:
            first = scenes[0]
            narr  = (first.get("narration", first.get("dialogue", ""))
                     if isinstance(first, dict) else str(first))
            if narr:
                print(f"  │      └ 오프닝: {_truncate(narr, 60)}")
    if dna.has_shortform:
        _kv("숏폼 버전", "포함")


def _summary_marketing(dna: ConceptDNA, result: dict) -> None:
    _box("STEP 6  마케팅 전략 결과")
    _kv("전략 요약", _truncate(dna.distribution_strategy or "-", 80))
    channels = dna.distribution_channels or result.get("distribution_channels", [])
    if channels:
        ch_names = []
        for ch in channels[:5]:
            name = ch.get("channel", ch.get("platform", str(ch))) if isinstance(ch, dict) else str(ch)
            ch_names.append(name)
        _kv("주요 채널", "  /  ".join(ch_names))
    kpis = dna.kpi_targets or result.get("kpi_targets", [])
    if kpis:
        print("  ┌ KPI 목표")
        for kpi in kpis[:4]:
            if isinstance(kpi, dict):
                metric = kpi.get("metric", kpi.get("name", ""))
                target = kpi.get("target", kpi.get("value", ""))
                print(f"  │  • {metric}: {target}")
            else:
                print(f"  │  • {kpi}")
        print("  └")


def _summary_final(dna: ConceptDNA, result: dict) -> None:
    _box("STEP 7  최종 검수 결과")
    score = result.get("consistency_score", result.get("score", 0))
    _kv("일관성 점수", f"{score:.2f} / 1.00" if isinstance(score, float) else str(score))
    issues = result.get("issues", result.get("improvement_suggestions", []))
    if issues:
        print("  ┌ 개선 포인트")
        for iss in issues[:4]:
            print(f"  │  • {_truncate(str(iss), 70)}")
        print("  └")
    coverage = result.get("evaluation_coverage", {})
    if isinstance(coverage, dict) and coverage:
        covered   = sum(1 for v in coverage.values() if v)
        total_cnt = len(coverage)
        _kv("평가항목 커버율", f"{covered}/{total_cnt}")


# ─────────────────────────────────────────────
# 콘솔 출력 헬퍼
# ─────────────────────────────────────────────

_LINE  = "─" * 60
_LINE2 = "═" * 60


def _print_header(dna: ConceptDNA) -> None:
    print(f"\n{_LINE2}")
    print("  제안서 자동생성 파이프라인 시작  [인터랙티브 모드]")
    print(f"  발주처: {dna.client_name}  /  사업명: {dna.project_name}")
    print(f"  목표 페이지: {dna.pages}페이지")
    print(f"{_LINE2}\n")


def _print_step_start(display_name: str) -> None:
    print(f"\n{_LINE}")
    print(f"▶ {display_name} 실행 중...")


def _print_step_done(display_name: str, elapsed: float) -> None:
    print(f"✓ {display_name} 완료  ({elapsed:.1f}s)")


def _print_step_error(display_name: str, exc: Exception, elapsed: float, critical: bool) -> None:
    severity = "오류 (중단)" if critical else "경고 (계속 진행)"
    print(f"✗ {display_name} {severity}  ({elapsed:.1f}s)")
    print(f"  사유: {exc}")


def _print_abort(display_name: str) -> None:
    print(f"\n{'!' * 60}")
    print(f"  파이프라인 중단: {display_name} 실패")
    print(f"{'!' * 60}\n")


def _print_footer(elapsed: float) -> None:
    m, s = divmod(int(elapsed), 60)
    print(f"\n{_LINE2}")
    print(f"  파이프라인 완료  총 소요시간: {m}분 {s}초")
    print(f"{_LINE2}\n")


def _print_info(msg: str) -> None:
    print(f"  ℹ  {msg}")


def _box(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  ◆ {title}")
    print(f"{'─' * 60}")


def _kv(label: str, value: str) -> None:
    label_str = f"  {label:<12}"
    wrapped   = textwrap.fill(str(value), width=58,
                               subsequent_indent=" " * 15)
    print(f"{label_str}  {wrapped}")


def _truncate(text: str, max_len: int) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= max_len else text[:max_len - 1] + "…"

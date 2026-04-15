# web_pipeline.py
# 역할: Flask 웹 서비스용 파이프라인 실행기
# - input() 대신 push_event/wait_confirm 콜백으로 SSE + 사용자 입력 처리

import time
from core.dna import ConceptDNA
from core.claude_client import set_retry_callback, clear_retry_callback, OverloadError
from agents import (
    rfp_parser, narrator, researcher, strategist,
    creative, planner, scripter, marketer, orchestrator,
)

_STEPS = [
    ("rfp_analysis",      "STEP 0    RFP 분석",       rfp_parser,   True),
    ("research",          "STEP 1    리서치",           researcher,   False),
    ("narrative",         "STEP 1.5  내러티브",         narrator,     False),
    ("strategy",          "STEP 2    전략 수립",        strategist,   True),
    ("creative",          "STEP 3    컨셉 개발",        creative,     True),
    ("plan",              "STEP 4    실행 기획",        planner,      True),
    ("script",            "STEP 5    대본 제작",        scripter,     True),
    ("marketing",         "STEP 6    마케팅 전략",      marketer,     False),
    ("final_proposal",    "STEP 7    최종 검수·완성",   orchestrator, True),
    ("improvement_report","STEP 7.5  개선 제안",        None,         False),
]

# 컨펌 없이 자동 진행하는 스텝 (결과 표시 후 즉시 다음 스텝)
_AUTO_CONTINUE_STEPS = {"improvement_report"}

_STEP_INDEX          = {k: i for i, (k, *_) in enumerate(_STEPS)}
_DOWNSTREAM_CREATIVE = ["plan", "script", "marketing", "final_proposal"]

# 파이프라인 레벨 재시도 (OverloadError 발생 시)
_PIPE_RETRY_MAX  = 3
_PIPE_RETRY_WAIT = 120   # 2분


def run(dna: ConceptDNA, push_event, wait_confirm,
        rfp_file=None, concept=None,
        start_step_key=None, prior_results=None,
        notify_fn=None):
    """
    push_event(dict)          — SSE 이벤트 전송 (non-blocking)
    wait_confirm(step_key)→str — 사용자 입력 대기 (blocking)
        반환: 'y'|''=계속  '__abort__'=중단  '1'..'N'=슬로건 선택  기타=수정지시
    notify_fn(message)        — 텔레그램 알림 (optional, None이면 생략)
    """
    if concept:
        dna.concept = concept

    results = dict(prior_results) if prior_results else {}

    # 이미 완료된 파이프라인 재실행 방지
    if results.get("__pipeline_complete__"):
        push_event({"type": "pipeline_done"})
        return results
    start_idx = _STEP_INDEX.get(start_step_key, 0) if start_step_key else 0

    # 이번 실행에서 각 스텝 실행 횟수 추적 (최대 1회 제한)
    step_executed: dict = {}
    # 스킵 모드: 이전 스텝을 스킵한 경우 다음 스텝 실행 전 확인 요청
    _skip_mode = False

    i = start_idx
    while i < len(_STEPS):
        step_key, step_name, agent_mod, critical = _STEPS[i]

        # 이번 실행에서 이미 실행한 스텝은 무조건 스킵 (피드백 재실행 방지)
        if step_executed.get(step_key, 0) >= 1:
            i += 1
            continue

        # prior_results에 결과 있으면 스킵 (이전 실행 결과 재사용)
        if step_key in results and results[step_key]:
            push_event({"type": "step_skip", "step": step_key,
                        "name": step_name + " (이전 결과 사용)"})
            i += 1
            continue

        # 스킵 모드: 다음 스텝 실행 전 실행/건너뛰기 확인
        if _skip_mode:
            push_event({
                "type":       "confirm_needed",
                "step":       step_key,
                "name":       step_name,
                "is_pre_run": True,
                "slogans":    [],
            })
            pre_input = wait_confirm(step_key + "_pre")
            if pre_input == "__abort__":
                push_event({"type": "pipeline_aborted", "step": step_key})
                results["__aborted_at__"] = step_key
                return results
            if pre_input == "s":
                push_event({"type": "step_skipped", "step": step_key, "name": step_name})
                i += 1
                continue
            # y 또는 기타 입력 → 스킵 모드 해제 후 정상 실행
            _skip_mode = False

        # concept 주입 시 STEP 3 스킵 (단, DB에는 최소 레코드 저장)
        if step_key == "creative" and concept:
            results["creative"] = {"concept": concept}
            try:
                from database.db import save_creative
                save_creative(dna.client_name, dna.project_name, {
                    "concept":               concept,
                    "concept_description":   dna.concept_description or "",
                    "confirmed_slogan":      dna.slogan or "",
                    "slogans":               dna.slogans or [],
                    "tone_keywords":         dna.tone_keywords or [],
                    "tone_description":      dna.tone_and_manner or "",
                    "forbidden_expressions": dna.forbidden_expressions or [],
                    "visual_direction":      dna.visual_direction or "",
                    "agency_type":           dna.agency_type or "",
                }, case_id=getattr(dna, "case_id", 0) or 0)
            except Exception as _e:
                print(f"  [경고] 크리에이티브 스킵 DB 저장 실패: {_e}")
            push_event({"type": "step_skip", "step": step_key, "name": step_name})
            i += 1
            continue

        # ── 스텝 실행 (파이프라인 레벨 재시도 포함)
        push_event({"type": "step_start", "step": step_key, "name": step_name})

        def _api_retry_cb(attempt, max_retries, status_code, wait_sec):
            push_event({
                "type":        "api_retry",
                "step":        step_key,
                "attempt":     attempt,
                "max_retries": max_retries,
                "status_code": status_code,
                "wait_sec":    wait_sec,
            })

        pipe_exc = None
        elapsed  = 0.0

        # improvement_report: DB/AI 호출 없이 final_proposal 결과에서 인라인 생성
        if step_key == "improvement_report":
            fp = results.get("final_proposal", {})
            result = {
                "issues":              fp.get("issues", []),
                "evaluation_coverage": fp.get("evaluation_coverage", {}),
                "consistency_score":   fp.get("consistency_score", 0),
            }
            elapsed = 0.0
            pipe_exc = None
            # 재시도 루프를 통하지 않고 직접 처리
            step_executed[step_key] = 1
            results[step_key] = result
            _push_summary(push_event, step_key, step_name, elapsed,
                          _build_summary(step_key, dna, result))
            i += 1
            continue

        # script: 대본 생성 전 편수 확인
        if step_key == "script":
            ep_count = len(getattr(dna, "episodes", []) or []) or getattr(dna, "quantity", 3) or 3
            push_event({
                "type":          "episode_count_needed",
                "step":          "script",
                "default_count": min(ep_count, 3),
                "max_count":     ep_count,
            })
            ep_input = wait_confirm("script_episode_count")
            if ep_input == "__abort__":
                push_event({"type": "pipeline_aborted", "step": step_key})
                results["__aborted_at__"] = step_key
                return results
            try:
                _max_ep = max(1, int(str(ep_input).strip()))
            except (ValueError, TypeError):
                _max_ep = min(ep_count, 3)
        else:
            _max_ep = 0  # 제한 없음 (scripter 기본값)

        for pipe_attempt in range(1, _PIPE_RETRY_MAX + 1):
            t0 = time.time()
            set_retry_callback(_api_retry_cb)
            try:
                if step_key == "rfp_analysis":
                    result = agent_mod.run(dna, file_path=rfp_file)
                elif step_key == "final_proposal":
                    result = agent_mod.run(dna, pipeline_results=results)
                elif step_key == "script":
                    result = agent_mod.run(dna, progress_fn=push_event, max_episodes=_max_ep)
                else:
                    result = agent_mod.run(dna)
                elapsed = round(time.time() - t0, 1)
                clear_retry_callback()
                pipe_exc = None
                # 스텝별 사전 지시는 한 번 사용 후 초기화
                dna.step_instruction = ""
                break   # 성공

            except OverloadError as e:
                clear_retry_callback()
                pipe_exc = e
                if pipe_attempt < _PIPE_RETRY_MAX:
                    push_event({
                        "type":         "step_retry_wait",
                        "step":         step_key,
                        "name":         step_name,
                        "attempt":      pipe_attempt,
                        "max_attempts": _PIPE_RETRY_MAX,
                        "wait_sec":     _PIPE_RETRY_WAIT,
                    })
                    time.sleep(_PIPE_RETRY_WAIT)
                    push_event({"type": "step_start", "step": step_key, "name": step_name})
                # else: 마지막 시도도 실패 → 루프 종료, pipe_exc 유지

            except Exception as e:
                clear_retry_callback()
                pipe_exc = e
                break   # 과부하 외 오류는 즉시 포기

        if pipe_exc is not None:
            elapsed = round(time.time() - t0, 1)
            push_event({
                "type": "step_error", "step": step_key,
                "name": step_name, "message": str(pipe_exc), "elapsed": elapsed,
            })
            if notify_fn:
                try:
                    notify_fn(f"❌ {dna.project_name} — {step_name.strip()} 오류 발생\n{str(pipe_exc)[:200]}")
                except Exception:
                    pass
            if critical:
                push_event({"type": "pipeline_aborted", "step": step_key})
                results["__aborted_at__"] = step_key
                return results
            results[step_key] = {}
            i += 1
            continue

        results[step_key] = result
        step_executed[step_key] = step_executed.get(step_key, 0) + 1

        _push_summary(push_event, step_key, step_name, elapsed,
                      _build_summary(step_key, dna, result))

        if notify_fn:
            mins, secs = divmod(int(elapsed), 60)
            elapsed_str = f"{mins}분 {secs}초" if mins else f"{secs}초"
            try:
                notify_fn(f"✅ {dna.project_name} — {step_name.strip()} 완료 (소요: {elapsed_str})")
            except Exception:
                pass

        # ── 컨펌 요청
        push_event({
            "type":        "confirm_needed",
            "step":        step_key,
            "name":        step_name,
            "is_creative": (step_key == "creative" and not concept),
            "slogans": [
                {
                    "text":      s.get("text", str(s)) if isinstance(s, dict) else str(s),
                    "rationale": (s.get("rationale", "")[:120] if isinstance(s, dict) else ""),
                }
                for s in (dna.slogans or [])
            ] if step_key == "creative" else [],
        })

        user_input = wait_confirm(step_key)

        if user_input == "__abort__":
            push_event({"type": "pipeline_aborted", "step": step_key})
            results["__aborted_at__"] = step_key
            return results

        # ── 스킵 처리 (실행 결과는 보존, 다음 스텝 실행 전 확인 모드 진입)
        if user_input == "s":
            push_event({"type": "step_skipped", "step": step_key, "name": step_name})
            results[step_key] = result  # 결과 보존
            dna.user_feedback = ""
            _skip_mode = True           # 연속 스킵 선택 모드 활성화
            i += 1
            continue

        # ── creative 분기
        if step_key == "creative" and not concept:
            if user_input.isdigit():
                idx = int(user_input) - 1
                slogans = dna.slogans or []
                if 0 <= idx < len(slogans):
                    s = slogans[idx]
                    dna.slogan = s.get("text", str(s)) if isinstance(s, dict) else str(s)
                    push_event({"type": "slogan_selected", "slogan": dna.slogan})
                # 다운스트림 재실행 없이 다음 스텝
            elif user_input not in ("y", "yes", "예", "ㅇ", ""):
                dna.user_feedback = user_input
                push_event({"type": "step_rerun", "step": step_key, "name": step_name})
                for k in _DOWNSTREAM_CREATIVE:
                    results.pop(k, None)
                continue
        else:
            if user_input not in ("y", "yes", "예", "ㅇ", ""):
                dna.user_feedback = user_input
                push_event({"type": "step_rerun", "step": step_key, "name": step_name})
                continue

        dna.user_feedback = ""
        i += 1

    results["__pipeline_complete__"] = True
    if notify_fn:
        try:
            notify_fn(f"🎉 {dna.project_name} 제안서 생성 완료!\n결과 확인: /history")
        except Exception:
            pass
    return results


# ─────────────────────────────────────────────
# 요약 데이터 빌더 (프론트엔드 표시용)
# ─────────────────────────────────────────────

_CHUNK_SIZE = 4096  # 한 SSE 이벤트당 최대 텍스트 길이


def _push_summary(push_event, step_key: str, step_name: str,
                  elapsed: float, summary: dict) -> None:
    """step_done 이벤트 전송. 4096자 초과 필드는 step_chunk 이벤트로 분리 전송."""
    base: dict = {}
    chunks_map: dict = {}  # field → [chunk2, chunk3, ...]

    for k, v in summary.items():
        if isinstance(v, str) and len(v) > _CHUNK_SIZE:
            parts = [v[i:i + _CHUNK_SIZE] for i in range(0, len(v), _CHUNK_SIZE)]
            base[k] = parts[0]
            if len(parts) > 1:
                chunks_map[k] = parts[1:]
        else:
            base[k] = v

    push_event({
        "type":       "step_done",
        "step":       step_key,
        "name":       step_name,
        "elapsed":    elapsed,
        "summary":    base,
        "has_chunks": bool(chunks_map),
    })

    for field, parts in chunks_map.items():
        for i, part in enumerate(parts):
            push_event({
                "type":  "step_chunk",
                "step":  step_key,
                "field": field,
                "text":  part,
                "final": (i == len(parts) - 1),
            })


def _build_summary(step_key: str, dna: ConceptDNA, result: dict) -> dict:
    s = {}

    if step_key == "rfp_analysis":
        s["기관 유형"] = dna.agency_type or result.get("agency_type", "")
        s["예산"]      = dna.budget or "-"
        s["납품기한"]  = dna.deadline or "-"
        items = dna.evaluation_items or result.get("evaluation_items", [])
        s["평가항목"] = [
            (f"{it.get('item', str(it))} [{it.get('score', '')}점]"
             if isinstance(it, dict) and it.get("score")
             else (it.get("item", str(it)) if isinstance(it, dict) else str(it)))
            for it in items
        ]
        s["핵심키워드"] = dna.evaluation_keywords or result.get("top_keywords", [])

    elif step_key == "narrative":
        s["내러티브"] = result.get("narrative") or dna.narrative or ""

    elif step_key == "research":
        # 12개 항목 전체 — 절삭 없이 전송
        s["① 기관 특성/정책"]    = result.get("agency_policy", "") or dna.agency_characteristics or ""
        s["② 기관장 메시지"]     = result.get("leadership_message", "")
        s["③ 기존 콘텐츠 현황"]  = result.get("existing_content", "")
        s["④ 우수사례"]          = result.get("best_cases", "")
        s["⑤ 콘텐츠 트렌드"]    = result.get("content_trends", "")
        s["⑥ 경쟁사 패턴"]      = result.get("competitor_patterns", "")
        s["⑦ 타겟 소비행태"]    = result.get("target_media_habits", "")
        s["⑧ 반응 콘텐츠"]      = result.get("target_content_preference", "")
        s["⑨ 플랫폼 패턴"]      = result.get("platform_patterns", "")
        s["⑩ 정책/법령 변화"]   = result.get("policy_changes", "")
        s["⑪ 알고리즘 동향"]    = result.get("algorithm_trends", "")
        s["⑫ 시장 단가"]        = result.get("market_pricing", "")

    elif step_key == "strategy":
        s["핵심 문제"] = dna.core_problem or ""
        s["위기 제시"] = dna.crisis_statement or ""
        s["현황 진단"] = dna.current_situation or ""
        effects = dna.expected_effects or result.get("expected_effects", [])
        s["기대 효과"] = [str(e) for e in effects]

    elif step_key == "creative":
        s["핵심 컨셉"]   = dna.concept or ""
        s["컨셉 설명"]   = dna.concept_description or ""
        s["톤앤매너"]    = dna.tone_and_manner or ""
        slogans = dna.slogans or []
        s["슬로건 후보"] = [
            f"{i+1}. {sl.get('text', str(sl)) if isinstance(sl, dict) else str(sl)}"
            for i, sl in enumerate(slogans)
        ]

    elif step_key == "plan":
        episodes = dna.episodes or result.get("episodes", [])
        s["편별 계획"] = [
            f"{ep.get('episode_number', ep.get('ep_num', i+1))}편. "
            f"{ep.get('title', '')} — {ep.get('core_message', ep.get('key_message', ''))}"
            if isinstance(ep, dict) else str(ep)
            for i, ep in enumerate(episodes)
        ]
        schedule = dna.production_schedule or result.get("production_schedule", [])
        s["제작 단계"] = " → ".join(
            p.get("phase", str(p)) if isinstance(p, dict) else str(p)
            for p in schedule
        )

    elif step_key == "script":
        scripts = dna.scripts or result.get("scripts", [])
        s["대본 수"] = f"{len(scripts)}편"
        ep_lines = []
        for i, sc in enumerate(scripts):
            if not isinstance(sc, dict):
                ep_lines.append(str(sc))
                continue
            ep_num  = sc.get('episode', sc.get('ep_num', i + 1))
            title   = sc.get('title', '')
            n_scene = len(sc.get('scenes', []))
            hook    = sc.get('opening_hook', {})
            hook_line = (hook.get('hook_line', '') if isinstance(hook, dict) else '') or ''
            line = f"{ep_num}편 [{title}] — {n_scene}씬"
            if hook_line:
                line += f"\n오프닝 훅: {hook_line}"
            ep_lines.append(line)
        s["편별 대본"] = ep_lines
        # 첫 편 핵심 메시지
        if scripts and isinstance(scripts[0], dict):
            core_msg = scripts[0].get("core_message", "")
            if core_msg:
                s["핵심 메시지 (1편)"] = core_msg

    elif step_key == "marketing":
        # result 키: "platforms" (distribution_channels 아님), "kpi" → "primary_kpi"
        channels = dna.distribution_channels or result.get("platforms", [])
        s["주요 채널"] = [
            ch.get("channel", ch.get("platform", str(ch))) if isinstance(ch, dict) else str(ch)
            for ch in channels
        ]
        kpis = dna.kpi_targets or (result.get("kpi") or {}).get("primary_kpi", [])
        s["KPI 목표"] = [
            f"{kpi.get('metric', '')}: {kpi.get('target', '')}" if isinstance(kpi, dict) else str(kpi)
            for kpi in kpis
        ]
        # 유튜브 SEO 핵심 전략
        yt = result.get("youtube_seo", {})
        if isinstance(yt, dict):
            kw = yt.get("keyword_strategy", "") or yt.get("keywords", "")
            if kw:
                s["유튜브 키워드 전략"] = kw[:300] if isinstance(kw, str) else str(kw)
        # 예산 배분 요약
        budget = result.get("marketing_budget", {})
        if isinstance(budget, dict):
            total_b = budget.get("total", budget.get("total_budget", ""))
            if total_b:
                s["마케팅 예산"] = str(total_b)

    elif step_key == "improvement_report":
        _sev_icon = {"critical": "🔴 필수 개선", "warning": "🟡 권장 개선", "info": "🔵 참고"}
        issues = result.get("issues", [])
        formatted = []
        for iss in issues:
            if not isinstance(iss, dict):
                formatted.append(str(iss))
                continue
            sev     = _sev_icon.get(iss.get("severity", ""), iss.get("severity", ""))
            section = iss.get("section", iss.get("field", ""))
            desc    = iss.get("description", iss.get("message", ""))
            suggest = iss.get("suggestion", "")
            line = sev
            if section: line += f" | {section}"
            if desc:    line += f"\n{desc}"
            if suggest: line += f"\n→ {suggest}"
            formatted.append(line.strip())
        if formatted:
            s["개선 포인트"] = formatted
        else:
            s["개선 포인트"] = ["개선 필요 항목 없음 — 모든 섹션 기준 충족"]
        cov = result.get("evaluation_coverage", {})
        if isinstance(cov, dict):
            covered = cov.get("covered", [])
            missing = cov.get("missing", [])
            total = len(covered) + len(missing)
            if total:
                s["평가항목 커버율"] = f"{len(covered)}/{total}개 커버 ({len(covered)/total:.0%})"
        score = result.get("consistency_score", 0)
        if score:
            s["일관성 점수"] = f"{score:.0%}" if isinstance(score, float) else str(score)

    elif step_key == "final_proposal":
        score = result.get("consistency_score", result.get("score", 0))
        s["일관성 점수"] = f"{score:.0%}" if isinstance(score, float) else str(score)

        # ── 평가항목 커버율 ────────────────────────
        cov = result.get("evaluation_coverage", {})
        if isinstance(cov, dict):
            covered_list = cov.get("covered", [])
            missing_list = cov.get("missing", [])
            total = len(covered_list) + len(missing_list)
            if total:
                pct = f"{len(covered_list)/total:.0%}"
                s["평가항목 커버율"] = (
                    f"평가항목 {total}개 중 {len(covered_list)}개 커버 ({pct})"
                )

        # ── 개선 포인트 (severity 아이콘 변환) ───────
        _sev_icon = {"critical": "🔴 필수 개선", "warning": "🟡 권장 개선", "info": "🔵 참고"}
        issues = result.get("issues", result.get("improvement_suggestions", []))
        formatted = []
        for iss in issues:
            if not isinstance(iss, dict):
                formatted.append(str(iss))
                continue
            sev     = _sev_icon.get(iss.get("severity", ""), iss.get("severity", ""))
            section = iss.get("section", iss.get("field", ""))
            desc    = iss.get("description", iss.get("message", ""))
            suggest = iss.get("suggestion", "")
            line    = f"{sev}"
            if section:
                line += f" | 섹션: {section}"
            if desc:
                line += f"\n내용: {desc}"
            if suggest:
                line += f"\n개선 제안: {suggest}"
            formatted.append(line)
        if formatted:
            s["개선 포인트"] = formatted

    return s

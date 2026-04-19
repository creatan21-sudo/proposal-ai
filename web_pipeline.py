# web_pipeline.py
# 역할: Flask 웹 서비스용 파이프라인 실행기
# - input() 대신 push_event/wait_confirm 콜백으로 SSE + 사용자 입력 처리

import concurrent.futures as _cf
import threading
import time
import traceback
from core.dna import ConceptDNA
from core.claude_client import set_retry_callback, clear_retry_callback, OverloadError
from agents import (
    rfp_parser, narrator, researcher, strategist,
    creative, planner, scripter, marketer, orchestrator,
    storyboard,
)

try:
    from config import OPENAI_API_KEY as _OAI_KEY_STARTUP
    print(f"[startup] OPENAI_API_KEY: {'SET' if _OAI_KEY_STARTUP else 'NOT SET'}", flush=True)
except Exception:
    pass

_STEPS = [
    ("rfp_analysis",      "STEP 1   RFP 분석",         rfp_parser,   True),
    ("research",          "STEP 2   리서치",             researcher,   False),
    ("narrative",         "STEP 3   내러티브",           narrator,     False),
    ("strategy",          "STEP 4   전략",               strategist,   True),
    ("creative",          "STEP 5   컨셉",               creative,     True),
    ("plan",              "STEP 6   기획",               planner,      True),
    ("script",            "STEP 7   대본",               scripter,     False),
    ("storyboard",        "STEP 8   스토리보드",         storyboard,   False),
    ("platform",          "STEP 9   플랫폼 운영전략",   marketer,     False),
    ("marketing",         "STEP 10  마케팅/홍보 전략",  marketer,     False),
    ("final_proposal",    "STEP 11  PT/Q&A",            orchestrator, True),
    ("improvement_report","STEP 12  크리틱",             None,         False),
]

# 컨펌 없이 자동 진행하는 스텝 (결과 표시 후 즉시 다음 스텝)
_AUTO_CONTINUE_STEPS = {"improvement_report"}

_STEP_INDEX          = {k: i for i, (k, *_) in enumerate(_STEPS)}
_DOWNSTREAM_CREATIVE = ["plan", "script", "platform", "marketing", "final_proposal"]

# 파이프라인 레벨 재시도 (OverloadError 발생 시)
_PIPE_RETRY_MAX  = 3     # OverloadError: 최대 3회 시도 (2회 재시도)
_PIPE_RETRY_WAIT = 120   # 2분

# 타임아웃 전용 최대 재시도 횟수 (OverloadError와 별도 관리)
_TIMEOUT_RETRY_MAX = 2   # 타임아웃: 최대 2회 시도 (1회 재시도)

# 비중요 스텝은 OverloadError 재시도 대기 시간을 단축
_PIPE_RETRY_WAIT_NONCRITICAL = 30   # 30초

# 스텝별 타임아웃 (초) — None이면 무제한
# Claude API 응답은 max_tokens 크기에 따라 30~120s 소요되며,
# call_json 내부 JSON 재시도·품질 재시도까지 합산하면 3배까지 늘어날 수 있음.
# 300s = 5분: 어떤 스텝도 정상 동작 시 넘기지 않을 안전 상한값.
_STEP_TIMEOUT: dict = {
    "research":       300,   # Claude 10항목 분석 ~200s + 여유분
    "strategy":       300,   # call_json + validate 재시도 최대 ~180s
    "creative":       300,
    "plan":           300,
    "script":         300,
    "platform":       300,   # 2개 섹션 병렬 (youtube + sns)
    "marketing":      300,   # 2개 섹션 병렬 (influencer + kpi)
    "final_proposal": 300,
}


def _apply_storyboard_instruction(dna: ConceptDNA, mode: str) -> None:
    """스토리보드 모드를 step_instruction으로 주입 (스크립터 프롬프트에 반영)."""
    if mode == "none":
        dna.step_instruction = (
            "스토리보드(씬 구성)는 작성하지 말고, 실제 런닝타임에 맞는 완성 대본(나레이션·대사·자막)만 작성하세요."
        )
    elif mode.startswith("cuts:"):
        try:
            n = int(mode.split(":")[1])
            dna.step_instruction = f"씬(컷) 수를 편당 {n}개로 제한하세요. 컷 수를 지키되 각 씬은 충분한 분량으로 작성하세요."
        except (ValueError, IndexError):
            pass
    # "auto": 기본값 — step_instruction 변경 없음


def run(dna: ConceptDNA, push_event, wait_confirm,
        rfp_file=None, concept=None,
        start_step_key=None, prior_results=None,
        notify_fn=None, auto_run: bool = False,
        selected_steps=None):
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

        # selected_steps 필터: 선택되지 않은 스텝 건너뛰기
        if selected_steps is not None and step_key not in selected_steps:
            push_event({"type": "step_skipped", "step": step_key,
                        "name": step_name + " (선택 제외)"})
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

        # improvement_report(크리틱): DB/AI 호출 없이 final_proposal 결과에서 인라인 생성
        if step_key == "improvement_report":
            fp = results.get("final_proposal", {})
            result = {
                "issues":               fp.get("issues", []),
                "evaluation_coverage":  fp.get("evaluation_coverage", {}),
                "consistency_score":    fp.get("consistency_score", 0),
                "predicted_scores":     fp.get("predicted_scores", []),
                "competitive_analysis": fp.get("competitive_analysis", {}),
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

        # storyboard: 스타일 선택 후 DALL-E 생성
        if step_key == "storyboard":
            from config import OPENAI_API_KEY as _OAI_KEY
            if not _OAI_KEY:
                push_event({
                    "type": "log",
                    "message": "ℹ️ OPENAI_API_KEY 미설정 — 스토리보드 스텝 스킵",
                })
                results[step_key] = {}
                i += 1
                continue

            if auto_run:
                _sb_style = getattr(dna, "storyboard_style", "line") or "line"
                push_event({
                    "type": "log",
                    "message": f"✓ 스토리보드: 스타일={_sb_style} 자동 진행",
                })
            else:
                push_event({
                    "type":          "storyboard_style_needed",
                    "step":          "storyboard",
                    "default_style": getattr(dna, "storyboard_style", "line") or "line",
                })
                sb_input = wait_confirm("storyboard_style")
                if sb_input == "__abort__":
                    push_event({"type": "pipeline_aborted", "step": step_key})
                    results["__aborted_at__"] = step_key
                    return results
                if sb_input == "s":
                    push_event({"type": "step_skipped", "step": step_key,
                                "name": step_name + " (스킵)"})
                    results[step_key] = {}
                    i += 1
                    continue
                _sb_style = sb_input if sb_input in ("line", "color", "photo") else "line"

            import functools as _fc
            _call = _fc.partial(storyboard.run, dna, _sb_style, push_event)
            _ka_stop_sb = _keepalive_start(push_event, step_key)
            try:
                result = _call()
                elapsed = 0.0
                pipe_exc = None
            except Exception as _sb_e:
                result = {}
                pipe_exc = _sb_e
                elapsed = 0.0
            _ka_stop_sb.set()

            results[step_key] = result
            step_executed[step_key] = 1
            _push_summary(push_event, step_key, step_name, elapsed,
                          _build_summary(step_key, dna, result))

            if auto_run:
                push_event({"type": "log",
                            "message": f"✓ {step_name.strip()} 자동 완료"})
            else:
                push_event({
                    "type":        "confirm_needed",
                    "step":        step_key,
                    "name":        step_name,
                    "is_creative": False,
                    "slogans":     [],
                })
                user_input = wait_confirm(step_key)
                if user_input == "__abort__":
                    push_event({"type": "pipeline_aborted", "step": step_key})
                    results["__aborted_at__"] = step_key
                    return results

            i += 1
            continue

        # script: 대본 생성 전 편수·스토리보드 설정
        if step_key == "script":
            ep_count = len(getattr(dna, "episodes", []) or []) or getattr(dna, "quantity", 3) or 3
            preset_ep = getattr(dna, "script_preset_episodes", 0)
            preset_sb = getattr(dna, "script_preset_storyboard", "")

            if auto_run and preset_ep:
                # 자동 실행 + 사전 설정 편수 사용
                _max_ep = max(1, min(preset_ep, ep_count))
                if preset_sb:
                    _apply_storyboard_instruction(dna, preset_sb)
                push_event({"type": "log",
                            "message": f"✓ 대본: {_max_ep}편 / 스토리보드: {preset_sb or 'auto'} 자동 설정"})
            elif auto_run:
                # 자동 실행 + 사전 설정 없음 → 기본값
                _max_ep = min(ep_count, 3)
                push_event({"type": "log",
                            "message": f"✓ 대본: {_max_ep}편 (기본) 자동 진행"})
            else:
                # 인터랙티브: 다이얼로그 표시
                push_event({
                    "type":          "episode_count_needed",
                    "step":          "script",
                    "default_count": min(preset_ep or ep_count, 3),
                    "max_count":     ep_count,
                    "preset_storyboard": preset_sb or "auto",
                })
                ep_input = wait_confirm("script_episode_count")
                if ep_input == "__abort__":
                    push_event({"type": "pipeline_aborted", "step": step_key})
                    results["__aborted_at__"] = step_key
                    return results
                # 새 형식: "N|storyboard_mode"  (레거시: 숫자만)
                ep_parts = str(ep_input).strip().split("|")
                try:
                    _max_ep = max(1, int(ep_parts[0]))
                except (ValueError, TypeError):
                    _max_ep = min(ep_count, 3)
                if len(ep_parts) > 1:
                    _apply_storyboard_instruction(dna, ep_parts[1])
        else:
            _max_ep = 0  # 제한 없음 (scripter 기본값)

        # 장시간 스텝 킵얼라이브 (Railway 프록시 60s 타임아웃 방지)
        _ka_stop = _keepalive_start(push_event, step_key)

        # 비중요 스텝은 재시도 대기 시간 단축
        _retry_wait = _PIPE_RETRY_WAIT if critical else _PIPE_RETRY_WAIT_NONCRITICAL
        # 스텝별 상한 타임아웃
        _step_timeout = _STEP_TIMEOUT.get(step_key)

        for pipe_attempt in range(1, _PIPE_RETRY_MAX + 1):
            t0 = time.time()
            set_retry_callback(_api_retry_cb)
            try:
                # 에이전트별 호출 인자 구성
                import functools
                if step_key == "rfp_analysis":
                    _call = functools.partial(agent_mod.run, dna, file_path=rfp_file)
                elif step_key == "final_proposal":
                    _call = functools.partial(agent_mod.run, dna, pipeline_results=results,
                                              generate_ppt=getattr(dna, "generate_ppt", False))
                elif step_key == "script":
                    _call = functools.partial(agent_mod.run, dna, progress_fn=push_event, max_episodes=_max_ep)
                elif step_key == "platform":
                    _call = functools.partial(agent_mod.run_platform, dna, push_event)
                elif step_key == "marketing":
                    _call = functools.partial(agent_mod.run_marketing, dna, push_event)
                else:
                    _call = functools.partial(agent_mod.run, dna)

                # 스텝 타임아웃 적용 (설정된 경우 별도 스레드에서 실행)
                if _step_timeout:
                    with _cf.ThreadPoolExecutor(max_workers=1) as _tex:
                        _f = _tex.submit(_call)
                        result = _f.result(timeout=_step_timeout)
                else:
                    result = _call()
                elapsed = round(time.time() - t0, 1)
                clear_retry_callback()
                pipe_exc = None
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
                        "wait_sec":     _retry_wait,
                    })
                    time.sleep(_retry_wait)
                    push_event({"type": "step_start", "step": step_key, "name": step_name})

            except (TimeoutError, _cf.TimeoutError) as e:
                clear_retry_callback()
                pipe_exc = e
                elapsed = round(time.time() - t0, 1)
                if pipe_attempt < _TIMEOUT_RETRY_MAX:
                    # 타임아웃: 1회만 재시도 (critical 여부 무관)
                    push_event({
                        "type": "log",
                        "message": (
                            f"⏱ {step_name.strip()} {elapsed:.0f}s 타임아웃 — "
                            f"재시도 중 ({pipe_attempt}/{_TIMEOUT_RETRY_MAX})"
                        ),
                    })
                    push_event({"type": "step_start", "step": step_key, "name": step_name})
                    # 재시도 시 대기 없음 — 서버는 살아있으므로 바로 재시도
                else:
                    # 타임아웃 재시도 소진 → critical 여부 무관, 빈 결과로 계속 진행
                    print(f"  [{step_key}] 타임아웃 {_TIMEOUT_RETRY_MAX}회 소진 — 빈 결과로 계속 진행")
                    pipe_exc = e
                    break

            except Exception as e:
                clear_retry_callback()
                exc_type = type(e).__name__
                exc_msg  = str(e)
                print(f"  [{step_key}] 예외 발생 — {exc_type}: {exc_msg}")
                traceback.print_exc()
                is_timeout_exc = 'timeout' in exc_msg.lower()
                if is_timeout_exc and pipe_attempt < _TIMEOUT_RETRY_MAX:
                    pipe_exc = e
                    push_event({
                        "type": "log",
                        "message": f"⏱ {step_name.strip()} 타임아웃 — 재시도 중 ({pipe_attempt}/{_TIMEOUT_RETRY_MAX})",
                    })
                    push_event({"type": "step_start", "step": step_key, "name": step_name})
                else:
                    pipe_exc = e
                    break

        _ka_stop.set()  # 킵얼라이브 스레드 종료

        if pipe_exc is not None:
            elapsed = round(time.time() - t0, 1)
            exc_type = type(pipe_exc).__name__
            exc_msg  = str(pipe_exc)
            # 서버 로그에 전체 traceback 출력
            print(f"\n[step_error] {step_key} — {exc_type}: {exc_msg}")
            traceback.print_exc()
            push_event({
                "type":    "step_error",
                "step":    step_key,
                "name":    step_name,
                "message": f"[{exc_type}] {exc_msg}",
                "elapsed": elapsed,
            })
            if notify_fn:
                try:
                    notify_fn(f"❌ {dna.project_name} — {step_name.strip()} 오류\n[{exc_type}] {exc_msg[:200]}")
                except Exception:
                    pass
            # 타임아웃 오류는 critical 여부 무관하게 계속 진행 (빈 결과 저장)
            _is_timeout_failure = isinstance(pipe_exc, (TimeoutError, _cf.TimeoutError)) or \
                                  'timeout' in exc_msg.lower()
            if critical and not _is_timeout_failure:
                push_event({"type": "pipeline_aborted", "step": step_key})
                results["__aborted_at__"] = step_key
                return results
            if _is_timeout_failure and critical:
                push_event({
                    "type": "log",
                    "message": f"⚠️ {step_name.strip()} 타임아웃 — 빈 결과로 다음 스텝 진행",
                })
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

        # ── 컨펌 요청 (자동 실행 모드에서는 생략)
        if auto_run:
            user_input = "y"
            push_event({"type": "log", "message": f"✓ {step_name.strip()} 자동 완료"})
        else:
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
# SSE 킵얼라이브 헬퍼
# ─────────────────────────────────────────────

_KA_INTERVAL = 15  # 15초마다 step_progress 이벤트 전송 (Railway 60s 프록시 타임아웃 방지)


def _keepalive_start(push_event, step_key: str) -> threading.Event:
    """백그라운드 킵얼라이브 스레드 시작.

    Returns:
        stop_event — .set()을 호출하면 스레드가 종료됨
    """
    stop_ev = threading.Event()

    def _loop():
        msg_map = {
            "research":     "리서치 진행 중...",
            "narrative":    "내러티브 작성 중...",
            "strategy":     "전략 수립 중...",
            "creative":     "컨셉 개발 중...",
            "plan":         "기획 작성 중...",
            "script":       "대본 작성 중...",
            "storyboard":   "스토리보드 이미지 생성 중...",
            "platform":     "플랫폼 운영전략 수립 중...",
            "marketing":    "마케팅/홍보 전략 수립 중...",
            "final_proposal": "PT/Q&A 완성 중...",
            "improvement_report": "크리틱 분석 중...",
        }
        msg = msg_map.get(step_key, "처리 중...")
        while not stop_ev.wait(_KA_INTERVAL):
            try:
                push_event({"type": "step_progress", "step": step_key, "message": msg})
            except Exception:
                break

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return stop_ev


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
        if dna.agency_characteristics:
            s["기관 특성"] = dna.agency_characteristics
        items = dna.evaluation_items or result.get("evaluation_items", [])
        if items:
            s["평가배점표"] = [
                {
                    "item":           it.get("item", "") if isinstance(it, dict) else str(it),
                    "score":          it.get("score", "") if isinstance(it, dict) else "",
                    "category":       it.get("category", "") if isinstance(it, dict) else "",
                    "criteria":       it.get("criteria", "") if isinstance(it, dict) else "",
                    "detail_criteria": it.get("detail_criteria", "") if isinstance(it, dict) else "",
                    "strategic_hint": it.get("strategic_hint", "") if isinstance(it, dict) else "",
                    "warning":        it.get("warning", "") if isinstance(it, dict) else "",
                    "importance":     it.get("importance", "") if isinstance(it, dict) else "",
                }
                for it in items
                if it
            ]
        else:
            s["평가항목"] = []
        # 핵심 과업
        tasks = dna.core_tasks or result.get("core_tasks", [])
        if tasks:
            s["핵심 과업"] = [str(t) for t in tasks]
        # 금지·주의 사항
        notes = dna.forbidden_notes or result.get("forbidden_notes", [])
        if notes:
            s["금지·주의 사항"] = [str(n) for n in notes]
        # 배점표 전략 분석
        es = dna.evaluation_strategy or result.get("evaluation_strategy", {})
        if isinstance(es, dict):
            checklist = es.get("정량항목_체크리스트", es.get("정량체크리스트", []))
            if checklist:
                s["정량평가 체크리스트"] = [str(c) for c in checklist]
            focus = es.get("집중공략", "")
            if focus:
                s["집중공략"] = focus
        s["핵심키워드"] = dna.evaluation_keywords or result.get("top_keywords", [])

    elif step_key == "narrative":
        s["내러티브"] = result.get("narrative") or dna.narrative or ""

    elif step_key == "research":
        # 10개 항목 전체 — 절삭 없이 전송
        s["① 기관 특성/정책"]          = result.get("agency_policy", "") or dna.agency_characteristics or ""
        s["② 기관장 메시지"]           = result.get("leadership_message", "")
        s["③ 기존 콘텐츠 현황"]        = result.get("existing_content", "")
        s["④ 최근 이슈/뉴스"]          = result.get("recent_issues_news", "")
        s["⑤ 유사 기관 홍보전략"]      = result.get("best_cases", "")
        s["⑥ 유사 과업 분석"]          = result.get("task_patterns", "")
        s["⑦ 타겟 미디어 소비 행태"]   = result.get("target_media_habits", "")
        s["⑧ 타겟 반응 콘텐츠 선호도"] = result.get("target_content_preference", "")
        s["⑨ 플랫폼 최적화 패턴"]      = result.get("platform_patterns", "")
        s["⑩ 경쟁사 패턴"]             = result.get("competitor_patterns", "")

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
        # 숏폼 여부
        if dna.has_shortform:
            s["포맷"] = "숏폼 (15/30/60초 버전)"
        elif scripts and isinstance(scripts[0], dict):
            fmt = scripts[0].get("format", "")
            if fmt:
                s["포맷"] = fmt

        # 편별 대본 전체 내용 (청킹으로 전송)
        for i, sc in enumerate(scripts):
            if not isinstance(sc, dict):
                continue
            ep_num   = sc.get('episode', sc.get('ep_num', i + 1))
            title    = sc.get('title', '')
            scenes   = sc.get('scenes', [])
            hook     = sc.get('opening_hook', {})
            cta      = sc.get('closing_cta', {})
            core_msg = sc.get('core_message', sc.get('key_message', ''))

            lines = [f"## {ep_num}편 [{title}]"]
            if core_msg:
                lines.append(f"핵심 메시지: {core_msg}")

            # 오프닝 훅
            if isinstance(hook, dict) and hook:
                hook_line = hook.get('hook_line', '')
                hook_narr = hook.get('narration', hook.get('script', ''))
                if hook_line:
                    lines.append(f"\n### 오프닝 훅\n{hook_line}")
                if hook_narr:
                    lines.append(f"나레이션: {hook_narr}")

            # 숏폼 버전 (15/30/60초)
            versions = sc.get('versions', {})
            if versions and isinstance(versions, dict):
                for ver_key, ver in versions.items():
                    if not isinstance(ver, dict):
                        continue
                    ver_narr = ver.get('narration', ver.get('script', ''))
                    if ver_narr:
                        lines.append(f"\n### {ver_key}\n{ver_narr}")
            else:
                # 롱폼 씬 전체
                for si, scene in enumerate(scenes):
                    if not isinstance(scene, dict):
                        lines.append(str(scene))
                        continue
                    tc      = scene.get('timecode', scene.get('time', ''))
                    narr    = scene.get('narration', scene.get('script', ''))
                    dialogue = scene.get('dialogue', '')
                    caption  = scene.get('caption', scene.get('subtitle', ''))
                    lines.append(
                        f"\n### S#{si+1}"
                        + (f" [{tc}]" if tc else "")
                    )
                    if narr:
                        lines.append(f"나레이션: {narr}")
                    if dialogue:
                        lines.append(f"대사: {dialogue}")
                    if caption:
                        lines.append(f"자막: {caption}")

            # 클로징 CTA
            if isinstance(cta, dict) and cta:
                cta_text = cta.get('text', cta.get('narration', cta.get('message', '')))
                if cta_text:
                    lines.append(f"\n### 클로징 CTA\n{cta_text}")
            elif isinstance(cta, str) and cta:
                lines.append(f"\n### 클로징 CTA\n{cta}")

            ep_key = f"{ep_num}편 대본 [{title}]"
            s[ep_key] = "\n".join(lines)

    elif step_key == "storyboard":
        frames = result.get("frames", [])
        ok_frames = [f for f in frames if f.get("ok")]
        s["생성 결과"] = f"{len(ok_frames)}/{len(frames)}컷 생성 완료"
        s["스타일"] = result.get("style", "line")
        if result.get("error"):
            s["오류"] = result["error"]

    elif step_key == "marketing":
        # result 키: "platforms", "youtube_strategy"(텍스트), "sns_strategy"(텍스트),
        #            "influencer_strategy"(텍스트), "kpi_targets"(텍스트), "marketing_budget"
        channels = dna.distribution_channels or result.get("platforms", [])
        if channels:
            s["주요 채널"] = [
                ch.get("channel", ch.get("platform", str(ch))) if isinstance(ch, dict) else str(ch)
                for ch in channels
            ]
        # 유튜브 전략 (텍스트 우선, dict 레거시 호환)
        yt = dna.youtube_strategy or result.get("youtube_strategy", result.get("youtube_seo", ""))
        if isinstance(yt, str) and yt:
            s["유튜브 전략"] = yt[:400]
        elif isinstance(yt, dict) and yt:
            title_formula = yt.get("title_formula", yt.get("title_format", ""))
            kw = yt.get("keyword_strategy", yt.get("keywords", yt.get("main_keywords", "")))
            yt_lines = []
            if title_formula:
                yt_lines.append(f"제목 공식: {str(title_formula)[:120]}")
            if kw:
                yt_lines.append(f"키워드: {str(kw)[:200]}")
            if yt_lines:
                s["유튜브 전략"] = "\n".join(yt_lines)
        # SNS 전략 (텍스트 우선)
        sns = dna.sns_strategy or result.get("sns_strategy", result.get("sns_channels", ""))
        if isinstance(sns, str) and sns:
            s["SNS 전략"] = sns[:400]
        elif isinstance(sns, dict) and sns:
            sns_lines = []
            for ch, plan in list(sns.items())[:3]:
                if isinstance(plan, dict):
                    freq = plan.get("posting_frequency", plan.get("frequency", ""))
                    content = plan.get("content_format", plan.get("format", ""))
                    detail = f"{freq}" if freq else ""
                    if content:
                        detail += f" / {content}" if detail else content
                    sns_lines.append(f"{ch}: {detail[:100]}" if detail else ch)
                else:
                    sns_lines.append(f"{ch}: {str(plan)[:100]}")
            if sns_lines:
                s["SNS 전략"] = sns_lines
        # KPI 목표 (텍스트 우선)
        kpi = dna.kpi_targets or result.get("kpi_targets", result.get("kpi", ""))
        if isinstance(kpi, str) and kpi:
            s["KPI 목표"] = kpi[:300]
        elif isinstance(kpi, dict):
            kpis = kpi.get("primary_kpi", [])
            if kpis:
                s["KPI 목표"] = [
                    f"{k.get('metric', '')}: {k.get('target', '')}" if isinstance(k, dict) else str(k)
                    for k in kpis
                ]
        # 예산 배분 요약
        budget = dna.marketing_budget or result.get("marketing_budget", {})
        if isinstance(budget, dict) and budget:
            total_b = budget.get("total", budget.get("total_budget", budget.get("마케팅_예산", "")))
            breakdown = budget.get("breakdown", budget.get("배분", []))
            if total_b:
                s["마케팅 예산"] = str(total_b)
            if breakdown and isinstance(breakdown, list):
                s["예산 배분"] = [
                    f"{b.get('category', b.get('항목', ''))}: {b.get('amount', b.get('금액', ''))}"
                    if isinstance(b, dict) else str(b)
                    for b in breakdown[:4]
                ]

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
        critical_cnt = sum(1 for iss in issues if isinstance(iss, dict) and iss.get("severity") == "critical")
        warning_cnt  = sum(1 for iss in issues if isinstance(iss, dict) and iss.get("severity") != "critical")
        if formatted:
            s["필수 개선"] = f"{critical_cnt}건"
            s["보완 권고"] = f"{warning_cnt}건"
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
        pred = result.get("predicted_scores", [])
        if pred:
            s["예상 점수 항목 수"] = f"{len(pred)}개 항목 분석"
        ca = result.get("competitive_analysis", {})
        if ca and isinstance(ca, dict):
            diff_score = ca.get("differentiation_score", 0)
            if diff_score:
                s["경쟁 차별화"] = f"{diff_score:.0%}"

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

        # ── 예상 질의응답 ──────────────────────────
        qa = result.get("qa_prep", [])
        if qa and isinstance(qa, list):
            s["예상 질의응답"] = [
                f"[{q.get('category', '')}] {q.get('question', '')}\n→ {q.get('answer', '')}"
                for q in qa if isinstance(q, dict)
            ]

    return s

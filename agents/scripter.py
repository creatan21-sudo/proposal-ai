# agents/scripter.py
# STEP 5: 대본/기획 에이전트 (방송작가)
# 역할: 편별 완성 대본 자동 생성
#
# 공통:
#   - 오프닝 훅 (첫 5초 시청자 잡기)
#   - 장면별 구성 S#1, S#2... (타임코드 포함)
#   - 나레이션 전문 (문어체)
#   - 실제 대사 (구어체)
#   - 인터뷰 질문 목록
#   - 자막 포인트
#   - 클로징 CTA
#
# 숏폼 (60초 이하): 15/30/60초 3개 버전 동시 생성
# 롱폼 (60초 초과): 장면별 타임코드 + 인터뷰 포함
# 시리즈 (2편 이상): 편간 클리프행어/복선 후처리 패스

import concurrent.futures
import re
import threading

from core import claude_client
from core.dna import ConceptDNA, update_dna, dna_to_context_string
from database.db import save_script


# ─────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────

# 장면당 평균 초 (롱폼 기준)
_SECS_PER_SCENE = 12

# 숏폼 판단 기준 (초)
_SHORTFORM_THRESHOLD = 60

# 숏폼 3종 버전
_SHORTFORM_VERSIONS = ["15sec", "30sec", "60sec"]


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────

def run(dna: ConceptDNA, progress_fn=None, max_episodes: int = 0) -> dict:
    """편별 대본 전체 생성.

    Args:
        dna: STEP 0~4 결과가 모두 반영된 ConceptDNA
        progress_fn: 선택적 진행상황 콜백 (SSE push_event 함수)
                     progress_fn({"type": "step_progress", ...}) 형태로 호출
        max_episodes: 생성할 최대 편수 (0이면 기획된 전체 편수)

    Returns:
        {
            "scripts": [          # 편별 대본 목록
                { ...script_object... }
            ],
            "series_hooks": [...] # 시리즈 연결고리 (2편 이상일 때)
        }
    """
    ep_plans   = _get_episode_plans(dna)
    if max_episodes > 0:
        ep_plans = ep_plans[:max_episodes]
    total      = len(ep_plans)
    is_series  = total > 1
    duration_s = _duration_to_seconds(dna.duration)
    is_short   = _is_shortform(duration_s)

    print(f"  대본 생성: {total}편 / {'숏폼' if is_short else '롱폼'} "
          f"({dna.duration}) {'| 시리즈 연결고리 포함' if is_series else ''}")

    _EP_TIMEOUT = 300  # 편당 최대 5분 (씬별 개별 API 호출로 시간 증가)

    def _generate_one(idx: int, ep_plan: dict) -> dict:
        ep_num = idx + 1
        if is_short:
            return _generate_shortform_outline(dna, ep_plan, ep_num, ep_plans, is_series)
        is_sample = (ep_num == 1)
        return _generate_longform_outline(dna, ep_plan, ep_num, ep_plans, is_series, is_sample)

    def _fallback_script(idx: int, ep_plan: dict) -> dict:
        ep_num = idx + 1
        title  = ep_plan.get("title", f"{ep_num}편")
        if is_short:
            return {
                "episode": ep_num, "title": title, "format": "shortform",
                "duration": dna.duration, "versions": {}, "scenes": [],
                "closing_cta": {}, "series_hook": {},
                "_timeout": True,
            }
        return {
            "episode": ep_num, "title": title, "format": "longform",
            "duration": dna.duration, "opening_hook": {}, "scenes": [],
            "interview_questions": [], "closing_cta": {}, "series_hook": {},
            "_timeout": True,
        }

    scripts = []
    for idx, ep_plan in enumerate(ep_plans):
        ep_num = idx + 1
        ep_title = ep_plan.get("title", f"{ep_num}편")
        print(f"  [{ep_num}/{total}] {ep_title} 대본 생성 중...")
        if progress_fn:
            try:
                progress_fn({
                    "type":    "step_progress",
                    "step":    "script",
                    "message": f"대본 생성 중... {ep_num}/{total}편 ({ep_title})",
                })
            except Exception:
                pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_generate_one, idx, ep_plan)
            try:
                script = future.result(timeout=_EP_TIMEOUT)
            except concurrent.futures.TimeoutError:
                print(f"  [경고] {ep_num}편 대본 생성 타임아웃 ({_EP_TIMEOUT}초) — 빈 개요로 대체")
                future.cancel()
                script = _fallback_script(idx, ep_plan)
            except Exception as e:
                print(f"  [경고] {ep_num}편 대본 생성 오류: {e} — 빈 개요로 대체")
                script = _fallback_script(idx, ep_plan)

        scripts.append(script)

        # 편별 DB 저장
        try:
            save_script(dna.client_name, dna.project_name, script,
                        case_id=getattr(dna, "case_id", 0) or 0)
        except Exception as e:
            print(f"  [경고] 대본 DB 저장 실패: {e}")

    # 시리즈 연결고리 후처리 (2편 이상)
    series_hooks = []
    if is_series:
        print("  시리즈 연결고리 생성 중...")
        series_hooks = _generate_series_hooks(dna, scripts)
        _inject_hooks(scripts, series_hooks)

    result = {"scripts": scripts, "series_hooks": series_hooks}

    # DNA 업데이트
    outline = [
        {"episode": s["episode"], "title": s["title"],
         "format": s["format"], "scene_count": len(s.get("scenes", []))}
        for s in scripts
    ]
    update_dna(dna, {
        "scripts":       scripts,
        "script_outline": outline,
        "has_shortform": is_short,
    })
    print(f"  대본 생성 완료: {total}편")

    return result


# ─────────────────────────────────────────────
# 에피소드 플랜 조회
# ─────────────────────────────────────────────

def _get_episode_plans(dna: ConceptDNA) -> list:
    """dna.episodes에서 편 계획 반환.
    - quantity보다 episodes가 적으면 빈 플랜으로 나머지 채움 (절대 1편에서 멈추지 않음)
    - episodes가 없으면 quantity 전체 기반 플랜 생성
    """
    qty = max(dna.quantity, 1)
    base = list(dna.episodes) if dna.episodes else []

    # 부족한 편수 채우기
    for i in range(len(base), qty):
        base.append({
            "episode_number": i + 1,
            "title":          f"{dna.project_name} {i + 1}편",
            "core_message":   "",
            "target_audience": "",
            "key_scene":      "",
        })

    return base


# ─────────────────────────────────────────────
# 제안서용 개요 모드 (속도 우선)
# ─────────────────────────────────────────────

def _generate_longform_outline(
    dna: ConceptDNA,
    ep_plan: dict,
    ep_num: int,
    all_plans: list,
    is_series: bool,
    is_sample: bool,
) -> dict:
    """제안서 개요용 대본 생성.

    모든 편: 메타데이터 JSON(소형) + 씬별 개별 텍스트 API 호출 → 합산
    1편(샘플): 5씬까지, 나머지: 3씬
    씬 텍스트는 max_tokens=2000으로 실제 방송 대본 형식으로 작성
    """
    duration_s  = _duration_to_seconds(dna.duration)
    full_scenes = _calc_scene_count(duration_s)
    title       = ep_plan.get("title", f"{ep_num}편")
    word_count  = _calc_word_count(duration_s)

    # 1편 샘플은 5씬, 나머지는 3씬
    scene_count = min(full_scenes, 5) if is_sample else min(full_scenes, 3)

    # 1단계: 메타데이터 JSON (작은 호출)
    meta_prompt = _build_meta_only_prompt(dna, ep_plan, ep_num, all_plans if is_sample else [], is_series)
    meta = claude_client.call_json(meta_prompt, max_tokens=250, _validate=False)
    if meta.get("_parse_failed"):
        meta = {}

    # 2단계: 씬별 개별 텍스트 API 호출 (max_tokens=2000)
    scenes = []
    for scene_num in range(1, scene_count + 1):
        scene_prompt = _build_scene_text_prompt_v2(
            dna, ep_plan, ep_num, scene_num, scene_count, duration_s, word_count
        )
        scene_text = claude_client.call(scene_prompt, max_tokens=2000)
        scene_obj  = _parse_scene_text_v2(scene_text, scene_num, duration_s, scene_count)
        scenes.append(scene_obj)
        print(f"  [씬] {ep_num}편 S#{scene_num}/{scene_count} 완료 ({len(scene_text)}자)")

    result = {
        "episode":             ep_num,
        "title":               meta.get("title", title),
        "format":              "longform",
        "duration":            dna.duration,
        "opening_hook":        {"hook_line": meta.get("opening_hook", "")},
        "scenes":              scenes,
        "interview_questions": meta.get("interview_questions", []),
        "closing_cta":         {"cta_direction": meta.get("closing_cta", "")},
        "series_hook": {
            "cliffhanger_line": meta.get("cliffhanger_line") if is_series else None,
            "callback_line":    None,
        },
    }
    label = "샘플" if is_sample else "개요"
    print(f"  [확인] {ep_num}편 {label} 대본 완료: {len(scenes)}씬")
    return result


def _build_meta_only_prompt(
    dna: ConceptDNA,
    ep_plan: dict,
    ep_num: int,
    all_plans: list,
    is_series: bool,
) -> str:
    """편 메타데이터 전용 소형 JSON 프롬프트."""
    title = ep_plan.get("title", f"{ep_num}편")
    cliff = '"cliffhanger_line":"다음편연결문구"' if is_series else '"cliffhanger_line":null'
    return (
        f"영상대본메타데이터JSON만출력(설명없이).\n"
        f"발주처:{dna.client_name} 사업:{dna.project_name} 컨셉:{dna.concept or '미정'}"
        f" 톤:{dna.tone_and_manner or '미정'} {ep_num}편\"{title}\"\n\n"
        f'{{"title":"{title}","opening_hook":"오프닝훅자막15자내",'
        f'"interview_questions":["질문1","질문2"],'
        f'"closing_cta":"CTA방향1문장",{cliff}}}'
    )


def _build_scene_text_prompt_v2(
    dna: ConceptDNA,
    ep_plan: dict,
    ep_num: int,
    scene_num: int,
    total_scenes: int,
    duration_s: int,
    word_count: int,
) -> str:
    """씬 개별 호출용 실제 방송 대본 프롬프트 (JSON 없이 텍스트 출력)."""
    title     = ep_plan.get("title", f"{ep_num}편")
    core_msg  = ep_plan.get("core_message", "")
    dur_label = f"{duration_s // 60}분 {duration_s % 60}초" if duration_s % 60 else f"{duration_s // 60}분"

    scene_dur = max(1, duration_s // total_scenes)
    start_s   = (scene_num - 1) * scene_dur
    end_s     = scene_num * scene_dur
    def _tc(s): return f"{s // 60}:{s % 60:02d}"
    timecode  = f"{_tc(start_s)}~{_tc(end_s)}"
    min_chars = max(200, word_count // total_scenes)

    return (
        f"실제 방송용 대본을 작성하라. 요약이나 메타 설명은 절대 금지.\n"
        f"발주처:{dna.client_name} 사업:{dna.project_name} {ep_num}편 제목:{title}\n"
        f"컨셉:{dna.concept or '미정'} 톤:{dna.tone_and_manner or '미정'} 러닝타임:{dur_label}\n"
        f"핵심메시지:{core_msg}\n\n"
        f"【이 씬 정보】 S#{scene_num}/{total_scenes} | 타임코드:{timecode} (약 {scene_dur}초)\n\n"
        f"【필수 포함 항목 — 각 항목 실제 내용으로 작성】\n"
        f"▶ 나레이션: 나레이터가 실제로 읽을 완성된 문장 전문 (문어체 격식체, 2~4문장)\n"
        f"▶ 대사: 출연자 실제 대사 전문 (구어체, 감정지문 포함) — 없으면 '없음'\n"
        f"▶ 화면: 카메라 앵글 + 피사체 + 움직임 + 인물 표정 묘사 (2~3문장)\n"
        f"▶ 자막: 화면에 표시될 자막 문구 그대로 (**핵심단어** 강조)\n\n"
        f"분량 기준: 최소 {min_chars}자 이상. 실제 촬영 가능한 수준으로 상세히 작성.\n"
        f"'설명한다', '보여준다', '삽입한다' 같은 메타 설명 절대 금지.\n\n"
        f"[출력 형식 — 아래 형식 그대로 시작]\n"
        f"S#{scene_num} [장소명 — 구체적 촬영지] ({timecode})\n"
        f"▶ 나레이션: ...\n"
        f"▶ 대사: ...\n"
        f"▶ 화면: ...\n"
        f"▶ 자막: ..."
    )


def _parse_scene_text_v2(text: str, scene_num: int, duration_s: int, total_scenes: int) -> dict:
    """방송 대본 형식 씬 텍스트 → 씬 dict 파싱."""
    lines = text.strip().splitlines()
    location = ""
    fields: dict = {"narration": [], "dialogue": [], "visual": [], "caption": []}
    current_field = None

    scene_dur = max(1, duration_s // total_scenes)
    start_s   = (scene_num - 1) * scene_dur
    end_s     = scene_num * scene_dur
    def _tc(s): return f"{s // 60}:{s % 60:02d}"
    timecode  = f"{_tc(start_s)}~{_tc(end_s)}"

    MARKERS = [
        ("▶ 나레이션:", "narration"), ("▶나레이션:", "narration"), ("나레이션:", "narration"),
        ("▶ 대사:", "dialogue"),   ("▶대사:", "dialogue"),   ("대사:", "dialogue"),
        ("▶ 화면:", "visual"),     ("▶화면:", "visual"),     ("화면:", "visual"),
        ("▶ 자막:", "caption"),    ("▶자막:", "caption"),    ("자막:", "caption"),
    ]

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # 첫 줄에서 장소 추출
        if not location:
            m = re.match(r'^S#\d+\s*\[([^\]]+)\]', stripped)
            if m:
                location = m.group(1).strip()
                continue

        matched = False
        for marker, field in MARKERS:
            if stripped.startswith(marker):
                current_field = field
                rest = stripped[len(marker):].strip()
                if rest:
                    fields[field].append(rest)
                matched = True
                break
        if not matched and current_field:
            fields[current_field].append(stripped)

    narration = "\n".join(fields["narration"]).strip()
    dialogue  = "\n".join(fields["dialogue"]).strip()
    visual    = "\n".join(fields["visual"]).strip()
    caption   = "\n".join(fields["caption"]).strip()

    # key_point: 나레이션 요약 (없으면 전체 텍스트 앞부분)
    key_point = narration[:200] if narration else text.strip()[:200]

    return {
        "scene_number": scene_num,
        "timecode":     timecode,
        "location":     location or f"씬{scene_num} 촬영지",
        "narration":    narration,
        "dialogue":     dialogue if dialogue and "없음" not in dialogue else None,
        "visual":       visual,
        "caption":      caption,
        "key_point":    key_point,
    }


# 하위 호환 유지 (레거시 — 직접 호출 안 함)
def _build_scene_text_prompt(dna, ep_plan, ep_num, scene_count, duration_s, is_series):
    return _build_scene_text_prompt_v2(
        dna, ep_plan, ep_num, 1, scene_count, duration_s,
        _calc_word_count(duration_s)
    )


def _parse_scene_text(text: str, expected_count: int) -> list:
    """레거시 호환 — 단일 텍스트에서 씬 목록 추출."""
    scenes = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^씬\s*(\d+)\s*(?:\[([^\]]*)\])?\s*[:\-]\s*(.*)', line)
        if not m:
            m = re.match(r'^S#(\d+)\s*(?:\[([^\]]*)\])?\s*[:\-]\s*(.*)', line, re.IGNORECASE)
        if not m:
            m = re.match(r'^(\d+)[.\)]\s+(.+)', line)
            if m:
                scenes.append({"scene_number": int(m.group(1)), "location": "", "key_point": m.group(2).strip()})
                continue
        if m:
            num = int(m.group(1))
            loc = (m.group(2) or "").strip()
            kp  = (m.group(3) if len(m.groups()) >= 3 else m.group(2) or "").strip()
            scenes.append({"scene_number": num, "location": loc, "key_point": kp})
    if not scenes:
        for i, line in enumerate(text.strip().splitlines()[:expected_count], 1):
            if line.strip():
                scenes.append({"scene_number": i, "location": "", "key_point": line.strip()})
    return scenes[:expected_count]


def _build_outline_sample_prompt(
    dna: ConceptDNA,
    ep_plan: dict,
    ep_num: int,
    all_plans: list,
    is_series: bool,
    scene_count: int,
    duration_s: int,
) -> str:
    """1편 샘플 개요 — 씬 번호·장소·핵심 메시지만 (속도 최우선)."""
    title      = ep_plan.get("title", f"{ep_num}편")
    series_tag = f" 시리즈{len(all_plans)}편중{ep_num}편" if is_series else ""
    cliff      = '"cliffhanger_line":"다음편연결문구"' if is_series else '"cliffhanger_line":null'

    scenes_template = ",".join(
        f'{{"scene_number":{n},"location":"장소","key_point":"핵심내용1문장"}}'
        for n in range(1, scene_count + 1)
    )

    return (
        f"영상대본개요JSON만출력(설명없이).\n"
        f"발주처:{dna.client_name} 사업:{dna.project_name} 컨셉:{dna.concept or '미정'}"
        f" 톤:{dna.tone_and_manner or '미정'} {ep_num}편\"{title}\""
        f" 러닝타임:{dna.duration}{series_tag}\n\n"
        f'{{"episode":{ep_num},"title":"{title}","format":"longform","duration":"{dna.duration}",'
        f'"opening_hook":{{"hook_line":"오프닝훅자막15자내"}},'
        f'"scenes":[{scenes_template}],'
        f'"interview_questions":["질문1","질문2"],'
        f'"closing_cta":{{"cta_direction":"CTA방향1문장"}},'
        f'"series_hook":{{{cliff},"callback_line":null}}}}'
    )


def _build_outline_minimal_prompt(
    dna: ConceptDNA,
    ep_plan: dict,
    ep_num: int,
    scene_count: int,
) -> str:
    """2편 이상 — 씬 번호·장소·핵심 메시지만 (속도 최우선)."""
    title      = ep_plan.get("title", f"{ep_num}편")
    core_msg   = ep_plan.get("core_message", "")

    scenes_template = ",".join(
        f'{{"scene_number":{n},"location":"장소","key_point":"핵심내용"}}'
        for n in range(1, scene_count + 1)
    )

    return (
        f"영상대본최소개요JSON만출력.\n"
        f"발주처:{dna.client_name} 사업:{dna.project_name} {ep_num}편\"{title}\""
        f" 핵심:{core_msg} 러닝타임:{dna.duration}\n\n"
        f'{{"episode":{ep_num},"title":"{title}","format":"longform","duration":"{dna.duration}",'
        f'"core_message":"핵심메시지1문장",'
        f'"opening_hook":{{"hook_line":"훅자막"}},'
        f'"scenes":[{scenes_template}],'
        f'"closing_cta":"CTA방향",'
        f'"series_hook":{{"cliffhanger_line":null,"callback_line":null}}}}'
    )


def _generate_shortform_outline(
    dna: ConceptDNA,
    ep_plan: dict,
    ep_num: int,
    all_plans: list,
    is_series: bool,
) -> dict:
    """숏폼 제안서 개요 — 15/30/60초 각 버전 핵심 포인트만 (속도 최우선)."""
    title = ep_plan.get("title", f"{ep_num}편")

    prompt = (
        f"숏폼대본개요JSON만출력(설명없이).\n"
        f"발주처:{dna.client_name} 사업:{dna.project_name} 컨셉:{dna.concept or '미정'}"
        f" {ep_num}편\"{title}\" 러닝타임:{dna.duration}\n\n"
        f'{{"episode":{ep_num},"title":"{title}","format":"shortform","duration":"{dna.duration}",'
        f'"versions":{{'
        f'"15sec":{{"hook_line":"훅10자내","scenes":[{{"scene_number":1,"key_point":"핵심"}},{{"scene_number":2,"key_point":"CTA"}}]}},'
        f'"30sec":{{"hook_line":"훅12자내","scenes":[{{"scene_number":1,"key_point":"문제"}},{{"scene_number":2,"key_point":"해결"}},{{"scene_number":3,"key_point":"CTA"}}]}},'
        f'"60sec":{{"hook_line":"훅15자내","scenes":[{{"scene_number":1,"key_point":"훅"}},{{"scene_number":2,"key_point":"공감"}},{{"scene_number":3,"key_point":"해결"}},{{"scene_number":4,"key_point":"CTA"}}]}}'
        f'}},'
        f'"closing_cta":{{"cta_direction":"CTA방향"}},'
        f'"series_hook":{{"cliffhanger_line":null,"callback_line":null}}}}'
    )

    raw = claude_client.call_json(prompt, max_tokens=500, _validate=False)
    raw.setdefault("episode",  ep_num)
    raw.setdefault("title",    ep_plan.get("title", f"{ep_num}편"))
    raw.setdefault("format",   "shortform")
    raw.setdefault("duration", dna.duration)
    raw.setdefault("versions", {})
    raw.setdefault("scenes",   [])
    raw.setdefault("closing_cta", {})
    raw.setdefault("series_hook", {})

    versions = raw.get("versions") or {}
    if versions:
        print(f"  [확인] {ep_num}편 숏폼 개요 완료: {list(versions.keys())}")
    else:
        print(f"  [경고] {ep_num}편 숏폼 개요: versions 비어있음!")
    return raw


# ─────────────────────────────────────────────
# 롱폼 대본 생성 (레거시 — 직접 호출 안 함)
# ─────────────────────────────────────────────

def _generate_longform(
    dna: ConceptDNA,
    ep_plan: dict,
    ep_num: int,
    all_plans: list,
    is_series: bool,
) -> dict:
    """롱폼(60초 초과) 대본 생성.

    Args:
        dna: ConceptDNA
        ep_plan: 이 편의 플래너 계획
        ep_num: 편 번호 (1-based)
        all_plans: 전체 편 계획 (시리즈 컨텍스트용)
        is_series: 시리즈 여부

    Returns:
        롱폼 script dict
    """
    duration_s   = _duration_to_seconds(dna.duration)
    scene_count  = _calc_scene_count(duration_s)
    prompt       = _build_longform_prompt(dna, ep_plan, ep_num, all_plans,
                                          is_series, scene_count, duration_s)
    raw          = claude_client.call_json(prompt, max_tokens=2000)

    raw.setdefault("episode",   ep_num)
    raw.setdefault("title",     ep_plan.get("title", f"{ep_num}편"))
    raw.setdefault("format",    "longform")
    raw.setdefault("duration",  dna.duration)
    raw.setdefault("opening_hook", {})
    raw.setdefault("scenes",    [])
    raw.setdefault("interview_questions", [])
    raw.setdefault("closing_cta", {})
    raw.setdefault("series_hook", {})

    # 품질 경고 로그
    scene_count = len(raw.get("scenes") or [])
    if scene_count == 0:
        print(f"  [경고] {ep_num}편 롱폼 대본: scenes 배열이 비어있음! (Claude 응답 확인 필요)")
    else:
        print(f"  [확인] {ep_num}편 롱폼 대본 생성 완료: {scene_count}씬")

    return raw


def _build_longform_prompt(
    dna: ConceptDNA,
    ep_plan: dict,
    ep_num: int,
    all_plans: list,
    is_series: bool,
    scene_count: int,
    duration_s: int,
) -> str:
    dna_ctx      = dna_to_context_string(dna)
    ep_ctx       = _format_episode_plan(ep_plan)
    series_ctx   = _format_series_context(ep_num, all_plans) if is_series else ""
    creative_ctx = _format_creative_context(dna)
    forbidden    = "\n".join(f"  - {e}" for e in dna.forbidden_expressions[:5]) or "  (없음)"

    total_min  = duration_s // 60
    total_sec  = duration_s % 60
    dur_label  = f"{total_min}분 {total_sec}초" if total_sec else f"{total_min}분"

    series_instruction = ""
    if is_series:
        prev_note = "없음 (첫 편)" if ep_num == 1 else f"{ep_num-1}편과 이어지는 복선/콜백 포함"
        next_note = "없음 (마지막 편)" if ep_num == len(all_plans) else f"{ep_num+1}편으로 이어지는 클리프행어 포함"
        series_instruction = f"""
━━━━━━━━━━━━━━━━━━━━━━━
[시리즈 연결 지침]
━━━━━━━━━━━━━━━━━━━━━━━
- 이전 편 연결 (callback): {prev_note}
- 다음 편 예고 (cliffhanger): {next_note}
- series_hook 객체에 cliffhanger_line과 callback_line을 구체적으로 작성
{series_ctx}"""

    return f"""당신은 20년 경력의 공공 캠페인 전문 방송작가입니다.
아래 정보를 바탕으로 {dna.client_name} {ep_num}편 영상 대본을 완성본 수준으로 작성해주세요.

【절대 원칙】
- 개요나 방향만 적는 것은 금지. 실제 방송에 그대로 쓸 수 있는 완성 대본을 작성하라.
- narration: 실제 나레이터가 읽을 전체 문장을 빠짐없이 작성 (문어체 격식체).
- dialogue: 출연자가 실제로 할 대사 전문을 구어체로 작성.
- visual: 카메라 앵글, 피사체 위치, 움직임, 인물 표정까지 묘사.
- caption: 자막 문구 그대로 작성 (3초 이내에 읽힐 분량, 강조 포인트 **볼드** 표시).
- "○○를 설명한다", "인터뷰를 삽입한다" 같은 메타 설명은 절대 금지.

━━━━━━━━━━━━━━━━━━━━━━━
[프로젝트 컨텍스트]
━━━━━━━━━━━━━━━━━━━━━━━
{dna_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[크리에이티브 기준 — 모든 장면에서 준수]
━━━━━━━━━━━━━━━━━━━━━━━
{creative_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[이 편 제작 계획]
━━━━━━━━━━━━━━━━━━━━━━━
{ep_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[금지 표현·이미지 방향]
━━━━━━━━━━━━━━━━━━━━━━━
{forbidden}
{series_instruction}
━━━━━━━━━━━━━━━━━━━━━━━
[대본 작성 세부 지침]
━━━━━━━━━━━━━━━━━━━━━━━
총 러닝타임: {dur_label} / 장면 수: {scene_count}개 (각 장면 평균 {duration_s // scene_count}초)

① 오프닝 훅 (0:00~0:05) — 첫 5초가 시청 지속을 결정한다
   - 충격 통계: "대한민국 국민 ○명 중 ○명이..." 형식의 수치 직구
   - 감성 질문: 시청자가 "나 이야기인가?" 싶게 만드는 공감 질문
   - 역설 장면: 예상과 정반대의 화면으로 시선 붙잡기
   - hook_line은 실제 자막 문구 그대로 작성 (15자 이내 임팩트)

② 장면 구성 (S#1 ~ S#{scene_count}) 【씬 제목만 나열 절대 금지 — 전부 실제 내용 작성】
   각 씬은 반드시 아래를 모두 포함해서 실제로 작성:
   - 씬 번호와 장소/상황 (예: S#3 — 서울 마포구 주택가 골목, 저녁)
   - 나레이션 전문 (실제 나레이터가 읽을 수 있는 완성된 문장 전체)
   - 화면 묘사 (카메라 앵글·피사체 위치·움직임·표정까지 구체적으로)
   - 자막/그래픽 텍스트 (화면에 표시될 문구 그대로, **강조** 표시 포함)
   - 예상 러닝타임 (timecode 형식)

   {scene_count}개 씬이면 {scene_count}개 씬 전부 실제 내용을 작성해야 한다.
   "○○ 장면을 보여준다", "○○에 대해 설명한다" 같은 메타 설명은 절대 금지.
   각 장면 visual은 "클로즈업으로 손을 잡는 장면" 같은 구체적 영상 언어로.
   narration은 문단이 아닌 실제 읽힐 문장 단위로 작성. 줄바꿈으로 호흡 표시.
   dialogue는 "(웃으며) 그때는 정말 몰랐어요, 이게 이렇게 중요한 줄." 같은 구어체 전문.
   caption은 화면에 표시될 자막 그대로. 핵심 단어 **강조** 표시.

③ 인터뷰 질문 5~7개
   - 출연자가 핵심 메시지를 자연스럽게 발화하도록 유도하는 개방형 질문.
   - "○○에 대해 어떻게 생각하세요?" (X) → "처음 ○○을 경험했을 때 어떤 느낌이었나요?" (O)

④ 클로징 CTA
   - 구체적 행동 유도: URL, QR, 해시태그, 전화번호 등 실제 접점 포함.
   - narration과 자막(cta_text)을 구분해서 작성.


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
  "episode": {ep_num},
  "title": "{ep_plan.get('title', f'{ep_num}편')}",
  "format": "longform",
  "duration": "{dna.duration}",

  "scenes": [
    {{
      "scene_number": 1,
      "timecode": "0:00~0:{duration_s // scene_count}",
      "location": "구체적 촬영 장소 (예: 서울 ○구 ○○공원 광장, 실내 스튜디오 세트)",
      "visual": "카메라 앵글(풀샷/미디엄/클로즈업) + 피사체 + 움직임 + 표정·행동 묘사 (2~3문장)",
      "narration": "나레이터가 읽을 실제 문장 전문. 호흡 단위로 줄바꿈. (문어체 격식체)",
      "dialogue": "출연자 실제 대사 전문. 감정 지문 포함. (구어체) 없으면 null",
      "caption": "화면에 표시될 자막 문구 그대로. **핵심단어** 강조 표시.",
      "sound": "배경음악 장르·BPM·감성 방향 또는 효과음 구체적으로"
    }}
  ],

  "opening_hook": {{
    "timecode": "0:00~0:05",
    "hook_type": "충격통계|감성질문|역설장면|공감한마디 중 하나",
    "visual": "카메라 앵글·피사체·움직임·인물 표정까지 구체적으로 묘사 (2~3문장)",
    "audio": "나레이션 실제 문장 또는 현장음 방향 또는 음악 장르+BPM",
    "hook_line": "자막으로 쓸 실제 문구 (15자 이내, 임팩트 최대화)"
  }},

  "interview_questions": [
    "인터뷰 질문 1 — 핵심 메시지를 자연스럽게 이끌어내는 개방형 질문",
    "인터뷰 질문 2",
    "인터뷰 질문 3",
    "인터뷰 질문 4",
    "인터뷰 질문 5"
  ],

  "closing_cta": {{
    "timecode": "X:XX~{dur_label}",
    "visual": "마지막 장면 구체적 묘사",
    "narration": "클로징 나레이션 실제 문장 전문 (문어체)",
    "cta_text": "자막 CTA 문구 그대로 (예: 지금 바로 검색하세요! #○○○)",
    "cta_type": "구독|공유|참여|신청|방문 중 하나",
    "end_card": "엔드카드 구성 요소 (로고 위치/웹사이트 URL/QR코드/해시태그)"
  }},

  "series_hook": {{
    "cliffhanger_line": "다음 편 궁금증 유발 자막 문구 그대로 (없으면 null)",
    "callback_line": "이전 편 키워드를 이어받는 첫 대사 또는 나레이션 (없으면 null)"
  }}
}}"""


# ─────────────────────────────────────────────
# 숏폼 대본 생성
# ─────────────────────────────────────────────

def _generate_shortform(
    dna: ConceptDNA,
    ep_plan: dict,
    ep_num: int,
    all_plans: list,
    is_series: bool,
) -> dict:
    """숏폼(60초 이하) 15/30/60초 3개 버전 동시 생성.

    Args:
        dna: ConceptDNA
        ep_plan: 이 편의 플래너 계획
        ep_num: 편 번호 (1-based)
        all_plans: 전체 편 계획
        is_series: 시리즈 여부

    Returns:
        숏폼 script dict (versions 키에 3개 버전 포함)
    """
    prompt = _build_shortform_prompt(dna, ep_plan, ep_num, all_plans, is_series)
    raw    = claude_client.call_json(prompt, max_tokens=2000)

    raw.setdefault("episode",  ep_num)
    raw.setdefault("title",    ep_plan.get("title", f"{ep_num}편"))
    raw.setdefault("format",   "shortform")
    raw.setdefault("duration", dna.duration)
    raw.setdefault("versions", {})
    raw.setdefault("interview_questions", [])
    raw.setdefault("closing_cta", {})
    raw.setdefault("series_hook", {})
    # 롱폼과 통일성을 위해 대표 장면도 포함
    raw.setdefault("scenes", raw.get("versions", {}).get("60sec", {}).get("scenes", []))

    # 품질 경고 로그
    versions = raw.get("versions") or {}
    if not versions:
        print(f"  [경고] {ep_num}편 숏폼 대본: versions 딕셔너리가 비어있음! (Claude 응답 확인 필요)")
    else:
        ver_info = ", ".join(f"{k}={len((v or {}).get('scenes',[]))}씬" for k, v in versions.items())
        print(f"  [확인] {ep_num}편 숏폼 대본 생성 완료: {ver_info}")

    return raw


def _build_shortform_prompt(
    dna: ConceptDNA,
    ep_plan: dict,
    ep_num: int,
    all_plans: list,
    is_series: bool,
) -> str:
    dna_ctx      = dna_to_context_string(dna)
    ep_ctx       = _format_episode_plan(ep_plan)
    creative_ctx = _format_creative_context(dna)
    forbidden    = "\n".join(f"  - {e}" for e in dna.forbidden_expressions[:5]) or "  (없음)"
    series_ctx   = _format_series_context(ep_num, all_plans) if is_series else ""

    return f"""당신은 숏폼 콘텐츠 전문 방송작가입니다.
아래 정보를 바탕으로 {dna.client_name} 숏폼 영상 {ep_num}편의 15초·30초·60초 버전을 각각 완성본으로 작성해주세요.

【절대 원칙】
- 세 버전은 각각 독립적으로 완결되는 별개의 스크립트다 (긴 버전의 요약이 절대 아님).
- 각 씬(S#)은 반드시 아래를 포함해서 실제로 작성:
  • 씬 번호와 장소/상황
  • 나레이션 전문 (실제 읽을 수 있는 완성된 문장)
  • 화면 묘사 (무엇이 보이는지 구체적으로)
  • 자막/그래픽 텍스트
  • 예상 러닝타임
- 씬 제목만 나열하고 내용을 비우는 것은 절대 금지.
- 각 장면의 audio 필드에는 실제 나레이션 전문 또는 대사 전문을 작성하라.
- caption 필드에는 화면에 표시될 자막 문구를 그대로 작성하라.
- "○○를 보여준다", "메시지를 전달한다" 같은 메타 설명은 절대 금지.
- 15초 버전: 단 하나의 메시지만. 군더더기 없이.
- 30초 버전: 문제-해결 구조. 시청자가 고개를 끄덕이게.
- 60초 버전: 감정 곡선이 있어야 함 (긴장-공감-해소-행동).

━━━━━━━━━━━━━━━━━━━━━━━
[프로젝트 컨텍스트]
━━━━━━━━━━━━━━━━━━━━━━━
{dna_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[크리에이티브 기준]
━━━━━━━━━━━━━━━━━━━━━━━
{creative_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[이 편 계획]
━━━━━━━━━━━━━━━━━━━━━━━
{ep_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[금지 표현]
━━━━━━━━━━━━━━━━━━━━━━━
{forbidden}
{series_ctx}
━━━━━━━━━━━━━━━━━━━━━━━
[숏폼 구조 기준 — 초 단위 엄수]
━━━━━━━━━━━━━━━━━━━━━━━
• 15초: 훅(0~3초) + 핵심메시지(3~12초) + CTA(12~15초)
  - 훅: 스크롤을 멈추게 만드는 첫 프레임. 음소거 상태에서도 자막만으로 전달 가능해야 함.
  - 핵심메시지: 단 하나. 복수 메시지 금지.
  - CTA: "지금 바로 ○○하세요" 형식의 직접 행동 유도.

• 30초: 훅(0~3초) + 문제(3~11초) + 해결(11~25초) + CTA(25~30초)
  - 문제 장면: 타겟이 공감할 상황 설정.
  - 해결 장면: 제품/서비스/정보가 어떻게 변화를 만드는지.

• 60초: 훅(0~5초) + 문제심화(5~17초) + 전환(17~47초) + 결론+CTA(47~60초)
  - 감정 곡선: 불안/걱정 → 공감 → 희망 → 행동.
  - 전환점에서 슬로건 자막 삽입 필수.


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
  "episode": {ep_num},
  "title": "{ep_plan.get('title', f'{ep_num}편')}",
  "format": "shortform",
  "duration": "{dna.duration}",

  "versions": {{
    "15sec": {{
      "structure": "훅(0~3초) + 핵심메시지(3~12초) + CTA(12~15초)",
      "scenes": [
        {{
          "scene_number": 1,
          "timecode": "0:00~0:03",
          "visual": "카메라 앵글·피사체·움직임 구체적 묘사",
          "caption": "화면에 표시될 자막 실제 문구",
          "audio": "나레이션 실제 문장(문어체) 또는 대사 전문(구어체) 또는 음악 방향"
        }}
      ],
      "key_caption": "이 버전에서 가장 강조할 자막 한 줄 (실제 문구)",
      "cta": "CTA 자막 문구 그대로"
    }},
    "30sec": {{
      "structure": "훅(0~3초) + 문제(3~11초) + 해결(11~25초) + CTA(25~30초)",
      "scenes": [
        {{
          "scene_number": 1,
          "timecode": "0:00~0:03",
          "visual": "구체적 묘사",
          "caption": "자막 실제 문구",
          "audio": "나레이션 또는 대사 전문"
        }}
      ],
      "key_caption": "핵심 자막 (실제 문구)",
      "cta": "CTA 문구 그대로"
    }},
    "60sec": {{
      "structure": "훅(0~5초) + 문제심화(5~17초) + 전환(17~47초) + 결론+CTA(47~60초)",
      "scenes": [
        {{
          "scene_number": 1,
          "timecode": "0:00~0:05",
          "visual": "구체적 묘사",
          "caption": "자막 실제 문구",
          "audio": "나레이션 또는 대사 전문"
        }}
      ],
      "key_caption": "슬로건이 들어가는 핵심 자막 (실제 문구)",
      "cta": "CTA 문구 그대로"
    }}
  }},

  "interview_questions": [],

  "closing_cta": {{
    "cta_type": "구독|공유|참여|신청 중 하나",
    "cta_text": "공통 CTA 자막 문구 그대로",
    "end_card": "로고 위치 / 계정 태그 / URL / 해시태그 구성"
  }},

  "series_hook": {{
    "cliffhanger_line": "다음 편 궁금증 유발 자막 문구 그대로 (없으면 null)",
    "callback_line": "이전 편 키워드를 이어받는 자막 또는 나레이션 (없으면 null)"
  }}
}}"""


# ─────────────────────────────────────────────
# 시리즈 연결고리 (후처리 패스)
# ─────────────────────────────────────────────

def _generate_series_hooks(dna: ConceptDNA, scripts: list) -> list:
    """모든 편 대본 완성 후 시리즈 연결고리 일괄 생성.

    각 편의 핵심 메시지를 파악한 뒤 클리프행어·복선·콜백을 설계.

    Args:
        dna: ConceptDNA
        scripts: 완성된 편별 대본 목록

    Returns:
        [{"episode": n, "cliffhanger": str, "callback": str}, ...]
    """
    prompt  = _build_series_hook_prompt(dna, scripts)
    result  = claude_client.call_json(prompt, max_tokens=2000)
    return result.get("hooks", [])


def _inject_hooks(scripts: list, hooks: list) -> None:
    """생성된 시리즈 훅을 각 편 대본의 series_hook에 주입.

    Args:
        scripts: 편별 대본 목록 (in-place 수정)
        hooks: _generate_series_hooks() 반환값
    """
    hook_map = {h.get("episode"): h for h in hooks}
    for script in scripts:
        ep = script.get("episode")
        if ep in hook_map:
            hook = hook_map[ep]
            script["series_hook"] = {
                "cliffhanger_line": hook.get("cliffhanger", ""),
                "callback_line":    hook.get("callback", ""),
            }


def _build_series_hook_prompt(dna: ConceptDNA, scripts: list) -> str:
    """시리즈 연결고리 생성용 프롬프트."""
    total = len(scripts)

    ep_summaries = []
    for s in scripts:
        ep_num   = s.get("episode", 0)
        title    = s.get("title", "")
        # 핵심 메시지: opening_hook 또는 첫 장면에서 추출
        hook_line = (s.get("opening_hook") or {}).get("hook_line", "")
        cta_raw   = s.get("closing_cta") or {}
        cta_text  = cta_raw.get("cta_text", cta_raw.get("cta_direction", "")) if isinstance(cta_raw, dict) else str(cta_raw)
        ep_summaries.append(
            f"  {ep_num}편 《{title}》\n"
            f"    오프닝 훅: {hook_line or '(없음)'}\n"
            f"    CTA: {cta_text or '(없음)'}"
        )
    episodes_block = "\n".join(ep_summaries)

    return f"""아래 {total}편 시리즈 영상의 대본 개요를 읽고,
편간 연결고리(클리프행어·복선·콜백)를 설계해주세요.

[시리즈 컨셉]
{dna.concept}

[확정 슬로건]
{dna.slogan}

[편별 개요]
{episodes_block}

[연결고리 설계 원칙]
- 클리프행어: 다음 편이 궁금해지는 질문·미완결 상황 (10~15자 내외)
- 복선: 현재 편에 심어두는 다음 편의 단서 (장면·대사·자막으로 구현 가능한 것)
- 콜백: 이전 편의 키워드/장면을 이번 편에서 회수하는 방식
- 1편은 클리프행어만 / 마지막 편은 콜백+전체 마무리 / 중간 편은 둘 다

아래 JSON으로만 출력하세요:
{{
  "hooks": [
    {{
      "episode": 1,
      "cliffhanger": "다음 편이 궁금해지는 문구",
      "callback": null
    }},
    {{
      "episode": 2,
      "cliffhanger": "...",
      "callback": "이전 편에서 이어받는 연결 요소"
    }}
  ]
}}"""


# ─────────────────────────────────────────────
# 유틸리티
# ─────────────────────────────────────────────

def _duration_to_seconds(duration_str: str) -> int:
    """러닝타임 문자열을 초 단위 정수로 변환.

    지원: '3분', '3분 30초', '90초', '3:30', '180'
    """
    if not duration_str:
        return 180  # 기본값 3분

    s     = duration_str.strip()
    total = 0

    m = re.search(r"(\d+)분", s)
    if m:
        total += int(m.group(1)) * 60

    m = re.search(r"(\d+)초", s)
    if m:
        total += int(m.group(1))

    # MM:SS
    m = re.fullmatch(r"(\d+):(\d{2})", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    # 숫자만
    if total == 0:
        m = re.fullmatch(r"\d+", s)
        if m:
            total = int(s)

    return total if total > 0 else 180


def _is_shortform(duration_seconds: int) -> bool:
    """초 단위 러닝타임이 숏폼 기준(60초) 이하인지 판단."""
    return duration_seconds <= _SHORTFORM_THRESHOLD


def _calc_scene_count(duration_seconds: int) -> int:
    """러닝타임(초) → 적정 장면 수 계산 (장면당 평균 12초)."""
    return max(3, min(duration_seconds // _SECS_PER_SCENE, 20))


def _calc_word_count(duration_s: int) -> int:
    """러닝타임(초) 기준 최소 글자 수 (30초당 300자 기준)."""
    return max(300, (duration_s // 30) * 300)


def _format_episode_plan(ep_plan: dict) -> str:
    """플래너 에피소드 계획을 프롬프트 텍스트로 포맷."""
    lines = [
        f"- 편 번호: {ep_plan.get('episode_number', '')}편",
        f"- 제목: {ep_plan.get('title', '')}",
        f"- 핵심 메시지: {ep_plan.get('core_message', '')}",
        f"- 타겟 시청자: {ep_plan.get('target_audience', '')}",
        f"- 핵심 장면 방향: {ep_plan.get('key_scene', '')}",
        f"- 차별화 포인트: {ep_plan.get('differentiation', '')}",
    ]
    return "\n".join(l for l in lines if not l.endswith(": "))


def _format_creative_context(dna: ConceptDNA) -> str:
    """크리에이티브 기준 블록 포맷."""
    lines = [
        f"- 핵심 컨셉: {dna.concept}",
        f"- 확정 슬로건: {dna.slogan}",
        f"- 톤앤매너: {dna.tone_and_manner}",
        f"- 감성 키워드: {', '.join(dna.tone_keywords)}" if dna.tone_keywords else "",
        f"- 비주얼 방향: {dna.visual_direction}",
        f"- 위기 제시: {dna.crisis_statement}",
        f"- 해결책 방향: {dna.solution_direction}",
    ]
    return "\n".join(l for l in lines if l and not l.endswith(": "))


def _format_series_context(ep_num: int, all_plans: list) -> str:
    """시리즈 전체 편 계획 요약 블록."""
    if not all_plans:
        return ""
    lines = ["\n━━━━━━━━━━━━━━━━━━━━━━━",
             "[시리즈 전체 구성]",
             "━━━━━━━━━━━━━━━━━━━━━━━"]
    for p in all_plans:
        n      = p.get("episode_number", "")
        title  = p.get("title", "")
        msg    = p.get("core_message", "")
        marker = " ← 현재 편" if n == ep_num else ""
        lines.append(f"  {n}편 《{title}》: {msg}{marker}")
    return "\n".join(lines)

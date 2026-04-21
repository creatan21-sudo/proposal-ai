# agents/storyboard.py
# STEP 8: DALL-E 3로 씬별 스토리보드 이미지 생성

import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from core.dna import ConceptDNA

_CRITICAL_PREFIX = (
    "ABSOLUTE RULES - NO EXCEPTIONS: "
    "1. ZERO text in image. Not a single letter, number, or symbol. "
    "No signs, no papers, no whiteboards, no screens with text, "
    "no cue cards, no scripts, no documents, no books with visible text, "
    "no name tags, no banners, no captions, no watermarks. "
    "If an object would normally have text, show it without text. "
    "2. No historical Korean elements. Modern only. "
    "3. Single frame, single scene. No panels, no collage. "
    "4. Pure visual storytelling. No text whatsoever. "
)


def _remove_text_props(scene_desc: str) -> str:
    """씬 설명에서 텍스트가 생성될 수 있는 소품 관련 단어 제거."""
    remove_words = [
        '큐시트', '대본', '스크립트', '종이', '문서',
        '칠판', '화이트보드', '현수막', '포스터', '간판',
        'cue card', 'script', 'paper', 'whiteboard',
        'sign', 'banner', 'poster', 'document',
    ]
    for word in remove_words:
        scene_desc = scene_desc.replace(word, '')
    return scene_desc.strip()

_STYLE_TEMPLATES = {
    "line": (
        _CRITICAL_PREFIX
        + "Style: storyboard frame, pen sketch, black and white line art. "
        + "장면: {scene_description}"
    ),
    "color": (
        _CRITICAL_PREFIX
        + "Style: storyboard frame, illustration style, colorful. "
        + "장면: {scene_description}"
    ),
    "photo": (
        _CRITICAL_PREFIX
        + "Style: storyboard frame, cinematic photo style, realistic. "
        + "장면: {scene_description}"
    ),
}

_DEFAULT_STYLE = "line"

_IS_PRODUCTION = bool(os.environ.get("RAILWAY_ENVIRONMENT"))
_STORYBOARD_BASE = Path("/app/data/storyboards") if _IS_PRODUCTION else Path("output/storyboards")


def _generate_one(scene: dict, scene_num: int, style: str,
                  case_id: int, api_key: str) -> dict:
    """씬 하나에 대해 DALL-E 3 이미지를 생성 후 로컬 저장."""
    try:
        from openai import OpenAI
    except ImportError:
        return {
            "scene_num": scene_num, "image_path": "", "image_url": "",
            "scene_description": "", "style": style,
            "ok": False, "error": "openai 패키지 없음 (pip install openai)",
        }

    scene_desc = (
        scene.get("visual_concept", "")
        or scene.get("visual", "")
        or scene.get("key_point", "")
        or scene.get("narration_key", "")
        or f"씬 {scene_num}"
    )
    scene_desc = _remove_text_props(scene_desc)
    # "cinematic still, ..., no text, no letters, photorealistic" 형식으로 래핑
    enhanced_desc = f"cinematic still, {scene_desc[:380]}, no text, no letters, photorealistic"
    template = _STYLE_TEMPLATES.get(style, _STYLE_TEMPLATES[_DEFAULT_STYLE])
    prompt = template.format(scene_description=enhanced_desc)

    client = OpenAI(api_key=api_key)
    try:
        resp = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            n=1,
        )
        image_url = resp.data[0].url

        save_dir = _STORYBOARD_BASE / str(case_id)
        save_dir.mkdir(parents=True, exist_ok=True)
        image_path = str(save_dir / f"{scene_num}.png")

        try:
            urllib.request.urlretrieve(image_url, image_path)
        except Exception as dl_err:
            print(f"  [스토리보드] 이미지 저장 실패 (씬 {scene_num}): {dl_err}")
            image_path = ""

        return {
            "scene_num": scene_num,
            "image_path": image_path,
            "image_url": image_url,
            "scene_description": scene_desc,
            "style": style,
            "ok": True,
        }
    except Exception as e:
        err_str = str(e).lower()
        if any(k in err_str for k in ("billing", "insufficient_quota", "quota", "credit", "payment")):
            err_msg = "OpenAI 크레딧 부족"
        else:
            err_msg = str(e)
        print(f"  [스토리보드] DALL-E 호출 실패 (씬 {scene_num}): {err_msg}")
        return {
            "scene_num": scene_num,
            "image_path": "",
            "image_url": "",
            "scene_description": scene_desc,
            "style": style,
            "ok": False,
            "error": str(e),
        }


def _extract_scenes(dna: ConceptDNA, max_cuts: int) -> list:
    """대본 스크립트에서 씬 목록 추출."""
    scripts = dna.scripts or []
    print(f"  [스토리보드] 파싱: scripts 항목 수={len(scripts)}")
    scenes = []
    for idx, sc in enumerate(scripts):
        if not isinstance(sc, dict):
            print(f"  [스토리보드] scripts[{idx}] 타입 오류: {type(sc)}")
            continue
        raw_scenes = sc.get("scenes", [])
        print(f"  [스토리보드] scripts[{idx}] scenes 수={len(raw_scenes)}")
        for scene in raw_scenes:
            if isinstance(scene, dict):
                scenes.append(scene)
            else:
                print(f"  [스토리보드] 씬 타입 오류: {type(scene)}, 값={str(scene)[:80]}")
            if len(scenes) >= max_cuts:
                break
        if len(scenes) >= max_cuts:
            break
    print(f"  [스토리보드] 씬 파싱 완료: {len(scenes)}개 (max_cuts={max_cuts})")
    return scenes


def run(dna: ConceptDNA, style: str = "line", progress_fn=None) -> dict:
    """스토리보드 이미지 생성.

    Returns:
        {
            "frames": [ {scene_num, image_path, image_url, scene_description, style}, ... ],
            "style": str,
            "total_scenes": int,
        }
    """
    from config import OPENAI_API_KEY

    if not OPENAI_API_KEY:
        return {"frames": [], "style": style, "total_scenes": 0,
                "error": "OPENAI_API_KEY 미설정"}

    # dna에서 max_cuts 결정 (step_instruction에 cuts:N 형태로 기록)
    step_inst = getattr(dna, "step_instruction", "") or ""
    max_cuts = 30
    if "cuts:" in step_inst:
        try:
            max_cuts = int(step_inst.split("cuts:")[1].split()[0])
        except Exception:
            pass

    scenes = _extract_scenes(dna, max_cuts)
    if not scenes:
        print(f"  [스토리보드] 씬 없음 — dna.scripts={dna.scripts!r:.200}")
        return {"frames": [], "style": style, "total_scenes": 0,
                "error": "씬 데이터 없음 — 대본 스텝 먼저 실행하세요"}

    total = min(len(scenes), max_cuts)
    case_id = getattr(dna, "case_id", 0) or 0
    print(f"  [스토리보드] 이미지 생성 시작: {total}컷 (case_id={case_id}, style={style})")
    results = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_generate_one, scene, i + 1, style, case_id, OPENAI_API_KEY): i
            for i, scene in enumerate(scenes[:total])
        }
        for future in as_completed(futures):
            try:
                res = future.result()
                results.append(res)
                if progress_fn:
                    done = len(results)
                    progress_fn({
                        "type": "step_progress",
                        "step": "storyboard",
                        "message": f"스토리보드 생성 중... ({done}/{total}컷)",
                    })
            except Exception as e:
                print(f"  [스토리보드] future 오류: {e!r}")

    results.sort(key=lambda x: x.get("scene_num", 0))

    ok_count   = sum(1 for r in results if r.get("ok"))
    fail_count = len(results) - ok_count
    print(f"  [스토리보드] 완료: {ok_count}/{len(results)}컷 성공, {fail_count}컷 실패")

    # 크레딧 부족 감지
    credit_error = any(
        "크레딧 부족" in (r.get("error") or "")
        for r in results
    )
    if credit_error and progress_fn:
        try:
            progress_fn({
                "type":    "log",
                "message": "❌ OpenAI 크레딧이 부족합니다. platform.openai.com → Billing에서 충전 후 재시도하세요.",
            })
        except Exception:
            pass

    if ok_count == 0:
        if progress_fn:
            try:
                progress_fn({
                    "type":    "log",
                    "message": "❌ 스토리보드 생성 실패 — 모든 씬 이미지 생성 불가",
                })
            except Exception:
                pass

    # DB 저장 (성공 씬만)
    if ok_count > 0:
        try:
            from database.db import save_storyboard
            save_storyboard(case_id=case_id, frames=results, style=style)
        except Exception as db_err:
            print(f"  [스토리보드] DB 저장 실패: {db_err}")

    return {
        "frames":       results,
        "style":        style,
        "total_scenes": len(results),
        "ok_count":     ok_count,
        "fail_count":   fail_count,
        "error":        "일부 씬 생성 실패" if fail_count > 0 else "",
    }

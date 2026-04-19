# agents/storyboard.py
# STEP 5.5: DALL-E 3로 씬별 스토리보드 이미지 생성

import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from core.dna import ConceptDNA

_STYLE_TEMPLATES = {
    "line":  "storyboard frame, pen sketch, black and white line art, {scene_description}",
    "color": "storyboard frame, illustration style, colorful, {scene_description}",
    "photo": "storyboard frame, cinematic photo style, realistic, {scene_description}",
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
        or scene.get("key_point", "")
        or scene.get("narration_key", "")
        or f"씬 {scene_num}"
    )
    template = _STYLE_TEMPLATES.get(style, _STYLE_TEMPLATES[_DEFAULT_STYLE])
    prompt = template.format(scene_description=scene_desc[:500])

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
        print(f"  [스토리보드] DALL-E 호출 실패 (씬 {scene_num}): {e}")
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
    scenes = []
    for sc in (dna.scripts or []):
        if not isinstance(sc, dict):
            continue
        for scene in sc.get("scenes", []):
            if isinstance(scene, dict):
                scenes.append(scene)
            if len(scenes) >= max_cuts:
                break
        if len(scenes) >= max_cuts:
            break
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
        return {"frames": [], "style": style, "total_scenes": 0,
                "error": "씬 데이터 없음 — 대본 스텝 먼저 실행하세요"}

    total = min(len(scenes), max_cuts)
    case_id = getattr(dna, "case_id", 0) or 0
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
                print(f"  [스토리보드] future 오류: {e}")

    results.sort(key=lambda x: x.get("scene_num", 0))

    # DB 저장
    try:
        from database.db import save_storyboard
        save_storyboard(case_id=case_id, frames=results, style=style)
    except Exception as db_err:
        print(f"  [스토리보드] DB 저장 실패: {db_err}")

    return {
        "frames": results,
        "style": style,
        "total_scenes": len(results),
    }

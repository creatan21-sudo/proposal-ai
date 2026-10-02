# agents/minutes_summarizer.py
# 역할: 제작부문 — 사전협상 회의록(착수보고 / 기술협상) 파일을 읽어 해당 항목에 들어갈 정리문 작성
#
# 파일 읽기(PDF·문서·이미지)와 큰 자료 나눠 읽기는 proposal_summarizer 의 것을 그대로 쓴다.
# 결과는 JSON 이 아닌 일반 텍스트 한 편 (■ 제목 / "- " 항목).

from concurrent.futures import ThreadPoolExecutor

from config import DEFAULT_MODEL
from agents.proposal_summarizer import (
    ALLOWED_EXT, IMAGES_PER_UNIT, DIRECT_TEXT_LIMIT, MAX_PARALLEL,
    ProposalSummaryError, _ask, _build_units, _unit_content, _pdf_block, _image_block,
    _pdf_pages_text, _friendly,
)

MINUTES_SECTIONS = {
    "kickoff_report": {
        "label": "착수보고",
        "spec": """■ 회의 개요
  일시, 장소, 참석자(발주처 / 우리 회사)
■ 보고 내용 요약
■ 발주처 의견·요청사항
■ 합의·결정 사항
■ 후속 조치 (할 일 / 담당 / 기한)
■ 제작 반영 포인트 (제작팀이 꼭 알아야 할 것)""",
    },
    "technical_discussion": {
        "label": "기술협상",
        "spec": """■ 협상 개요
  일시, 장소, 참석자(발주처 / 우리 회사)
■ 과업 범위 조정 사항 (제안서 대비 추가·삭제·변경)
■ 제작물·사양 변경 (종류·수량·분량·납품 형태)
■ 일정·납품 조건
■ 예산·계약 조건
■ 발주처 요구·확인 사항
■ 후속 조치 (할 일 / 담당 / 기한)
■ 제작 반영 포인트 (제작팀이 꼭 알아야 할 것)""",
    },
}

_RULES = """[작성 규칙]
- 회의록에 있는 내용만 쓴다. 해당 내용이 없는 항목은 "회의록에 언급 없음"이라고 적고 추측하지 않는다.
- 숫자·날짜·이름·발언 요지는 회의록 표기를 그대로 옮긴다.
- 사진·스캔 이미지라면 보이는 글자를 읽어서 옮긴다.
- 마크다운 기호(#, **, 표)를 쓰지 않는다. 제목은 ■, 항목은 "- "로 시작하는 일반 텍스트로 쓴다.
- 다른 설명 없이 정리문만 출력한다."""

_HEAD = """너는 영상·콘텐츠 제작사의 제작 PD다. 아래는 우리 회사가 수주한 사업의 {label} 회의록이다.
제작팀이 이 정리만 보고 회의 결과를 제작에 반영할 수 있도록 정리하라.

[사업 정보]
- 사업명: {project_name}
- 발주처: {client_name}
- 받은 자료: {file_list}

[정리 형식] 아래 순서와 제목을 그대로 사용.
{spec}

{rules}"""

_UNIT_PROMPT = """너는 영상·콘텐츠 제작사의 제작 PD다. 첨부는 {label} 회의록의 일부({part})다.
회의 일시·참석자, 논의·보고 내용, 발주처 의견·요구, 합의·결정 사항, 범위·사양·일정·예산 변경, 후속 조치를
빠짐없이 메모로 뽑아라. 원문 표현과 숫자를 그대로 살리고, 이미지라면 보이는 글자를 읽어 옮긴다.
마크다운 기호 없이 "- "로 시작하는 일반 텍스트로만 쓴다."""


def _clean(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1].strip()
    if not text:
        raise ProposalSummaryError("AI 응답이 비어 있습니다. 다시 시도해 주세요.")
    return text


def summarize_minutes_files(files: list, section_type: str, project_name: str = "", client_name: str = "",
                            model: str = DEFAULT_MODEL, progress=None) -> dict:
    """회의록 파일(여러 개) → {"content", "mode", "skipped"}"""
    cfg = MINUTES_SECTIONS[section_type]
    say = progress or (lambda s: None)
    say("회의록을 읽을 준비를 하는 중")
    units, skipped = _build_units(files, say)
    if not units:
        raise ProposalSummaryError("읽을 수 있는 자료가 없습니다" + (f": {', '.join(skipped)}" if skipped else ""))

    head = _HEAD.format(label=cfg["label"], project_name=project_name or "-", client_name=client_name or "-",
                        file_list=", ".join(n for _, n in files), spec=cfg["spec"], rules=_RULES)

    n_pdf = sum(1 for u in units if u["kind"] == "pdf")
    n_img_units = sum(1 for u in units if u["kind"] == "images")
    text_len = sum(len(u.get("text", "")) for u in units)
    if n_pdf <= 1 and n_img_units <= 1 and text_len <= DIRECT_TEXT_LIMIT:
        try:
            say(f"회의록 {len(files)}개를 읽고 정리하는 중")
            content = []
            for u in units:
                if u["kind"] == "pdf":
                    content += [{"type": "text", "text": f"[자료: {u['label']}]"}, _pdf_block(u["data"])]
                elif u["kind"] == "images":
                    for name, data in u["images"]:
                        content += [{"type": "text", "text": f"[이미지: {name}]"}, _image_block(data)]
                else:
                    content.append({"type": "text", "text": f"[자료: {u['label']}]\n{u['text']}"})
            content.append({"type": "text", "text": head})
            return {"content": _clean(_ask(content, model, 16000)), "mode": "direct", "skipped": skipped}
        except ProposalSummaryError:
            raise
        except Exception as e:
            print(f"[minutes] 1회 정리 실패 → 나눠 읽기로 재시도: {e}")

    say(f"회의록을 {len(units)}개로 나눠 읽는 중")

    def note(u):
        prompt = _UNIT_PROMPT.format(label=cfg["label"], part=u["label"])
        try:
            return u["label"], _ask(_unit_content(u, prompt), model, 8000)
        except Exception as e:
            if u["kind"] == "pdf":
                text = _pdf_pages_text(u["src"], *u["pages"])
                if len(text.strip()) >= 50:
                    return u["label"], _ask(f"{prompt}\n\n[자료: {u['label']}]\n{text}", model, 8000)
            raise

    errors, notes = [], []
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(units))) as ex:
        futures = [ex.submit(note, u) for u in units]
        for u, f in zip(units, futures):
            try:
                notes.append(f.result())
            except Exception as e:
                errors.append(e); skipped.append(f"{u['label']}(읽기 실패)")
    if not notes:
        raise ProposalSummaryError(_friendly(errors[0]) if errors else "회의록을 읽지 못했습니다")

    say("부분별 메모를 합쳐 정리하는 중")
    merged = "\n\n".join(f"[{label}]\n{text.strip()}" for label, text in notes)
    try:
        text = _clean(_ask(f"{head}\n\n아래는 회의록 전체를 부분별로 읽은 메모다(대괄호는 출처). 이 메모만을 근거로 정리하라.\n\n"
                           f"[부분별 메모]\n{merged}", model, 16000))
    except ProposalSummaryError:
        raise
    except Exception as e:
        raise ProposalSummaryError(_friendly(e)) from e
    return {"content": text, "mode": "notes", "skipped": skipped}

# agents/proposal_summarizer.py
# 역할: 제작부문 — 최종 제안서(PDF)를 읽어 '제작내용'과 '제안개요'를 생성
#
# 제안서는 디자인·이미지 비중이 높아 텍스트 추출만으로는 내용이 빠지기 쉬우므로
# 가능하면 PDF 자체를 Claude에 전달한다(페이지 이미지+텍스트를 함께 읽음).
# 페이지 수·용량 한도를 넘으면 pdfplumber 텍스트 추출로 대체한다.

import base64
from pathlib import Path

from config import DEFAULT_MODEL
from core.claude_client import get_client, _strip_markdown, _extract_object
from json_repair import repair_json

PDF_DIRECT_MAX_PAGES = 100            # Claude PDF 입력 한도
PDF_DIRECT_MAX_BYTES = 30 * 1024 * 1024
TEXT_MAX_CHARS = 150_000

_PROMPT = """너는 영상·콘텐츠 제작사의 제작 PD다. 첨부된 것은 우리 회사가 제출해 선정된 최종 제안서다.
제작팀이 이 문서만 보고 바로 제작 준비에 들어갈 수 있도록 두 가지 문서를 작성하라.

[사업 정보]
- 사업명: {project_name}
- 발주처: {client_name}

[1. production_content — 제작내용] 제작팀의 실행 기준 문서. 아래 순서와 제목을 그대로 사용.
■ 사업 개요
  목적, 예산, 사업 기간
■ 핵심 콘셉트·메시지
■ 제작물 목록
  제작물마다 한 줄: 종류 / 수량 / 분량(러닝타임·페이지 등) / 납품 형태
■ 콘텐츠별 구체 내용
  제작물(또는 편·회차)마다 소제목을 달고: 주제, 구성·줄거리(흐름 순서대로), 주요 장면·연출 포인트,
  출연자·내레이션·형식, 톤앤매너. 제안서에 적힌 구체적 내용을 빠짐없이 옮겨라.
■ 주요 일정
■ 발주처 요구사항·유의사항

[2. proposal_overview — 제안개요] 한 화면에 읽히는 요약.
■ 한 줄 정의
■ 제안 콘셉트
■ 핵심 전략·차별점 (3개 이내)
■ 평가 대응 포인트 (제안서가 강조한 심사 대응 내용)

[작성 규칙]
- 제안서에 있는 내용만 쓴다. 없는 항목은 "제안서에 명시 없음"이라고 적고 추측하지 않는다.
- 숫자(예산·수량·분량·날짜)는 제안서 표기를 그대로 옮긴다.
- 마크다운 기호(#, **, 표)를 쓰지 않는다. 제목은 ■, 항목은 "- "로 시작하는 일반 텍스트로 쓴다.

[출력] 다른 설명 없이 아래 JSON 하나만 출력.
{{"production_content": "...", "proposal_overview": "..."}}
"""


def _page_count(path: Path) -> int:
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


def _extract_text(path: Path) -> str:
    from agents.rfp_parser import extract_text
    return extract_text(str(path))[:TEXT_MAX_CHARS]


def _parse(raw: str) -> dict:
    cleaned = _strip_markdown(raw)
    obj = repair_json(_extract_object(cleaned) or cleaned, return_objects=True)
    if not isinstance(obj, dict):
        raise ValueError("AI 응답을 해석하지 못했습니다")
    pc = str(obj.get("production_content") or "").strip()
    po = str(obj.get("proposal_overview") or "").strip()
    if not pc and not po:
        raise ValueError("AI 응답이 비어 있습니다")
    return {"production_content": pc, "proposal_overview": po}


def summarize_proposal_pdf(pdf_path: str, project_name: str = "", client_name: str = "",
                           model: str = DEFAULT_MODEL) -> dict:
    """제안서 PDF → {"production_content", "proposal_overview", "mode"}

    mode: "pdf"(PDF 직접 전달) 또는 "text"(텍스트 추출 대체)
    """
    path = Path(pdf_path)
    prompt = _PROMPT.format(project_name=project_name or "-", client_name=client_name or "-")
    pages = _page_count(path)
    size = path.stat().st_size

    if 0 < pages <= PDF_DIRECT_MAX_PAGES and size <= PDF_DIRECT_MAX_BYTES:
        mode = "pdf"
        content = [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf",
                        "data": base64.standard_b64encode(path.read_bytes()).decode()}},
            {"type": "text", "text": prompt},
        ]
    else:
        mode = "text"
        text = _extract_text(path)
        if len(text.strip()) < 200:
            raise ValueError("PDF에서 글자를 거의 읽지 못했습니다 (페이지가 너무 많거나 이미지로만 된 PDF)")
        content = f"{prompt}\n\n[제안서 본문]\n{text}"

    resp = get_client().messages.create(
        model=model,
        max_tokens=16000,
        messages=[{"role": "user", "content": content}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    result = _parse(raw)
    result["mode"] = mode
    return result

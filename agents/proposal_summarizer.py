# agents/proposal_summarizer.py
# 역할: 제작부문 — 최종 제안서(PDF)를 읽어 '제작내용'과 '제안개요'를 생성
#
# 실제 제안서는 디자인·이미지 비중이 크고 용량·쪽수가 커서 한 번에 보내면
#   (1) 요청 용량 한도(32MB, base64로 약 1.33배 증가) 초과
#   (2) 페이지 이미지 토큰이 컨텍스트 한도 초과
#   (3) 긴 응답이 잘려 JSON 해석 실패
# 가 생긴다. 그래서:
#   - 작은 PDF(≤ CHUNK_PAGES쪽, ≤ CHUNK_BYTES)는 PDF 그대로 1회 호출
#   - 큰 PDF는 pypdf로 쪽 단위 분할 → 조각별로 '제작 관점 상세 메모' 추출(병렬) → 메모를 합쳐 최종 정리
#   - PDF 직접 전달이 실패하면 텍스트 추출 방식으로 대체
#   - 모든 호출은 스트리밍(긴 응답 타임아웃 방지)

import base64
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import DEFAULT_MODEL
from core.claude_client import get_client, _strip_markdown, _extract_object
from json_repair import repair_json

CHUNK_PAGES = 20                     # 조각당 최대 쪽수 (페이지 이미지 토큰 여유 확보)
CHUNK_BYTES = 18 * 1024 * 1024       # 조각당 최대 원본 크기 (base64 후 ~24MB < 32MB)
MAX_PARALLEL = 4
TEXT_MAX_CHARS = 150_000

_RULES = """[작성 규칙]
- 제안서에 있는 내용만 쓴다. 없는 항목은 "제안서에 명시 없음"이라고 적고 추측하지 않는다.
- 숫자(예산·수량·분량·날짜)는 제안서 표기를 그대로 옮긴다.
- 마크다운 기호(#, **, 표)를 쓰지 않는다. 제목은 ■, 항목은 "- "로 시작하는 일반 텍스트로 쓴다."""

_FINAL_SPEC = """[1. production_content — 제작내용] 제작팀의 실행 기준 문서. 아래 순서와 제목을 그대로 사용.
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
■ 평가 대응 포인트 (제안서가 강조한 심사 대응 내용)"""

_HEAD = """너는 영상·콘텐츠 제작사의 제작 PD다. 우리 회사가 제출해 선정된 최종 제안서를 바탕으로,
제작팀이 이 문서만 보고 바로 제작 준비에 들어갈 수 있도록 두 가지 문서를 작성하라.

[사업 정보]
- 사업명: {project_name}
- 발주처: {client_name}
"""

_OUT = """[출력] 다른 설명 없이 아래 JSON 하나만 출력.
{"production_content": "...", "proposal_overview": "..."}"""

_CHUNK_PROMPT = """너는 영상·콘텐츠 제작사의 제작 PD다. 첨부는 우리 회사 최종 제안서의 일부({part})다.
이 부분에서 제작에 필요한 정보를 빠짐없이 뽑아 메모로 정리하라:
사업 목적·예산·기간, 콘셉트·핵심 메시지, 제작물(종류·수량·분량·납품 형태),
콘텐츠별 구체 내용(편·회차별 주제, 구성·줄거리, 주요 장면·연출, 출연·내레이션·형식, 톤),
일정, 발주처 요구사항·유의사항, 제안 전략·차별점, 평가 대응 내용.
이 부분에 해당 내용이 없으면 그 항목은 생략한다. 원문 표현과 숫자를 그대로 살려라.
마크다운 기호 없이 "- "로 시작하는 일반 텍스트로만 쓴다."""


class ProposalSummaryError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 한글 오류"""


# ── Claude 호출 (스트리밍) ──────────────────────────────
def _ask(content, model: str, max_tokens: int) -> str:
    with get_client().messages.stream(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        msg = stream.get_final_message()
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    if getattr(msg, "stop_reason", "") == "max_tokens":
        print(f"[proposal] 경고: 응답이 max_tokens({max_tokens})에서 잘림")
    return text


def _pdf_block(data: bytes) -> dict:
    return {"type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.standard_b64encode(data).decode()}}


def _parse(raw: str) -> dict:
    cleaned = _strip_markdown(raw)
    obj = repair_json(_extract_object(cleaned) or cleaned, return_objects=True)
    if not isinstance(obj, dict):
        raise ProposalSummaryError("AI 응답을 해석하지 못했습니다. 다시 시도해 주세요.")
    pc = str(obj.get("production_content") or "").strip()
    po = str(obj.get("proposal_overview") or "").strip()
    if not pc and not po:
        raise ProposalSummaryError("AI 응답이 비어 있습니다. 다시 시도해 주세요.")
    return {"production_content": pc, "proposal_overview": po}


# ── PDF 분할 ─────────────────────────────────────────
def _split_pdf(data: bytes) -> list:
    """PDF bytes → [(시작쪽, 끝쪽, 조각 bytes)] — 쪽수·용량 한도에 맞춰 분할"""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(data))
    n = len(reader.pages)

    def build(a, b):
        w = PdfWriter()
        for i in range(a, b):
            w.add_page(reader.pages[i])
        buf = io.BytesIO(); w.write(buf)
        return buf.getvalue()

    chunks, start = [], 0
    while start < n:
        end = min(start + CHUNK_PAGES, n)
        part = build(start, end)
        while len(part) > CHUNK_BYTES and end - start > 1:      # 너무 크면 쪽수를 줄임
            end = start + max(1, (end - start) // 2)
            part = build(start, end)
        if len(part) > CHUNK_BYTES:
            raise ProposalSummaryError(
                f"{start + 1}쪽 한 장의 용량이 너무 큽니다({len(part) // (1024*1024)}MB). "
                "PDF를 '최적화/압축'해서 다시 올려 주세요.")
        chunks.append((start + 1, end, part))
        start = end
    return chunks


# ── 정리 방식별 구현 ─────────────────────────────────────
def _summarize_direct(data: bytes, head: str, model: str) -> dict:
    raw = _ask([_pdf_block(data), {"type": "text", "text": f"{head}\n{_FINAL_SPEC}\n\n{_RULES}\n\n{_OUT}"}],
               model, 32000)
    return _parse(raw)


def _summarize_chunked(chunks: list, head: str, model: str, total_pages: int) -> dict:
    def note(c):
        a, b, part = c
        label = f"{a}~{b}쪽 / 전체 {total_pages}쪽"
        return label, _ask([_pdf_block(part), {"type": "text", "text": _CHUNK_PROMPT.format(part=label)}],
                           model, 8000)
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(chunks))) as ex:
        notes = list(ex.map(note, chunks))
    merged = "\n\n".join(f"[{label}]\n{text.strip()}" for label, text in notes)
    prompt = (f"{head}\n아래는 제안서 전체를 부분별로 읽고 정리한 메모다. 이 메모만을 근거로 작성하라.\n\n"
              f"{_FINAL_SPEC}\n\n{_RULES}\n\n{_OUT}\n\n[부분별 메모]\n{merged}")
    return _parse(_ask(prompt, model, 32000))


def _summarize_text(path: Path, head: str, model: str) -> dict:
    from agents.rfp_parser import extract_text
    text = extract_text(str(path))[:TEXT_MAX_CHARS]
    if len(text.strip()) < 200:
        raise ProposalSummaryError("PDF에서 글자를 거의 읽지 못했습니다. 이미지로만 된 PDF라면 글자가 살아 있는 PDF로 다시 저장해 올려 주세요.")
    prompt = f"{head}\n{_FINAL_SPEC}\n\n{_RULES}\n\n{_OUT}\n\n[제안서 본문]\n{text}"
    return _parse(_ask(prompt, model, 32000))


def _friendly(e: Exception) -> str:
    """API 오류 → 사용자용 한글 설명 (원문 일부 포함)"""
    if isinstance(e, ProposalSummaryError):
        return str(e)
    msg = str(e)
    low = msg.lower()
    if "credit balance" in low or "billing" in low:
        return "AI 사용 크레딧이 부족합니다. 관리자에게 알려주세요."
    if "too long" in low or "too many tokens" in low or "context" in low:
        return "제안서 분량이 AI가 한 번에 읽을 수 있는 양을 넘었습니다."
    if "413" in low or "request_too_large" in low or "too large" in low:
        return "제안서 용량이 AI 요청 한도를 넘었습니다."
    if "overloaded" in low or "529" in low or "rate" in low:
        return "AI 서버가 혼잡합니다. 잠시 후 다시 시도해 주세요."
    return f"AI 정리 중 오류: {msg[:300]}"


def summarize_proposal_pdf(pdf_path: str, project_name: str = "", client_name: str = "",
                           model: str = DEFAULT_MODEL, progress=None) -> dict:
    """제안서 PDF → {"production_content", "proposal_overview", "mode"}

    mode: "pdf"(통째로) / "pdf-chunked"(분할) / "text"(텍스트 추출 대체)
    progress: 진행 상황 문자열을 받는 콜백 (선택)
    오류는 ProposalSummaryError(사용자용 메시지)로 올린다.
    """
    say = progress or (lambda s: None)
    path = Path(pdf_path)
    data = path.read_bytes()
    head = _HEAD.format(project_name=project_name or "-", client_name=client_name or "-")
    errors = []

    try:
        say("제안서를 나누는 중")
        chunks = _split_pdf(data)
        total = chunks[-1][1] if chunks else 0
        if len(chunks) == 1:
            say(f"제안서 {total}쪽을 읽는 중")
            r = _summarize_direct(chunks[0][2], head, model); r["mode"] = "pdf"
        else:
            say(f"제안서 {total}쪽을 {len(chunks)}개로 나눠 읽는 중")
            r = _summarize_chunked(chunks, head, model, total); r["mode"] = "pdf-chunked"
        return r
    except ProposalSummaryError as e:
        errors.append(e)
    except Exception as e:
        print(f"[proposal] PDF 방식 실패 → 텍스트 방식으로 재시도: {e}")
        errors.append(e)

    try:
        say("글자 추출 방식으로 다시 정리하는 중")
        r = _summarize_text(path, head, model); r["mode"] = "text"
        return r
    except Exception as e:
        print(f"[proposal] 텍스트 방식도 실패: {e}")
        first = errors[0] if errors else e
        raise ProposalSummaryError(_friendly(first) + (f" / 대체 방식: {_friendly(e)}" if errors else "")) from e

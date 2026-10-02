# agents/proposal_summarizer.py
# 역할: 제작부문 — 최종 제안서 자료(여러 파일)를 읽어 '제작내용'과 '제안개요'를 생성
#
# 지원 형식
#   - PDF                     : PDF 그대로 Claude에 전달(페이지 이미지+텍스트) — 쪽 단위 분할
#   - HWP/HWPX/DOCX/PPTX/TXT/MD/CSV : 글자 추출 후 전달
#   - JPG/PNG/GIF/WEBP/BMP    : 이미지로 전달(긴 변 1568px로 축소)
#
# 실제 자료는 크고 많아서 한 번에 보내면 요청 용량(32MB)·컨텍스트 한도를 넘거나 응답이 잘린다.
#   - 자료가 작으면(PDF 1조각 또는 짧은 글/이미지 몇 장) 1회 호출로 바로 정리
#   - 크면 '읽기 단위'(PDF 20쪽 조각 / 글 6만자 조각 / 이미지 8장 묶음)마다 제작 관점 메모를 뽑고(병렬),
#     메모를 합쳐 최종 정리 — 메모에는 출처(파일명·쪽)를 붙인다
#   - PDF 조각 읽기가 실패하면 그 조각은 글자 추출로 대체
#   - 모든 호출은 스트리밍(긴 응답 타임아웃 방지)

import base64
import io
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import DEFAULT_MODEL
from core.claude_client import get_client, _strip_markdown, _extract_object
from json_repair import repair_json

CHUNK_PAGES = 20                     # PDF 조각당 최대 쪽수
CHUNK_BYTES = 18 * 1024 * 1024       # PDF 조각당 최대 원본 크기 (base64 후 ~24MB < 32MB)
TEXT_CHUNK_CHARS = 60_000            # 글 조각 크기
IMAGES_PER_UNIT = 8                  # 이미지 묶음 크기
IMAGE_MAX_SIDE = 1568                # 이미지 긴 변 (Claude 권장 해상도)
DIRECT_TEXT_LIMIT = 120_000          # 글 합계가 이보다 작고 PDF·이미지가 적으면 1회 호출
MAX_PARALLEL = 4

PDF_EXT = {".pdf"}
DOC_EXT = {".hwp", ".hwpx", ".docx", ".pptx", ".txt", ".md", ".csv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
ALLOWED_EXT = PDF_EXT | DOC_EXT | IMAGE_EXT

_RULES = """[작성 규칙]
- 자료에 있는 내용만 쓴다. 없는 항목은 "제안서에 명시 없음"이라고 적고 추측하지 않는다.
- 숫자(예산·수량·분량·날짜)는 자료 표기를 그대로 옮긴다.
- 자료끼리 내용이 다르면 최종 제안서(본문)를 우선하고, 다른 자료의 내용은 "(참고: 파일명)"으로 덧붙인다.
- 마크다운 기호(#, **, 표)를 쓰지 않는다. 제목은 ■, 항목은 "- "로 시작하는 일반 텍스트로 쓴다."""

_FINAL_SPEC = """[1. production_content — 제작내용] 제작팀의 실행 기준 문서. 아래 순서와 제목을 그대로 사용.
■ 사업 개요
  목적, 예산, 사업 기간
■ 핵심 콘셉트·메시지
■ 제작물 목록
  제작물마다 한 줄: 종류 / 수량 / 분량(러닝타임·페이지 등) / 납품 형태
■ 콘텐츠별 구체 내용
  제작물(또는 편·회차)마다 소제목을 달고: 주제, 구성·줄거리(흐름 순서대로), 주요 장면·연출 포인트,
  출연자·내레이션·형식, 톤앤매너. 자료에 적힌 구체적 내용을 빠짐없이 옮겨라.
■ 주요 일정
■ 발주처 요구사항·유의사항

[2. proposal_overview — 제안개요] 한 화면에 읽히는 요약.
■ 한 줄 정의
■ 제안 콘셉트
■ 핵심 전략·차별점 (3개 이내)
■ 평가 대응 포인트 (제안서가 강조한 심사 대응 내용)"""

_HEAD = """너는 영상·콘텐츠 제작사의 제작 PD다. 우리 회사가 제출해 선정된 제안서와 관련 자료를 바탕으로,
제작팀이 이 문서만 보고 바로 제작 준비에 들어갈 수 있도록 두 가지 문서를 작성하라.

[사업 정보]
- 사업명: {project_name}
- 발주처: {client_name}
- 받은 자료: {file_list}
"""

_OUT = """[출력] 다른 설명 없이 아래 JSON 하나만 출력.
{"production_content": "...", "proposal_overview": "..."}"""

_UNIT_PROMPT = """너는 영상·콘텐츠 제작사의 제작 PD다. 첨부는 우리 회사 제안 관련 자료의 일부({part})다.
이 부분에서 제작에 필요한 정보를 빠짐없이 뽑아 메모로 정리하라:
사업 목적·예산·기간, 콘셉트·핵심 메시지, 제작물(종류·수량·분량·납품 형태),
콘텐츠별 구체 내용(편·회차별 주제, 구성·줄거리, 주요 장면·연출, 출연·내레이션·형식, 톤),
일정, 발주처 요구사항·유의사항, 제안 전략·차별점, 평가 대응 내용.
이미지라면 화면에 보이는 글자·장면·구성도 읽어서 옮긴다.
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


def _image_block(data: bytes) -> dict:
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
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


# ── 파일 → 읽기 단위 ─────────────────────────────────────
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
        while len(part) > CHUNK_BYTES and end - start > 1:
            end = start + max(1, (end - start) // 2)
            part = build(start, end)
        if len(part) > CHUNK_BYTES:
            raise ProposalSummaryError(
                f"{start + 1}쪽 한 장의 용량이 너무 큽니다({len(part) // (1024*1024)}MB). "
                "PDF를 '최적화/압축'해서 다시 올려 주세요.")
        chunks.append((start + 1, end, part))
        start = end
    return chunks


def _pdf_pages_text(data: bytes, a: int, b: int) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join((pdf.pages[i].extract_text() or "") for i in range(a - 1, min(b, len(pdf.pages))))


def _extract_docx(path: Path) -> str:
    """DOCX 본문 글자 (python-docx 없이 XML에서 직접)"""
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    lines = []
    for p in root.iter(f"{ns}p"):
        t = "".join(x.text or "" for x in p.iter(f"{ns}t"))
        if t.strip():
            lines.append(t)
    return "\n".join(lines)


def _extract_doc_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".txt", ".md", ".csv"):
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp949", "euc-kr"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="ignore")
    if ext == ".docx":
        return _extract_docx(path)
    if ext == ".pptx":
        from agents.rfp_parser import _extract_pptx_text
        return _extract_pptx_text(path)
    from agents.rfp_parser import extract_text      # hwp / hwpx
    return extract_text(str(path))


def _prepare_image(path: Path) -> bytes:
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=85)
        return buf.getvalue()


def _build_units(files: list, say) -> tuple:
    """files: [(경로, 원래 파일명)] → (읽기 단위 목록, 건너뛴 파일 안내)
    단위: {"kind": "pdf"|"text"|"images", "label": 출처, ...}"""
    units, skipped, images = [], [], []
    for path, name in files:
        path = Path(path)
        ext = Path(name).suffix.lower() or path.suffix.lower()
        try:
            if ext in PDF_EXT:
                data = path.read_bytes()
                chunks = _split_pdf(data)
                total = chunks[-1][1] if chunks else 0
                for a, b, part in chunks:
                    label = f"{name} {a}~{b}쪽" if len(chunks) > 1 else f"{name} ({total}쪽)"
                    units.append({"kind": "pdf", "label": label, "data": part, "src": data, "pages": (a, b)})
            elif ext in IMAGE_EXT:
                images.append((name, _prepare_image(path)))
            elif ext in DOC_EXT:
                text = (_extract_doc_text(path) or "").strip()
                if len(text) < 20:
                    skipped.append(f"{name}(글자를 읽지 못함)")
                    continue
                for i in range(0, len(text), TEXT_CHUNK_CHARS):
                    part = text[i:i + TEXT_CHUNK_CHARS]
                    label = name if len(text) <= TEXT_CHUNK_CHARS else f"{name} {i // TEXT_CHUNK_CHARS + 1}부분"
                    units.append({"kind": "text", "label": label, "text": part})
            else:
                skipped.append(f"{name}(지원하지 않는 형식)")
        except ProposalSummaryError:
            raise
        except Exception as e:
            print(f"[proposal] {name} 읽기 실패: {e}")
            skipped.append(f"{name}(읽기 실패)")
    for i in range(0, len(images), IMAGES_PER_UNIT):
        group = images[i:i + IMAGES_PER_UNIT]
        units.append({"kind": "images", "label": "이미지: " + ", ".join(n for n, _ in group), "images": group})
    return units, skipped


def _unit_content(u: dict, prompt_text: str) -> list:
    if u["kind"] == "pdf":
        return [_pdf_block(u["data"]), {"type": "text", "text": prompt_text}]
    if u["kind"] == "images":
        blocks = []
        for name, data in u["images"]:
            blocks += [{"type": "text", "text": f"[이미지: {name}]"}, _image_block(data)]
        return blocks + [{"type": "text", "text": prompt_text}]
    return [{"type": "text", "text": f"{prompt_text}\n\n[자료: {u['label']}]\n{u['text']}"}]


def _friendly(e: Exception) -> str:
    """API 오류 → 사용자용 한글 설명 (원문 일부 포함)"""
    if isinstance(e, ProposalSummaryError):
        return str(e)
    msg = str(e)
    low = msg.lower()
    if "credit balance" in low or "billing" in low:
        return "AI 사용 크레딧이 부족합니다. 관리자에게 알려주세요."
    if "too long" in low or "too many tokens" in low or "context" in low:
        return "자료 분량이 AI가 한 번에 읽을 수 있는 양을 넘었습니다."
    if "413" in low or "request_too_large" in low or "too large" in low:
        return "자료 용량이 AI 요청 한도를 넘었습니다."
    if "overloaded" in low or "529" in low or "rate" in low:
        return "AI 서버가 혼잡합니다. 잠시 후 다시 시도해 주세요."
    return f"AI 정리 중 오류: {msg[:300]}"


def summarize_proposal_files(files: list, project_name: str = "", client_name: str = "",
                             model: str = DEFAULT_MODEL, progress=None) -> dict:
    """제안 자료 여러 개 → {"production_content", "proposal_overview", "mode", "skipped"}

    files: [(저장 경로, 원래 파일명)] — 올린 순서대로 (먼저 올린 PDF를 본문으로 우선)
    mode: "direct"(1회 호출) / "notes"(단위별 메모 → 통합)
    오류는 ProposalSummaryError(사용자용 메시지)로 올린다.
    """
    say = progress or (lambda s: None)
    say("자료를 읽을 준비를 하는 중")
    units, skipped = _build_units(files, say)
    if not units:
        raise ProposalSummaryError("읽을 수 있는 자료가 없습니다" + (f": {', '.join(skipped)}" if skipped else ""))

    file_list = ", ".join(n for _, n in files)
    head = _HEAD.format(project_name=project_name or "-", client_name=client_name or "-", file_list=file_list)
    final_prompt = f"{head}\n{_FINAL_SPEC}\n\n{_RULES}\n\n{_OUT}"

    n_pdf = sum(1 for u in units if u["kind"] == "pdf")
    n_img = sum(len(u["images"]) for u in units if u["kind"] == "images")
    text_len = sum(len(u.get("text", "")) for u in units)
    direct = (n_pdf <= 1 and n_img <= IMAGES_PER_UNIT and text_len <= DIRECT_TEXT_LIMIT
              and sum(1 for u in units if u["kind"] == "images") <= 1)

    try:
        if direct:
            say(f"자료 {len(files)}개를 읽고 정리하는 중")
            content = []
            for u in units:
                if u["kind"] == "pdf":
                    content += [{"type": "text", "text": f"[자료: {u['label']}]"}, _pdf_block(u["data"])]
                elif u["kind"] == "images":
                    for name, data in u["images"]:
                        content += [{"type": "text", "text": f"[이미지: {name}]"}, _image_block(data)]
                else:
                    content.append({"type": "text", "text": f"[자료: {u['label']}]\n{u['text']}"})
            content.append({"type": "text", "text": final_prompt})
            r = _parse(_ask(content, model, 32000)); r["mode"] = "direct"
            r["skipped"] = skipped
            return r
    except ProposalSummaryError:
        raise
    except Exception as e:
        print(f"[proposal] 1회 정리 실패 → 나눠 읽기로 재시도: {e}")

    say(f"자료를 {len(units)}개로 나눠 읽는 중")

    def note(u):
        prompt = _UNIT_PROMPT.format(part=u["label"])
        try:
            return u["label"], _ask(_unit_content(u, prompt), model, 8000)
        except Exception as e:
            if u["kind"] == "pdf":            # PDF 조각을 못 읽으면 그 쪽들의 글자로 대체
                print(f"[proposal] {u['label']} PDF 읽기 실패 → 글자 추출로 대체: {e}")
                text = _pdf_pages_text(u["src"], *u["pages"])
                if len(text.strip()) >= 50:
                    return u["label"], _ask(f"{prompt}\n\n[자료: {u['label']}]\n{text}", model, 8000)
            raise

    errors = []
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(units))) as ex:
        futures = [ex.submit(note, u) for u in units]
        notes = []
        for u, f in zip(units, futures):
            try:
                notes.append(f.result())
            except Exception as e:
                errors.append(e)
                skipped.append(f"{u['label']}(읽기 실패)")
    if not notes:
        raise ProposalSummaryError(_friendly(errors[0]) if errors else "자료를 읽지 못했습니다")

    say("부분별 메모를 합쳐 최종 정리하는 중")
    merged = "\n\n".join(f"[{label}]\n{text.strip()}" for label, text in notes)
    prompt = (f"{head}\n아래는 자료 전체를 부분별로 읽고 정리한 메모다(대괄호는 출처). 이 메모만을 근거로 작성하라.\n\n"
              f"{_FINAL_SPEC}\n\n{_RULES}\n\n{_OUT}\n\n[부분별 메모]\n{merged}")
    try:
        r = _parse(_ask(prompt, model, 32000))
    except ProposalSummaryError:
        raise
    except Exception as e:
        raise ProposalSummaryError(_friendly(e)) from e
    r["mode"] = "notes"
    r["skipped"] = skipped
    return r


def summarize_proposal_pdf(pdf_path: str, project_name: str = "", client_name: str = "",
                           model: str = DEFAULT_MODEL, progress=None) -> dict:
    """(이전 호환) PDF 한 개 정리"""
    return summarize_proposal_files([(pdf_path, Path(pdf_path).name)], project_name, client_name, model, progress)

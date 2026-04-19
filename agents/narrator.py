# agents/narrator.py
# STEP 3: 전략 내러티브 생성
# - 리서치 직후 실행, 전략 수립 전 방향 설정
# - 20줄 내외 5섹션 내러티브 → 사용자 컨펌 or 재생성

from core.dna import ConceptDNA, dna_to_context_string, update_dna
from core.claude_client import call

_NARRATOR_MODEL     = "claude-sonnet-4-6"
_NARRATOR_MAX_TOKENS = 2000


def run(dna: ConceptDNA) -> dict:
    """전략 내러티브 생성.

    Returns:
        {
          "narrative": str,          # 전체 내러티브 텍스트
          "sections": {              # 5개 섹션별 파싱 결과
            "real_needs": str,
            "strategy_direction": str,
            "concept_hint": str,
            "differentiation": str,
            "overall_flow": str,
          }
        }
    """
    context = dna_to_context_string(dna)

    feedback_block = ""
    if dna.user_feedback:
        feedback_block = f"\n\n[사용자 수정 지시]\n{dna.user_feedback}\n위 지시를 반영하여 재작성하세요."

    prompt = f"""당신은 정부 입찰 제안서의 전략 총괄 디렉터입니다.
아래 프로젝트 정보를 바탕으로 이 제안의 전략 방향을 20줄 내외의 내러티브로 작성하세요.

[프로젝트 정보]
{context}{feedback_block}

다음 5개 섹션을 각각 3~5줄로 작성하세요. 각 섹션은 【섹션명】으로 시작하세요.

【발주처 진짜 니즈】
- 제안요청서 이면의 진짜 필요를 꿰뚫어 작성
- 표면적 요구사항이 아니라 발주처가 해결하고 싶은 근본 문제
- 구체적 수치·상황·배경을 포함할 것

【전략 방향】
- 이 니즈를 해결하기 위한 우리만의 전략적 접근법
- 경쟁사와 차별되는 방향성을 명확히 제시
- "~을 통해 ~을 달성한다" 형태의 실행 가능한 방향

【컨셉 힌트】
- 영상 컨셉의 핵심 빅아이디어 방향 (슬로건이 아닌 전략 프레임)
- 발주처의 정체성과 니즈를 하나로 꿰는 개념적 실마리
- 이후 크리에이티브 개발의 기준점이 될 아이디어

【차별화 포인트】
- 인터즈(우리 회사)만이 줄 수 있는 차별화 근거 3가지
- 추상적 선언 금지, 각각 구체적 근거 또는 방법론 제시
- 평가위원이 수주 이유로 납득할 수 있는 수준으로 작성

【전체 흐름】
- STEP 1(리서치)부터 STEP 7(최종 제안)까지의 제안 전략 전체 흐름
- 각 단계에서 무엇을 강조하고 어떻게 연결되는지 설명
- 이 제안이 평가위원에게 어떤 인상을 남길지 한 줄 마무리

【절대 원칙】
- 각 섹션은 반드시 구체적이고 실행 가능한 내용으로 채울 것
- "~할 것입니다", "~를 위해 노력하겠습니다" 같은 의례적 표현 금지
- 발주처명, 사업명, 영상 종류를 자연스럽게 녹여 쓸 것
- 전체 분량 20줄 이상"""

    try:
        raw = call(prompt, model=_NARRATOR_MODEL, max_tokens=_NARRATOR_MAX_TOKENS)
    except Exception as e:
        # 오류를 그대로 re-raise 하되, context 정보 추가
        raise RuntimeError(
            f"내러티브 생성 실패 ({type(e).__name__}): {e}\n"
            f"발주처={dna.client_name!r}, 과업={dna.project_name!r}"
        ) from e

    if not raw or not raw.strip():
        raise RuntimeError(
            f"내러티브 생성 결과가 비어있습니다. "
            f"발주처={dna.client_name!r}, 과업={dna.project_name!r}"
        )

    sections = _parse_sections(raw)
    # DNA에 내러티브 저장 — 파이프라인 완료 시 dna_json으로 DB에 영속
    update_dna(dna, {"narrative": raw})
    return {"narrative": raw, "sections": sections}


# ─────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────

_SECTION_KEYS = [
    ("real_needs",          "발주처 진짜 니즈"),
    ("strategy_direction",  "전략 방향"),
    ("concept_hint",        "컨셉 힌트"),
    ("differentiation",     "차별화 포인트"),
    ("overall_flow",        "전체 흐름"),
]


def _parse_sections(text: str) -> dict:
    """내러티브 텍스트를 5개 섹션으로 파싱."""
    sections = {key: "" for key, _ in _SECTION_KEYS}
    current_key = None
    lines = text.split("\n")

    for line in lines:
        stripped = line.strip()
        matched = False
        for key, label in _SECTION_KEYS:
            if label in stripped and stripped.startswith("【"):
                current_key = key
                matched = True
                break
        if not matched and current_key:
            sections[current_key] += line + "\n"

    # 각 섹션 앞뒤 공백 정리
    return {k: v.strip() for k, v in sections.items()}

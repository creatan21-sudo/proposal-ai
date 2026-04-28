# agents/creative.py
# STEP 3: 컨셉/슬로건 에이전트 (크리에이티브 디렉터)
# 역할: 전략 방향(STEP 2)을 창의적 표현으로 전환
#
# 출력:
#   1. 핵심 컨셉 (캠페인 빅아이디어 한 줄)
#   2. 슬로건 3개 후보 + 확정 1순위
#   3. 톤앤매너 (감성 키워드 5개 + 설명)
#   4. 금지 표현/이미지 방향
#   5. 비주얼 레퍼런스 방향
#
# 핵심 원칙:
#   - 기관 유형별 톤앤매너 프리셋 자동 주입
#   - 전략의 crisis_statement·solution_direction을 컨셉으로 승화
#   - 확정된 컨셉·슬로건은 이후 모든 에이전트의 기준점이 됨

from core import claude_client
from core.dna import ConceptDNA, update_dna, dna_to_context_string, wrap_prompt_with_instruction
from database.db import save_creative


# ─────────────────────────────────────────────
# 기관 유형별 톤앤매너 프리셋
# ─────────────────────────────────────────────

_TONE_PRESETS: dict[str, dict] = {
    "중앙부처": {
        "base_tone":    "신뢰·전문·권위",
        "keywords":     ["신뢰감", "전문성", "공신력", "안정감", "명확성"],
        "visual_hint":  "정제된 화면 구성, 공식적 공간(청사·현장), 절제된 색감(딥블루·그레이)",
        "avoid":        ["과도한 감성·눈물 소구", "유머·코믹 연출", "캐주얼 자막체", "연예인 중심 구성"],
    },
    "지자체": {
        "base_tone":    "친근·공감·생활밀착",
        "keywords":     ["친근함", "공감", "따뜻함", "생활감", "지역애"],
        "visual_hint":  "따뜻한 자연광, 실제 주민 인터뷰, 지역 랜드마크·골목, 포근한 색감",
        "avoid":        ["딱딱한 공문체 내레이션", "과도한 전문용어", "수도권 중심 배경"],
    },
    "의회": {
        "base_tone":    "투명·소통·민주",
        "keywords":     ["투명성", "소통", "민주주의", "참여", "책임감"],
        "visual_hint":  "개방적 공간감(본회의장·광장), 다양한 시민 목소리, 대화형 구성, 밝은 톤",
        "avoid":        ["일방적 홍보 메시지", "관료적·권위적 언어", "정치적 편향 연상 표현"],
    },
    "공공기관": {
        "base_tone":    "전문·혁신·미래지향",
        "keywords":     ["전문성", "혁신", "미래", "신뢰", "서비스"],
        "visual_hint":  "모던하고 세련된 영상미, 전문가 인터뷰, 인포그래픽·모션그래픽 활용",
        "avoid":        ["구태의연한 관공서 이미지", "딱딱한 나레이션 일변도", "저해상도 자료화면"],
    },
    "기타": {
        "base_tone":    "균형·신뢰·공공성",
        "keywords":     ["신뢰", "균형", "공공성", "소통", "변화"],
        "visual_hint":  "균형 잡힌 화면 구성, 다양성 반영(연령·지역·성별), 보편적 공감대",
        "avoid":        ["특정 계층 편향 표현", "과도한 상업적 연출", "선정적·자극적 이미지"],
    },
}

# 기관 유형 키 정규화 매핑 (부분 매치)
_AGENCY_KEY_MAP: list[tuple[str, str]] = [
    ("중앙부처", "중앙부처"),
    ("지자체",  "지자체"),
    ("의회",    "의회"),
    ("공공기관", "공공기관"),
]


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────

def run(dna: ConceptDNA) -> dict:
    """크리에이티브 방향 설계 실행.

    Args:
        dna: STEP 1~4 결과가 모두 반영된 ConceptDNA

    Returns:
        {
            "concept":               str,   # 핵심 컨셉 (빅아이디어 한 줄)
            "concept_description":   str,   # 컨셉 상세 설명
            "confirmed_slogan":      str,   # 확정 슬로건 (1순위)
            "slogans":               list,  # 슬로건 후보 3개 [{text, rationale}]
            "tone_keywords":         list,  # 감성 키워드 5개
            "tone_description":      str,   # 톤앤매너 설명
            "forbidden_expressions": list,  # 금지 표현/이미지 목록
            "visual_direction":      str,   # 비주얼 레퍼런스 방향
            "agency_type":           str,   # 적용된 기관 유형
        }
    """
    # 1. 기관 유형 프리셋 로드
    preset = _get_tone_preset(dna.agency_type)
    print(f"  톤앤매너 프리셋: [{dna.agency_type or '기타'}] {preset['base_tone']}")

    # 2. Claude API로 크리에이티브 생성
    print("  크리에이티브 컨셉 생성 중...")
    prompt = wrap_prompt_with_instruction(_build_prompt(dna, preset), dna)
    result = claude_client.call_json(prompt, max_tokens=4096)

    # 3. 필수 키 보정
    result.setdefault("concept", "")
    result.setdefault("concept_description", "")
    result.setdefault("confirmed_slogan", "")
    result.setdefault("slogans", [])
    result.setdefault("tone_keywords", preset["keywords"])
    result.setdefault("tone_description", preset["base_tone"])
    result.setdefault("forbidden_expressions", preset["avoid"])
    result.setdefault("visual_direction", preset["visual_hint"])
    result["agency_type"] = dna.agency_type

    # confirmed_slogan이 없으면 slogans[0]에서 추출
    if not result["confirmed_slogan"] and result["slogans"]:
        first = result["slogans"][0]
        result["confirmed_slogan"] = first.get("text", "") if isinstance(first, dict) else str(first)

    # 4. DNA 업데이트 (이후 모든 에이전트의 기준점)
    update_dna(dna, {
        "concept":               result["concept"],
        "concept_description":   result["concept_description"],
        "slogan":                result["confirmed_slogan"],
        "slogans":               result["slogans"],
        "tone_and_manner":       result["tone_description"],
        "tone_keywords":         result["tone_keywords"],
        "forbidden_expressions": result["forbidden_expressions"],
        "visual_direction":      result["visual_direction"],
    })

    # 창작 DNA 잠금 — STEP 6 이후 모든 에이전트에 강제 주입
    dna.locked_slogan    = result["confirmed_slogan"]
    dna.locked_keywords  = result["tone_keywords"][:5] if result["tone_keywords"] else []
    dna.locked_tone      = result["tone_description"]
    dna.locked_narrative = result.get("concept_description", "")
    dna.dna_locked       = True

    print(f"  컨셉 확정: {result['concept']}")
    print(f"  슬로건 1순위: {result['confirmed_slogan']}")
    print(f"  [DNA 잠금] 슬로건·키워드·톤앤매너 고정 완료")

    # 5. DB 저장
    try:
        save_creative(dna.client_name, dna.project_name, result,
                      case_id=getattr(dna, "case_id", 0) or 0)
        print("  크리에이티브 결과 DB 저장 완료")
    except Exception as e:
        print(f"  [경고] DB 저장 실패 (계속 진행): {e}")

    return result


# ─────────────────────────────────────────────
# 톤앤매너 프리셋
# ─────────────────────────────────────────────

def _get_tone_preset(agency_type: str) -> dict:
    """기관 유형 문자열로 톤앤매너 프리셋 반환.

    부분 매치를 지원하므로 '경기도(지자체)' 같은 혼합 문자열도 처리.

    Args:
        agency_type: DNA의 agency_type 문자열

    Returns:
        _TONE_PRESETS의 해당 항목 (매치 없으면 '기타')
    """
    if not agency_type:
        return _TONE_PRESETS["기타"]
    for key, preset_key in _AGENCY_KEY_MAP:
        if key in agency_type:
            return _TONE_PRESETS[preset_key]
    return _TONE_PRESETS["기타"]


# ─────────────────────────────────────────────
# 프롬프트 생성
# ─────────────────────────────────────────────

def _build_prompt(dna: ConceptDNA, preset: dict) -> str:
    """크리에이티브 생성용 Claude 프롬프트.

    Args:
        dna: 현재 ConceptDNA (STEP 1~4 누적)
        preset: _get_tone_preset()이 반환한 기관 유형 프리셋

    Returns:
        Claude에 전달할 프롬프트 문자열
    """
    dna_ctx = dna_to_context_string(dna)

    # 설득 구조 요약 블록
    strategy_block = _format_strategy_block(dna)

    # 프리셋 블록
    preset_block = (
        f"- 기본 톤: {preset['base_tone']}\n"
        f"- 권장 감성 키워드: {', '.join(preset['keywords'])}\n"
        f"- 비주얼 힌트: {preset['visual_hint']}\n"
        f"- 반드시 피해야 할 방향: {' / '.join(preset['avoid'])}"
    )

    # 금지/주의사항 블록
    if dna.forbidden_notes:
        forbidden_block = "\n".join(f"  - {n}" for n in dna.forbidden_notes[:5])
    else:
        forbidden_block = "  (RFP에서 별도 명시 없음)"

    return f"""당신은 대한민국 최고의 공공 캠페인 크리에이티브 디렉터입니다.
아래 전략 분석을 바탕으로 영상콘텐츠 캠페인의 크리에이티브 방향을 설계해주세요.

【절대 원칙】
- 개요나 목차가 아닌 실제 크리에이티브 결과물을 작성하라.
- 컨셉은 단순 슬로건이 아니라 영상 전체를 관통하는 캠페인 빅아이디어여야 한다.
- 슬로건 3개는 각각 배경 논리(왜 이 문구인가, 타겟은 누구인가, 어떤 감성을 자극하는가)를
  최소 3문장 이상 구체적으로 설명하라.
- 톤앤매너 설명에는 실제 사용 가능한 표현 예시를 5개 이상 포함하라.
- 비주얼 방향은 실제 촬영 현장에서 참고할 수 있는 수준으로 구체적으로 작성하라.
- 【수치 의무】 모든 주장과 분석에는 반드시 구체적인 수치 데이터를 포함해야 한다.
  '증가했다' (X) → '2024년 대비 23% 증가했다' (O) / '낮다' (X) → '전체의 7%에 불과하다' (O)
  수치 없는 문장은 작성하지 마라.
- 【출처 의무】 모든 통계·수치·사실에는 반드시 출처를 표기해야 한다.
  형식: (출처명, 발행연도) — 예: '국내 숏폼 소비율 65% (YouTube Creator Insider, 2024)'
  출처 불명확한 수치는 '추정치' 또는 '자체 분석'으로 명시. 출처 없는 수치는 제시 금지.
  AI가 생성하거나 확인되지 않은 추정값은 반드시 ⚠️ AI 추정값 — 제출 전 직접 확인 필요 표시.
- 【데이터 적합성】 인용하는 모든 데이터는 현재 다루는 주제와 직접 관련이 있어야 한다.
  유사하지만 다른 주제의 통계·사례 사용 금지 (예: 데이트 폭력 주제 → 가정 폭력 통계 X).
  주제와 100% 일치하지 않으면 '관련 데이터 없음'으로 표기. 유사 데이터로 절대 대체하지 말 것.

━━━━━━━━━━━━━━━━━━━━━━━
[프로젝트 컨텍스트]
━━━━━━━━━━━━━━━━━━━━━━━
{dna_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[전략 핵심 (STEP 2 결과 — 이것을 크리에이티브로 승화)]
━━━━━━━━━━━━━━━━━━━━━━━
{strategy_block}

━━━━━━━━━━━━━━━━━━━━━━━
[기관 유형별 톤앤매너 가이드 — 반드시 준수]
━━━━━━━━━━━━━━━━━━━━━━━
{preset_block}

━━━━━━━━━━━━━━━━━━━━━━━
[RFP 금지·주의 사항]
━━━━━━━━━━━━━━━━━━━━━━━
{forbidden_block}

━━━━━━━━━━━━━━━━━━━━━━━
[크리에이티브 설계 세부 지침]
━━━━━━━━━━━━━━━━━━━━━━━
① 핵심 컨셉(빅아이디어)
   - "왜 이 영상인가"의 답이 되는 캠페인 전략적 아이디어.
   - 추상적 슬로건이 아니라, 영상의 기획 방향·제작 방식·타겟 감성을 하나로 묶는 프레임.
   - 예: "일상 속 위험을 '나의 이야기'로 바꾸다 — 제3자 관찰이 아닌 당사자 체험 방식으로
     안전 습관화를 유도하는 참여형 캠페인 영상"
   - concept_description은 반드시 4문장 이상, 영상에서 어떻게 구현되는지 구체적으로.

② 슬로건 5개 이상 후보 — 각각 다른 접근각
   컨셉과 슬로건 작성 원칙:
   1. 심플 — 5단어 이내로 핵심 전달
   2. 위트 — 예상 못한 반전이나 유머
   3. 임팩트 — 강렬한 동사나 숫자 활용
   4. 언어유희 — 동음이의어, 중의적 표현
   5. 새로운 조합 — 기존에 없던 단어 결합

   나쁜 예: '함께 만드는 안전한 대한민국'
   좋은 예: '3초가 3명을 살린다' / '몰랐던 날씨, 알게 되는 오늘'

   슬로건 후보 5개 이상 제시.
   각 슬로건마다 왜 효과적인지 한 줄 설명.

   - 후보 1: 감성 공감형 — 타겟의 마음속 언어를 그대로 꺼낸 공감 문구
   - 후보 2: 행동 촉구형 — 동사 중심, 지금 당장 행동하게 만드는 문구
   - 후보 3: 비전 제시형 — 변화 후의 밝은 미래를 상상하게 하는 문구
   - 후보 4: 언어유희형 — 동음이의어·중의적 표현으로 기억에 남는 문구
   - 후보 5: 숫자 임팩트형 — 구체적 수치로 강렬한 인상을 주는 문구
   - 각 슬로건의 rationale은 반드시 3문장 이상: ① 이 문구를 택한 전략적 이유
     ② 타겟 오디언스에게 미치는 심리적 효과 ③ 전략 방향과의 연결고리

③ 톤앤매너 설명
   - tone_description에 실제 내레이션/대사에서 쓸 수 있는 표현 예시 5개 이상 포함.
   - 예: "○○○처럼 딱딱하게 말하지 않고, '우리 함께'처럼 포용적 언어 사용"
   - 금지 표현도 구체적 예시 문장으로 작성 (단순 카테고리 나열 금지).

④ 비주얼 방향
   - 색온도(K값 또는 느낌), 조명 방향(자연광/인공광/혼합), 카메라 무빙 스타일,
     편집 리듬(컷 속도, 전환 방식), 자막 디자인 방향, 레퍼런스 작품·채널명 포함.
   - 촬영 현장 디렉터가 이 텍스트만 보고 방향을 잡을 수 있는 수준으로 작성.


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
  "concept": "캠페인 빅아이디어 한 줄 — 영상 기획 전체를 관통하는 전략적 프레임 (20~40자)",

  "concept_description": "컨셉의 의미, 전략과의 연결, 영상에서 구현 방법, 타겟에 미치는 효과를 구체적으로 설명. 최소 4문장, 200자 이상.",

  "confirmed_slogan": "3개 중 1순위 추천 슬로건 (선택 이유 한 줄 포함)",

  "slogans": [
    {{
      "type": "감성 공감형",
      "text": "슬로건 문구 (5단어 이내, 10~20자 내외)",
      "rationale": "① 전략적 선택 이유 ② 타겟 심리 효과 ③ 전략 방향과의 연결. 최소 3문장 이상.",
      "why_effective": "왜 이 슬로건이 효과적인지 한 줄 설명"
    }},
    {{
      "type": "행동 촉구형",
      "text": "슬로건 문구 — 강렬한 동사 포함",
      "rationale": "① 전략적 선택 이유 ② 타겟 심리 효과 ③ 전략 방향과의 연결. 최소 3문장 이상.",
      "why_effective": "왜 이 슬로건이 효과적인지 한 줄 설명"
    }},
    {{
      "type": "비전 제시형",
      "text": "슬로건 문구 — 미래 상상 유도",
      "rationale": "① 전략적 선택 이유 ② 타겟 심리 효과 ③ 전략 방향과의 연결. 최소 3문장 이상.",
      "why_effective": "왜 이 슬로건이 효과적인지 한 줄 설명"
    }},
    {{
      "type": "언어유희형",
      "text": "슬로건 문구 — 동음이의어·중의적 표현 활용",
      "rationale": "① 전략적 선택 이유 ② 타겟 심리 효과 ③ 전략 방향과의 연결. 최소 3문장 이상.",
      "why_effective": "왜 이 슬로건이 효과적인지 한 줄 설명"
    }},
    {{
      "type": "숫자 임팩트형",
      "text": "슬로건 문구 — 구체적 수치 포함 (예: '3초가 3명을 살린다')",
      "rationale": "① 전략적 선택 이유 ② 타겟 심리 효과 ③ 전략 방향과의 연결. 최소 3문장 이상.",
      "why_effective": "왜 이 슬로건이 효과적인지 한 줄 설명"
    }}
  ],

  "tone_keywords": ["키워드1", "키워드2", "키워드3", "키워드4", "키워드5"],

  "tone_description": "톤앤매너 종합 설명. 반드시 실제 나레이션/대사 표현 예시 5개 이상 포함. 예: '○○한 상황에서 ○○처럼 말한다'. 금지 표현도 구체적 예시 문장으로. 최소 6문장, 300자 이상.",

  "tone_examples": [
    "표현 예시 1: 실제 나레이션/자막으로 쓸 수 있는 문장",
    "표현 예시 2: ...",
    "표현 예시 3: ...",
    "표현 예시 4: ...",
    "표현 예시 5: ..."
  ],

  "forbidden_expressions": [
    "금지 표현 1: 구체적 예시 문장과 금지 이유",
    "금지 표현 2: ...",
    "금지 표현 3: ...",
    "금지 표현 4: ...",
    "금지 표현 5: ..."
  ],

  "visual_direction": "비주얼 방향 — 색온도·조명·카메라 무빙·편집 리듬·자막 디자인·레퍼런스 작품 포함. 촬영 현장 디렉터가 참고할 수 있는 수준으로. 최소 5문장, 250자 이상."
}}"""


def _format_strategy_block(dna: ConceptDNA) -> str:
    """설득 구조 4단계를 프롬프트용 텍스트로 포맷.

    persuasion_structure가 있으면 단계별 headline을 사용하고,
    없으면 core_problem·crisis_statement·solution_direction으로 구성.

    Args:
        dna: 현재 ConceptDNA

    Returns:
        전략 요약 블록 문자열
    """
    lines = []

    if dna.core_problem:
        lines.append(f"[핵심 문제] {dna.core_problem}")

    if dna.persuasion_structure:
        for step in dna.persuasion_structure:
            if isinstance(step, dict):
                stage    = step.get("stage", "")
                headline = step.get("headline", "")
                body     = step.get("body", "")
                lines.append(f"[{stage}] {headline}")
                if body:
                    # 첫 문장만 요약으로 사용
                    first_sentence = body.split(".")[0].strip()
                    lines.append(f"  → {first_sentence}")
    else:
        # persuasion_structure 없을 때 fallback
        if dna.crisis_statement:
            lines.append(f"[위기 제시] {dna.crisis_statement}")
        if dna.current_situation:
            lines.append(f"[현황 진단] {dna.current_situation}")
        if dna.solution_direction:
            lines.append(f"[해결책 방향] {dna.solution_direction}")

    if dna.expected_effects:
        effects = " / ".join(dna.expected_effects[:3])
        lines.append(f"[기대 효과] {effects}")

    return "\n".join(lines) if lines else "(전략 정보 없음 — 기본 컨셉으로 진행)"

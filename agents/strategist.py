# agents/strategist.py
# STEP 2: 문제정의/전략 에이전트
# 역할: RFP 분석 + 리서치 결과를 바탕으로 4단계 설득 구조 설계
#
# 설득 구조:
#   1. 위기 제시   — 발주처가 직면한 문제/현황 (수치·사례 포함)
#   2. 현황 진단   — 기존 방식의 한계, 데이터 기반 문제 심화
#   3. 해결책 제시 — 인터즈의 접근 방식이 왜 최적인지
#   4. 기대 효과   — 구체적 성과 지표
#
# 핵심 원칙:
#   - 평가항목 배점 높은 순으로 전략 집중
#   - 발주처 키워드를 설득 논리에 자연스럽게 통합
#   - DNA 전체 컨텍스트(STEP 1+2)를 모두 반영

import re
import json

from core import claude_client
from core.dna import ConceptDNA, update_dna, dna_to_context_string
from database.db import save_strategy


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────

def run(dna: ConceptDNA) -> dict:
    """전략 수립 실행.

    Args:
        dna: STEP 1(rfp_parser) + STEP 2(researcher) 결과가 반영된 ConceptDNA

    Returns:
        {
            "core_problem":             str,   # 핵심 문제 한 문장
            "crisis_statement":         str,   # 위기 제시 (수치/사례 포함)
            "current_situation":        str,   # 현황 진단
            "solution_direction":       str,   # 해결책 방향
            "expected_effects":         list,  # 기대 효과 목록
            "persuasion_structure":     list,  # 4단계 설득 구조 상세
            "high_priority_eval_items": list,  # 배점 높은 순 평가항목
            "keyword_integration_map":  dict,  # 키워드 → 전략 내 활용 위치
        }
    """
    # 1. 평가항목 배점 높은 순으로 정렬
    sorted_eval = _sort_eval_items_by_score(dna.evaluation_items)
    print(f"  평가항목 {len(sorted_eval)}개 배점순 정렬 완료")

    # 2. 전략 집중 키워드 추출 (배점 상위 항목 + evaluation_keywords 교집합)
    priority_keywords = _extract_priority_keywords(sorted_eval, dna.evaluation_keywords)
    print(f"  전략 집중 키워드: {', '.join(priority_keywords[:6])}")

    # 3. Claude API로 설득 구조 생성
    print("  설득 구조 생성 중...")
    prompt = _build_prompt(dna, sorted_eval, priority_keywords)
    result = claude_client.call_json(prompt, max_tokens=2000)

    # 4. 필수 키 보정 (Claude 응답 누락 방지)
    result.setdefault("core_problem", "")
    result.setdefault("crisis_statement", "")
    result.setdefault("current_situation", "")
    result.setdefault("solution_direction", "")
    result.setdefault("expected_effects", [])
    result.setdefault("persuasion_structure", [])
    result.setdefault("keyword_integration_map", {})

    # expected_effects 누락/부족 시 persuasion_structure "기대 효과" 스테이지에서 추출
    if len(result["expected_effects"]) < 3:
        for stage in result.get("persuasion_structure", []):
            if stage.get("stage", "") in ("기대 효과", "기대효과", "effects"):
                body = stage.get("body", "")
                if body and body not in result["expected_effects"]:
                    result["expected_effects"].append(body)
                break

    # expected_effects 여전히 부족하면 재시도
    if len(result.get("expected_effects", [])) < 3:
        print("  [경고] expected_effects 부족 — 재시도 중...")
        retry_prompt = (
            "기대효과(expected_effects)가 비어있습니다.\n"
            "정량적 효과 3개(수치 포함) + 정성적 효과 2개를 반드시 작성하세요.\n\n"
            f"원본 요청:\n{prompt[:3000]}\n\n"
            "아래 JSON 형식으로만 출력하세요:\n"
            '{"expected_effects": ["정량적 효과 1 (수치 포함)", "정량적 효과 2", '
            '"정량적 효과 3", "정성적 효과 1", "정성적 효과 2"]}'
        )
        try:
            retry_result = claude_client.call_json(retry_prompt, max_tokens=2048)
            if retry_result.get("expected_effects"):
                result["expected_effects"] = retry_result["expected_effects"]
                print(f"  [확인] expected_effects 재시도 성공: {len(result['expected_effects'])}개")
        except Exception as e:
            print(f"  [경고] expected_effects 재시도 실패: {e}")
    result["high_priority_eval_items"] = sorted_eval

    # 5. DNA 업데이트
    update_dna(dna, {
        "core_problem":             result["core_problem"],
        "crisis_statement":         result["crisis_statement"],
        "current_situation":        result["current_situation"],
        "solution_direction":       result["solution_direction"],
        "expected_effects":         result["expected_effects"],
        "persuasion_structure":     result["persuasion_structure"],
        "high_priority_eval_items": sorted_eval,
    })

    # 6. DB 저장
    try:
        save_strategy(dna.client_name, dna.project_name, result,
                      case_id=getattr(dna, "case_id", 0) or 0)
        print("  전략 결과 DB 저장 완료")
    except Exception as e:
        print(f"  [경고] DB 저장 실패 (계속 진행): {e}")

    return result


# ─────────────────────────────────────────────
# 평가항목 배점 처리
# ─────────────────────────────────────────────

def _sort_eval_items_by_score(evaluation_items: list) -> list:
    """평가항목을 배점 내림차순으로 정렬.

    evaluation_items 형식: [{"item": "...", "score": "20점"}, ...]
    score가 없거나 파싱 불가면 0점으로 처리.

    Args:
        evaluation_items: DNA의 evaluation_items 리스트

    Returns:
        배점 내림차순 정렬된 리스트 (score_int 필드 추가)
    """
    enriched = []
    for item in evaluation_items:
        score_int = _parse_score(item.get("score", ""))
        enriched.append({**item, "score_int": score_int})
    return sorted(enriched, key=lambda x: x["score_int"], reverse=True)


_TRAILING_PARTICLES = re.compile(r"(의|을|를|이|가|은|는|과|와|에|서|로|으로|에서|이며|이고|이나|이라)$")


def _strip_particle(word: str) -> str:
    """한국어 단어 끝의 조사/어미 제거. 예) '구성의' → '구성'."""
    return _TRAILING_PARTICLES.sub("", word)


def _parse_score(score_str: str) -> int:
    """'20점', '20/100', '20점(배점)' 등 다양한 형식에서 숫자 추출.

    Args:
        score_str: 배점 문자열

    Returns:
        정수 배점 (파싱 실패 시 0)
    """
    if not score_str:
        return 0
    match = re.search(r"\d+", str(score_str))
    return int(match.group()) if match else 0


def _extract_priority_keywords(sorted_eval: list, eval_keywords: list) -> list:
    """배점 상위 항목명 + evaluation_keywords를 합산해 중복 제거한 우선순위 키워드 반환.

    배점 상위 3개 항목의 단어를 먼저 배치하고, 나머지 evaluation_keywords를 뒤에 추가.

    Args:
        sorted_eval: 배점순 정렬된 evaluation_items
        eval_keywords: DNA의 evaluation_keywords (rfp_parser가 추출한 TOP 10)

    Returns:
        우선순위 키워드 리스트 (최대 15개)
    """
    seen = set()
    result = []

    # 배점 상위 3개 항목명에서 키워드 추출
    for item in sorted_eval[:3]:
        item_name = item.get("item", "")
        # 의미 있는 명사 추출 후 조사 제거
        words = [_strip_particle(w) for w in re.findall(r"[가-힣a-zA-Z]{2,}", item_name)
                 if len(w) >= 3 or re.match(r"[a-zA-Z]{2,}", w)]
        words = [w for w in words if len(w) >= 2]  # 제거 후 1글자 된 것 필터
        for w in words:
            if w not in seen:
                seen.add(w)
                result.append(w)

    # evaluation_keywords 추가
    for kw in eval_keywords:
        if kw not in seen:
            seen.add(kw)
            result.append(kw)

    return result[:15]


# ─────────────────────────────────────────────
# 프롬프트 생성
# ─────────────────────────────────────────────

def _build_prompt(dna: ConceptDNA, sorted_eval: list, priority_keywords: list) -> str:
    """설득 구조 생성용 Claude 프롬프트.

    Args:
        dna: 현재 ConceptDNA
        sorted_eval: 배점순 정렬된 평가항목
        priority_keywords: 전략 집중 키워드

    Returns:
        Claude에 전달할 프롬프트 문자열
    """
    dna_ctx = dna_to_context_string(dna)

    # 평가항목 블록 (배점 순)
    if sorted_eval:
        eval_block = "\n".join(
            f"  {i+1}. {item['item']} ({item.get('score', '배점미상')}) ← {'★ 최우선' if i < 2 else '우선'}"
            for i, item in enumerate(sorted_eval[:8])
        )
    else:
        eval_block = "  (평가항목 정보 없음)"

    # 배점 TOP 3 항목 블록
    top_criteria = getattr(dna, "top_criteria", []) or []
    if top_criteria:
        top3_block = "  ⚠️ " + " / ".join(top_criteria)
    elif sorted_eval:
        top3_block = "  ⚠️ " + " / ".join(it["item"] for it in sorted_eval[:3])
    else:
        top3_block = "  (배점 정보 없음)"

    # 키워드 블록
    keyword_block = ", ".join(f"'{k}'" for k in priority_keywords) if priority_keywords else "(없음)"

    # 최근 이슈 블록
    if dna.recent_issues:
        issues_block = "\n".join(
            f"  - {issue.get('issue', issue) if isinstance(issue, dict) else issue}"
            + (f": {issue.get('description', '')}" if isinstance(issue, dict) else "")
            for issue in dna.recent_issues[:5]
        )
    else:
        issues_block = "  (이슈 정보 없음)"

    # 핵심 과업 블록
    tasks_block = "\n".join(f"  - {t}" for t in dna.core_tasks[:5]) if dna.core_tasks else "  (없음)"

    return f"""당신은 대한민국 정부 입찰 전문 전략 컨설턴트입니다.
아래 정보를 바탕으로 영상콘텐츠 제안서의 핵심 설득 전략을 수립해주세요.

【절대 원칙】
- 개요나 목차가 아닌 실제 내용을 작성하라.
- 각 body 필드는 반드시 300자 이상의 구체적 문장으로 채워라.
- 모든 수치는 실제 값(통계, 설문, 연구 결과)을 사용하고 출처를 괄호 안에 표기하라.
- "효과적입니다", "중요합니다" 같은 추상적 선언은 금지. 구체적 근거와 사례로 대체하라.
- 인터즈의 해결책은 반드시 이 사업과 이 발주처에만 해당하는 맞춤 논리여야 한다.
- 【수치 의무】 모든 주장과 분석에는 반드시 구체적인 수치 데이터를 포함해야 한다.
  '증가했다' (X) → '2024년 대비 23% 증가했다' (O) / '낮다' (X) → '전체의 7%에 불과하다' (O)
  수치 없는 문장은 작성하지 마라.
- 【출처 의무】 모든 통계·수치·사실에는 반드시 출처를 표기해야 한다.
  형식: (출처명, 발행연도) — 예: '조회수 300만 회 (아리랑TV 공식 발표, 2024)'
  출처 불명확한 수치는 '추정치' 또는 '자체 분석'으로 명시. 출처 없는 수치는 제시 금지.

━━━━━━━━━━━━━━━━━━━━━━━
[프로젝트 컨텍스트]
━━━━━━━━━━━━━━━━━━━━━━━
{dna_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[⚠️ 배점 TOP 3 — 전략의 70%를 이 항목에 집중]
━━━━━━━━━━━━━━━━━━━━━━━
{top3_block}
배점 상위 3개 항목에 대해: 위기 제시·현황 진단·해결책·기대효과 모두 이 항목들을 직접적으로 언급하고 논거를 집중시켜라.

━━━━━━━━━━━━━━━━━━━━━━━
[평가항목 전체 (배점 높은 순)]
━━━━━━━━━━━━━━━━━━━━━━━
{eval_block}

━━━━━━━━━━━━━━━━━━━━━━━
[발주처 강조 키워드 (설득 논리에 자연스럽게 녹여야 함)]
━━━━━━━━━━━━━━━━━━━━━━━
{keyword_block}

━━━━━━━━━━━━━━━━━━━━━━━
[발주처 최근 이슈/정책]
━━━━━━━━━━━━━━━━━━━━━━━
{issues_block}

━━━━━━━━━━━━━━━━━━━━━━━
[핵심 과업]
━━━━━━━━━━━━━━━━━━━━━━━
{tasks_block}

━━━━━━━━━━━━━━━━━━━━━━━
[기관 특성 — 현황 진단에 깊이 반영 필수]
━━━━━━━━━━━━━━━━━━━━━━━
{dna.agency_characteristics or "(리서치 정보 없음)"}

━━━━━━━━━━━━━━━━━━━━━━━
[전략 수립 세부 지침]
━━━━━━━━━━━━━━━━━━━━━━━
① 위기 제시(crisis)
   - 발주처가 현재 직면한 문제를 구체적 수치/통계로 시작하라.
   - 예: "국민 10명 중 7명이 ○○ 정보를 잘못 알고 있다 (○○연구원, 2024)"
   - 수치가 없으면 추정 근거를 명시한 추정치를 사용하라.
   - 슬라이드 헤드카피로 쓸 수 있는 임팩트 있는 crisis_statement 한 문장 필수.

② 현황 진단(situation)
   - {dna.agency_characteristics or dna.agency_type or "이 기관"}의 구체적 특성을 반영해
     기존 홍보·영상 방식이 왜 한계에 부딪혔는지 데이터로 논증하라.
   - 타겟 오디언스의 미디어 소비 패턴 변화, 디지털 전환 맥락 포함.
   - current_situation은 2~3문장, 각 문장은 독립적 논거를 가져야 한다.

③ 해결책 제시(solution)
   - "인터즈만이 이 사업을 이렇게 해결할 수 있다"는 논리를 전개하라.
   - 차별화 포인트: ① 공공기관 영상 200편+ 제작 경험 ② 데이터 기반 콘텐츠 기획 방법론
     ③ 기획→제작→유통 원스톱 역량 ④ 정부 평가 키워드 최적화 노하우
   - solution_direction에는 왜 영상콘텐츠가 이 문제의 최적 해법인지 설명하라.

④ 기대 효과(effects) 【필수 — 반드시 4개 이상 작성, 비워두면 안 됨】
   - expected_effects 배열에 최소 4개 항목을 반드시 작성한다. 절대 빈 배열로 두지 말 것.
   - 각 항목은 "효과 N: [수치] [달성 시점] [벤치마크 근거]" 형식으로 작성.
   - 조회수/도달/인지도 향상율/공유수/완주율 등 정량 목표를 구체적 숫자로 제시하라.
   - 유사 공공캠페인 벤치마크 수치를 근거로 사용하라.
   - 효과 항목마다 달성 시점(납품 후 N개월)을 명시하라.

   기대효과는 반드시 아래 형식으로 3개 이상 작성:

   ## 기대효과

   ### 정량적 효과
   - **조회수**: 목표 수치와 달성 근거 (출처 포함)
   - **도달률**: 타겟 오디언스 기준 수치
   - **참여율**: 업계 평균 대비 목표치

   ### 정성적 효과
   - 브랜드 인식 변화
   - 정책 이해도 향상
   - 국민 참여 활성화

   절대 빈칸으로 두지 마라. 수치 없는 기대효과는 작성하지 마라.


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
  "core_problem": "발주처가 직면한 핵심 문제를 한 문장으로 (주어-서술어 완결, 수치 포함)",

  "persuasion_structure": [
    {{
      "stage": "위기 제시",
      "headline": "심사위원의 주의를 집중시키는 강렬한 한 문장 (수치 포함)",
      "body": "문제의 실체와 심각성을 구체적 통계·사례로 설명. 최소 5문장 이상, 300자 이상. 발주처 키워드를 2개 이상 자연스럽게 포함. 예시 수치와 출처를 괄호 표기로 인용할 것.",
      "evidence": "핵심 뒷받침 데이터 목록 (출처 포함). 예: '유사 캠페인 조회수 50만 달성 (행안부, 2023)' 형식으로 최소 2개 이상",
      "keywords_used": ["이 단계에서 사용된 발주처 키워드 목록"]
    }},
    {{
      "stage": "현황 진단",
      "headline": "기존 방식의 핵심 한계를 드러내는 한 문장",
      "body": "기관 특성을 깊이 반영한 현황 분석. 기존 홍보 방식의 구체적 한계 3가지 이상, 타겟 오디언스 미디어 소비 패턴 변화, 왜 지금 변화가 필요한지. 최소 5문장, 300자 이상.",
      "evidence": "현황 진단을 뒷받침하는 데이터·설문·통계 (최소 2개, 출처 포함)",
      "keywords_used": [...]
    }},
    {{
      "stage": "해결책 제시",
      "headline": "인터즈의 접근 방식이 왜 최적인지 한 문장",
      "body": "인터즈만의 차별화된 해결 방법론을 구체적으로 설명. 단계별 프로세스(기획→제작→유통), 공공기관 영상 전문 경험, 데이터 기반 방법론, 유사 사업 성공 사례 포함. 최소 5문장, 300자 이상.",
      "evidence": "인터즈 차별화 역량 근거 (실적 수치, 보유 시스템, 검증된 방법론 등, 최소 2개)",
      "keywords_used": [...]
    }},
    {{
      "stage": "기대 효과",
      "headline": "이 영상이 만들어낼 변화를 수치로 표현한 한 문장",
      "body": "납품 후 예상되는 구체적 성과를 단계별(1개월/3개월/6개월)로 기술. 조회수·도달·인지도 향상율·공유수 등 정량 목표 포함. 유사 공공캠페인 벤치마크 근거 제시. 최소 4문장, 250자 이상.",
      "evidence": "정량 목표 근거 (유사 캠페인 성과 데이터, 최소 2개)",
      "keywords_used": [...]
    }}
  ],

  "crisis_statement": "슬라이드 헤드카피로 바로 쓸 수 있는 임팩트 있는 한 문장 (구체적 수치 포함, 30자 내외)",

  "current_situation": "현황 진단 핵심 요약 — 각각 독립적 논거를 가진 3문장. 기관 특성과 기존 방식의 한계를 명확히 드러낼 것.",

  "solution_direction": "해결책 방향 핵심 요약 — 인터즈의 차별화 포인트를 포함한 3문장. 왜 영상콘텐츠가 최적 해법인지 논리적으로 전개할 것.",

  "expected_effects": [
    "효과 1: 납품 후 3개월 내 누적 조회수 ○만 뷰 달성 (유사 캠페인 벤치마크)",
    "효과 2: 타겟 인지도 ○% 향상 (사전-사후 설문 측정 기준)",
    "효과 3: SNS 자발적 공유 ○회 이상 (바이럴 지수 ○)",
    "효과 4: 영상 완주율 ○% 이상 유지 (플랫폼 평균 대비 ○%p 상회)"
  ],

  "keyword_integration_map": {{
    "키워드1": "위기 제시 — 구체적 활용 위치와 문맥",
    "키워드2": "현황 진단 — 구체적 활용 위치와 문맥",
    "키워드3": "해결책 제시 — 구체적 활용 위치와 문맥"
  }}
}}"""

# agents/planner.py
# STEP 4: 실행전략 에이전트
# 역할: 확정된 컨셉을 기반으로 제작 실행 계획 수립
#
# 일반 영상 사업:
#   1. 편별 제목 + 핵심 메시지 설계 (quantity만큼)
#   2. 단계별 제작 일정 (기획→촬영→편집→납품)
#   3. 투입 인력 구성 (역할·인원·책임)
#   4. 예산 배분 계획
#
# 유튜브 채널 포함 사업 (자동 감지):
#   +시리즈 포맷 기획 (롱폼/숏폼/카드뉴스/라이브)
#   +시즌 구성안 (월별 테마, 아크)
#   +구독자 유입→팬덤화→전환 퍼널 설계
#   +인플루언서/출연자 섭외 방향

import re
from datetime import datetime, timedelta

from core import claude_client
from core.dna import ConceptDNA, update_dna, dna_to_context_string, dna_lock_block, wrap_prompt_with_instruction
from database.db import save_plan


# 유튜브 채널 사업 감지 키워드
_YOUTUBE_SIGNALS = [
    "유튜브", "youtube", "채널", "시리즈", "콘텐츠 운영",
    "숏폼", "릴스", "쇼츠", "정기", "주기적",
]

# 단계별 표준 일정 비율 (총 기간 대비)
_SCHEDULE_PHASES = [
    {"phase": "기획·사전제작", "ratio": 0.25,
     "tasks": ["기획서·콘티 작성", "대본 집필", "출연자·장소 섭외", "소품·의상 준비"]},
    {"phase": "촬영",         "ratio": 0.20,
     "tasks": ["본 촬영", "인터뷰 촬영", "B-roll 촬영", "현장 음향 녹음"]},
    {"phase": "편집·후반작업", "ratio": 0.35,
     "tasks": ["1차 편집(러프컷)", "색보정·그레이딩", "음악·효과음 삽입", "자막·수어·화면해설 작업", "모션그래픽"]},
    {"phase": "검수·납품",    "ratio": 0.20,
     "tasks": ["발주처 1차 시사", "수정 반영", "최종 시사·승인", "파일 납품·아카이빙"]},
]

# 예산 배분 기본 비율
_BUDGET_RATIOS = [
    ("기획·작가비",      0.12),
    ("촬영·장비·스튜디오", 0.30),
    ("편집·색보정·VFX",   0.28),
    ("음악·효과음",       0.07),
    ("자막·수어·화면해설", 0.05),
    ("출연·섭외비",       0.08),
    ("관리·간접비",       0.10),
]

# 편수별 표준 총 제작 기간 (일 단위)
_DURATION_BY_QUANTITY = {1: 35, 2: 49, 3: 63, 4: 77, 5: 84}


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────

_DATA_RELIABILITY_BLOCK = """
========================================
🚨 데이터 신뢰성 절대 원칙 (반드시 준수)
========================================
1. 모든 수치/통계/데이터는 실제 존재하는 자료만 사용
2. 출처 없는 수치 사용 절대 금지
3. 상상하거나 추정한 수치 사용 절대 금지
4. 유사한 주제의 데이터로 대체 절대 금지
   (데이트폭력 주제에 가정폭력 통계 사용 금지 등)
5. 확실하지 않으면 데이터 없이 서술
6. 출처 표기 형식: (출처: 기관명, 연도, 자료명)
7. Perplexity 검색으로 확인된 데이터만 수치로 인용
8. AI가 생성한 추정값은 반드시 '추정' 명시

위 원칙 위반 시 해당 내용 삭제 후 재작성하세요.
========================================
"""


def run(dna: ConceptDNA) -> dict:
    """제작 실행 계획 수립.

    Args:
        dna: STEP 1~5 결과가 모두 반영된 ConceptDNA

    Returns:
        {
            "is_youtube_channel":    bool,
            "episodes":              list,  # 편별 제목·메시지·방향
            "production_schedule":   list,  # 단계별 일정
            "team_composition":      dict,  # 역할·인원·책임
            "budget_plan":           dict,  # 예산 배분
            "quality_management":    str,   # 품질 관리 방안
            "differentiation":       str,   # 차별화 포인트
            "series_plan":           dict,  # 유튜브 시리즈 기획 (해당 시만)
        }
    """
    # 1. 유튜브 채널 사업 여부 자동 감지
    is_youtube = _detect_youtube_project(dna)
    if is_youtube:
        print("  유튜브 채널 포함 사업 감지 → 시리즈 기획 포함")
    else:
        print("  일반 납품형 영상 사업")

    # 2. 일정·예산 사전 계산 (프롬프트에 컨텍스트로 제공)
    schedule_skeleton = _calc_schedule_skeleton(dna)
    budget_skeleton   = _calc_budget_skeleton(dna)

    # 3. Claude API로 전체 계획 생성
    print("  제작 계획 생성 중...")
    prompt = wrap_prompt_with_instruction(_build_prompt(dna, is_youtube, schedule_skeleton, budget_skeleton), dna)
    result = claude_client.call_json(prompt, max_tokens=8000)

    # DNA 잠금 검증 — 슬로건 미포함 시 재시도 1회
    _locked = getattr(dna, "locked_slogan", "")
    if _locked:
        _result_str = str(result)
        if _locked not in _result_str:
            print(f"  [DNA경고] 슬로건 미포함 — 재시도")
            result2 = claude_client.call_json(prompt + f"\n\n⚠️ 슬로건 '{_locked}'을 episodes의 core_message에 반드시 포함하라.", max_tokens=8000)
            if result2:
                result = result2

    # 4. 필수 키 보정
    result.setdefault("is_youtube_channel", is_youtube)
    result.setdefault("episodes", [])
    result.setdefault("production_schedule", schedule_skeleton)
    result.setdefault("team_composition", {})
    result.setdefault("budget_plan", budget_skeleton)
    result.setdefault("quality_management", "")
    result.setdefault("differentiation", "")
    result.setdefault("series_plan", {})
    result["is_youtube_channel"] = is_youtube  # 감지 결과 우선 적용

    # 5. DNA 업데이트
    update_dna(dna, {
        "is_youtube_channel":  is_youtube,
        "episodes":            result["episodes"],
        "production_schedule": result["production_schedule"],
        "team_composition":    result["team_composition"],
        "budget_plan":         result["budget_plan"],
        "series_plan":         result["series_plan"],
        "execution_plan":      result,
    })
    ep_count = len(result["episodes"])
    print(f"  편별 계획 {ep_count}편 생성 완료")

    # 6. DB 저장
    try:
        save_plan(dna.client_name, dna.project_name, result,
                  case_id=getattr(dna, "case_id", 0) or 0)
        print("  제작 계획 DB 저장 완료")
    except Exception as e:
        print(f"  [경고] DB 저장 실패 (계속 진행): {e}")

    return result


# ─────────────────────────────────────────────
# 유튜브 채널 감지
# ─────────────────────────────────────────────

def _detect_youtube_project(dna: ConceptDNA) -> bool:
    """video_type·project_name·core_tasks에서 유튜브/채널 사업 여부 감지.

    Args:
        dna: 현재 ConceptDNA

    Returns:
        True이면 유튜브 시리즈 기획 포함
    """
    targets = [
        dna.video_type.lower(),
        dna.project_name.lower(),
        " ".join(dna.core_tasks).lower(),
    ]
    combined = " ".join(targets)
    return any(sig in combined for sig in _YOUTUBE_SIGNALS)


# ─────────────────────────────────────────────
# 일정·예산 사전 계산
# ─────────────────────────────────────────────

def _calc_schedule_skeleton(dna: ConceptDNA) -> list:
    """납품 수량·기한 기반 단계별 일정 골격 계산.

    deadline이 명시되면 역산, 없으면 편수 기준 표준 기간 사용.

    Args:
        dna: 현재 ConceptDNA

    Returns:
        [{"phase": str, "period": str, "tasks": list}, ...]
    """
    qty = max(dna.quantity, 1)
    total_days = _DURATION_BY_QUANTITY.get(qty, 63 + (qty - 3) * 14)

    # 시작일 기준 계산
    start = datetime.today()
    if dna.deadline:
        end = _parse_deadline(dna.deadline)
        if end and (end - start).days > 14:
            total_days = (end - start).days

    skeleton = []
    cursor = start
    for phase_info in _SCHEDULE_PHASES:
        phase_days = max(int(total_days * phase_info["ratio"]), 5)
        end_date   = cursor + timedelta(days=phase_days)
        skeleton.append({
            "phase":       phase_info["phase"],
            "period":      f"{cursor.strftime('%m/%d')} ~ {end_date.strftime('%m/%d')}",
            "duration":    f"{phase_days}일",
            "tasks":       phase_info["tasks"],
            "deliverable": "",   # Claude가 채울 항목
        })
        cursor = end_date

    return skeleton


def _parse_deadline(deadline_str: str) -> datetime | None:
    """납품기한 문자열에서 datetime 파싱.

    지원 형식: '2025-12-31', '2025년 12월 31일', '12월 31일', '3개월 후'
    """
    s = deadline_str.strip()

    # ISO 형식
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # 한국어 날짜
    m = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # 월·일만
    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일", s)
    if m:
        now = datetime.today()
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year if month >= now.month else now.year + 1
        return datetime(year, month, day)

    # N개월 후
    m = re.search(r"(\d+)\s*개월", s)
    if m:
        return datetime.today() + timedelta(days=int(m.group(1)) * 30)

    return None


def _calc_budget_skeleton(dna: ConceptDNA) -> dict:
    """예산 문자열에서 금액 파싱 후 항목별 배분 계산.

    Args:
        dna: 현재 ConceptDNA

    Returns:
        {"total": str, "breakdown": [{"category", "ratio", "amount"}]}
    """
    total_won = _parse_budget_won(dna.budget)

    breakdown = []
    for category, ratio in _BUDGET_RATIOS:
        if total_won:
            amount_won = int(total_won * ratio)
            amount_str = _format_won(amount_won)
        else:
            amount_str = "협의"
        breakdown.append({
            "category": category,
            "ratio":    f"{int(ratio * 100)}%",
            "amount":   amount_str,
        })

    return {
        "total":     dna.budget or "협의",
        "breakdown": breakdown,
    }


def _parse_budget_won(budget_str: str) -> int | None:
    """예산 문자열에서 원화 금액(정수) 추출.

    지원: '1억 5천만원', '15000만원', '150000000', '1.5억'
    """
    if not budget_str:
        return None
    s = budget_str.replace(",", "").replace(" ", "")

    # '1억 5천만원' 형태
    m = re.search(r"(\d+(?:\.\d+)?)억\s*(\d+)?천?만?", s)
    if m:
        uk  = float(m.group(1)) * 100_000_000
        man = int(m.group(2) or 0) * 10_000_000
        return int(uk + man)

    # '1억' 단독
    m = re.search(r"(\d+(?:\.\d+)?)억", s)
    if m:
        return int(float(m.group(1)) * 100_000_000)

    # 'N천만원' 형태
    m = re.search(r"(\d+)천만", s)
    if m:
        return int(m.group(1)) * 10_000_000

    # 'N만원' 형태
    m = re.search(r"(\d+)만", s)
    if m:
        return int(m.group(1)) * 10_000

    # 숫자만 (원 단위 가정)
    m = re.search(r"(\d{6,})", s)
    if m:
        return int(m.group(1))

    return None


def _format_won(amount: int) -> str:
    """원화 금액을 '억/천만/만원' 단위로 포맷."""
    if amount >= 100_000_000:
        uk  = amount // 100_000_000
        rem = (amount % 100_000_000) // 10_000_000
        return f"{uk}억 {rem}천만원" if rem else f"{uk}억원"
    if amount >= 10_000_000:
        return f"{amount // 10_000_000}천만원"
    if amount >= 10_000:
        return f"{amount // 10_000}만원"
    return f"{amount:,}원"


# ─────────────────────────────────────────────
# 프롬프트 생성
# ─────────────────────────────────────────────

def _build_prompt(
    dna: ConceptDNA,
    is_youtube: bool,
    schedule_skeleton: list,
    budget_skeleton: dict,
) -> str:
    """제작 계획 전체 생성용 Claude 프롬프트.

    Args:
        dna: 현재 ConceptDNA
        is_youtube: 유튜브 채널 포함 여부
        schedule_skeleton: 사전 계산된 일정 골격
        budget_skeleton: 사전 계산된 예산 배분

    Returns:
        Claude에 전달할 프롬프트 문자열
    """
    dna_ctx = dna_to_context_string(dna)

    # 일정 골격 텍스트
    sched_lines = "\n".join(
        f"  {s['phase']} ({s['period']}, {s['duration']}): "
        f"{' / '.join(s['tasks'][:3])}"
        for s in schedule_skeleton
    )

    # 예산 골격 텍스트
    budget_lines = "\n".join(
        f"  {b['category']}: {b['ratio']} ({b['amount']})"
        for b in budget_skeleton["breakdown"]
    )

    # 컨셉 블록
    concept_block = "\n".join(filter(None, [
        f"- 핵심 컨셉: {dna.concept}",
        f"- 확정 슬로건: {dna.slogan}",
        f"- 톤앤매너: {dna.tone_and_manner}",
        f"- 감성 키워드: {', '.join(dna.tone_keywords)}" if dna.tone_keywords else "",
        f"- 비주얼 방향: {dna.visual_direction}",
    ]))

    # 유튜브 추가 지침 블록
    youtube_section = ""
    if is_youtube:
        youtube_section = """
━━━━━━━━━━━━━━━━━━━━━━━
[유튜브 채널 시리즈 기획 — 아래 항목을 series_plan에 추가 설계]
━━━━━━━━━━━━━━━━━━━━━━━
다음 6가지를 series_plan 객체에 포함하세요:

1. channel_concept: 채널 전체 운영 컨셉 (2문장)

2. formats: 콘텐츠 포맷 목록 (롱폼/숏폼/카드뉴스/라이브 중 적합한 것)
   각 포맷: {format, duration, frequency, concept, example_title}

3. season_arc: 시즌 구성안 (최소 2시즌)
   각 시즌: {season, period, theme, monthly_themes: [{month, theme, episode_title}]}

4. funnel: 구독자 유입 → 팬덤화 → 전환 단계별 콘텐츠 전략
   {awareness: "유입 전략", engagement: "팬덤화 전략", conversion: "전환 전략"}

5. talent_direction: 인플루언서/출연자 섭외 방향 (유형, 조건, 콘셉트 적합성)

6. upload_schedule: 주차별 업로드 계획 (예: 매주 화·목요일 숏폼, 격주 롱폼)
"""

    # episodes 지침 (유튜브는 파일럿 에피소드, 일반은 전 편)
    if is_youtube:
        episodes_guide = f"""episodes에는 파일럿 에피소드 {min(dna.quantity, 3)}편의 계획만 작성하세요.
각 에피소드: {{episode_number, format, title, core_message, target_audience, key_scene, series_position}}"""
    else:
        episodes_guide = f"""episodes에는 납품할 전체 {dna.quantity}편 각각의 계획을 작성하세요.
각 에피소드: {{episode_number, title, core_message, target_audience, key_scene, differentiation}}"""

    lock = dna_lock_block(dna)
    return f"""{lock}당신은 대한민국 정부 영상콘텐츠 제작 전문 PD이자 제작사 PM입니다.
아래 정보를 바탕으로 영상 제작 실행 계획을 수립해주세요.

━━━━━━━━━━━━━━━━━━━━━━━
[프로젝트 컨텍스트]
━━━━━━━━━━━━━━━━━━━━━━━
{dna_ctx}

━━━━━━━━━━━━━━━━━━━━━━━
[확정된 크리에이티브 방향 — 모든 편이 이 컨셉 기반으로 설계되어야 함]
━━━━━━━━━━━━━━━━━━━━━━━
{concept_block}

━━━━━━━━━━━━━━━━━━━━━━━
[사전 계산된 제작 일정 골격 — 아래를 기반으로 상세화]
━━━━━━━━━━━━━━━━━━━━━━━
총 예산: {budget_skeleton['total']}
{sched_lines}

━━━━━━━━━━━━━━━━━━━━━━━
[사전 계산된 예산 배분 골격 — 아래를 기반으로 조정]
━━━━━━━━━━━━━━━━━━━━━━━
{budget_lines}
{youtube_section}
━━━━━━━━━━━━━━━━━━━━━━━
{_DATA_RELIABILITY_BLOCK}

【절대 원칙】
━━━━━━━━━━━━━━━━━━━━━━━
- 개요나 방향만 제시하는 것은 금지. 실제로 집행할 수 있는 구체적 내용을 작성하라.
- 【수치 의무】 모든 주장과 분석에는 반드시 구체적인 수치 데이터를 포함해야 한다.
  '증가했다' (X) → '2024년 대비 23% 증가했다' (O) / '낮다' (X) → '전체의 7%에 불과하다' (O)
  수치 없는 문장은 작성하지 마라.
- 【출처 의무】 모든 통계·수치·사실에는 반드시 출처를 표기해야 한다.
  형식: (출처명, 발행연도) — 예: '평균 제작 기간 8주 (자체 분석, 2024)'
  출처 불명확한 수치는 '추정치' 또는 '자체 분석'으로 명시. 출처 없는 수치는 제시 금지.
  AI가 생성하거나 확인되지 않은 추정값은 반드시 ⚠️ AI 추정값 — 제출 전 직접 확인 필요 표시.
- 【데이터 적합성】 인용하는 모든 데이터는 현재 다루는 주제와 직접 관련이 있어야 한다.
  유사하지만 다른 주제의 통계·사례 사용 금지 (예: 데이트 폭력 주제 → 가정 폭력 통계 X).
  주제와 100% 일치하지 않으면 '관련 데이터 없음'으로 표기. 유사 데이터로 절대 대체하지 말 것.

━━━━━━━━━━━━━━━━━━━━━━━
[계획 수립 지침]
━━━━━━━━━━━━━━━━━━━━━━━
1. 편별 계획: {episodes_guide}
2. 제작 일정: 골격 기반으로 각 단계의 deliverable(산출물)을 구체적으로 작성
3. 인력 구성: 감독(PD)/작가/촬영감독/편집감독/조감독 등 역할별 인원수와 주요 책임 명시
4. 예산 배분: 골격 비율 유지하되 사업 특성에 맞게 조정. 총액 기준 정합성 유지
5. 품질 관리: 단계별 검수 프로세스, 수정 횟수, 최종 승인 절차 포함
6. 차별화: 타 업체 대비 인터즈만의 구체적 강점 3가지 이상


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
[출력 형식]
━━━━━━━━━━━━━━━━━━━━━━━
반드시 아래 JSON 형식으로만 출력하세요.

{{
  "is_youtube_channel": {str(is_youtube).lower()},

  "episodes": [
    {{
      "episode_number": 1,
      "title": "편명",
      "core_message": "이 편의 핵심 메시지 한 문장",
      "target_audience": "주요 시청 대상",
      "key_scene": "기억에 남을 핵심 장면 방향",
      "differentiation": "이 편만의 차별화 포인트"
    }}
  ],

  "production_schedule": [
    {{
      "phase": "기획·사전제작",
      "period": "MM/DD ~ MM/DD",
      "duration": "N일",
      "tasks": ["세부 task 1", "세부 task 2"],
      "deliverable": "이 단계 완료 시 산출물"
    }}
  ],

  "team_composition": {{
    "roles": [
      {{
        "role": "감독(PD)",
        "count": 1,
        "responsibility": "주요 책임 사항"
      }}
    ],
    "total_people": 0,
    "operation_note": "팀 운영 방식 특이사항"
  }},

  "budget_plan": {{
    "total": "{budget_skeleton['total']}",
    "breakdown": [
      {{
        "category": "항목명",
        "ratio": "N%",
        "amount": "N천만원",
        "note": "특이사항"
      }}
    ],
    "contingency": "예비비 및 리스크 대응 예산 설명"
  }},

  "quality_management": "단계별 검수 프로세스·수정 횟수·최종 승인 절차 설명 (3~4문장)",

  "differentiation": "인터즈의 타 업체 대비 구체적 차별화 포인트 3가지 이상 (3~4문장)",

  "series_plan": {{}}
}}"""

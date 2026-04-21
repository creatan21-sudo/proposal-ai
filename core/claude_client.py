# core/claude_client.py
# 역할: Claude API 호출 공통 래퍼
# - 모든 에이전트가 이 모듈을 통해 API 호출
# - JSON 파싱, 재시도, 오류처리 일괄 담당

import json
import os
import re
import threading
import time
import anthropic
from json_repair import repair_json
from config import DEFAULT_MODEL, MAX_TOKENS

_client: anthropic.Anthropic | None = None

# Rate limit 재시도 설정
_RETRY_WAIT_SEC = 60
_MAX_RETRIES    = 3

# ── 공통 출처 기재 규칙 (모든 에이전트 프롬프트에 자동 주입) ──
_CITATION_SYSTEM = """[출처 기재 규칙 — 절대 준수]
• 출처 없는 수치·통계는 절대 작성 금지. 확인된 출처가 없으면 해당 수치를 아예 제외하라.
• 반드시 (기관명, 연도) 형식으로 표기.  예: (행정안전부, 2023) / (연합뉴스, 2024)
• 불확실하거나 기억 기반 지식은 (추정: 출처명) 으로 표기.  예: (추정: 문화체육관광부 지침)
• 출처 불명 정보는 (출처 불명 — 검증 필요) 로 명시하거나 해당 항목을 삭제하라.
• '자체 분석', '자체 추정', '~로 알려진' 등 모호한 표현 단독 사용 금지.
• 위 규칙을 위반한 수치·통계는 검수 단계에서 모두 제거된다."""


class OverloadError(RuntimeError):
    """429/529 API 과부하로 재시도를 모두 소진했을 때 발생.
    web_pipeline 에서 파이프라인 레벨 재시도 판단에 사용."""
    def __init__(self, status_code: int, max_retries: int):
        self.status_code = status_code
        super().__init__(
            f"API 과부하 ({status_code}) — "
            f"API 레벨 {max_retries}회 재시도 모두 실패"
        )

# 스레드 로컬 재시도 콜백 (web_pipeline 에서 스텝별로 설정)
# fn(attempt: int, max_retries: int, status_code: int, wait_sec: int)
_retry_cb: threading.local = threading.local()


def set_retry_callback(fn) -> None:
    """현재 스레드에 재시도 알림 콜백 등록."""
    _retry_cb.fn = fn


def clear_retry_callback() -> None:
    _retry_cb.fn = None


def _fire_retry_cb(attempt: int, max_retries: int, status_code: int, wait_sec: int) -> None:
    fn = getattr(_retry_cb, "fn", None)
    if fn:
        try:
            fn(attempt, max_retries, status_code, wait_sec)
        except Exception:
            pass


def get_client() -> anthropic.Anthropic:
    """Anthropic 클라이언트 인스턴스 반환 (싱글톤)."""
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def call(prompt: str, model: str = DEFAULT_MODEL, max_tokens: int = MAX_TOKENS,
         max_retries: int = _MAX_RETRIES, _skip_citation: bool = False,
         temperature: float | None = None) -> str:
    """Claude API 호출 후 응답 텍스트 반환.

    Args:
        prompt: 사용자 프롬프트
        model: 사용할 Claude 모델명
        max_tokens: 최대 토큰 수
        max_retries: 429/529 시 최대 재시도 횟수 (기본 3)
        _skip_citation: 내부 재시도용 — True이면 출처 규칙 시스템 프롬프트 생략
        temperature: API temperature (None이면 기본값 사용)

    Returns:
        응답 텍스트 (str)
    """
    client = get_client()
    last_exc = None
    system_prompt = None if _skip_citation else _CITATION_SYSTEM

    for attempt in range(1, max_retries + 1):
        try:
            kwargs = dict(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            if system_prompt:
                kwargs["system"] = system_prompt
            if temperature is not None:
                kwargs["temperature"] = temperature
            message = client.messages.create(**kwargs)
            return message.content[0].text.strip()

        except anthropic.RateLimitError as e:
            last_exc = e
            if attempt < max_retries:
                print(f"  [Rate Limit] {attempt}/{max_retries}회 — {_RETRY_WAIT_SEC}초 후 재시도...")
                _fire_retry_cb(attempt, max_retries, 429, _RETRY_WAIT_SEC)
                time.sleep(_RETRY_WAIT_SEC)
            else:
                print(f"  [Rate Limit] 최대 재시도 횟수 초과.")

        except anthropic.APIStatusError as e:
            if e.status_code in (424, 429, 529):
                last_exc = e
                wait = 10 if e.status_code == 424 else _RETRY_WAIT_SEC
                if attempt < max_retries:
                    print(f"  [API {e.status_code}] {attempt}/{max_retries}회 — {wait}초 후 재시도...")
                    _fire_retry_cb(attempt, max_retries, e.status_code, wait)
                    time.sleep(wait)
                else:
                    print(f"  [API {e.status_code}] 최대 재시도 횟수 초과.")
            elif e.status_code == 400:
                body = getattr(e, "body", None) or {}
                msg = body.get("error", {}).get("message", str(e)) if isinstance(body, dict) else str(e)
                if "credit balance" in msg.lower() or "billing" in msg.lower():
                    raise RuntimeError(
                        f"Anthropic API 크레딧 부족 — 충전 후 재시도하세요.\n"
                        f"충전: https://console.anthropic.com/settings/billing\n"
                        f"원본 오류: {msg}"
                    ) from e
                raise
            else:
                raise

    # 재시도 소진 → OverloadError
    code = getattr(last_exc, "status_code", 529)
    raise OverloadError(code, max_retries) from last_exc


def call_json(prompt: str, model: str = DEFAULT_MODEL, max_tokens: int = MAX_TOKENS,
              max_retries: int = _MAX_RETRIES, _validate: bool = True,
              progress_fn=None, label: str = "") -> dict:
    """Claude API 호출 후 JSON 파싱 결과 반환.

    파싱 전략 (최대 3회 시도):
      각 시도에서:
        1. 표준 json.loads
        2. json_repair 라이브러리로 자동 복구
        3. Claude에게 "JSON만 다시 출력해줘" 재요청
      재시도마다 temperature 낮춤: 기본 → 0.5 → 0.1
      재시도 시 "반드시 JSON만 출력, 한국어 설명 절대 금지" 강조 프롬프트 추가.
    완전 실패 시: {"_raw": ..., "_parse_failed": True} 반환 (예외 없이 계속 진행).

    Args:
        prompt: JSON 응답을 요청하는 프롬프트
        model: 사용할 Claude 모델명
        max_tokens: 최대 토큰 수 (기본 8192)
        max_retries: 429/529 시 최대 재시도 횟수 (기본 3)
        _validate: 빈 필드 자동 재시도 활성화 (기본 True)
        progress_fn: SSE 이벤트 콜백 (선택) — 재시도/실패 시 사용자에게 알림
        label: SSE 메시지에 포함할 스텝 이름 (예: "시나리오", "기획")

    Returns:
        파싱된 dict. 완전 실패 시 {"_raw": str, "_parse_failed": True}
    """
    if max_tokens == MAX_TOKENS:
        max_tokens = 8192

    _pfx = f"[{label}] " if label else ""

    def _notify(msg: str, is_error: bool = False):
        print(msg)
        if progress_fn:
            try:
                progress_fn({
                    "type": "log",
                    "message": msg,
                })
            except Exception:
                pass

    _JSON_RETRY_NOTE = (
        "\n\n⚠️ 이전 응답을 JSON으로 파싱할 수 없었습니다. "
        "반드시 JSON만 출력하세요. "
        "한국어 설명·안내문·사과문 절대 금지. "
        "마크다운 코드블록(```json ```) 절대 금지. "
        "첫 글자는 반드시 { 이어야 하고 마지막 글자는 반드시 } 이어야 합니다. "
        "JSON 외 어떤 문자도 출력하지 마세요."
    )
    _TEMPS = [None, 0.5, 0.1]   # None = API 기본값

    last_raw = ""

    for attempt in range(3):
        cur_prompt = prompt if attempt == 0 else (prompt + _JSON_RETRY_NOTE)
        cur_temp   = _TEMPS[attempt]

        try:
            raw = call(cur_prompt, model=model, max_tokens=max_tokens,
                       max_retries=max_retries, temperature=cur_temp)
            last_raw = raw
            result = _extract_json(raw, prompt=prompt, model=model, max_tokens=max_tokens)
            if _validate:
                result = _validate_and_retry(result, prompt, model, max_tokens)
            return result
        except (ValueError, json.JSONDecodeError):
            if attempt < 2:
                next_t = _TEMPS[attempt + 1]
                _notify(f"  [JSON] {_pfx}파싱 실패 (시도 {attempt+1}/3) — 재시도 중... (temperature={next_t})")
            else:
                _notify(f"  [JSON] {_pfx}3회 모두 실패 — raw 텍스트로 대체 (파이프라인 계속)")
        except Exception:
            raise  # OverloadError 등은 그대로 전파

    _notify(f"  [JSON] {_pfx}경고: raw 저장됨 (앞 200자): {last_raw[:200]}")
    if progress_fn and label:
        try:
            progress_fn({
                "type": "log",
                "message": f"❌ {label} 생성 실패 — JSON 응답을 받지 못했습니다. 재실행을 시도하세요.",
            })
        except Exception:
            pass
    return {"_raw": last_raw, "_parse_failed": True}


def _extract_json(
    raw: str,
    prompt: str = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 8192,
) -> dict:
    """응답 텍스트에서 JSON 객체 추출 및 파싱 (3단계 폴백).

    1단계: 표준 json.loads
    2단계: json_repair 자동 복구
    3단계: Claude 재요청
    """
    if not raw:
        raise ValueError("API 응답이 비어 있습니다.")

    # ── 공통 전처리: 마크다운 코드블록 제거, JSON 객체 범위 추출 ──
    cleaned = _strip_markdown(raw)
    json_str = _extract_object(cleaned)

    if not json_str:
        # 중괄호가 아예 없는 경우 → 바로 3단계로
        return _fallback_ask_claude(raw, prompt, model, max_tokens)

    # ── 전처리: 문자열 내 닫히지 않은 괄호 보정 ──
    json_str = _close_unclosed_parens(json_str)

    # ── 1단계: 표준 파싱 ──
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    # ── 2단계: json_repair 복구 ──
    try:
        repaired = repair_json(json_str, return_objects=True)
        if isinstance(repaired, dict) and repaired:
            print("  [JSON] json_repair로 복구 성공")
            return repaired
    except Exception:
        pass

    # repair_json이 문자열 반환한 경우 재파싱
    try:
        repaired_str = repair_json(json_str)
        result = json.loads(repaired_str)
        if isinstance(result, dict) and result:
            print("  [JSON] json_repair로 복구 성공")
            return result
    except Exception:
        pass

    # ── 3단계: Claude 재요청 ──
    return _fallback_ask_claude(raw, prompt, model, max_tokens)


def _fallback_ask_claude(
    raw: str,
    prompt: str | None,
    model: str,
    max_tokens: int,
) -> dict:
    """Claude에게 JSON만 다시 출력해달라고 재요청."""
    print("  [JSON] 파싱 실패 → Claude에게 JSON 재출력 요청 중...")

    retry_prompt = (
        "아래 텍스트에서 JSON 객체만 정확히 추출해서 출력해줘.\n"
        "마크다운 코드블록, 설명 텍스트 없이 순수 JSON만 출력해야 해.\n"
        "특수문자(—, ·, «», 등)가 있다면 안전한 유니코드로 유지해줘.\n\n"
        f"[원본 텍스트]\n{raw[:6000]}"
    )

    try:
        retry_raw = call(retry_prompt, model=model, max_tokens=max_tokens, _skip_citation=True)
        cleaned   = _strip_markdown(retry_raw)
        json_str  = _extract_object(cleaned) or cleaned

        result = json.loads(json_str)
        print("  [JSON] Claude 재요청으로 복구 성공")
        return result
    except json.JSONDecodeError:
        pass

    # 재요청 결과도 json_repair로 한 번 더 시도
    try:
        repaired = repair_json(json_str, return_objects=True)
        if isinstance(repaired, dict) and repaired:
            print("  [JSON] Claude 재요청 + json_repair로 복구 성공")
            return repaired
    except Exception:
        pass

    raise ValueError(
        f"JSON 파싱 3단계 폴백 모두 실패.\n"
        f"응답 미리보기:\n{raw[:400]}"
    )


def _validate_and_retry(
    result: dict,
    prompt: str,
    model: str,
    max_tokens: int,
    max_retries: int = 2,
) -> dict:
    """JSON 결과에서 빈 문자열 필드를 찾아 재시도. 100자 미만 필드는 경고만."""
    empty_fields = _find_empty_fields(result)
    if not empty_fields:
        return result

    for attempt in range(1, max_retries + 1):
        print(f"  [품질검사] 빈 필드 발견 ({', '.join(empty_fields[:5])}) — 재시도 {attempt}/{max_retries}")
        retry_prompt = (
            f"이전 응답에서 {', '.join(empty_fields)} 필드가 비어있었습니다.\n"
            "반드시 구체적인 내용으로 채워서 다시 작성해주세요.\n\n"
            f"원본 요청:\n{prompt[:4000]}"
        )
        try:
            raw = call(retry_prompt, model=model, max_tokens=max_tokens, _skip_citation=True)
            new_result = _extract_json(raw, model=model, max_tokens=max_tokens)
            # 빈 필드를 새 결과로 보충
            for field in empty_fields:
                keys = field.split(".")
                _deep_set(result, keys, _deep_get(new_result, keys))
        except Exception as e:
            print(f"  [품질검사] 재시도 실패: {e}")

        remaining = _find_empty_fields(result)
        if not remaining:
            print(f"  [품질검사] 재시도 성공 — 모든 빈 필드 채움")
            break
        empty_fields = remaining

    # 재시도 후에도 남은 빈 필드 로그
    for field in _find_empty_fields(result):
        print(f"  [품질검사] 경고: {field} 비어있음 (재시도 소진)")

    # 100자 미만 문자열 필드 경고
    _warn_short_fields(result)
    return result


def _find_empty_fields(obj: dict, prefix: str = "") -> list:
    """dict에서 빈 문자열("")인 필드 경로 목록 반환 (최대 5개)."""
    found = []
    for k, v in obj.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, str) and v == "":
            found.append(path)
        elif isinstance(v, dict):
            found.extend(_find_empty_fields(v, path))
        if len(found) >= 5:
            break
    return found


def _warn_short_fields(obj: dict, prefix: str = "") -> None:
    """100자 미만 문자열 필드 경고 로그."""
    for k, v in obj.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, str) and 0 < len(v) < 100:
            print(f"  [품질검사] 경고: {path} 내용 부족 ({len(v)}자)")
        elif isinstance(v, dict):
            _warn_short_fields(v, path)


def _deep_get(obj: dict, keys: list):
    """중첩 dict에서 키 경로로 값 추출."""
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return None
    return obj


def _deep_set(obj: dict, keys: list, value) -> None:
    """중첩 dict에서 키 경로로 값 설정. 값이 None이거나 현재 값이 비어있지 않으면 skip."""
    if value is None or value == "":
        return
    for k in keys[:-1]:
        if not isinstance(obj.get(k), dict):
            return
        obj = obj[k]
    last = keys[-1]
    if last in obj and obj[last] == "":
        obj[last] = value


def _close_unclosed_parens(text: str) -> str:
    """JSON 문자열 값 내 닫히지 않은 괄호 자동 보정.

    Claude가 (출처, 2025 처럼 닫는 괄호 없이 문자열을 끝내면
    json_repair가 해당 지점에서 내용을 잘라낼 수 있다.
    패턴: ( ... ) 없이 " 로 닫히는 경우 → ) 자동 삽입.
    """
    return re.sub(r'\(([^)"]{1,100})(?=")', r'(\1)', text)


def _strip_markdown(text: str) -> str:
    """마크다운 코드블록 및 앞뒤 공백 제거."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.replace("```", "")
    return text.strip()


def _extract_object(text: str) -> str | None:
    """가장 바깥쪽 { } 범위를 추출. 중첩 괄호 정확히 처리."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start: i + 1]

    # 닫는 괄호 없이 끝남 → 마지막까지 반환 (repair_json이 처리)
    return text[start:]

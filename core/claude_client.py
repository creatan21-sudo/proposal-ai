# core/claude_client.py
# 역할: Claude API 호출 공통 래퍼
# - 모든 에이전트가 이 모듈을 통해 API 호출
# - tool use(function calling)로 JSON 출력 강제 — 파싱 실패 없음
# - 재시도, 오류처리 일괄 담당

import os
import threading
import time
import anthropic
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

# ── tool use에 사용할 공통 도구 정의 ──
# 스키마를 비워두면 Claude가 프롬프트에서 요청한 필드를 자유롭게 반환
_RESULT_TOOL = {
    "name": "return_result",
    "description": (
        "작업 결과를 구조화된 JSON으로 반환합니다. "
        "프롬프트에서 요청한 모든 필드를 빠짐없이 포함해야 합니다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {}
    },
}
_TOOL_CHOICE = {"type": "tool", "name": "return_result"}


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
    """Claude API 호출 후 응답 텍스트 반환 (비JSON 텍스트 전용).

    Args:
        prompt: 사용자 프롬프트
        model: 사용할 Claude 모델명
        max_tokens: 최대 토큰 수
        max_retries: 429/529 시 최대 재시도 횟수 (기본 3)
        _skip_citation: True이면 출처 규칙 시스템 프롬프트 생략
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
    """tool use(function calling)로 JSON 결과를 강제 반환.

    tool_choice={"type":"tool","name":"return_result"}로 Claude가
    반드시 구조화된 dict를 반환하게 강제합니다 — 파싱 실패 없음.

    Args:
        prompt:      프롬프트 (원하는 JSON 구조를 설명)
        model:       Claude 모델명
        max_tokens:  최대 토큰 수 (기본 8192)
        max_retries: 429/529 시 최대 재시도 횟수
        _validate:   빈 필드 자동 보완 활성화 (기본 True)
        progress_fn: SSE 콜백 — API 오류 시 사용자에게 알림
        label:       SSE 메시지에 포함할 스텝 이름 (예: "시나리오", "기획")

    Returns:
        Claude가 반환한 dict. API 완전 실패 시 {"_parse_failed": True}.
    """
    if max_tokens == MAX_TOKENS:
        max_tokens = 8192

    _pfx = f"[{label}] " if label else ""

    def _notify(msg: str) -> None:
        print(msg)
        if progress_fn:
            try:
                progress_fn({"type": "log", "message": msg})
            except Exception:
                pass

    client = get_client()
    last_exc = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=_CITATION_SYSTEM,
                tools=[_RESULT_TOOL],
                tool_choice=_TOOL_CHOICE,
                messages=[{"role": "user", "content": prompt}],
            )

            result = None
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use" \
                        and getattr(block, "name", None) == "return_result":
                    result = block.input
                    break

            if result is None:
                raise ValueError("tool_use 응답 블록 없음")
            if not isinstance(result, dict):
                result = {"_raw": result}

            if _validate:
                result = _validate_and_retry(result, prompt, model, max_tokens)

            return result

        except anthropic.RateLimitError as e:
            last_exc = e
            if attempt < max_retries:
                _notify(f"  [Rate Limit] {_pfx}{attempt}/{max_retries}회 — {_RETRY_WAIT_SEC}초 후 재시도...")
                _fire_retry_cb(attempt, max_retries, 429, _RETRY_WAIT_SEC)
                time.sleep(_RETRY_WAIT_SEC)
            else:
                _notify(f"  [Rate Limit] {_pfx}최대 재시도 횟수 초과")

        except anthropic.APIStatusError as e:
            if e.status_code in (424, 429, 529):
                last_exc = e
                wait = 10 if e.status_code == 424 else _RETRY_WAIT_SEC
                if attempt < max_retries:
                    _notify(f"  [API {e.status_code}] {_pfx}{attempt}/{max_retries}회 — {wait}초 후 재시도...")
                    _fire_retry_cb(attempt, max_retries, e.status_code, wait)
                    time.sleep(wait)
                else:
                    _notify(f"  [API {e.status_code}] {_pfx}최대 재시도 횟수 초과")
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

        except ValueError as e:
            # tool_use 블록 구조 오류 (거의 발생 안 함)
            _notify(f"  [tool_use] {_pfx}응답 구조 오류 (시도 {attempt}/{max_retries}): {e}")
            last_exc = e
            if attempt >= max_retries:
                break

    code = getattr(last_exc, "status_code", 0) if last_exc else 0
    if code in (429, 529):
        raise OverloadError(code, max_retries) from last_exc

    if progress_fn and label:
        try:
            progress_fn({
                "type": "log",
                "message": f"❌ {label} 생성 실패 — API 오류. 재실행을 시도하세요.",
            })
        except Exception:
            pass
    return {"_parse_failed": True}


# ─────────────────────────────────────────────
# 내부 유틸리티
# ─────────────────────────────────────────────

def _validate_and_retry(
    result: dict,
    prompt: str,
    model: str,
    max_tokens: int,
    max_retries: int = 2,
) -> dict:
    """JSON 결과에서 빈 문자열 필드를 찾아 tool use로 재시도."""
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
            new_result = call_json(retry_prompt, model=model, max_tokens=max_tokens,
                                   _validate=False)
            for field in empty_fields:
                keys = field.split(".")
                _deep_set(result, keys, _deep_get(new_result, keys))
        except Exception as e:
            print(f"  [품질검사] 재시도 실패: {e}")

        remaining = _find_empty_fields(result)
        if not remaining:
            print("  [품질검사] 재시도 성공 — 모든 빈 필드 채움")
            break
        empty_fields = remaining

    for field in _find_empty_fields(result):
        print(f"  [품질검사] 경고: {field} 비어있음 (재시도 소진)")

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

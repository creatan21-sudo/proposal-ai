# app.py
# Flask 웹 서비스 — 다중 사용자 + 작업 큐 지원

import os
from dotenv import load_dotenv
load_dotenv(override=False)
print(f"[startup] ANTHROPIC_API_KEY: {'SET' if os.environ.get('ANTHROPIC_API_KEY') else 'NOT SET'}", flush=True)
print(f"[startup] OPENAI_API_KEY: {'SET' if os.environ.get('OPENAI_API_KEY') else 'NOT SET'}", flush=True)
print(f"[startup] GAMMA_API_KEY: {'SET' if os.environ.get('GAMMA_API_KEY') else 'NOT SET'}", flush=True)

import dataclasses
import datetime as _dt
import json
import os
import threading
import time
import traceback
import urllib.request as _urllib_req
import uuid
from collections import deque
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, Response, abort, flash, jsonify, redirect,
    render_template, request, send_file, session, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from core.dna import create_dna
from database.db import (
    change_password, create_user, delete_user, get_case_detail, get_connection,
    get_telegram_chat_id, get_user_by_id, init_db, init_users, list_users,
    save_case, set_telegram_chat_id, verify_user,
    hide_case, unhide_case,
    save_learning_case, list_learning_cases, delete_learning_case,
    update_user_role,
    share_proposal, unshare_proposal, get_shared_cases, get_case_shares,
    list_all_cases,
    mark_case_stopped,
    # 삭제 요청
    create_delete_request, list_delete_requests,
    approve_delete_request, reject_delete_request, delete_case,
    get_admin_telegram_ids,
    # PPT 버전
    save_ppt_version, get_ppt_versions, get_ppt_version_data, update_ppt_version_memo,
    # 스토리보드
    save_storyboard, get_storyboards,
    # 스텝 수정 오버라이드
    save_step_override, get_step_override, get_all_step_overrides,
    # PPT 작업 영속화
    save_ppt_job, update_ppt_job, get_ppt_job,
    # PPT 설계 내러티브
    save_ppt_narrative, get_ppt_narrative,
    # 이중 로그인 방지
    set_session_token, get_session_token,
    # 시나리오 재시도용
    get_completed_episodes,
    # 제안서 수정
    save_case_revision,
    # 스텝 미리보기 채택
    activate_step_result, get_step_candidates,
    get_step_preview_row, mark_rows_inactive_after,
    _RERUN_STEP_TABLE_MAP,
)
from output.txt_writer import write_txt
from utils.telegram_notify import send_telegram
from config import GAMMA_API_KEY
from utils.nara import manual_scan, fetch_bid_by_no, start_scheduler
from database.db import (get_nara_keywords, delete_nara_keyword, list_nara_bids,
                          get_nara_settings, save_nara_settings,
                          add_nara_candidate, list_nara_candidates, delete_nara_candidate,
                          get_candidate_bid_nos, list_nara_bids_paged,
                          confirm_nara_candidate, list_nara_confirmed,
                          add_nara_result, list_nara_results,
                          add_candidate_comment, list_candidate_comments,
                          add_nara_pickup, list_nara_pickups, delete_nara_pickup,
                          get_pickup_candidate_ids, confirm_nara_pickup,
                          get_confirmed_by_id,
                          get_confirmed_narrative, save_confirmed_narrative,
                          add_confirmed_comment, list_confirmed_comments,
                          add_confirmed_schedule, list_confirmed_schedule,
                          update_confirmed_schedule, delete_confirmed_schedule,
                          get_confirmed_bid_info, save_confirmed_bid_info,
                          create_notification, list_notifications,
                          mark_notification_read, mark_all_read, count_unread,
                          get_notification_settings, save_notification_settings,
                          get_last_notification_time, record_notification_sent,
                          save_confirmed_rfp_file, list_confirmed_rfp_files,
                          get_confirmed_research, save_confirmed_research,
                          get_or_create_default_schedules,
                          request_completion, approve_completion, set_final_result,
                          get_proposal_design, save_proposal_design)

app = Flask(__name__)
# Railway 등 역방향 프록시 환경에서 X-Forwarded-* 헤더 올바르게 처리
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.environ.get("SECRET_KEY", "prointerz-web-secret-2024")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

_IS_PRODUCTION = os.environ.get("FLASK_ENV", "production") == "production"
_default_upload = "/tmp/uploads" if _IS_PRODUCTION else str(Path(__file__).parent / "uploads")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", _default_upload))

# 시작 시 필요한 폴더 자동 생성
# /app/data: Railway Volume 마운트 경로 (DB 영구 보존용)
_extra_dirs = [Path("/app/data"), Path("/app/data/storyboards")] if os.environ.get("RAILWAY_ENVIRONMENT") else []
for _d in [UPLOAD_DIR, Path(__file__).parent / "database",
           Path(__file__).parent / "output" / "proposals",
           Path(__file__).parent / "output" / "storyboards"] + _extra_dirs:
    _d.mkdir(parents=True, exist_ok=True)

# gunicorn 등 외부 서버 기동 시에도 DB/테이블이 반드시 존재하도록 초기화
with app.app_context():
    init_db()
    init_users()
    # 서버 재시작 시 stale 'running' 리서치 레코드 초기화
    try:
        from database.db import get_connection as _gc_init
        with _gc_init() as _conn_init:
            _conn_init.execute(
                "UPDATE confirmed_research SET status='error', research_result='서버 재시작으로 중단됨' WHERE status='running'"
            )
    except Exception as _e_init:
        print(f"[startup] stale 리서치 초기화 오류: {_e_init}")

start_scheduler(app)

VIDEO_TYPES = ["홍보영상", "다큐멘터리", "교육영상", "캠페인영상", "뉴스형영상"]
ALLOWED_EXT     = {".hwp", ".hwpx", ".pdf", ".txt"}
ALLOWED_REF_EXT = {".hwp", ".hwpx", ".pdf", ".txt", ".docx", ".pptx"}


def _safe_upload_name(original_filename: str, forced_ext: str) -> str:
    """업로드 파일명을 안전하게 변환하되 원본 확장자를 반드시 보존.

    - secure_filename()은 한글 등 비ASCII 문자를 제거해 확장자가 사라질 수 있음.
    - 확장자를 forced_ext(소문자)로 강제 통일하고, base만 secure_filename으로 처리.
    - UUID 8자리 접두사로 동시 업로드 시 파일명 충돌 방지.
    """
    stem = Path(original_filename).stem
    safe_stem = secure_filename(stem)
    if not safe_stem:
        safe_stem = "upload"
    return uuid.uuid4().hex[:8] + "_" + safe_stem + forced_ext

# ── 인메모리 파이프라인 세션
_sessions: dict = {}
_sessions_lock = threading.Lock()

# ── PPT 설계 재실행 진행 중 case_id 집합 (폴링 충돌 방지)
_ppt_generating: set = set()

# ── 케이스별 재실행 충돌 방지 상태
# case_id → {type:'step'|'ppt', step_key, step_label, sid?(step), abort_event?(ppt)}
_case_rerun_state: dict = {}
_case_rerun_lock  = threading.Lock()

# ── 작업 큐 (FIFO, 메모리 전용 — 서버 재시작 시 자동 초기화)
_job_queue: deque = deque()   # session_id 목록 (queued 순서 보존)
_queue_lock = threading.Lock()
_queue_notify = threading.Event()
_worker_started = False

# ── 동시 실행 제어
_MAX_CONCURRENT = 2           # 동시 실행 최대 작업 수 (최대 2개)
_active_sids: set = set()     # 현재 실행 중인 sid 집합
_active_lock = threading.Lock()

# ── 작업 타임아웃 / 세션 보존
_JOB_TIMEOUT_SEC      = 1800  # 30분 이상 running이면 장기실행 로그 (강제 종료 안 함)
_SESSION_RETENTION_SEC = 1800 # 30분 — 완료/중지/오류 세션 메모리 보존 기간
_STALE_WAITING_SEC     = 3600 # 60분 이상 waiting_confirm 무응답이면 사용자 이탈로 간주

# ── PPT 생성 작업
_ppt_jobs: dict = {}
_ppt_jobs_lock = threading.Lock()


# ─────────────────────────────────────────────
# 큐 워커
# ─────────────────────────────────────────────

def _ensure_worker():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        threading.Thread(target=_queue_worker, daemon=True).start()
        threading.Thread(target=_timeout_monitor, daemon=True).start()
        _schedule_credit_report()


def _queue_worker():
    """큐에서 작업을 꺼내 최대 _MAX_CONCURRENT개 병렬 실행."""
    while True:
        _queue_notify.wait()
        _queue_notify.clear()
        _dispatch_jobs()


def _dispatch_jobs():
    """대기 중인 작업을 동시 실행 한도만큼 스레드로 디스패치."""
    while True:
        sid = None
        # _queue_lock + _active_lock 동시 획득으로 슬롯 예약을 원자적으로 처리
        with _queue_lock:
            with _active_lock:
                if len(_active_sids) >= _MAX_CONCURRENT:
                    return
                for s in _job_queue:
                    if s not in _active_sids:
                        sid = s
                        _active_sids.add(sid)   # 슬롯 선점
                        break

        if sid is None:
            return

        with _sessions_lock:
            sess = _sessions.get(sid)

        if not sess or sess.get("status") != "queued":
            # 유효하지 않은 항목 → 큐·슬롯 정리 후 다음 항목 시도
            with _active_lock:
                _active_sids.discard(sid)
            with _queue_lock:
                try:
                    _job_queue.remove(sid)
                except ValueError:
                    pass
            continue

        with _sessions_lock:
            sess["status"]     = "running"
            sess["started_at"] = time.time()

        threading.Thread(target=_run_job, args=(sid,), daemon=False).start()


def _run_job(sid: str):
    """개별 파이프라인을 전용 스레드에서 실행하고 반드시 정리."""
    with _sessions_lock:
        sess = _sessions.get(sid)
    if not sess:
        _finish_job(sid)
        return
    try:
        _push(sid, {"type": "pipeline_starting"})
        _run_pipeline_sync(sid, sess)
    except Exception as e:
        # _run_pipeline_sync 내부에서 대부분 처리되지만 혹시 새는 예외 대비
        print(f"\n[_run_job 예외 누출] sid={sid} — {type(e).__name__}: {e}")
        traceback.print_exc()
        _push(sid, {"type": "pipeline_error", "message": f"[{type(e).__name__}] {e}"})
        with _sessions_lock:
            s = _sessions.get(sid)
            if s:
                s["status"]       = "error"
                s["completed_at"] = time.time()
                s["sse_event"].set()
    finally:
        _finish_job(sid)


def _finish_job(sid: str):
    """작업 완료/오류 시 큐·슬롯에서 즉시 제거하고 후속 작업 디스패치."""
    with _queue_lock:
        try:
            _job_queue.remove(sid)
        except ValueError:
            pass
    with _active_lock:
        _active_sids.discard(sid)
    _broadcast_positions()
    _dispatch_jobs()    # 빈 슬롯에 대기 중인 작업 즉시 투입


def _timeout_monitor():
    """주기적 세션 상태 감시: 타임아웃·메모리 누수·장기 대기 처리."""
    while True:
        time.sleep(60)
        now = time.time()
        with _sessions_lock:
            sids = list(_sessions.keys())
        for sid in sids:
            with _sessions_lock:
                sess = _sessions.get(sid)
            if not sess:
                continue
            status = sess.get("status", "")

            # ① running 상태 장기 체크 — abort는 하지 않고 경고만 로깅
            #    (파이프라인은 15~20분 소요될 수 있으므로 강제 종료 금지)
            if (status == "running"
                    and (now - sess.get("started_at", now)) > _JOB_TIMEOUT_SEC):
                elapsed_min = int((now - sess.get("started_at", now)) / 60)
                print(f"  [장기실행] {sid} {elapsed_min}분 경과 — 계속 실행 중 (정상)")

            # ② 완료/중지/오류 30분 초과 → 메모리에서 제거
            elif status in ("done", "error", "stopped"):
                completed_at = sess.get("completed_at", 0)
                if completed_at and (now - completed_at) > _SESSION_RETENTION_SEC:
                    with _sessions_lock:
                        _sessions.pop(sid, None)
                    print(f"  [정리] {sid} 완료 세션 해제 (status={status})")

            # ③ waiting_confirm 60분 초과 → 사용자 이탈로 간주, abort 신호
            elif status == "waiting_confirm":
                # platform / marketing 완료 후 대기: abort 제외 (장시간 생성 스텝)
                _last_step = sess.get("last_completed_step", "")
                if _last_step in ("platform", "marketing", "platform_ops"):
                    pass  # 이 스텝 완료 후 대기는 abort하지 않음
                else:
                    # waiting_since: wait_confirm 진입 시각 (없으면 started_at 폴백)
                    _since = (sess.get("waiting_since")
                              or sess.get("last_active_at")
                              or sess.get("started_at")
                              or sess.get("created_at_ts", now))
                    if (now - _since) > _STALE_WAITING_SEC:
                        print(f"  [정리] {sid} {int((now-_since)//60)}분 응답 없음 → abort")
                        with _sessions_lock:
                            s = _sessions.get(sid)
                            if s:
                                s["user_input"] = "__abort__"
                                s["confirm_event"].set()


def _broadcast_positions():
    """대기 중(queued) 세션에만 큐 순서 이벤트 전송."""
    with _queue_lock:
        queue_list = list(_job_queue)
    with _active_lock:
        active = set(_active_sids)
    # 실행 중이 아닌(대기 중인) 항목만 순서 표시
    waiting = [s for s in queue_list if s not in active]
    for pos, sid in enumerate(waiting):
        _push(sid, {
            "type":     "queue_position",
            "position": pos + 1,
            "total":    len(waiting),
        })


def _push(sid: str, event: dict):
    with _sessions_lock:
        sess = _sessions.get(sid)
        if sess:
            sess["events"].append(event)
            sess["sse_event"].set()
            # 스텝 완료 시 활성 시간 갱신 (abort 타이머 리셋)
            if event.get("type") == "step_summary":
                sess["last_active_at"]      = time.time()
                sess["last_completed_step"] = event.get("step", "")


def _run_pipeline_sync(sid: str, sess: dict):
    """파이프라인을 워커 스레드에서 동기 실행."""
    import dataclasses as _dc

    dna     = sess["dna"]
    rfp_file = sess["rfp_file"]
    concept  = sess["concept"]
    user_id  = sess["user_id"]

    from web_pipeline import run as wp_run

    def push(event):
        _push(sid, event)

    def wait_confirm(step_key):
        with _sessions_lock:
            s = _sessions.get(sid)
            if not s:
                return "__abort__"
            # 대기 진입 전 이미 중단 신호가 있으면 즉시 반환 (스텝 실행 중 stop 클릭 대응)
            if s.get("user_input") == "__abort__":
                s["user_input"] = None
                return "__abort__"
            s["status"]        = "waiting_confirm"
            s["waiting_since"] = time.time()  # abort 타이머 기준점
        s = _sessions.get(sid)
        if not s:
            return "__abort__"
        s["confirm_event"].clear()
        s["confirm_event"].wait(timeout=1200)  # 20분 타임아웃
        with _sessions_lock:
            s = _sessions.get(sid)
            if not s:
                return "__abort__"
            inp         = s.get("user_input") or "y"
            instruction = s.get("step_instruction") or ""
            s["user_input"]       = None
            s["step_instruction"] = None
            s["status"]           = "running"
        # 다음 스텝에 주입할 사전 지시 설정
        if instruction:
            dna.step_instruction = instruction
        return inp

    # 텔레그램 알림 콜백 생성
    chat_id = get_telegram_chat_id(user_id)
    if chat_id:
        def notify_fn(message):
            send_telegram(chat_id, message)
    else:
        notify_fn = None

    # 파이프라인 시작 전 케이스 선등록 — 모든 스텝 결과의 created_at이 case보다 늦도록
    # (get_case_detail의 created_at >= case_ts 필터가 올바르게 작동하기 위해 필수)
    # 이어서 하기(resume): 세션에 case_id가 이미 있으면 선등록 스킵
    existing_case_id = sess.get("case_id") or (dna.case_id or 0)
    if existing_case_id:
        saved_case_id = existing_case_id
        dna.case_id   = saved_case_id
    else:
        try:
            dna_json_init = json.dumps(_dc.asdict(dna), ensure_ascii=False)
            saved_case_id = save_case(
                client_name=dna.client_name,
                project_name=dna.project_name,
                video_type=dna.video_type,
                dna_json=dna_json_init,
                result_json="{}",
                agency_type=dna.agency_type,
                budget=dna.budget,
                deadline=dna.deadline,
                user_id=user_id,
            )
            dna.case_id = saved_case_id
            with _sessions_lock:
                s = _sessions.get(sid)
                if s:
                    s["case_id"] = saved_case_id
        except Exception as e:
            saved_case_id = None
            push({"type": "log", "message": f"케이스 선등록 오류: {e}"})

    # 미리보기 모드: 실행 전 각 스텝 테이블 최대 ID 기록
    _preview_mode    = sess.get("preview_mode", False)
    _preview_pre_ids = {}
    if _preview_mode and saved_case_id:
        for _sk in (sess.get("selected_steps") or set()):
            _tbl = _RERUN_STEP_TABLE_MAP.get(_sk)
            if _tbl:
                try:
                    with get_connection() as _pc:
                        _mr = _pc.execute(
                            f"SELECT MAX(id) AS mx FROM {_tbl} WHERE case_id=?",
                            (saved_case_id,)
                        ).fetchone()
                    _preview_pre_ids[_tbl] = _mr["mx"] or 0
                except Exception:
                    _preview_pre_ids[_tbl] = 0

    try:
        results = wp_run(
            dna, push, wait_confirm,
            rfp_file=rfp_file, concept=concept,
            start_step_key=sess.get("retry_from"),
            prior_results=sess.get("results") or {},
            notify_fn=notify_fn,
            auto_run=sess.get("auto_run", False),
            selected_steps=sess.get("selected_steps"),
        )

        # 미리보기 모드: 새로 저장된 행을 is_active=0으로 표시
        if _preview_mode and saved_case_id and _preview_pre_ids:
            _new_row_ids = {}
            for _tbl, _old_max in _preview_pre_ids.items():
                cnt = mark_rows_inactive_after(_tbl, saved_case_id, _old_max)
                if cnt:
                    with get_connection() as _pc:
                        _nr = _pc.execute(
                            f"SELECT id FROM {_tbl} WHERE case_id=? AND id>? ORDER BY id DESC LIMIT 1",
                            (saved_case_id, _old_max)
                        ).fetchone()
                    if _nr:
                        _new_row_ids[_tbl] = _nr["id"]
            with _sessions_lock:
                _s = _sessions.get(sid)
                if _s:
                    _s["preview_row_ids"] = _new_row_ids
            print(f"  [미리보기] 신규 행 is_active=0 마킹: {_new_row_ids}")

        # 사용자가 직접 중지한 경우
        with _sessions_lock:
            _stopped = _sessions.get(sid, {}).get("stopped_by_user", False)

        if _stopped and "__aborted_at__" in results:
            # 중단 시점까지의 DNA 스냅샷 + stopped 플래그 저장
            try:
                dna_json = json.dumps(_dc.asdict(dna), ensure_ascii=False)
                if saved_case_id:
                    mark_case_stopped(saved_case_id, dna_json=dna_json)
            except Exception as e:
                push({"type": "log", "message": f"중지 저장 오류: {e}"})
            if saved_case_id:
                push({"type": "case_saved", "case_id": saved_case_id})
            push({"type": "pipeline_stopped",
                  "step": results.get("__aborted_at__", "")})
            with _sessions_lock:
                s = _sessions.get(sid)
                if s:
                    s["status"]       = "stopped"
                    s["results"]      = results
                    s["completed_at"] = time.time()
                    s["sse_event"].set()
            return

        aborted_at = results.get("__aborted_at__")

        txt_path = None
        if not aborted_at:
            try:
                txt_path = write_txt(dna, results)
            except Exception as e:
                push({"type": "log", "message": f"TXT 생성 오류: {e}"})
            try:
                dna_json    = json.dumps(_dc.asdict(dna), ensure_ascii=False)
                result_json = json.dumps(results.get("final_proposal", {}), ensure_ascii=False)
                if saved_case_id:
                    from database.db import update_case
                    update_case(saved_case_id, dna_json=dna_json, result_json=result_json)
                else:
                    saved_case_id = save_case(
                        client_name=dna.client_name,
                        project_name=dna.project_name,
                        video_type=dna.video_type,
                        dna_json=dna_json,
                        result_json=result_json,
                        agency_type=dna.agency_type,
                        budget=dna.budget,
                        deadline=dna.deadline,
                        user_id=user_id,
                    )
            except Exception as e:
                push({"type": "log", "message": f"DB 업데이트 오류: {e}"})

            if saved_case_id:
                push({"type": "case_saved", "case_id": saved_case_id})
            push({"type": "pipeline_done"})

            with _sessions_lock:
                s = _sessions.get(sid)
                if s:
                    s["status"]       = "done"
                    s["txt_path"]     = txt_path
                    s["results"]      = results
                    s["completed_at"] = time.time()
                    s["sse_event"].set()

        else:
            # 중요 스텝 실패로 파이프라인 중단 — pipeline_aborted는 이미 전송됨
            # status를 "error"로 설정해 retry_pipeline이 재시도를 허용하도록 함
            if saved_case_id:
                try:
                    dna_json = json.dumps(_dc.asdict(dna), ensure_ascii=False)
                    update_case(saved_case_id, dna_json=dna_json)
                except Exception:
                    pass
            with _sessions_lock:
                s = _sessions.get(sid)
                if s:
                    s["status"]       = "error"
                    s["results"]      = results
                    s["completed_at"] = time.time()
                    s["sse_event"].set()
            print(f"  [파이프라인 중단] {dna.project_name} — {aborted_at} 단계")

    except Exception as e:
        print(f"\n[pipeline_error] {dna.project_name if 'dna' in dir() else sid} — {type(e).__name__}: {e}")
        traceback.print_exc()
        push({"type": "pipeline_error", "message": f"[{type(e).__name__}] {e}"})
        with _sessions_lock:
            s = _sessions.get(sid)
            if s:
                s["status"]       = "error"
                s["completed_at"] = time.time()
                s["sse_event"].set()


# ─────────────────────────────────────────────
# 전역 인증 체크 (로그인 페이지·static은 예외)
# ─────────────────────────────────────────────

# 로그인 없이 접근 허용할 엔드포인트
_PUBLIC_ENDPOINTS = frozenset({"login", "logout", "static"})

@app.before_request
def check_login():
    """모든 요청에서 로그인 여부 확인. 로그인·static은 무조건 통과."""
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    uid = session.get("user_id")
    if not uid:
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"ok": False, "error": "로그인이 필요합니다"}), 401
        return redirect(url_for("login"))

    # 이중 로그인 방지: 세션 토큰이 DB와 불일치하면 강제 만료
    session_token = session.get("session_token")
    if session_token:
        try:
            db_token = get_session_token(uid)
            if db_token and db_token != session_token:
                session.clear()
                if request.path.startswith("/api/") or request.is_json:
                    return jsonify({
                        "ok": False,
                        "error": "다른 기기에서 로그인되어 세션이 종료되었습니다.",
                        "redirect": "/login",
                    }), 401
                return redirect(url_for("login") + "?reason=duplicate")
        except Exception:
            pass  # DB 오류 시 통과 (서비스 중단 방지)

    return None


# ─────────────────────────────────────────────
# 인증 데코레이터
# ─────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped


def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if not session.get("is_admin"):
            abort(403)
        return f(*args, **kwargs)
    return wrapped


def operator_or_admin_required(f):
    """operator 이상 권한 필요 (파이프라인 생성, 학습 데이터 관리 등).
    user 역할은 열람 전용 — 생성/수정 엔드포인트 403 차단.
    """
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "로그인이 필요합니다."}), 401
            return redirect(url_for("login"))
        role = session.get("role", "")
        if role not in ("admin", "operator"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "권한이 없습니다. 열람 전용 계정입니다."}), 403
            abort(403)
        return f(*args, **kwargs)
    return wrapped


# ─────────────────────────────────────────────
@app.route("/health")
def health():
    return "OK", 200


# 인증 라우트
# ─────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("ongoing"))
    error = None
    if request.args.get("reason") == "duplicate":
        error = "다른 기기에서 로그인되어 세션이 종료되었습니다."
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = verify_user(username, password)
        if user:
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            session["is_admin"] = bool(user["is_admin"])
            session["role"] = user.get("role") or ("admin" if user["is_admin"] else "operator")
            # 이중 로그인 방지: 새 세션 토큰 발급 → 기존 세션 자동 만료
            token = str(uuid.uuid4())
            session["session_token"] = token
            set_session_token(user["id"], token)
            # user 역할: 공유받은 제안서 목록으로 바로 이동
            if session["role"] == "user":
                return redirect(url_for("history"))
            return redirect(url_for("ongoing"))
        error = "아이디 또는 비밀번호가 틀렸습니다."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─────────────────────────────────────────────
# 메인 페이지
# ─────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return redirect(url_for("ongoing"))


@app.route("/ongoing")
@login_required
def ongoing():
    username = session.get("username")
    role     = session.get("role", "user")
    is_admin = session.get("is_admin", False)

    with get_connection() as conn:
        confirmed_list = conn.execute("""
            SELECT cf.id,
                   cf.confirmed_by, cf.notes, cf.assignee, cf.created_at,
                   cf.completion_status, cf.final_result,
                   COALESCE(pk.bid_ntce_no, ca.bid_ntce_no) AS bid_ntce_no,
                   COALESCE(pk.bid_ntce_nm, ca.bid_ntce_nm) AS bid_ntce_nm,
                   COALESCE(pk.ntce_instt_nm, ca.ntce_instt_nm) AS ntce_instt_nm,
                   COALESCE(pk.presmpt_prce, ca.presmpt_prce) AS presmpt_prce,
                   COALESCE(pk.bid_clse_dt, ca.bid_clse_dt) AS bid_clse_dt,
                   COALESCE(pk.ntce_url, ca.ntce_url) AS ntce_url,
                   bi.submit_deadline, bi.submit_method,
                   bi.pt_date, bi.price_bid_date,
                   n.content AS narrative_content,
                   COALESCE(rfp.cnt, 0) AS rfp_count,
                   COALESCE(sch.cnt, 0) AS schedule_count,
                   COALESCE(res.status, 'pending') AS research_status,
                   CASE WHEN pd.confirmed_id IS NOT NULL THEN 1 ELSE 0 END AS has_proposal_design
            FROM nara_confirmed cf
            LEFT JOIN nara_pickups pk    ON pk.id = cf.pickup_id    AND cf.pickup_id > 0
            LEFT JOIN nara_candidates ca ON ca.id = cf.candidate_id AND cf.pickup_id = 0
            LEFT JOIN confirmed_bid_info bi ON bi.confirmed_id = cf.id
            LEFT JOIN confirmed_narratives n ON n.confirmed_id = cf.id
            LEFT JOIN (SELECT confirmed_id, COUNT(*) AS cnt
                       FROM confirmed_rfp_files GROUP BY confirmed_id) rfp
                   ON rfp.confirmed_id = cf.id
            LEFT JOIN (SELECT confirmed_id, COUNT(*) AS cnt
                       FROM confirmed_schedule GROUP BY confirmed_id) sch
                   ON sch.confirmed_id = cf.id
            LEFT JOIN confirmed_research res ON res.confirmed_id = cf.id
            LEFT JOIN (SELECT confirmed_id FROM proposal_design WHERE content != '') pd
                   ON pd.confirmed_id = cf.id
            LEFT JOIN nara_results r ON r.confirmed_id = cf.id
            WHERE r.id IS NULL
              AND (cf.final_result IS NULL OR cf.final_result NOT IN ('won','lost'))
            ORDER BY COALESCE(pk.bid_clse_dt, ca.bid_clse_dt) ASC
        """).fetchall()

    tasks = []
    for row in confirmed_list:
        c = dict(row)
        is_assignee = (c.get("assignee") == username)
        can_edit = is_admin or role == "operator" or is_assignee

        incomplete = []
        if c["rfp_count"] == 0:
            incomplete.append({
                "icon": "📄",
                "label": "RFP 미등록",
                "link": f"/nara/confirmed/{c['id']}/workspace?tab=research",
            })
        if c.get("research_status") == "done":
            incomplete.append({
                "icon": "🔍",
                "label": "리서치 결과 보기",
                "link": f"/nara/confirmed/{c['id']}/workspace?tab=research",
            })
        else:
            incomplete.append({
                "icon": "🔍",
                "label": "리서치 미실시",
                "link": f"/nara/confirmed/{c['id']}/workspace?tab=research",
            })
        if c["schedule_count"] <= 1:
            incomplete.append({
                "icon": "📅",
                "label": "일정 미등록",
                "link": f"/nara/confirmed/{c['id']}/workspace?tab=narrative",
            })
        if not c["narrative_content"] or c["narrative_content"] in ("{}", ""):
            incomplete.append({
                "icon": "✍️",
                "label": "내러티브 미작성",
                "link": f"/nara/confirmed/{c['id']}/workspace?tab=narrative",
            })
        if c.get("has_proposal_design"):
            incomplete.append({
                "icon": "✏️",
                "label": "제안서 설계 작성됨",
                "link": f"/nara/confirmed/{c['id']}?tab=proposal_design",
            })

        c["incomplete"] = incomplete
        c["can_edit"]   = can_edit
        tasks.append(c)

    my_tasks  = [t for t in tasks if t.get("assignee") == username]
    all_tasks = tasks

    if not all_tasks:
        return redirect(url_for("nara_dashboard"))

    from datetime import datetime as _dt, timedelta as _td
    _now = _dt.now()
    return render_template(
        "ongoing.html",
        my_tasks=my_tasks,
        all_tasks=all_tasks,
        username=username,
        role=role,
        is_admin=is_admin,
        is_ops=(is_admin or role == "operator"),
        now=_now.strftime("%Y-%m-%d %H:%M"),
        cutoff_d3=(_now + _td(days=3)).strftime("%Y-%m-%d"),
    )

@app.route("/proposal")
@login_required
def new_proposal():
    if session.get("role") == "user":
        return redirect(url_for("history"))
    return render_template("index.html", video_types=VIDEO_TYPES)


# ─────────────────────────────────────────────
# 파이프라인 시작
# ─────────────────────────────────────────────

@app.route("/start", methods=["POST"])
@operator_or_admin_required
def start():
    client        = request.form.get("client", "").strip()
    project       = request.form.get("project", "").strip()
    video_type    = request.form.get("video_type", "홍보영상")
    quantity      = int(request.form.get("quantity") or 1)
    duration      = request.form.get("duration", "3분").strip() or "3분"
    pages         = int(request.form.get("pages") or 50)
    concept       = request.form.get("concept", "").strip() or None
    budget        = request.form.get("budget", "").strip()
    deadline      = request.form.get("deadline", "").strip()
    target        = request.form.get("target", "").strip()
    key_message   = request.form.get("key_message", "").strip()
    user_direction = request.form.get("user_direction", "").strip()
    reference_case_id = int(request.form.get("reference_case_id") or 0)

    # 고급 설정: 스텝 선택 (미선택 시 기본값 = 전체 스텝, 단 스토리보드 제외)
    _default_steps = ["rfp_analysis","research","narrative","strategy",
                      "creative","plan","script","platform","marketing","final_proposal",
                      "improvement_report","ppt_design"]
    _all_steps     = _default_steps + ["storyboard"]
    selected_steps_raw = request.form.getlist("selected_steps")
    selected_steps = set(selected_steps_raw) if selected_steps_raw else set(_default_steps)

    # 실행 모드
    auto_run = (request.form.get("run_mode", "interactive") == "auto")

    # 시나리오 사전 설정 (기본 1편)
    script_preset_episodes   = int(request.form.get("script_preset_episodes") or 1)
    if script_preset_episodes < 1:
        script_preset_episodes = 1
    script_preset_storyboard = request.form.get("script_preset_storyboard", "auto").strip() or "auto"
    # 스토리보드 사전 설정
    _sb_style_raw = request.form.get("storyboard_style", "line").strip()
    storyboard_style = _sb_style_raw if _sb_style_raw in ("line", "color", "photo") else "line"
    storyboard_cuts_per_ep = max(1, min(30, int(request.form.get("storyboard_cuts_per_ep") or 10)))
    generate_storyboard  = request.form.get("generate_storyboard") == "on"
    ppt_target_slides    = max(10, min(60, int(request.form.get("ppt_target_slides") or 50)))
    _sm_raw = request.form.get("script_mode", "full").strip()
    script_mode = _sm_raw if _sm_raw in ("full", "summary") else "full"

    if not client or not project:
        return redirect(url_for("index"))

    rfp_file = None
    f = request.files.get("rfp_file")
    if f and f.filename:
        orig_name = f.filename
        ext = Path(orig_name).suffix.lower()
        if ext in ALLOWED_EXT:
            safe = _safe_upload_name(orig_name, ext)
            rfp_file = str(UPLOAD_DIR / safe)
            f.save(rfp_file)
            print(f"  [업로드] RFP 파일: {orig_name!r} → 저장: {rfp_file}")

    # 참고 자료 처리 (복수)
    ref_files_raw = request.files.getlist("ref_files")
    ref_purposes_raw = request.form.getlist("ref_purposes")
    ref_structures = []

    for i, ref_f in enumerate(ref_files_raw):
        if not ref_f or not ref_f.filename:
            continue
        purpose = ref_purposes_raw[i] if i < len(ref_purposes_raw) else "기타 참고자료"
        orig_ref = ref_f.filename
        ext = Path(orig_ref).suffix.lower()
        if ext not in ALLOWED_REF_EXT:
            continue
        safe_ref = f"ref_{i}_" + _safe_upload_name(orig_ref, ext)
        ref_path = str(UPLOAD_DIR / safe_ref)
        ref_f.save(ref_path)
        print(f"  [업로드] 참고자료 {i+1}: {orig_ref!r} / 용도: {purpose} → {ref_path}")
        try:
            from agents.rfp_parser import parse_reference_proposal
            parsed = parse_reference_proposal(ref_path)
            ref_structures.append(f"[{purpose}]\n{parsed}")
        except Exception as e:
            print(f"[경고] 참고자료 {i+1} 분석 실패: {e}")

    init_db()
    dna = create_dna({
        "client_name":     client,
        "project_name":    project,
        "video_type":      video_type,
        "quantity":        quantity,
        "duration":        duration,
        "budget":          budget,
        "deadline":        deadline,
        "target_audience": target or "일반 국민",
        "key_message":     key_message,
        "rfp_text":        "",
        "user_direction":  user_direction,
    })
    dna.pages = pages
    dna.script_preset_episodes   = script_preset_episodes
    dna.script_preset_storyboard = script_preset_storyboard
    dna.storyboard_style         = storyboard_style
    dna.storyboard_cuts_per_ep   = storyboard_cuts_per_ep
    dna.generate_storyboard      = generate_storyboard
    dna.ppt_target_slides        = ppt_target_slides
    dna.script_mode              = script_mode
    if ref_structures:
        dna.reference_structure = "\n\n---\n\n".join(ref_structures)

    # 재활용 참고 케이스 처리
    if reference_case_id:
        dna.reference_case_id = reference_case_id
        try:
            with get_connection() as _rc:
                ref_row = _rc.execute(
                    "SELECT client_name, project_name, dna_json FROM rfp_cases WHERE id=?",
                    (reference_case_id,)
                ).fetchone()
            if ref_row:
                ref_dna = json.loads(ref_row["dna_json"] or "{}")
                parts = [f"발주처: {ref_row['client_name']} / 사업명: {ref_row['project_name']}"]
                if ref_dna.get("core_problem"):
                    parts.append(f"핵심 문제 정의: {ref_dna['core_problem']}")
                if ref_dna.get("persuasion_structure"):
                    ps = ref_dna["persuasion_structure"]
                    if isinstance(ps, list):
                        parts.append("설득 구조: " + " → ".join(
                            (p.get("stage") or p.get("headline") or str(p))
                            if isinstance(p, dict) else str(p)
                            for p in ps[:4]
                        ))
                if ref_dna.get("concept"):
                    parts.append(f"핵심 컨셉: {ref_dna['concept']}")
                if ref_dna.get("tone_and_manner"):
                    parts.append(f"톤앤매너: {ref_dna['tone_and_manner']}")
                if ref_dna.get("slogan"):
                    parts.append(f"슬로건: {ref_dna['slogan']}")
                dna.reference_case_context = "\n".join(parts)
        except Exception as _e:
            print(f"[경고] 참고 케이스 로드 실패: {_e}")

    # ── 사용자당 동시 실행 1개 제한
    uid = session["user_id"]
    with _sessions_lock:
        existing_sid = next(
            (s_id for s_id, d in _sessions.items()
             if d.get("user_id") == uid
             and d.get("status") in ("queued", "running", "waiting_confirm")),
            None,
        )
    if existing_sid:
        flash("이미 진행 중인 작업이 있습니다. 완료 후 새 제안서를 시작하세요.", "warning")
        return redirect(url_for("run_page", sid=existing_sid))

    # ── 시스템 전체 동시 실행 한도 초과 시 즉시 거부
    with _active_lock:
        _system_full = len(_active_sids) >= _MAX_CONCURRENT
    if _system_full:
        flash("현재 다른 작업 진행 중입니다. 잠시 후 시도해주세요.", "warning")
        return redirect(url_for("index"))

    sid = str(uuid.uuid4())
    with _sessions_lock:
        _sessions[sid] = {
            "status":           "queued",
            "user_id":          uid,
            "username":         session["username"],
            "dna":              dna,
            "rfp_file":         rfp_file,
            "concept":          concept,
            "results":          {},
            "events":           [],
            "sse_event":        threading.Event(),
            "confirm_event":    threading.Event(),
            "user_input":       None,
            "step_instruction": None,
            "txt_path":         None,
            "client":           client,
            "project":          project,
            "created_at":       datetime.now().isoformat(),
            "created_at_ts":    time.time(),   # 타임아웃 모니터용 타임스탬프
            "auto_run":         auto_run,
            "selected_steps":   selected_steps,
        }

    with _queue_lock:
        _job_queue.append(sid)
    _ensure_worker()
    _dispatch_jobs()        # 슬롯 여유 있으면 즉시 실행
    _broadcast_positions()

    # 현재 실행 중인 작업을 세션에 기록 (네비게이션 "진행 중" 표시용)
    session["current_run_sid"] = sid

    return redirect(url_for("run_page", sid=sid))


# ─────────────────────────────────────────────
# 진행 페이지 + SSE
# ─────────────────────────────────────────────

@app.route("/run/<sid>")
@login_required
def run_page(sid):
    with _sessions_lock:
        sess = _sessions.get(sid)
    if not sess:
        abort(404)
    # 자기 세션만 접근 가능 (관리자 예외)
    if sess["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)

    # 현재 큐 위치
    with _queue_lock:
        queue_list = list(_job_queue)
    position = (queue_list.index(sid) + 1) if sid in queue_list else 0

    return render_template(
        "run.html", sid=sid,
        client=sess["client"], project=sess["project"],
        initial_position=position,
        initial_total=len(queue_list),
    )


@app.route("/stream/<sid>")
@login_required
def stream(sid):
    def generate():
        # 세션 없으면 (Flask 재시작 등) 즉시 server_restart 이벤트 전송
        with _sessions_lock:
            sess = _sessions.get(sid)
        if not sess:
            payload = json.dumps({"type": "server_restart"}, ensure_ascii=False)
            yield f"data: {payload}\n\n"
            return

        idx = 0
        _research_review_resolved = False  # SSE 재연결 시 이미 완료된 리뷰 다이얼로그 재출력 방지
        try:
            while True:
                with _sessions_lock:
                    sess = _sessions.get(sid)
                if not sess:
                    payload = json.dumps({"type": "server_restart"}, ensure_ascii=False)
                    yield f"data: {payload}\n\n"
                    break
                events = sess["events"]
                # 리뷰 완료 여부 갱신 (한 번 True가 되면 유지)
                if not _research_review_resolved:
                    _research_review_resolved = any(
                        e.get("type") == "research_review_done" for e in events
                    )
                while idx < len(events):
                    ev = events[idx]
                    # 이미 완료된 리서치 리뷰 요청은 재전송 안 함
                    if ev.get("type") == "research_review_needed" and _research_review_resolved:
                        idx += 1
                        continue
                    data = json.dumps(ev, ensure_ascii=False)
                    yield f"data: {data}\n\n"
                    idx += 1
                # 완료/중지/오류 → 스트림 종료
                if sess["status"] in ("done", "error", "stopped"):
                    break
                fired = sess["sse_event"].wait(timeout=10)
                sess["sse_event"].clear()
                # Railway 60초 타임아웃 방지: 새 이벤트 없으면 keepalive 핑
                if not fired:
                    yield "data: {\"type\": \"ping\"}\n\n"
        except GeneratorExit:
            # 클라이언트가 연결을 끊음 — 정상 종료, 별도 처리 불필요
            pass

    return Response(
        generate(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/confirm/<sid>", methods=["POST"])
@operator_or_admin_required
def confirm(sid):
    with _sessions_lock:
        sess = _sessions.get(sid)
    if not sess:
        return jsonify({"ok": False}), 404
    if sess["user_id"] != session["user_id"]:
        return jsonify({"ok": False}), 403

    data        = request.get_json(force=True) or {}
    value       = str(data.get("input", "y")).strip()
    instruction = str(data.get("instruction", "")).strip()
    with _sessions_lock:
        sess["user_input"]       = value
        sess["step_instruction"] = instruction
        sess["confirm_event"].set()
    return jsonify({"ok": True})


@app.route("/stop/<sid>", methods=["POST"])
@operator_or_admin_required
def stop_pipeline(sid):
    """사용자가 진행 중인 파이프라인을 강제 중지."""
    with _sessions_lock:
        sess = _sessions.get(sid)
    if not sess:
        return jsonify({"ok": False, "error": "세션 없음"}), 404
    if sess["user_id"] != session["user_id"]:
        return jsonify({"ok": False}), 403

    with _sessions_lock:
        s = _sessions.get(sid)
        if not s:
            return jsonify({"ok": False}), 404
        status = s.get("status", "")
        if status in ("done", "error", "stopped"):
            return jsonify({"ok": False, "error": "이미 종료된 작업입니다."}), 400
        s["stopped_by_user"] = True
        s["user_input"]      = "__abort__"
        s["confirm_event"].set()

    return jsonify({"ok": True})


@app.route("/rfp_analyze", methods=["POST"])
@operator_or_admin_required
def rfp_analyze():
    """RFP 파일 업로드 → 폼 자동채우기용 필드 추출."""
    f = request.files.get("rfp_file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "파일 없음"}), 400

    orig_name = f.filename
    ext = Path(orig_name).suffix.lower()

    # 확장자 없거나 모름 → 일단 .bin으로 저장 후 매직바이트로 감지
    if ext not in ALLOWED_EXT:
        if ext:
            return jsonify({"ok": False, "error": f"지원하지 않는 파일 형식: {ext!r} (지원: .hwp .hwpx .pdf .txt)"}), 400
        # 확장자 없는 경우: 저장 후 감지
        safe = "analyze_noext_" + secure_filename(Path(orig_name).stem or "upload")
        tmp_path = str(UPLOAD_DIR / safe)
        f.save(tmp_path)
    else:
        safe     = _safe_upload_name(orig_name, ext)
        tmp_path = str(UPLOAD_DIR / ("analyze_" + safe))
        f.save(tmp_path)

    fsize = Path(tmp_path).stat().st_size if Path(tmp_path).exists() else 0
    print(f"  [rfp_analyze] {orig_name!r} → {tmp_path} ({fsize:,}bytes)")

    if fsize == 0:
        try: os.remove(tmp_path)
        except Exception: pass
        return jsonify({"ok": False, "error": "파일 저장 실패 (0바이트)"}), 500

    try:
        import traceback as _tb
        from agents.rfp_parser import extract_text, rfp_quick_extract
        try:
            rfp_text = extract_text(tmp_path)
        except Exception as e:
            print(f"  [rfp_analyze] extract_text 오류:\n{_tb.format_exc()}")
            return jsonify({"ok": False, "error": f"텍스트 추출 실패: {e}"}), 500

        print(f"  [rfp_analyze] 추출 텍스트 {len(rfp_text):,}자")
        if not rfp_text.strip():
            return jsonify({"ok": False, "error": "파일에서 텍스트를 추출할 수 없습니다 (스캔본이거나 이미지 PDF일 수 있음)"}), 500

        try:
            fields = rfp_quick_extract(rfp_text)
        except Exception as e:
            print(f"  [rfp_analyze] rfp_quick_extract 오류:\n{_tb.format_exc()}")
            return jsonify({"ok": False, "error": f"Claude API 오류: {e}"}), 500

        print(f"  [rfp_analyze] 추출 필드: {fields}")
        # 모든 문자열 필드가 비어 있으면 API가 빈 결과를 반환한 것 → 에러로 처리
        has_any = any(v for k, v in fields.items() if k != "quantity" and isinstance(v, str) and v.strip())
        if not has_any:
            return jsonify({"ok": False, "error": "Claude API가 빈 결과를 반환했습니다. API 키 및 네트워크 상태를 확인하세요."}), 500

        return jsonify({"ok": True, "fields": fields})
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@app.route("/retry/<sid>", methods=["POST"])
@operator_or_admin_required
def retry_pipeline(sid):
    """실패/중단된 파이프라인을 재시작. 중단 지점부터 재개.
    ?skip=1 이면 실패 스텝을 건너뛰고 다음 스텝부터 재개.
    """
    skip = request.args.get("skip") == "1"

    with _sessions_lock:
        sess = _sessions.get(sid)
    if not sess:
        return jsonify({"ok": False, "error": "세션 없음"}), 404
    if sess["user_id"] != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음 — 다시 로그인 후 시도하세요"}), 403
    if sess["status"] not in ("error", "done"):
        return jsonify({"ok": False, "error": "재시도 불가 상태"}), 400

    with _sessions_lock:
        prior = dict(sess.get("results", {}))
        aborted_at = prior.pop("__aborted_at__", None)
        start_from = aborted_at  # 기본: 실패 스텝부터 재시도

        if skip and aborted_at:
            # 실패 스텝을 건너뛰고 다음 스텝으로
            from web_pipeline import _STEPS
            step_keys = [k for k, *_ in _STEPS]
            idx = step_keys.index(aborted_at) if aborted_at in step_keys else -1
            if idx >= 0 and idx + 1 < len(step_keys):
                start_from = step_keys[idx + 1]
            else:
                start_from = None  # 마지막 스텝이면 처음부터

        sess["results"]       = prior
        sess["retry_from"]    = start_from
        sess["status"]        = "queued"
        sess["sse_event"]     = threading.Event()
        sess["confirm_event"] = threading.Event()
        sess["user_input"]    = None
        sess["txt_path"]      = None
        sess["events"].append({"type": "retry_start", "step": start_from, "skipped": aborted_at if skip else None})
        sess["sse_event"].set()

    with _queue_lock:
        _job_queue.append(sid)
    _queue_notify.set()
    _dispatch_jobs()
    _broadcast_positions()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# 다운로드
# ─────────────────────────────────────────────

@app.route("/active_run")
@login_required
def active_run():
    """현재 사용자의 진행 중인 작업 정보 반환 (네비게이션용)."""
    uid = session.get("user_id")
    # 세션에 저장된 sid 확인
    sid = session.get("current_run_sid")
    if sid:
        with _sessions_lock:
            sess = _sessions.get(sid)
        if sess and sess.get("user_id") == uid and sess.get("status") in ("queued", "running", "waiting_confirm"):
            return jsonify({"active": True, "sid": sid,
                            "client": sess.get("client", ""),
                            "project": sess.get("project", ""),
                            "status": sess.get("status", "")})
    # 세션에 없으면 _sessions에서 탐색
    with _sessions_lock:
        for s_id, s in _sessions.items():
            if s.get("user_id") == uid and s.get("status") in ("queued", "running", "waiting_confirm"):
                session["current_run_sid"] = s_id
                return jsonify({"active": True, "sid": s_id,
                                "client": s.get("client", ""),
                                "project": s.get("project", ""),
                                "status": s.get("status", "")})
    return jsonify({"active": False})


@app.route("/download/<sid>")
@login_required
def download(sid):
    with _sessions_lock:
        sess = _sessions.get(sid)
    if not sess or not sess.get("txt_path"):
        abort(404)
    if sess["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)
    fname = f"{sess['client']}_{sess['project'][:20]}_제안서.txt"
    return send_file(sess["txt_path"], as_attachment=True, download_name=fname)


# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# DB 페이지
# ─────────────────────────────────────────────

@app.route("/db")
@login_required
def db_page():
    return redirect(url_for("history"))


@app.route("/db/my_work")
@login_required
def db_my_work():
    username = session.get("username")
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT cf.id,
                   COALESCE(pk.bid_ntce_nm, ca.bid_ntce_nm) AS bid_ntce_nm,
                   COALESCE(pk.ntce_instt_nm, ca.ntce_instt_nm) AS ntce_instt_nm,
                   cf.assignee,
                   n.updated_by, n.updated_at
            FROM nara_confirmed cf
            LEFT JOIN nara_pickups pk    ON pk.id = cf.pickup_id    AND cf.pickup_id > 0
            LEFT JOIN nara_candidates ca ON ca.id = cf.candidate_id AND cf.pickup_id = 0
            JOIN confirmed_narratives n ON n.confirmed_id = cf.id
            WHERE n.updated_by = ? AND n.content != '' AND n.content != '{}'
            ORDER BY n.updated_at DESC
        """, (username,)).fetchall()
    return render_template("db_my_work.html", narratives=[dict(r) for r in rows])


# 이력 페이지
# ─────────────────────────────────────────────

@app.route("/history")
@login_required
def history():
    q           = request.args.get("q", "").strip()
    view_all    = request.args.get("all") == "1"
    show_hidden = request.args.get("show_hidden") == "1"

    with get_connection() as conn:
        # step_count: case_id 기반 우선 조회, case_id=0인 레거시는 client/project명으로 폴백
        def _step_exists(table):
            return (
                f"(CASE WHEN EXISTS("
                f"SELECT 1 FROM {table} WHERE (case_id=r.id AND case_id>0)"
                f" OR (case_id=0 AND client_name=r.client_name AND project_name=r.project_name)"
                f") THEN 1 ELSE 0 END)"
            )
        _step_subq = (
            "("
            + _step_exists("rfp_analyses")      + "+"
            + _step_exists("research_results")  + "+"
            + _step_exists("strategy_results")  + "+"
            + _step_exists("creative_results")  + "+"
            + _step_exists("plan_results")      + "+"
            + _step_exists("script_results")    + "+"
            + _step_exists("marketing_results") + "+"
            + _step_exists("final_proposals")
            + ") AS step_count"
        )
        base = (
            f"SELECT r.id, r.created_at, r.client_name, r.project_name, "
            f"r.video_type, r.budget, r.agency_type, r.user_id, r.stopped, r.hidden, "
            f"u.username, {_step_subq} "
            "FROM rfp_cases r LEFT JOIN users u ON r.user_id=u.id "
        )
        conditions = []
        params     = []

        # 관리자이면서 전체 보기가 아닐 때 or 일반 사용자 → 내 것만
        # user_id=0 인 레거시 케이스(user_id 마이그레이션 전 저장분)도 포함
        if not (session.get("is_admin") and view_all):
            conditions.append("(r.user_id=? OR r.user_id=0 OR r.user_id IS NULL)")
            params.append(session["user_id"])

        # 숨김 필터
        if not show_hidden:
            conditions.append("(r.hidden IS NULL OR r.hidden=0)")

        if q:
            conditions.append("(r.client_name LIKE ? OR r.project_name LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]

        if conditions:
            base += "WHERE " + " AND ".join(conditions) + " "
        base += "ORDER BY r.created_at DESC LIMIT 100"

        rows = conn.execute(base, params).fetchall()

    is_user_role = (session.get("role") == "user")
    if is_user_role:
        # user 역할: 본인 생성 케이스 없음 → 공유받은 케이스만 표시
        cases = []
        shared = get_shared_cases(session["user_id"])
    else:
        cases = [dict(r) for r in rows]
        # 공유된 제안서 (내 것이 아닌 것 중 나에게 공유된 것)
        my_ids = {c["id"] for c in cases}
        shared = [c for c in get_shared_cases(session["user_id"]) if c["id"] not in my_ids]

    return render_template(
        "history.html", cases=cases, shared_cases=shared, q=q,
        view_all=view_all,
        show_hidden=show_hidden,
        is_admin=session.get("is_admin", False),
        is_user_role=is_user_role,
    )


@app.route("/history/<int:case_id>")
@login_required
def history_detail(case_id):
    """제안서 상세 페이지."""
    detail = get_case_detail(case_id)
    if not detail:
        abort(404)
    case = detail["case"]
    uid = session["user_id"]
    is_owner = (case.get("user_id") == uid or bool(session.get("is_admin")))
    if not is_owner:
        # 공유된 케이스인지 확인
        shares = get_case_shares(case_id)
        if not any(s["shared_with"] == uid for s in shares):
            abort(403)
    return render_template("detail.html", detail=detail, case_id=case_id,
                           is_owner=is_owner)


@app.route("/history/<int:case_id>/download")
@login_required
def history_download(case_id):
    """이력에서 TXT 다운로드 — dna_json 기반 재생성."""
    import io
    detail = get_case_detail(case_id)
    if not detail:
        abort(404)
    case = detail["case"]
    uid = session["user_id"]
    if case.get("user_id") != uid and not session.get("is_admin"):
        shares = get_case_shares(case_id)
        if not any(s["shared_with"] == uid for s in shares):
            abort(403)

    lines = _build_history_txt(detail)
    txt = "\n".join(lines)
    buf = io.BytesIO(txt.encode("utf-8"))
    fname = f"{case['client_name']}_{case['project_name'][:20]}_제안서.txt"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="text/plain; charset=utf-8")


def _build_history_txt(detail: dict) -> list:
    """상세 데이터를 TXT 라인 목록으로 변환."""
    case  = detail["case"]
    dna   = case.get("dna", {})
    steps = detail["steps"]
    lines = []

    def sec(title):
        lines.extend(["", "=" * 60, f"  {title}", "=" * 60, ""])

    sec(f"제안서 — {case['client_name']} / {case['project_name']}")
    lines += [
        f"영상 종류: {case.get('video_type', '-')}",
        f"예산:      {case.get('budget', '-')}",
        f"납품 기한: {case.get('deadline', '-')}",
        f"생성 일시: {case.get('created_at', '-')[:16].replace('T', ' ')}",
    ]

    step_labels = {
        "rfp_analysis":  "STEP 1   RFP 분석",
        "research":      "STEP 2   리서치",
        "narrative":     "STEP 3   내러티브",
        "strategy":      "STEP 4   전략",
        "creative":      "STEP 5   컨셉",
        "plan":          "STEP 6   기획",
        "script":        "STEP 7   시나리오",
        "storyboard":    "STEP 8   스토리보드",
        "platform":      "STEP 9   플랫폼 운영전략",
        "marketing":     "STEP 10  마케팅/홍보 전략",
        "final_proposal":"STEP 11  PT/Q&A",
    }

    for key, label in step_labels.items():
        data = steps.get(key)
        if not data:
            continue
        sec(label)
        if key == "script" and isinstance(data, list):
            for s in data:
                lines.append(f"[{s.get('episode_title', '')}]")
                _flatten_to_lines(s.get("script", {}), lines)
        else:
            _flatten_to_lines(data, lines)

    return lines


def _flatten_to_lines(obj, lines: list, indent: int = 0):
    prefix = "  " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("id", "created_at", "client_name", "project_name", "raw_search"):
                continue
            if isinstance(v, (dict, list)):
                lines.append(f"{prefix}[{k}]")
                _flatten_to_lines(v, lines, indent + 1)
            elif v:
                lines.append(f"{prefix}{k}: {v}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _flatten_to_lines(item, lines, indent)
                lines.append("")
            elif item:
                lines.append(f"{prefix}- {item}")
    elif obj:
        lines.append(f"{prefix}{obj}")


@app.route("/history/<int:case_id>/reuse")
@operator_or_admin_required
def reuse(case_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        abort(404)
    case = dict(row)
    if case["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)
    return render_template(
        "index.html",
        video_types=VIDEO_TYPES,
        prefill=case,
        reference_case_id=case_id,
        reuse_notice=f"이전 제안서 구조를 참고해서 새 제안서를 생성합니다. 발주처와 사업명을 수정하고 실행하세요.",
        reuse_from=f"{case['client_name']} / {case['project_name']}",
    )


@app.route("/history/<int:case_id>/hide", methods=["POST"])
@operator_or_admin_required
def hide_case_route(case_id):
    with get_connection() as conn:
        row = conn.execute("SELECT user_id FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        abort(404)
    if row["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)
    hide_case(case_id)
    return jsonify({"ok": True})


@app.route("/history/<int:case_id>/unhide", methods=["POST"])
@operator_or_admin_required
def unhide_case_route(case_id):
    with get_connection() as conn:
        row = conn.execute("SELECT user_id FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        abort(404)
    if row["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)
    unhide_case(case_id)
    return jsonify({"ok": True})


@app.route("/history/<int:case_id>/share", methods=["POST"])
@operator_or_admin_required
def share_case_route(case_id):
    """케이스를 한 명 또는 여러 명에게 공유 (user_ids 리스트 또는 단일 user_id 지원)."""
    data = request.get_json(force=True) or {}
    user_ids = data.get("user_ids") or []
    if not user_ids and data.get("user_id"):
        user_ids = [data["user_id"]]
    if not user_ids:
        return jsonify({"ok": False, "error": "user_ids 필요"}), 400
    with get_connection() as conn:
        row = conn.execute("SELECT user_id FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        abort(404)
    if dict(row)["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)
    for uid in user_ids:
        try:
            share_proposal(case_id, session["user_id"], int(uid))
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/history/<int:case_id>/unshare", methods=["POST"])
@operator_or_admin_required
def unshare_case_route(case_id):
    """공유 취소."""
    data = request.get_json(force=True) or {}
    target_uid = int(data.get("user_id", 0))
    if not target_uid:
        return jsonify({"ok": False, "error": "user_id 필요"}), 400
    with get_connection() as conn:
        row = conn.execute("SELECT user_id FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        abort(404)
    if dict(row)["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)
    unshare_proposal(case_id, session["user_id"], target_uid)
    return jsonify({"ok": True})


@app.route("/api/shareable-users")
@login_required
def api_shareable_users():
    """공유 가능한 사용자 목록 (자기 자신 제외)."""
    users = list_users()
    result = [
        {"id": u["id"], "username": u["username"]}
        for u in users
        if u["id"] != session["user_id"]
    ]
    return jsonify(result)


# ─────────────────────────────────────────────
# 삭제 요청
# ─────────────────────────────────────────────

@app.route("/history/<int:case_id>/request_delete", methods=["POST"])
@operator_or_admin_required
def request_delete_case(case_id):
    """일반 사용자: 삭제 요청 전송 (운영자에게 알림)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT client_name, project_name, user_id FROM rfp_cases WHERE id=?",
            (case_id,)
        ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    row = dict(row)
    if row["user_id"] != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음"}), 403

    username    = session.get("username", "")
    client_name = row["client_name"]
    project_name = row["project_name"]

    create_delete_request(case_id, username, client_name, project_name)

    # 관리자 텔레그램 알림
    msg = (
        f"🗑️ <b>삭제 요청</b>\n"
        f"프로젝트: {project_name}\n"
        f"발주처: {client_name}\n"
        f"요청자: {username}\n"
        f"요청 시간: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"승인: /admin/delete_requests"
    )
    for chat_id in get_admin_telegram_ids():
        try:
            send_telegram(chat_id, msg)
        except Exception:
            pass

    return jsonify({"ok": True, "message": "삭제 요청이 접수됐습니다. 운영자 확인 후 처리됩니다."})


@app.route("/history/<int:case_id>/delete", methods=["POST"])
@admin_required
def admin_delete_case(case_id):
    """관리자 직접 삭제."""
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    delete_case(case_id)
    return jsonify({"ok": True})


@app.route("/admin/delete_requests")
@admin_required
def admin_delete_requests():
    """삭제 요청 목록 페이지 (관리자)."""
    status = request.args.get("status", "pending")
    reqs   = list_delete_requests(status=status if status != "all" else "")
    return render_template("admin_delete_requests.html", requests=reqs, status=status)


@app.route("/admin/delete_requests/<int:req_id>/approve", methods=["POST"])
@admin_required
def admin_approve_delete(req_id):
    """삭제 요청 승인."""
    ok = approve_delete_request(req_id, session.get("username", ""))
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "요청 없음"}), 404


@app.route("/admin/delete_requests/<int:req_id>/reject", methods=["POST"])
@admin_required
def admin_reject_delete(req_id):
    """삭제 요청 거절."""
    data   = request.get_json(force=True) or {}
    reason = data.get("reason", "")
    reject_delete_request(req_id, session.get("username", ""), reason)
    return jsonify({"ok": True})


@app.route("/history/<int:case_id>/resume")
@login_required
def resume_case(case_id):
    """중단된 케이스를 마지막 완료 스텝 다음부터 이어서 실행."""
    import dataclasses as _dc2
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        abort(404)
    case = dict(row)
    if case["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)

    # DNA 복원
    from core.dna import ConceptDNA
    dna_dict = json.loads(case.get("dna_json") or "{}")
    dna = ConceptDNA()
    for f in _dc2.fields(dna):
        if f.name in dna_dict:
            try:
                setattr(dna, f.name, dna_dict[f.name])
            except Exception:
                pass
    dna.case_id = case_id

    # 각 스텝 완료 여부 확인
    client  = case["client_name"]
    project = case["project_name"]
    STEP_TABLES = [
        ("rfp_analysis",   "rfp_analyses"),
        ("research",       "research_results"),
        ("strategy",       "strategy_results"),
        ("creative",       "creative_results"),
        ("plan",           "plan_results"),
        ("script",         "script_results"),
        ("storyboard",     "storyboard_results"),
        ("platform",       "platform_results"),
        ("marketing",      "marketing_results"),
        ("final_proposal", "final_proposals"),
    ]
    PIPELINE_ORDER = [
        "rfp_analysis", "research", "narrative", "strategy",
        "creative", "plan", "script", "storyboard", "platform", "marketing", "final_proposal",
    ]
    completed = set()
    with get_connection() as conn:
        for step_key, table in STEP_TABLES:
            exists = conn.execute(
                f"SELECT 1 FROM {table} WHERE case_id=? LIMIT 1", (case_id,)
            ).fetchone()
            if not exists:
                exists = conn.execute(
                    f"SELECT 1 FROM {table} WHERE client_name=? AND project_name=? LIMIT 1",
                    (client, project)
                ).fetchone()
            if exists:
                completed.add(step_key)
    # narrative는 DB 테이블 없음 — 항상 재실행 (completed에 추가 안 함)

    start_step_key = None
    for step_key in PIPELINE_ORDER:
        if step_key not in completed:
            start_step_key = step_key
            break

    if not start_step_key:
        # 이미 완료 → 상세 페이지로
        return redirect(url_for("history_detail", case_id=case_id))

    sid = str(uuid.uuid4())
    with _sessions_lock:
        _sessions[sid] = {
            "status":           "queued",
            "user_id":          session["user_id"],
            "username":         session["username"],
            "dna":              dna,
            "rfp_file":         None,
            "concept":          dna.concept or None,
            "results":          {},
            "events":           [],
            "sse_event":        threading.Event(),
            "confirm_event":    threading.Event(),
            "user_input":       None,
            "step_instruction": None,
            "txt_path":         None,
            "client":           client,
            "project":          project,
            "created_at":       datetime.now().isoformat(),
            "retry_from":       start_step_key,
            "case_id":          case_id,
        }

    with _queue_lock:
        _job_queue.append(sid)
    _ensure_worker()
    _dispatch_jobs()        # 슬롯 여유 있으면 즉시 실행
    _broadcast_positions()

    session["current_run_sid"] = sid
    return redirect(url_for("run_page", sid=sid))


# ─────────────────────────────────────────────
# 관리자 — 사용자 관리
# ─────────────────────────────────────────────

@app.route("/queue/clear", methods=["POST", "GET"])
@admin_required
def queue_clear():
    """큐 및 stale 세션 강제 초기화. Flask 재시작 없이 즉시 적용."""
    cleared_queue = 0
    cleared_sessions = 0

    with _queue_lock:
        cleared_queue = len(_job_queue)
        _job_queue.clear()

    # active 슬롯도 초기화
    with _active_lock:
        _active_sids.clear()

    with _sessions_lock:
        stale = [
            sid for sid, s in _sessions.items()
            if s.get("status") in ("queued", "running")
        ]
        for sid in stale:
            sess = _sessions[sid]
            sess["status"] = "error"
            sess["events"].append({
                "type":    "pipeline_error",
                "message": "관리자에 의해 큐가 초기화되었습니다.",
            })
            sess["sse_event"].set()
            cleared_sessions += 1

    print(f"[queue/clear] 큐 {cleared_queue}개, 세션 {cleared_sessions}개 초기화")
    return jsonify({
        "ok": True,
        "cleared_queue": cleared_queue,
        "cleared_sessions": cleared_sessions,
        "message": f"큐 {cleared_queue}개, 실행 중 세션 {cleared_sessions}개 초기화 완료",
    })


@app.route("/queue/status")
@admin_required
def admin_queue_status():
    """관리자 전용: 현재 큐·실행 상태 조회."""
    with _queue_lock:
        queue_list = list(_job_queue)
    with _active_lock:
        active = set(_active_sids)

    items = []
    for sid in queue_list:
        with _sessions_lock:
            sess = _sessions.get(sid)
        if sess:
            started_at = sess.get("started_at")
            items.append({
                "sid":        sid,
                "status":     sess.get("status"),
                "project":    sess.get("project", ""),
                "client":     sess.get("client", ""),
                "is_active":  sid in active,
                "elapsed_sec": round(time.time() - started_at, 0) if started_at else None,
            })

    return jsonify({
        "ok":     True,
        "queued": len(queue_list),
        "active": len(active),
        "max":    _MAX_CONCURRENT,
        "items":  items,
    })


def get_credit_status() -> dict:
    """SerpAPI / Tavily 크레딧 현황 조회."""
    result: dict = {}

    serpapi_key = os.environ.get("SERPAPI_KEY", os.environ.get("SERPER_API_KEY", ""))
    if serpapi_key:
        try:
            url = f"https://serpapi.com/account.json?api_key={serpapi_key}"
            req = _urllib_req.Request(url, headers={"User-Agent": "Prointerz/1.0"})
            with _urllib_req.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            used  = data.get("this_month_usage", 0)
            limit = data.get("searches_per_month", 0)
            result["serpapi"] = {
                "used":      used,
                "limit":     limit,
                "remaining": max(0, limit - used),
                "percent":   round(used / limit * 100, 1) if limit else 0,
            }
        except Exception as e:
            result["serpapi"] = {"error": str(e)[:120]}
    else:
        result["serpapi"] = {"no_key": True}

    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    result["tavily"] = {"key_set": bool(tavily_key), "note": "잔량 조회 미지원"}
    result["anthropic"] = {"note": "잔량 조회 미지원"}
    return result


def _schedule_credit_report():
    """매일 오전 9시 관리자에게 크레딧 현황 텔레그램 리포트."""
    def _send_report():
        try:
            status = get_credit_status()
            lines = ["📊 [매일 오전 9시] API 크레딧 현황"]
            s = status.get("serpapi", {})
            if "no_key" in s:
                lines.append("• SerpAPI: 키 미설정")
            elif "error" in s:
                lines.append(f"• SerpAPI: 조회 실패 ({s['error'][:60]})")
            else:
                lines.append(
                    f"• SerpAPI: {s.get('used',0):,}/{s.get('limit',0):,}회 사용 "
                    f"(잔량 {s.get('remaining',0):,}회, {s.get('percent',0)}%)"
                )
            t = status.get("tavily", {})
            lines.append(f"• Tavily: 키 {'설정됨' if t.get('key_set') else '미설정'} (잔량 조회 미지원)")
            msg = "\n".join(lines)
            with get_connection() as conn:
                admins = conn.execute(
                    "SELECT telegram_chat_id FROM users WHERE is_admin=1"
                ).fetchall()
            for row in admins:
                chat_id = dict(row).get("telegram_chat_id", "")
                if chat_id:
                    send_telegram(chat_id, msg)
        except Exception as e:
            print(f"[credit_report] 오류: {e}")
        finally:
            _schedule_credit_report()   # 다음 날 재예약

    now    = _dt.datetime.now()
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if target <= now:
        target += _dt.timedelta(days=1)
    delay = (target - now).total_seconds()
    t = threading.Timer(delay, _send_report)
    t.daemon = True
    t.start()


@app.route("/api/credit-status")
@admin_required
def api_credit_status():
    return jsonify(get_credit_status())


@app.route("/admin")
@admin_required
def admin():
    users      = list_users()
    user_filter = int(request.args.get("user_id", 0))
    all_cases  = list_all_cases(user_filter)
    credit     = get_credit_status()
    return render_template("admin.html", users=users,
                           all_cases=all_cases, user_filter=user_filter,
                           credit=credit)


@app.route("/admin/add-user", methods=["POST"])
@admin_required
def admin_add_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    role     = request.form.get("role", "user")
    if role not in ("admin", "operator", "user"):
        role = "user"
    is_admin = (role == "admin")
    error    = None

    if not username or not password:
        error = "아이디와 비밀번호를 입력하세요."
    elif len(password) < 4:
        error = "비밀번호는 4자 이상이어야 합니다."
    else:
        try:
            create_user(username, password, is_admin, role=role)
        except Exception as e:
            error = f"계정 생성 실패: {e}"

    if error:
        users = list_users()
        all_cases = list_all_cases()
        credit = get_credit_status()
        return render_template("admin.html", users=users, all_cases=all_cases,
                               credit=credit, user_filter=0, error=error)
    return redirect(url_for("admin"))


@app.route("/admin/delete-user/<int:uid>", methods=["POST"])
@admin_required
def admin_delete_user(uid):
    if uid == session["user_id"]:
        return redirect(url_for("admin"))  # 자기 자신 삭제 방지
    delete_user(uid)
    return redirect(url_for("admin"))


@app.route("/admin/change-password/<int:uid>", methods=["POST"])
@admin_required
def admin_change_password(uid):
    new_pw = request.form.get("new_password", "").strip()
    if new_pw and len(new_pw) >= 4:
        change_password(uid, new_pw)
    return redirect(url_for("admin"))


@app.route("/admin/change-role/<int:uid>", methods=["POST"])
@admin_required
def admin_change_role(uid):
    """사용자 역할 변경 (admin/operator/user)."""
    role = request.form.get("role", "user")
    if role not in ("admin", "operator", "user"):
        role = "user"
    if uid != session["user_id"]:  # 자기 자신 역할 변경 방지
        update_user_role(uid, role)
    return redirect(url_for("admin"))


@app.route("/admin/purge_user", methods=["POST"])
@login_required
def purge_user():
    """특정 사용자가 생성한 모든 데이터 삭제 (admin만)"""
    if not session.get('is_admin'):
        return jsonify({"ok": False, "error": "권한 없음"})

    data = request.get_json() or {}
    username = data.get('username', '').strip()
    if not username:
        return jsonify({"ok": False, "error": "username 필요"})

    from database.db import get_connection
    with get_connection() as conn:
        conn.execute("DELETE FROM nara_candidates WHERE registered_by=?", (username,))
        conn.execute("DELETE FROM nara_pickups WHERE registered_by=?", (username,))
        conn.execute("DELETE FROM confirmed_comments WHERE author=?", (username,))
        conn.execute("DELETE FROM confirmed_narratives WHERE updated_by=?", (username,))
        conn.execute("""
            DELETE FROM notifications WHERE user_id = (
                SELECT id FROM users WHERE username=?
            )
        """, (username,))
        conn.execute("DELETE FROM confirmed_rfp_files WHERE uploaded_by=?", (username,))

    return jsonify({"ok": True, "message": f"{username} 데이터 삭제 완료"})


@app.route("/my/change-password", methods=["POST"])
@login_required
def my_change_password():
    """본인 비밀번호 변경."""
    current = request.form.get("current_password", "")
    new_pw  = request.form.get("new_password", "").strip()
    user    = verify_user(session["username"], current)
    if user and new_pw and len(new_pw) >= 4:
        change_password(session["user_id"], new_pw)
    return redirect(url_for("index"))


# ─────────────────────────────────────────────
# 프로필 (텔레그램 Chat ID 설정)
# ─────────────────────────────────────────────

@app.route("/schedule")
@login_required
def schedule_page():
    from database.db import get_connection
    with get_connection() as conn:
        confirmed = conn.execute("""
            SELECT cf.id,
                   COALESCE(pk.bid_ntce_nm, ca.bid_ntce_nm) as bid_ntce_nm,
                   COALESCE(pk.ntce_instt_nm, ca.ntce_instt_nm) as ntce_instt_nm,
                   COALESCE(pk.bid_clse_dt, ca.bid_clse_dt) as bid_clse_dt,
                   cf.assignee, cf.confirmed_by, cf.created_at,
                   bi.submit_deadline, bi.submit_method
            FROM nara_confirmed cf
            LEFT JOIN nara_pickups pk    ON pk.id = cf.pickup_id    AND cf.pickup_id > 0
            LEFT JOIN nara_candidates ca ON ca.id = cf.candidate_id AND cf.pickup_id = 0
            LEFT JOIN confirmed_bid_info bi ON bi.confirmed_id = cf.id
            ORDER BY COALESCE(pk.bid_clse_dt, ca.bid_clse_dt) ASC
        """).fetchall()
        schedules = conn.execute("""
            SELECT s.*, COALESCE(pk.bid_ntce_nm, ca.bid_ntce_nm) as bid_ntce_nm
            FROM confirmed_schedule s
            JOIN nara_confirmed cf ON cf.id = s.confirmed_id
            LEFT JOIN nara_pickups pk    ON pk.id = cf.pickup_id    AND cf.pickup_id > 0
            LEFT JOIN nara_candidates ca ON ca.id = cf.candidate_id AND cf.pickup_id = 0
            ORDER BY s.due_date ASC
        """).fetchall()
    return render_template("schedule.html",
                           confirmed=[dict(r) for r in confirmed],
                           schedules=[dict(r) for r in schedules])


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user_id = session["user_id"]
    message = None
    if request.method == "POST":
        chat_id = request.form.get("telegram_chat_id", "").strip()
        set_telegram_chat_id(user_id, chat_id)
        message = "저장되었습니다."
        # 저장 후 테스트 메시지 발송
        if chat_id:
            print(f"  [Profile] 테스트 메시지 전송 시도 → chat_id={chat_id}", flush=True)
            ok = send_telegram(chat_id, f"✅ Prointerz 텔레그램 알림이 연결되었습니다.\n계정: {session['username']}")
            print(f"  [Profile] send_telegram 결과: {ok}", flush=True)
            message = "저장 완료. 테스트 메시지를 전송했습니다." if ok else "저장 완료. 테스트 메시지 전송 실패 (토큰/Chat ID 확인)"

    current_chat_id = get_telegram_chat_id(user_id) or ""
    return render_template("profile.html",
                           chat_id=current_chat_id,
                           message=message)


# ─────────────────────────────────────────────
# PPT 생성
# ─────────────────────────────────────────────

def _ppt_push(job_id: str, event: dict):
    with _ppt_jobs_lock:
        job = _ppt_jobs.get(job_id)
        if job:
            job["events"].append(event)
            job["sse_event"].set()


def _ppt_db_done(job_id: str, gamma_url: str = ""):
    """PPT 완료 시 DB 업데이트 (비동기 안전)."""
    try:
        update_ppt_job(job_id, "done", gamma_url=gamma_url)
    except Exception as e:
        print(f"[PPT/DB] done 업데이트 실패: {e}")


def _ppt_db_error(job_id: str, error_msg: str = ""):
    """PPT 오류 시 DB 업데이트 (비동기 안전)."""
    try:
        update_ppt_job(job_id, "error", error_msg=error_msg)
    except Exception as e:
        print(f"[PPT/DB] error 업데이트 실패: {e}")


# 템플릿 업로드: /tmp/tmpl_uploads/ 에 파일 저장 (멀티워커 간 공유)
_TMPL_UPLOAD_DIR = Path("/tmp/tmpl_uploads")
_TMPL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_TMPL_MAX_AGE = 1800   # 30분 TTL


def _tmpl_upload_path(upload_id: str) -> Path:
    return _TMPL_UPLOAD_DIR / upload_id


def _cleanup_template_uploads():
    """30분 이상 지난 업로드 파일 정리."""
    now = time.time()
    for meta_path in _TMPL_UPLOAD_DIR.glob("*.meta"):
        try:
            import json as _json
            meta = _json.loads(meta_path.read_text())
            if now - meta.get("ts", 0) > _TMPL_MAX_AGE:
                data_path = _TMPL_UPLOAD_DIR / meta_path.stem
                data_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.route("/ppt/upload_template", methods=["POST"])
@operator_or_admin_required
def ppt_upload_template():
    """옵션 3: 참고 파일 업로드 → upload_id 반환.
    프론트엔드 FormData 필드명: 'file' 또는 'template_file' 모두 허용.
    """
    import json as _json
    _ALLOWED_TMPL_EXT = {".pptx", ".pdf", ".hwp", ".hwpx"}

    # 필드명 'file' / 'template_file' 둘 다 허용
    f = request.files.get("file") or request.files.get("template_file")
    print(f"[Template Upload] files={list(request.files.keys())} f={f and f.filename!r}")

    if not f or not f.filename:
        return jsonify({"ok": False, "error": "파일이 전송되지 않았습니다. 다시 시도하세요."}), 400

    ext = Path(f.filename).suffix.lower()
    if ext not in _ALLOWED_TMPL_EXT:
        return jsonify({"ok": False,
                        "error": f"지원하지 않는 파일 형식입니다 (PPTX, PDF, HWP, HWPX만 가능)"}), 400

    data = f.read()
    size_mb = len(data) / (1024 * 1024)
    print(f"[Template Upload] filename={f.filename!r} ext={ext} size={size_mb:.1f}MB")

    if len(data) > 100 * 1024 * 1024:   # 100 MB 제한
        return jsonify({"ok": False, "error": "파일 크기가 너무 큽니다 (100MB 이하)"}), 400

    upload_id = str(uuid.uuid4())
    _cleanup_template_uploads()

    # 파일 본체와 메타 정보를 /tmp에 저장
    data_path = _tmpl_upload_path(upload_id)
    meta_path = _TMPL_UPLOAD_DIR / f"{upload_id}.meta"
    try:
        data_path.write_bytes(data)
        meta_path.write_text(_json.dumps({
            "ts":       time.time(),
            "user_id":  session["user_id"],
            "file_ext": ext,
            "filename": f.filename,
        }))
    except Exception as e:
        print(f"[Template Upload] 파일 저장 실패: {e}")
        return jsonify({"ok": False, "error": f"서버 파일 저장 오류: {e}"}), 500

    print(f"[Template Upload] 저장 완료 → {data_path} ({size_mb:.1f}MB)")
    return jsonify({"ok": True, "upload_id": upload_id})


@app.route("/ppt/has_gamma")
@login_required
def ppt_has_gamma():
    """GAMMA_API_KEY 설정 여부 반환 — UI 버튼 텍스트 결정에 사용."""
    from output.pptx_builder import has_gamma_key
    return jsonify({"has_gamma": has_gamma_key()})


@app.route("/ppt/start", methods=["POST"])
@operator_or_admin_required
def ppt_start():
    """PPT 생성 시작.
    mode: 'basic' (python-pptx) | 'gamma' | 'template' (참고 PPTX 스타일 적용)
    """
    data    = request.get_json(force=True) or {}
    case_id = int(data.get("case_id", 0))
    pages   = max(10, min(200, int(data.get("pages", 20))))

    if not case_id:
        return jsonify({"ok": False, "error": "case_id 필요"}), 400

    detail = get_case_detail(case_id)
    if not detail:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    if detail["case"].get("user_id") != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음 — 다시 로그인 후 시도하세요"}), 403

    # mode 결정: 명시적 mode 우선, 레거시 force_pptx/Gamma 폴백
    mode = data.get("mode", "")
    if not mode:
        force_pptx = bool(data.get("force_pptx", False))
        gamma_key  = GAMMA_API_KEY.strip()
        mode = "basic" if (force_pptx or not gamma_key) else "gamma"

    gamma_key = GAMMA_API_KEY.strip()
    use_gamma = (mode == "gamma") and bool(gamma_key)
    print(f"[PPT] mode={mode} use_gamma={use_gamma}")

    # 동일 case_id 중복 실행 방어
    with _ppt_jobs_lock:
        _running_ppt = next(
            (jid for jid, j in _ppt_jobs.items()
             if j.get("status") == "running" and j.get("case_id") == case_id),
            None,
        )
    if _running_ppt:
        return jsonify({"ok": False, "error": "이미 PPT 생성 중입니다"}), 409

    job_id  = str(uuid.uuid4())
    case    = detail["case"]
    user_id = session["user_id"]
    fname   = f"{case['client_name']}_{case['project_name'][:20]}_제안서.pptx"
    # Gamma는 PDF 형식으로 제공 — 실제 파일명은 _worker_gamma에서 .pdf로 교체

    try:
        save_ppt_job(job_id, case_id, user_id, ppt_type=mode)
    except Exception as _e:
        print(f"[PPT/DB] 작업 저장 실패 (계속 진행): {_e}")

    with _ppt_jobs_lock:
        _ppt_jobs[job_id] = {
            "status":     "running",
            "case_id":    case_id,
            "events":     [],
            "pptx_bytes": None,
            "filename":   fname,
            "sse_event":  threading.Event(),
            "user_id":    user_id,
        }

    def _save_version(pptx_bytes: bytes, filename: str, is_pdf: bool = False):
        """PPT 생성 완료 후 버전 DB 저장 + PT 원고 동시 생성."""
        import json as _json
        try:
            # 1. PT 원고: STEP 7 결과 우선, 없으면 orchestrator로 생성
            pt_script_dict = {}
            fp = detail.get("steps", {}).get("final_proposal", {})
            if fp.get("pt_script"):
                pt_script_dict = fp["pt_script"]
            if not pt_script_dict:
                try:
                    from core.dna import ConceptDNA, update_dna
                    from agents.orchestrator import generate_pt_script_for_ppt
                    case_dna = detail["case"].get("dna", {})
                    dna_obj  = ConceptDNA(
                        client_name=case_dna.get("client_name", case["client_name"]),
                        project_name=case_dna.get("project_name", case["project_name"]),
                    )
                    update_dna(dna_obj, case_dna)
                    pt_script_dict = generate_pt_script_for_ppt(dna_obj)
                    print(f"  [PPT버전] PT 원고 새로 생성 완료")
                except Exception as e_pt:
                    print(f"  [PPT버전] PT 원고 생성 실패: {e_pt}")

            # 2. 버전 DB 저장
            pt_script_str = _json.dumps(pt_script_dict, ensure_ascii=False)
            version_id, version_num = save_ppt_version(
                case_id     = case_id,
                ppt_data    = pptx_bytes,
                ppt_filename= filename,
                pt_script   = pt_script_str,
                created_by  = session.get("username", ""),
                is_pdf      = is_pdf,
            )
            print(f"  [PPT버전] v{version_num} 저장 완료 (DB id={version_id})")
        except Exception as e:
            print(f"  [PPT버전] 버전 저장 실패: {e}")

    def _worker():
        try:
            if use_gamma:
                _worker_gamma()
            elif mode == "template":
                _worker_template()
            else:
                _worker_pptx()
        except Exception as e:
            import traceback as _tb
            _tb.print_exc()
            _ppt_push(job_id, {"type": "ppt_error", "message": str(e)})
            with _ppt_jobs_lock:
                job = _ppt_jobs.get(job_id)
                if job:
                    job["status"] = "error"
            _ppt_db_error(job_id, str(e))

    def _worker_gamma():
        """Gamma API로 PPT 생성 후 PPTX 파일 다운로드."""
        import requests as _req
        from output.pptx_builder import generate_with_gamma

        _ppt_push(job_id, {"type": "ppt_progress",
                            "message": "Gamma API 요청 전송 중...", "current": 1, "total": 4})

        # 제안서 내용 → Gamma inputText 문자열로 조합
        topic = _build_gamma_topic(detail)

        _ppt_push(job_id, {"type": "ppt_progress",
                            "message": "Gamma AI 생성 중 (30~120초 소요)...", "current": 2, "total": 4})

        result = generate_with_gamma(topic, pages)

        pptx_export_url = result.get("pptx_url")

        if pptx_export_url:
            # ── PDF 파일 다운로드 → 서버에서 직접 제공 ──
            _ppt_push(job_id, {"type": "ppt_progress",
                                "message": "PDF 파일 다운로드 중...", "current": 3, "total": 4})

            dl_resp = _req.get(pptx_export_url, timeout=120)
            dl_resp.raise_for_status()
            pdf_bytes = dl_resp.content

            # 파일명 확장자를 .pdf로 교체
            pdf_fname = fname.replace(".pptx", ".pdf")

            with _ppt_jobs_lock:
                job = _ppt_jobs.get(job_id)
                if job:
                    job["status"]     = "done"
                    job["pptx_bytes"] = pdf_bytes
                    job["filename"]   = pdf_fname
                    job["is_pdf"]     = True

            _save_version(pdf_bytes, pdf_fname, is_pdf=True)
            _ppt_db_done(job_id, gamma_url=result.get("url", ""))

            _ppt_push(job_id, {
                "type":         "ppt_done",
                "download_url": f"/ppt/download/{job_id}",
                "gamma_url":    result.get("url", ""),
            })

        else:
            # ── PPTX URL 없음 → Gamma 웹 URL 폴백 ──
            with _ppt_jobs_lock:
                job = _ppt_jobs.get(job_id)
                if job:
                    job["status"] = "done"

            _ppt_db_done(job_id, gamma_url=result.get("url", ""))

            _ppt_push(job_id, {
                "type": "gamma_done",
                "url":  result.get("url", ""),
            })

        chat_id = get_telegram_chat_id(user_id)
        if chat_id:
            try:
                send_telegram(chat_id,
                    f"📊 <b>{case['project_name']}</b> Gamma PPT 생성 완료!")
            except Exception:
                pass

    def _worker_pptx():
        """python-pptx로 PPT 생성."""
        def progress_cb(message, current, total):
            _ppt_push(job_id, {"type": "ppt_progress",
                                "message": message, "current": current, "total": total})

        try:
            from agents import ppt_generator
        except ImportError as imp_err:
            import traceback as _tb
            _tb.print_exc()
            raise RuntimeError(f"ppt_generator 임포트 실패: {imp_err}")

        # narrative 모드: 저장된 설계안 → 직접 PPTX 빌드 (Claude 재호출 없음)
        if mode == "narrative":
            _ppt_push(job_id, {"type": "ppt_progress",
                                "message": "PPT 설계안 불러오는 중...", "current": 1, "total": 2})
            narrative = get_ppt_narrative(case_id)
            if narrative and narrative.get("slides"):
                try:
                    pptx_bytes = ppt_generator.build_pptx_from_narrative(
                        narrative["slides"], detail["case"], progress_cb)
                except Exception as run_err:
                    import traceback as _tb
                    _tb.print_exc()
                    _ppt_push(job_id, {"type": "ppt_progress",
                                        "message": f"오류 발생: {run_err}", "current": 0, "total": 2})
                    raise
            else:
                raise RuntimeError("저장된 PPT 설계안이 없습니다. 먼저 설계안을 생성해 주세요.")
        else:
            _ppt_push(job_id, {"type": "ppt_progress",
                                "message": "PPT 슬라이드 구성 중 (Claude AI)...",
                                "current": 1, "total": 3})
            try:
                pptx_bytes = ppt_generator.run(detail, pages, progress_cb)
            except Exception as run_err:
                import traceback as _tb
                _tb.print_exc()
                print(f"[PPT/basic] 생성 오류: {run_err}")
                _ppt_push(job_id, {"type": "ppt_progress",
                                    "message": f"오류 발생: {run_err}", "current": 0, "total": 3})
                raise

        with _ppt_jobs_lock:
            job = _ppt_jobs.get(job_id)
            if job:
                job["status"]     = "done"
                job["pptx_bytes"] = pptx_bytes

        _save_version(pptx_bytes, fname, is_pdf=False)
        _ppt_db_done(job_id)

        _ppt_push(job_id, {
            "type":         "ppt_done",
            "download_url": f"/ppt/download/{job_id}",
        })

        chat_id = get_telegram_chat_id(user_id)
        if chat_id:
            try:
                send_telegram(chat_id,
                    f"📊 <b>{case['project_name']}</b> PPT 생성 완료!\n"
                    f"다운로드: /history/{case_id}")
            except Exception:
                pass

    def _worker_template():
        """참고 파일 스타일 적용 PPT 생성."""
        import json as _json
        upload_id = data.get("upload_id", "")
        print(f"[Template Worker] upload_id={upload_id!r}")

        data_path = _tmpl_upload_path(upload_id)
        meta_path = _TMPL_UPLOAD_DIR / f"{upload_id}.meta"

        print(f"[Template Worker] data_path={data_path} exists={data_path.exists()}")
        print(f"[Template Worker] meta_path={meta_path} exists={meta_path.exists()}")

        if not data_path.exists() or not meta_path.exists():
            raise RuntimeError(
                f"템플릿 파일을 찾을 수 없습니다 (upload_id={upload_id}). "
                "파일을 다시 업로드하세요."
            )

        try:
            meta = _json.loads(meta_path.read_text())
        except Exception as e:
            raise RuntimeError(f"메타 파일 읽기 실패: {e}")

        if meta.get("user_id") != user_id and not session.get("is_admin"):
            raise RuntimeError("파일 접근 권한 없음")

        template_bytes = data_path.read_bytes()
        file_ext       = meta.get("file_ext", ".pptx")
        print(f"[Template Worker] 파일 로드 완료 ext={file_ext} size={len(template_bytes)//1024}KB")

        def progress_cb(msg, cur, tot):
            _ppt_push(job_id, {"type": "ppt_progress",
                                "message": msg, "current": cur, "total": tot})

        from output.pptx_builder import generate_from_template
        pptx_bytes = generate_from_template(detail, template_bytes, pages, progress_cb,
                                            file_ext=file_ext)

        with _ppt_jobs_lock:
            job = _ppt_jobs.get(job_id)
            if job:
                job["status"]     = "done"
                job["pptx_bytes"] = pptx_bytes

        _save_version(pptx_bytes, fname, is_pdf=False)
        _ppt_db_done(job_id)

        _ppt_push(job_id, {
            "type":         "ppt_done",
            "download_url": f"/ppt/download/{job_id}",
        })

        # 사용한 업로드 파일 정리
        try:
            _tmpl_upload_path(upload_id).unlink(missing_ok=True)
            (_TMPL_UPLOAD_DIR / f"{upload_id}.meta").unlink(missing_ok=True)
        except Exception:
            pass

        chat_id = get_telegram_chat_id(user_id)
        if chat_id:
            try:
                send_telegram(chat_id,
                    f"📊 <b>{case['project_name']}</b> 템플릿 PPT 생성 완료!")
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=False).start()
    return jsonify({"ok": True, "job_id": job_id, "use_gamma": use_gamma})


def _build_gamma_topic(detail: dict) -> str:
    """케이스 상세 정보를 Gamma topic 문자열로 조합."""
    case  = detail.get("case", {})
    steps = detail.get("steps", {})
    # steps가 없는 구버전 호환
    if not steps:
        steps = detail

    # RFP 기반 목차 추출 → Gamma 슬라이드 구성 순서 지시
    from output.pptx_builder import _build_rfp_toc
    rfp_toc_items = _build_rfp_toc(steps.get("rfp_analysis", {}))
    toc_lines     = "\n".join(f"{i+1}. {s}" for i, s in enumerate(rfp_toc_items))
    toc_instruction = (
        f"## 슬라이드 구성 순서 (반드시 이 목차 순서를 따르라)\n\n"
        f"아래 목차 순서를 반드시 따라서 슬라이드를 구성하라. "
        f"각 항목당 1~2장을 할당하되 목차 순서를 벗어나지 마라:\n\n"
        f"{toc_lines}\n"
    )

    parts = []
    project = case.get("project_name", "")
    client  = case.get("client_name", "")
    if project:
        parts.append(f"# {project}")
    if client:
        parts.append(f"발주처: {client}")

    def _s(key): return steps.get(key) or {}

    # 전략
    s = _s("strategy")
    for label, field in [("핵심 문제", "core_problem"), ("현황 진단", "crisis_statement"),
                         ("해결 방향", "solution_direction"), ("전략 요약", "strategy_summary")]:
        if s.get(field):
            parts.append(f"\n## {label}\n{s[field]}")
    ef = s.get("expected_effects")
    if isinstance(ef, list) and ef:
        parts.append("## 기대 효과\n" + "\n".join(f"- {x}" for x in ef[:5]))

    # 컨셉
    c = _s("creative")
    for label, field in [("핵심 컨셉", "concept"), ("슬로건", "confirmed_slogan"),
                         ("톤앤매너", "tone_description"), ("비주얼 방향", "visual_direction")]:
        if c.get(field):
            parts.append(f"\n## {label}\n{c[field]}")
    kws = c.get("tone_keywords") or []
    if kws:
        parts.append("## 감성 키워드\n" + ", ".join(str(k) for k in kws[:8]))

    # 편별 기획
    p = _s("plan")
    eps = p.get("episodes") or []
    if isinstance(eps, list) and eps:
        ep_lines = []
        for ep in eps[:5]:
            if isinstance(ep, dict):
                num = ep.get("episode_number", ep.get("ep_num", ""))
                title = ep.get("title", "")
                msg = ep.get("core_message", ep.get("key_message", ""))
                ep_lines.append(f"- {num}편 [{title}]: {msg}")
        if ep_lines:
            parts.append("## 편별 기획\n" + "\n".join(ep_lines))
    if p.get("production_schedule") and isinstance(p["production_schedule"], list):
        parts.append("## 제작 일정\n" + "\n".join(str(x) for x in p["production_schedule"][:6]))

    # 시나리오
    scripts_list = steps.get("script") or []
    if isinstance(scripts_list, list) and scripts_list:
        sc_lines = []
        for sc_row in scripts_list[:2]:
            ep_num = sc_row.get("episode_number", "")
            script_data = sc_row.get("script") or {}
            scenes = script_data.get("scenes") or []
            if scenes:
                sc_lines.append(f"### {ep_num}편 씬 구성")
                for sc in scenes[:4]:
                    if isinstance(sc, dict):
                        narr = sc.get("narration", sc.get("dialogue", ""))
                        sc_lines.append(f"- {str(narr)[:120]}")
        if sc_lines:
            parts.append("## 시나리오 개요\n" + "\n".join(sc_lines))

    # 마케팅
    m = _s("marketing")
    for label, field in [("유통·마케팅 전략", "youtube_strategy"),
                         ("SNS 전략", "sns_strategy"), ("KPI 목표", "kpi")]:
        v = m.get(field)
        if v:
            parts.append(f"## {label}\n{str(v)[:250]}")

    content = "\n\n".join(parts) if parts else f"{project} 제안서 PT 자료"

    # ── 이미지 가이드라인 + 디자인 방향 ──────────────────────────
    guidelines = """

---

【이미지 사용 가이드라인】

✅ 사용 권장:
- 보편적 인물 (일반 직장인, 학생, 가족 등 특정 신원 없는 사람)
- 자연 (풍경, 하늘, 바다 등)
- 사물 (제품, 건물, 도시 풍경 등)
- 추상적 비주얼 (컬러 블록, 패턴, 텍스처)
- 다이어그램, 차트, 인포그래픽, 아이콘

⚠️ 신뢰도 위험 — 이미지 자리만 표시:
- 특정 국가의 군복, 제복, 군사 장비
- 특정 국가 국기, 정부 상징
- 실존 인물, 유명인, 공인
- 특정 기관/브랜드 로고
- 역사적 사건 장면

위험 항목이 필요한 슬라이드는:
이미지 대신 [이미지: (실제 사진으로 교체 권장)] 텍스트로 표시하고
컬러 배경 + 텍스트 강조로 대체할 것.

【디자인 방향】
가능한 한 아래 요소를 활용해서 시각적으로 풍부하게 구성:
- 컬러 배경 블록
- 데이터 시각화 (차트, 그래프)
- 다이어그램 (화살표, 프로세스 흐름)
- 아이콘 기반 인포그래픽
- 강조 텍스트 카드"""

    return toc_instruction + "\n\n" + content + guidelines


@app.route("/ppt/stream/<job_id>")
@login_required
def ppt_stream(job_id):
    with _ppt_jobs_lock:
        job = _ppt_jobs.get(job_id)
    if not job:
        abort(404)
    if job["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)

    def generate():
        idx = 0
        while True:
            with _ppt_jobs_lock:
                j = _ppt_jobs.get(job_id)
            if not j:
                break
            while idx < len(j["events"]):
                yield f"data: {json.dumps(j['events'][idx], ensure_ascii=False)}\n\n"
                idx += 1
            if j["status"] in ("done", "error"):
                break
            j["sse_event"].wait(timeout=30)
            j["sse_event"].clear()

    return Response(
        generate(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/ppt/status/<job_id>")
@login_required
def ppt_status(job_id):
    """폴링용 — SSE 대신 사용. Gamma 생성처럼 장시간 작업에 활용."""
    with _ppt_jobs_lock:
        job = _ppt_jobs.get(job_id)

    if not job:
        # 메모리에 없으면 DB 폴백
        try:
            db_job = get_ppt_job(job_id)
        except Exception:
            db_job = None

        if not db_job:
            return jsonify({
                "ok": False,
                "error": "생성 중이거나 만료된 작업입니다. 다시 시도해주세요.",
            }), 404

        if db_job["user_id"] != session["user_id"] and not session.get("is_admin"):
            return jsonify({"ok": False, "error": "권한 없음"}), 403

        db_status = db_job["status"]
        resp = {"ok": True, "status": db_status, "from_db": True}
        if db_status == "done":
            if db_job.get("gamma_url"):
                resp["gamma_url"] = db_job["gamma_url"]
            else:
                resp["status"] = "expired"
                resp["error"]  = "파일이 만료되었습니다. PPT를 다시 생성해주세요."
        elif db_status == "error":
            resp["error"] = db_job.get("error_msg") or "알 수 없는 오류"
        return jsonify(resp)

    if job["user_id"] != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음"}), 403

    status = job["status"]
    resp   = {"ok": True, "status": status}

    if status == "done" and job.get("pptx_bytes"):
        resp["download_url"] = f"/ppt/download/{job_id}"

    # events에서 gamma_done / ppt_done / ppt_error 추출
    for ev in job["events"]:
        t = ev.get("type")
        if t == "gamma_done":
            resp["gamma_url"]      = ev.get("url", "")
            resp["gamma_pptx_url"] = ev.get("pptx_url", "")
        elif t == "ppt_done" and "download_url" not in resp:
            resp["download_url"] = ev.get("download_url", f"/ppt/download/{job_id}")
            if ev.get("gamma_url"):
                resp["gamma_url"] = ev["gamma_url"]
        elif t == "ppt_error":
            resp["error"] = ev.get("message", "알 수 없는 오류")

    # 최신 progress 메시지
    for ev in reversed(job["events"]):
        if ev.get("type") == "ppt_progress":
            resp["progress_msg"] = ev.get("message", "")
            break

    return jsonify(resp)


@app.route("/ppt/download/<job_id>")
@login_required
def ppt_download(job_id):
    import io as _io
    with _ppt_jobs_lock:
        job = _ppt_jobs.get(job_id)
    if not job or not job.get("pptx_bytes"):
        abort(404)
    if job["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)
    buf = _io.BytesIO(job["pptx_bytes"])
    mimetype = (
        "application/pdf"
        if job.get("is_pdf")
        else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    return send_file(
        buf,
        as_attachment=True,
        download_name=job["filename"],
        mimetype=mimetype,
    )


# ─────────────────────────────────────────────
# PPT 버전 관리
# ─────────────────────────────────────────────

@app.route("/api/ppt_versions/<int:case_id>")
@login_required
def api_ppt_versions(case_id):
    """PPT 버전 목록 조회."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM rfp_cases WHERE id=?", (case_id,)
        ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    if dict(row)["user_id"] != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    import json as _json
    versions = get_ppt_versions(case_id)
    # pt_script JSON 파싱
    for v in versions:
        try:
            v["pt_script_parsed"] = _json.loads(v.get("pt_script") or "{}")
        except Exception:
            v["pt_script_parsed"] = {}
        v.pop("pt_script", None)
    return jsonify({"ok": True, "versions": versions})


@app.route("/ppt/version/<int:version_id>/download")
@login_required
def ppt_version_download(version_id):
    """저장된 PPT 버전 다운로드."""
    import io as _io
    row = get_ppt_version_data(version_id)
    if not row or not row.get("ppt_data"):
        abort(404)
    # 케이스 소유권 확인
    with get_connection() as conn:
        case_row = conn.execute(
            "SELECT user_id FROM rfp_cases WHERE id=?", (row["case_id"],)
        ).fetchone()
    if case_row and dict(case_row)["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)
    buf = _io.BytesIO(row["ppt_data"])
    mimetype = (
        "application/pdf"
        if row.get("is_pdf")
        else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    return send_file(buf, as_attachment=True,
                     download_name=row["ppt_filename"] or f"v{row['version']}.pptx",
                     mimetype=mimetype)


@app.route("/api/ppt_version/<int:version_id>/pt_script")
@login_required
def api_ppt_version_pt_script(version_id):
    """PPT 버전의 PT 원고 조회."""
    import json as _json
    row = get_ppt_version_data(version_id)
    if not row:
        return jsonify({"ok": False, "error": "버전 없음"}), 404
    with get_connection() as conn:
        case_row = conn.execute(
            "SELECT user_id FROM rfp_cases WHERE id=?", (row["case_id"],)
        ).fetchone()
    if case_row and dict(case_row)["user_id"] != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    try:
        pt_script = _json.loads(row.get("pt_script") or "{}")
    except Exception:
        pt_script = {}
    return jsonify({"ok": True, "pt_script": pt_script, "version": row["version"]})


@app.route("/api/ppt_version/<int:version_id>/memo", methods=["POST"])
@login_required
def api_ppt_version_memo(version_id):
    """PPT 버전 메모 수정."""
    data = request.get_json(force=True) or {}
    memo = data.get("memo", "")
    row  = get_ppt_version_data(version_id)
    if not row:
        return jsonify({"ok": False, "error": "버전 없음"}), 404
    with get_connection() as conn:
        case_row = conn.execute(
            "SELECT user_id FROM rfp_cases WHERE id=?", (row["case_id"],)
        ).fetchone()
    if case_row and dict(case_row)["user_id"] != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    update_ppt_version_memo(version_id, memo)
    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# Gamma API PPT 생성
# ─────────────────────────────────────────────

@app.route("/ppt/gamma/start", methods=["POST"])
@operator_or_admin_required
def ppt_gamma_start():
    """Gamma API를 통한 고품질 PPT 생성 시작."""
    data    = request.get_json(force=True) or {}
    case_id = int(data.get("case_id", 0))
    pages   = max(5, min(50, int(data.get("pages", 20))))

    if not case_id:
        return jsonify({"ok": False, "error": "case_id 필요"}), 400

    detail = get_case_detail(case_id)
    if not detail:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    if detail["case"].get("user_id") != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음 — 다시 로그인 후 시도하세요"}), 403

    job_id  = str(uuid.uuid4())
    user_id = session["user_id"]

    try:
        save_ppt_job(job_id, case_id, user_id, ppt_type="gamma2")
    except Exception as _e:
        print(f"[PPT/DB] 작업 저장 실패 (계속 진행): {_e}")

    with _ppt_jobs_lock:
        _ppt_jobs[job_id] = {
            "status":    "running",
            "events":    [],
            "pptx_bytes": None,
            "filename":  None,
            "sse_event": threading.Event(),
            "user_id":   user_id,
        }

    def _worker():
        try:
            _ppt_push(job_id, {"type": "ppt_progress", "message": "Gamma API에 요청 전송 중...", "current": 0, "total": 3})

            # 제안서 텍스트 조합
            case  = detail["case"]
            steps = detail.get("steps", {})
            parts = []
            if case.get("project_name"):
                parts.append(f"# {case['project_name']}\n발주처: {case.get('client_name', '')}")
            if steps.get("strategy"):
                s = steps["strategy"]
                if s.get("core_problem"):
                    parts.append(f"## 핵심 문제\n{s['core_problem']}")
                if s.get("solution_direction"):
                    parts.append(f"## 해결 방향\n{s['solution_direction']}")
            if steps.get("creative"):
                c = steps["creative"]
                if c.get("concept"):
                    parts.append(f"## 핵심 컨셉\n{c['concept']}")
                if c.get("confirmed_slogan"):
                    parts.append(f"## 슬로건\n{c['confirmed_slogan']}")
            if steps.get("plan"):
                p = steps["plan"]
                if p.get("production_schedule"):
                    sched = p["production_schedule"]
                    if isinstance(sched, list):
                        parts.append("## 제작 일정\n" + "\n".join(str(x) for x in sched[:10]))
            if steps.get("script"):
                sc = steps["script"]
                scripts = sc.get("scripts") or sc.get("script_outline") or []
                if scripts and isinstance(scripts, list):
                    parts.append("## 시나리오 개요\n" + "\n".join(str(x)[:200] for x in scripts[:3]))
            if steps.get("marketing"):
                m = steps["marketing"]
                if m.get("youtube_strategy"):
                    parts.append(f"## 유통 전략\n{str(m['youtube_strategy'])[:300]}")

            content = "\n\n".join(parts) if parts else f"{case.get('project_name', '')} 제안서"

            # RFP 기반 목차 순서 지시 삽입
            from output.pptx_builder import _build_rfp_toc
            _gamma_rfp_toc = _build_rfp_toc(steps.get("rfp_analysis", {}))
            _gamma_toc_lines = "\n".join(f"{i+1}. {s}" for i, s in enumerate(_gamma_rfp_toc))
            _gamma_toc_instruction = (
                f"## 슬라이드 구성 순서 (반드시 이 목차 순서를 따르라)\n\n"
                f"아래 목차 순서를 반드시 따라서 슬라이드를 구성하라. "
                f"각 항목당 1~2장을 할당하되 목차 순서를 벗어나지 마라:\n\n"
                f"{_gamma_toc_lines}\n"
            )
            content = _gamma_toc_instruction + "\n\n" + content

            _ppt_push(job_id, {"type": "ppt_progress", "message": "Gamma AI 생성 중 (30~60초 소요)...", "current": 1, "total": 3})

            from output.pptx_builder import generate_with_gamma
            result = generate_with_gamma(content, pages)

            _ppt_push(job_id, {"type": "ppt_progress", "message": "완료!", "current": 3, "total": 3})

            with _ppt_jobs_lock:
                job = _ppt_jobs.get(job_id)
                if job:
                    job["status"] = "done"

            _ppt_db_done(job_id, gamma_url=result.get("url", ""))

            _ppt_push(job_id, {
                "type":         "gamma_done",
                "url":          result.get("url", ""),
                "pptx_url":     result.get("pptx_url") or "",
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            _ppt_push(job_id, {"type": "ppt_error", "message": str(e)})
            with _ppt_jobs_lock:
                job = _ppt_jobs.get(job_id)
                if job:
                    job["status"] = "error"
            _ppt_db_error(job_id, str(e))

    threading.Thread(target=_worker, daemon=False).start()
    return jsonify({"ok": True, "job_id": job_id})


# ─────────────────────────────────────────────
# 큐 상태 API (폴링 폴백용)
# ─────────────────────────────────────────────

@app.route("/learning", methods=["GET", "POST"])
@operator_or_admin_required
def learning():
    user_id   = session["user_id"]
    filter_type = request.args.get("type", "")
    msg       = None
    msg_ok    = True

    if request.method == "POST":
        action = request.form.get("action", "add")

        if action == "delete":
            lid = int(request.form.get("id", 0))
            ok  = delete_learning_case(lid, user_id)
            msg = "삭제되었습니다." if ok else "삭제 권한이 없습니다."
            msg_ok = ok

        else:  # add
            data_type    = request.form.get("data_type", "").strip()
            client_name  = request.form.get("client_name", "").strip()
            project_name = request.form.get("project_name", "").strip()
            bid_result   = request.form.get("bid_result", "미정")
            eval_score   = float(request.form.get("eval_score", 0) or 0)
            notes        = request.form.get("notes", "").strip()

            # 텍스트 직접 입력
            content   = request.form.get("content", "").strip()
            file_name = ""

            # 파일 업로드 처리
            uploaded = request.files.get("file")
            if uploaded and uploaded.filename:
                file_name = secure_filename(uploaded.filename)
                try:
                    raw_bytes = uploaded.read()
                    # 텍스트 계열은 그대로 디코딩
                    ext = Path(file_name).suffix.lower()
                    if ext in {".txt", ".md"}:
                        content = raw_bytes.decode("utf-8", errors="replace")
                    else:
                        # hwp/pdf 등 바이너리 — 파일명과 크기만 기록
                        content = content or f"[파일 업로드: {file_name}, {len(raw_bytes):,} bytes]"
                except Exception as e:
                    content = content or f"[파일 읽기 오류: {e}]"

            if not data_type:
                msg    = "데이터 종류를 선택해주세요."
                msg_ok = False
            elif not content and not file_name:
                msg    = "내용 또는 파일을 입력해주세요."
                msg_ok = False
            else:
                save_learning_case(
                    user_id=user_id,
                    data_type=data_type,
                    client_name=client_name,
                    project_name=project_name,
                    content=content,
                    file_name=file_name,
                    bid_result=bid_result,
                    eval_score=eval_score,
                    notes=notes,
                )
                msg = "학습 데이터가 등록되었습니다."

    cases = list_learning_cases(user_id, filter_type)
    return render_template(
        "learning.html",
        cases=cases,
        filter_type=filter_type,
        msg=msg,
        msg_ok=msg_ok,
    )


@app.route("/api/queue-status/<sid>")
@login_required
def queue_status(sid):
    with _queue_lock:
        queue_list = list(_job_queue)
    pos = (queue_list.index(sid) + 1) if sid in queue_list else 0
    with _sessions_lock:
        sess = _sessions.get(sid)
    status = sess["status"] if sess else "unknown"
    return jsonify({"position": pos, "total": len(queue_list), "status": status})


# ─────────────────────────────────────────────
# PPT 설계 내러티브 API
# ─────────────────────────────────────────────

@app.route("/api/ppt_narrative/<int:case_id>")
@login_required
def api_ppt_narrative_get(case_id):
    """저장된 PPT 설계안 조회."""
    detail = get_case_detail(case_id)
    if not detail:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    uid = session["user_id"]
    is_owner = (detail["case"].get("user_id") == uid or bool(session.get("is_admin")))
    if not is_owner:
        shares = get_case_shares(case_id)
        if not any(s["shared_with"] == uid for s in shares):
            return jsonify({"ok": False, "error": "권한 없음"}), 403
    if case_id in _ppt_generating:
        return jsonify({"ok": True, "narrative": None, "generating": True, "is_owner": is_owner})
    narrative = get_ppt_narrative(case_id)
    return jsonify({"ok": True, "narrative": narrative, "is_owner": is_owner})


@app.route("/api/ppt_narrative/<int:case_id>/generate", methods=["POST"])
@operator_or_admin_required
def api_ppt_narrative_generate(case_id):
    """PPT 설계안 생성 (Claude AI 호출 — 백그라운드). 소유자/admin 전용."""
    detail = get_case_detail(case_id)
    if not detail:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    uid = session["user_id"]
    if detail["case"].get("user_id") != uid and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음 — 소유자만 설계안을 생성할 수 있습니다."}), 403

    data          = request.get_json(force=True) or {}
    target_slides = max(10, min(60, int(data.get("target_slides", 50))))

    def _generate():
        try:
            from agents.ppt_narrator import run as narrator_run
            result = narrator_run(detail, target_slides)
            save_ppt_narrative(
                case_id       = case_id,
                slides        = result["slides"],
                rfp_coverage  = result["rfp_coverage"],
                target_slides = target_slides,
                content_chars = result["content_chars"],
            )
            print(f"  [PPT설계] case={case_id} {result['total_slides']}장 저장 완료")
        except Exception as e:
            import traceback as _tb
            _tb.print_exc()
            print(f"  [PPT설계] 생성 오류: {e}")

    t = threading.Thread(target=_generate, daemon=False)
    t.start()
    return jsonify({"ok": True, "message": "설계 시작됨", "target_slides": target_slides})


@app.route("/api/ppt_narrative/<int:case_id>/rerun", methods=["POST"])
@operator_or_admin_required
def api_ppt_narrative_rerun(case_id):
    """수정 범위 + 코멘트 기반 PPT 설계안 재실행. 소유자/admin 전용."""
    detail = get_case_detail(case_id)
    if not detail:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    uid = session["user_id"]
    if detail["case"].get("user_id") != uid and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음"}), 403

    data    = request.get_json(force=True) or {}
    comment = (data.get("comment") or "").strip()
    if not comment:
        return jsonify({"ok": False, "error": "코멘트를 입력해 주세요."}), 400
    force = bool(data.get("force", False))

    # ── 충돌 방지
    with _case_rerun_lock:
        existing_ppt = _case_rerun_state.get(case_id)
    if existing_ppt and not force:
        is_still_running = False
        if existing_ppt.get("type") == "step":
            sid_ = existing_ppt.get("sid")
            with _sessions_lock:
                sess_ = _sessions.get(sid_) if sid_ else None
            is_still_running = bool(sess_ and sess_.get("status") in ("queued", "running"))
        elif existing_ppt.get("type") == "ppt":
            ae = existing_ppt.get("abort_event")
            is_still_running = (case_id in _ppt_generating) and not (ae and ae.is_set())
        if is_still_running:
            return jsonify({
                "ok": False, "conflict": True,
                "step_key":   existing_ppt["step_key"],
                "step_label": existing_ppt["step_label"],
            }), 409
        else:
            with _case_rerun_lock:
                _case_rerun_state.pop(case_id, None)

    if force:
        with _case_rerun_lock:
            old_ppt = _case_rerun_state.pop(case_id, None)
        if old_ppt:
            if old_ppt.get("type") == "step":
                sid_ = old_ppt.get("sid")
                with _sessions_lock:
                    sess_ = _sessions.get(sid_) if sid_ else None
                if sess_:
                    sess_["user_input"] = "__abort__"
                    ev_ = sess_.get("sse_event")
                    if ev_: ev_.set()
            elif old_ppt.get("type") == "ppt":
                ae = old_ppt.get("abort_event")
                if ae: ae.set()

    target_slides = max(10, min(60, int(data.get("target_slides", 50))))
    print(f"  [PPT재실행] 목표 슬라이드: {target_slides}장")
    scope_type    = data.get("scope_type", "all")   # "all" | "from" | "specific"
    scope_value   = data.get("scope_value")          # int("from") | str("specific")

    # 수정 범위 파싱
    scope_from_page = None
    scope_pages: list[int] = []
    if scope_type == "from":
        try:
            scope_from_page = max(2, int(scope_value or 2))
        except (TypeError, ValueError):
            scope_from_page = 2
    elif scope_type == "specific":
        try:
            scope_pages = sorted({
                int(p.strip()) for p in str(scope_value or "").split(",")
                if p.strip().isdigit() and int(p.strip()) >= 1
            })
        except Exception:
            scope_pages = []

    # step_instruction 구성 (범위 + 코멘트)
    if scope_type == "from" and scope_from_page:
        scope_desc  = f"{scope_from_page}페이지 이후부터 전체 수정"
        keep_note   = (f"\n슬라이드 1~{scope_from_page - 1}번은 유지하고, "
                       f"{scope_from_page}번부터 새로 설계하세요.\n"
                       "위 범위 외 슬라이드는 절대 변경하지 마세요.")
    elif scope_type == "specific" and scope_pages:
        pages_str  = ", ".join(str(p) for p in scope_pages)
        scope_desc = f"{pages_str}페이지만 수정"
        keep_note  = (f"\n슬라이드 {pages_str}번만 새로 작성하고, "
                      "나머지 슬라이드는 절대 변경하지 마세요.\n"
                      "위 범위 외 슬라이드는 절대 변경하지 마세요.")
    else:
        scope_desc = "전체 재설계"
        keep_note  = ""

    instruction = f"수정 범위: {scope_desc}\n수정 지시사항: {comment}{keep_note}"

    # 기존 설계안 — 범위 처리용 + 참고용
    existing    = get_ppt_narrative(case_id) or {}
    prev_slides = existing.get("slides", [])
    prev_content = ""
    if prev_slides:
        prev_content = json.dumps(prev_slides[:8], ensure_ascii=False)[:3000]

    detail["case"].setdefault("dna", {})
    detail["case"]["dna"]["step_instruction"]  = instruction
    detail["case"]["dna"]["step_prev_content"] = prev_content
    detail["case"]["dna"]["ppt_target_slides"] = target_slides

    # 폴링 충돌 방지 플래그 + 재실행 상태 등록
    abort_event = threading.Event()
    _ppt_generating.add(case_id)
    with _case_rerun_lock:
        _case_rerun_state[case_id] = {
            "type":        "ppt",
            "step_key":    "ppt_design",
            "step_label":  "PPT 설계",
            "abort_event": abort_event,
        }

    def _generate():
        try:
            from agents.ppt_narrator import run as narrator_run
            result     = narrator_run(detail, target_slides)

            # abort 요청이 들어온 경우 결과 저장 생략
            if abort_event.is_set():
                print(f"  [PPT재실행] case={case_id} abort 신호로 결과 저장 생략")
                return

            new_slides = result["slides"]

            # ── 수정 범위에 따라 기존 슬라이드와 병합 ──
            if scope_type == "from" and scope_from_page and prev_slides:
                keep     = list(prev_slides[: scope_from_page - 1])
                new_part = list(new_slides[scope_from_page - 1 :]) if len(new_slides) >= scope_from_page else new_slides
                merged   = keep + new_part
                for i, s in enumerate(merged):
                    s["number"] = i + 1
                new_slides = merged

            elif scope_type == "specific" and scope_pages and prev_slides:
                merged = list(prev_slides)
                for page in scope_pages:
                    idx = page - 1
                    if 0 <= idx < len(new_slides) and idx < len(merged):
                        merged[idx] = {**new_slides[idx], "number": page}
                new_slides = merged

            save_ppt_narrative(
                case_id       = case_id,
                slides        = new_slides,
                rfp_coverage  = result["rfp_coverage"],
                target_slides = len(new_slides),
                content_chars = result["content_chars"],
            )
            print(f"  [PPT재실행] case={case_id} 범위={scope_type} {len(new_slides)}장 저장 완료")
        except Exception as e:
            import traceback as _tb
            _tb.print_exc()
            print(f"  [PPT재실행] 생성 오류: {e}")
        finally:
            _ppt_generating.discard(case_id)
            with _case_rerun_lock:
                st = _case_rerun_state.get(case_id)
                if st and st.get("abort_event") is abort_event:
                    _case_rerun_state.pop(case_id, None)

    threading.Thread(target=_generate, daemon=False).start()
    return jsonify({"ok": True, "message": "PPT 설계 재실행 시작됨"})


@app.route("/api/ppt_narrative/<int:case_id>/save", methods=["POST"])
@operator_or_admin_required
def api_ppt_narrative_save(case_id):
    """사용자 편집 설계안 저장. 소유자/admin 전용."""
    detail = get_case_detail(case_id)
    if not detail:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    uid = session["user_id"]
    if detail["case"].get("user_id") != uid and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음 — 소유자만 설계안을 저장할 수 있습니다."}), 403

    data   = request.get_json(force=True) or {}
    slides = data.get("slides", [])
    if not isinstance(slides, list):
        return jsonify({"ok": False, "error": "slides 형식 오류"}), 400

    existing = get_ppt_narrative(case_id) or {}
    save_ppt_narrative(
        case_id       = case_id,
        slides        = slides,
        rfp_coverage  = existing.get("rfp_coverage", {}),
        target_slides = existing.get("target_slides", len(slides)),
        content_chars = existing.get("content_chars", 0),
    )
    return jsonify({"ok": True, "saved": len(slides)})


# ─────────────────────────────────────────────
# 재실행 충돌 방지 API
# ─────────────────────────────────────────────

@app.route("/api/rerun_status/<int:case_id>")
@login_required
def api_rerun_status(case_id):
    """케이스의 현재 재실행 진행 상태 조회."""
    with _case_rerun_lock:
        state = dict(_case_rerun_state.get(case_id, {}))
    if not state:
        return jsonify({"running": False})

    if state.get("type") == "step":
        sid_ = state.get("sid")
        with _sessions_lock:
            sess_ = _sessions.get(sid_) if sid_ else None
        if not sess_ or sess_.get("status") not in ("queued", "running"):
            with _case_rerun_lock:
                _case_rerun_state.pop(case_id, None)
            return jsonify({"running": False})
        return jsonify({"running": True, "type": "step",
                        "step_key": state["step_key"], "step_label": state["step_label"]})

    elif state.get("type") == "ppt":
        ae = state.get("abort_event")
        if (ae and ae.is_set()) or case_id not in _ppt_generating:
            with _case_rerun_lock:
                _case_rerun_state.pop(case_id, None)
            return jsonify({"running": False})
        return jsonify({"running": True, "type": "ppt",
                        "step_key": "ppt_design", "step_label": "PPT 설계"})

    return jsonify({"running": False})


@app.route("/api/rerun_abort/<int:case_id>", methods=["POST"])
@operator_or_admin_required
def api_rerun_abort(case_id):
    """케이스 재실행 강제 중단."""
    with _case_rerun_lock:
        state = _case_rerun_state.pop(case_id, None)
    if not state:
        return jsonify({"ok": True, "message": "실행 중인 재실행 없음"})

    if state.get("type") == "step":
        sid_ = state.get("sid")
        if sid_:
            with _sessions_lock:
                sess_ = _sessions.get(sid_)
            if sess_:
                sess_["user_input"] = "__abort__"
                ev_ = sess_.get("sse_event")
                if ev_: ev_.set()
    elif state.get("type") == "ppt":
        ae = state.get("abort_event")
        if ae: ae.set()

    return jsonify({"ok": True, "message": "재실행 중단 요청됨"})


# ─────────────────────────────────────────────
# 스토리보드 API
# ─────────────────────────────────────────────

@app.route("/api/storyboard/<int:case_id>")
@login_required
def api_storyboard(case_id):
    """스토리보드 프레임 목록 조회."""
    frames = get_storyboards(case_id)
    return jsonify({"ok": True, "frames": frames})


@app.route("/api/storyboard/<int:case_id>/regenerate", methods=["POST"])
@operator_or_admin_required
def api_storyboard_regenerate(case_id):
    """개별 씬 또는 전체 스토리보드 재생성."""
    data = request.get_json(force=True) or {}
    style      = data.get("style", "line")
    scene_num  = data.get("scene_num")  # None이면 전체 재생성

    detail = get_case_detail(case_id)
    if not detail:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    if detail["case"].get("user_id") != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음"}), 403

    # DNA 복원
    import dataclasses as _dc2
    from core.dna import ConceptDNA
    dna_dict = detail["case"].get("dna", {})
    dna = ConceptDNA()
    for f in _dc2.fields(dna):
        if f.name in dna_dict:
            try:
                setattr(dna, f.name, dna_dict[f.name])
            except Exception:
                pass

    # 스크립트 복원
    scripts_steps = detail.get("steps", {}).get("script", [])
    if isinstance(scripts_steps, list):
        dna.scripts = [s.get("script", {}) for s in scripts_steps if isinstance(s, dict)]
    dna.case_id = case_id

    def _regen():
        from agents.storyboard import run as sb_run
        result = sb_run(dna, style=style)
        print(f"  [스토리보드 재생성] case={case_id} style={style} 완료 {result.get('total_scenes')}컷")

    t = threading.Thread(target=_regen, daemon=False)
    t.start()
    return jsonify({"ok": True, "message": "재생성 시작됨"})


@app.route("/storyboard_image/<int:case_id>/<int:scene_num>")
@login_required
def storyboard_image(case_id, scene_num):
    """저장된 스토리보드 이미지 제공."""
    import io as _io
    from config import IS_PRODUCTION
    base = Path("/app/data/storyboards") if IS_PRODUCTION else Path(__file__).parent / "output" / "storyboards"
    img_path = base / str(case_id) / f"{scene_num}.png"
    if not img_path.exists():
        abort(404)
    return send_file(str(img_path), mimetype="image/png")


# ─────────────────────────────────────────────
# 스텝 재실행
# ─────────────────────────────────────────────



# ─────────────────────────────────────────────
# 스텝 미리보기 재실행 (채택/폐기 플로우)
# ─────────────────────────────────────────────

@app.route("/api/rerun_step/<int:case_id>/<step_key>", methods=["POST"])
@operator_or_admin_required
def api_rerun_step(case_id, step_key):
    """완료된 케이스의 특정 스텝 재실행 — 결과는 is_active=0 (미리보기)."""
    import dataclasses as _dcrs

    if step_key not in _RERUN_STEP_TABLE_MAP:
        return jsonify({"ok": False, "error": f"지원하지 않는 스텝: {step_key}"}), 400

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    case = dict(row)
    if case["user_id"] != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음"}), 403

    data         = request.get_json(force=True) or {}
    comment      = (data.get("comment") or "").strip()

    # DNA 복원
    from core.dna import ConceptDNA
    dna_dict = json.loads(case.get("dna_json") or "{}")
    dna = ConceptDNA()
    for _f in _dcrs.fields(dna):
        if _f.name in dna_dict:
            try:
                setattr(dna, _f.name, dna_dict[_f.name])
            except Exception:
                pass
    dna.case_id = case_id
    if comment:
        dna.step_instruction = comment

    sid = str(uuid.uuid4())
    with _sessions_lock:
        _sessions[sid] = {
            "status":           "queued",
            "user_id":          session["user_id"],
            "username":         session["username"],
            "dna":              dna,
            "rfp_file":         None,
            "concept":          dna.concept or None,
            "results":          {},
            "events":           [],
            "sse_event":        threading.Event(),
            "confirm_event":    threading.Event(),
            "user_input":       None,
            "step_instruction": None,
            "txt_path":         None,
            "client":           case["client_name"],
            "project":          case["project_name"],
            "created_at":       datetime.now().isoformat(),
            "created_at_ts":    time.time(),
            "case_id":          case_id,
            "retry_from":       step_key,
            "auto_run":         True,
            "selected_steps":   {step_key},
            "preview_mode":     True,   # ← 미리보기 모드
            "preview_row_ids":  {},
        }

    with _queue_lock:
        _job_queue.append(sid)
    _ensure_worker()
    _dispatch_jobs()
    return jsonify({"ok": True, "sid": sid})


@app.route("/api/rerun_step_status/<sid>")
@login_required
def api_rerun_step_status(sid):
    """미리보기 재실행 세션 상태 조회."""
    with _sessions_lock:
        sess = _sessions.get(sid)
    if not sess:
        return jsonify({"ok": False, "status": "not_found"})
    return jsonify({
        "ok":              True,
        "status":          sess.get("status", "unknown"),
        "preview_row_ids": sess.get("preview_row_ids", {}),
    })


@app.route("/api/step_preview/<int:case_id>/<step_key>")
@login_required
def api_step_preview(case_id, step_key):
    """is_active=0인 미리보기 결과 반환."""
    table = _RERUN_STEP_TABLE_MAP.get(step_key)
    if not table:
        return jsonify({"ok": False, "error": "지원하지 않는 스텝"}), 400
    row = get_step_preview_row(table, case_id)
    if not row:
        return jsonify({"ok": False, "error": "미리보기 결과 없음"}), 404
    # JSON 컬럼 파싱
    preview = {}
    for k, v in row.items():
        if k in ("id", "case_id", "created_at", "is_active",
                 "client_name", "project_name"): continue
        if isinstance(v, str) and v.startswith(("[", "{")):
            try:
                preview[k] = json.loads(v)
                continue
            except Exception:
                pass
        preview[k] = v
    return jsonify({"ok": True, "row_id": row["id"], "data": preview,
                    "created_at": row.get("created_at", "")})


@app.route("/api/adopt_step/<int:case_id>/<step_key>", methods=["POST"])
@operator_or_admin_required
def api_adopt_step(case_id, step_key):
    """미리보기 결과 채택 — overwrite=true면 해당 row만 active, false면 새 버전으로 추가."""
    table = _RERUN_STEP_TABLE_MAP.get(step_key)
    if not table:
        return jsonify({"ok": False, "error": "지원하지 않는 스텝"}), 400

    # 소유자 확인
    with get_connection() as conn:
        row = conn.execute("SELECT user_id FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row or (dict(row)["user_id"] != session["user_id"] and not session.get("is_admin")):
        return jsonify({"ok": False, "error": "권한 없음"}), 403

    data      = request.get_json(force=True) or {}
    row_id    = data.get("row_id")
    overwrite = data.get("overwrite", True)   # True=덮어쓰기 / False=새 버전 추가

    if not row_id:
        # row_id 없으면 최신 is_active=0 row 사용
        preview = get_step_preview_row(table, case_id)
        if not preview:
            return jsonify({"ok": False, "error": "채택할 미리보기 없음"}), 404
        row_id = preview["id"]

    if overwrite:
        # 기존 active → inactive, 새 row → active
        activate_step_result(table, row_id, case_id)
    else:
        # 새 버전 추가: 기존 active 유지하고 새 row도 active (두 버전 공존)
        with get_connection() as conn:
            conn.execute(f"UPDATE {table} SET is_active=1 WHERE id=?", (row_id,))
    return jsonify({"ok": True})


@app.route("/api/discard_step/<int:case_id>/<step_key>", methods=["POST"])
@operator_or_admin_required
def api_discard_step(case_id, step_key):
    """미리보기 결과 폐기 — is_active=0인 최신 row 삭제."""
    table = _RERUN_STEP_TABLE_MAP.get(step_key)
    if not table:
        return jsonify({"ok": False, "error": "지원하지 않는 스텝"}), 400

    with get_connection() as conn:
        row = conn.execute("SELECT user_id FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row or (dict(row)["user_id"] != session["user_id"] and not session.get("is_admin")):
        return jsonify({"ok": False, "error": "권한 없음"}), 403

    preview = get_step_preview_row(table, case_id)
    if preview:
        with get_connection() as conn:
            conn.execute(f"DELETE FROM {table} WHERE id=?", (preview["id"],))
    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# 제안서 수정 (선택 스텝 재실행)
# ─────────────────────────────────────────────

_REVISION_STEP_LABELS = {
    "research":       "STEP1 리서치",
    "strategy":       "STEP2 전략",
    "creative":       "STEP3 컨셉",
    "plan":           "STEP4 기획",
    "script":         "STEP5 대본",
    "marketing":      "STEP6 마케팅",
    "final_proposal": "STEP7 최종검수",
}
_REVISION_STEP_TABLE = {
    "research":       "research_results",
    "strategy":       "strategy_results",
    "creative":       "creative_results",
    "plan":           "plan_results",
    "script":         "script_results",
    "marketing":      "marketing_results",
    "final_proposal": "final_proposals",
}
_REVISION_ALLOWED_EXT = {".hwp", ".hwpx", ".pdf", ".txt", ".jpg", ".jpeg", ".png", ".gif"}


@app.route("/api/revise/<int:case_id>", methods=["POST"])
@operator_or_admin_required
def api_revise(case_id):
    """제안서 수정: 선택 스텝만 재실행."""
    import dataclasses as _dcr

    scope               = request.form.get("scope", "all")
    selected_steps_raw  = request.form.getlist("selected_steps")
    request_text        = (request.form.get("request_text") or "").strip()

    # ── 권한 확인
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    case = dict(row)
    if case["user_id"] != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음"}), 403

    # ── 선택 스텝 결정
    all_revision_steps = list(_REVISION_STEP_LABELS.keys())
    if scope == "all":
        selected = set(all_revision_steps)
    else:
        selected = {s for s in selected_steps_raw if s in _REVISION_STEP_LABELS}
    if not selected:
        return jsonify({"ok": False, "error": "선택된 스텝이 없습니다"}), 400

    # ── 첨부파일 처리 (텍스트 추출 가능한 파일만)
    attached_names = []
    file_texts     = []
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in _REVISION_ALLOWED_EXT:
            continue
        safe  = "rev_" + _safe_upload_name(f.filename, ext)
        fpath = str(UPLOAD_DIR / safe)
        f.save(fpath)
        attached_names.append(f.filename)
        if ext in ALLOWED_EXT:
            try:
                from agents.rfp_parser import extract_text as _et
                txt = _et(fpath)
                if txt.strip():
                    file_texts.append(f"[첨부파일: {f.filename}]\n{txt[:3000]}")
            except Exception as _fe:
                print(f"[수정] 파일 파싱 실패 ({f.filename}): {_fe}")

    # ── 수정 지시 조합
    instruction_parts = []
    if request_text:
        instruction_parts.append(f"[수정 요청사항]\n{request_text}")
    if file_texts:
        instruction_parts.extend(file_texts)
    instruction = "\n\n".join(instruction_parts)

    # ── DNA 복원
    from core.dna import ConceptDNA
    dna_dict = json.loads(case.get("dna_json") or "{}")
    dna = ConceptDNA()
    for _f in _dcr.fields(dna):
        if _f.name in dna_dict:
            try:
                setattr(dna, _f.name, dna_dict[_f.name])
            except Exception:
                pass
    dna.case_id = case_id
    if instruction:
        dna.step_instruction = instruction

    # ── 선택 스텝 DB 레코드 삭제 (재실행을 위해)
    with get_connection() as conn:
        for sk in selected:
            tbl = _REVISION_STEP_TABLE.get(sk)
            if tbl:
                conn.execute(f"DELETE FROM {tbl} WHERE case_id=?", (case_id,))

    # ── 세션 생성
    sid = str(uuid.uuid4())
    with _sessions_lock:
        _sessions[sid] = {
            "status":           "queued",
            "user_id":          session["user_id"],
            "username":         session["username"],
            "dna":              dna,
            "rfp_file":         None,
            "concept":          dna.concept or None,
            "results":          {},
            "events":           [],
            "sse_event":        threading.Event(),
            "confirm_event":    threading.Event(),
            "user_input":       None,
            "step_instruction": None,
            "txt_path":         None,
            "client":           case["client_name"],
            "project":          case["project_name"],
            "created_at":       datetime.now().isoformat(),
            "created_at_ts":    time.time(),
            "case_id":          case_id,
            "retry_from":       None,
            "auto_run":         True,
            "selected_steps":   selected,
        }

    with _queue_lock:
        _job_queue.append(sid)
    _ensure_worker()
    _dispatch_jobs()
    _broadcast_positions()

    # ── 수정 이력 저장
    try:
        save_case_revision(
            case_id=case_id, scope=scope,
            selected_steps=list(selected),
            request_text=request_text,
            attached_files=attached_names,
            session_id=sid,
        )
    except Exception as _re:
        print(f"[수정] 이력 저장 실패: {_re}")

    # ── 충돌 방지 상태 등록
    with _case_rerun_lock:
        _case_rerun_state[case_id] = {
            "type":       "step",
            "step_key":   next(iter(selected)),
            "step_label": "수정 재실행",
            "sid":        sid,
        }

    session["current_run_sid"] = sid
    return jsonify({"ok": True, "sid": sid, "redirect": f"/run/{sid}"})

@app.route("/rerun_from_step/<int:case_id>/<step_key>", methods=["POST"])
@operator_or_admin_required
def rerun_from_step(case_id, step_key):
    """특정 스텝부터 파이프라인 재실행."""
    import dataclasses as _dc2

    VALID_STEPS = {
        "rfp_analysis", "research", "narrative", "strategy",
        "creative", "plan", "script", "storyboard",
        "platform", "marketing", "final_proposal",
    }
    if step_key not in VALID_STEPS:
        return jsonify({"ok": False, "error": f"유효하지 않은 step_key: {step_key}"}), 400

    req_data = request.get_json(force=True) or {}
    comment  = str(req_data.get("comment", "")).strip()
    force    = bool(req_data.get("force", False))

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404

    case = dict(row)
    if case["user_id"] != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음"}), 403

    _STEP_LABELS = {
        "rfp_analysis": "RFP 분석", "research": "리서치", "narrative": "내러티브",
        "strategy": "전략", "creative": "컨셉", "plan": "기획",
        "script": "시나리오", "storyboard": "스토리보드", "platform": "플랫폼 운영전략",
        "marketing": "마케팅/홍보 전략", "final_proposal": "PT/Q&A",
    }

    # ── 충돌 방지: 같은 케이스에서 이미 재실행 중인지 확인
    with _case_rerun_lock:
        existing = _case_rerun_state.get(case_id)

    if existing and not force:
        is_still_running = False
        if existing.get("type") == "step":
            sid_ = existing.get("sid")
            with _sessions_lock:
                sess_ = _sessions.get(sid_) if sid_ else None
            is_still_running = bool(sess_ and sess_.get("status") in ("queued", "running"))
        elif existing.get("type") == "ppt":
            ae = existing.get("abort_event")
            is_still_running = (case_id in _ppt_generating) and not (ae and ae.is_set())
        if is_still_running:
            return jsonify({
                "ok": False, "conflict": True,
                "step_key":   existing["step_key"],
                "step_label": existing["step_label"],
            }), 409
        else:
            with _case_rerun_lock:
                _case_rerun_state.pop(case_id, None)

    if force:
        with _case_rerun_lock:
            old_state = _case_rerun_state.pop(case_id, None)
        if old_state:
            if old_state.get("type") == "step":
                sid_ = old_state.get("sid")
                with _sessions_lock:
                    sess_ = _sessions.get(sid_) if sid_ else None
                if sess_:
                    sess_["user_input"] = "__abort__"
                    ev_ = sess_.get("sse_event")
                    if ev_: ev_.set()
            elif old_state.get("type") == "ppt":
                ae = old_state.get("abort_event")
                if ae: ae.set()

    # DNA 복원
    from core.dna import ConceptDNA
    dna_dict = json.loads(case.get("dna_json") or "{}")
    dna = ConceptDNA()
    for f in _dc2.fields(dna):
        if f.name in dna_dict:
            try:
                setattr(dna, f.name, dna_dict[f.name])
            except Exception:
                pass
    dna.case_id = case_id

    # 해당 스텝부터 이후 스텝 결과를 DB에서 삭제 (재실행을 위해)
    STEP_TABLE_MAP = {
        "rfp_analysis":   "rfp_analyses",
        "research":       "research_results",
        "strategy":       "strategy_results",
        "creative":       "creative_results",
        "plan":           "plan_results",
        "script":         "script_results",
        "storyboard":     "storyboard_results",
        "platform":       "platform_results",
        "marketing":      "marketing_results",
        "final_proposal": "final_proposals",
    }
    PIPELINE_ORDER = [
        "rfp_analysis", "research", "narrative", "strategy",
        "creative", "plan", "script", "storyboard",
        "platform", "marketing", "final_proposal",
    ]
    start_idx = PIPELINE_ORDER.index(step_key) if step_key in PIPELINE_ORDER else 0
    steps_to_clear = PIPELINE_ORDER[start_idx:]

    # 이전 결과 수집 (삭제 전)
    prev_content_text = ""
    if comment:
        table = STEP_TABLE_MAP.get(step_key)
        if table:
            with get_connection() as conn:
                prev_row = conn.execute(
                    f"SELECT * FROM {table} WHERE case_id=? ORDER BY created_at DESC LIMIT 1",
                    (case_id,),
                ).fetchone()
            if prev_row:
                try:
                    prev_dict = {k: v for k, v in dict(prev_row).items()
                                 if k not in ("id", "case_id", "created_at", "client_name", "project_name")}
                    prev_content_text = json.dumps(prev_dict, ensure_ascii=False)[:3000]
                except Exception:
                    pass

    # 코멘트 → step_instruction / step_prev_content 주입
    if comment:
        dna.step_instruction = comment
        if prev_content_text:
            dna.step_prev_content = prev_content_text

    with get_connection() as conn:
        for sk in steps_to_clear:
            table = STEP_TABLE_MAP.get(sk)
            if table:
                conn.execute(f"DELETE FROM {table} WHERE case_id=?", (case_id,))

    sid = str(uuid.uuid4())
    with _sessions_lock:
        _sessions[sid] = {
            "status":           "queued",
            "user_id":          session["user_id"],
            "username":         session["username"],
            "dna":              dna,
            "rfp_file":         None,
            "concept":          dna.concept or None,
            "results":          {},
            "events":           [],
            "sse_event":        threading.Event(),
            "confirm_event":    threading.Event(),
            "user_input":       None,
            "step_instruction": None,
            "txt_path":         None,
            "client":           case["client_name"],
            "project":          case["project_name"],
            "created_at":       datetime.now().isoformat(),
            "retry_from":       step_key,
            "case_id":          case_id,
            "auto_run":         True,
        }

    with _queue_lock:
        _job_queue.append(sid)
    _ensure_worker()
    _dispatch_jobs()
    _broadcast_positions()

    # 재실행 상태 등록
    with _case_rerun_lock:
        _case_rerun_state[case_id] = {
            "type":       "step",
            "step_key":   step_key,
            "step_label": _STEP_LABELS.get(step_key, step_key),
            "sid":        sid,
        }

    session["current_run_sid"] = sid
    return jsonify({"ok": True, "sid": sid, "redirect": f"/run/{sid}"})


# ─────────────────────────────────────────────
# 스텝 내용 수정
# ─────────────────────────────────────────────

@app.route("/update_step_content/<int:case_id>/<step_key>", methods=["POST"])
@operator_or_admin_required
def update_step_content(case_id, step_key):
    """스텝 내용을 직접 수정해 DB에 저장."""
    VALID_STEPS = {
        "rfp_analysis", "research", "narrative", "strategy",
        "creative", "plan", "script", "storyboard",
        "platform", "marketing", "final_proposal",
    }
    if step_key not in VALID_STEPS:
        return jsonify({"ok": False, "error": "유효하지 않은 step_key"}), 400

    with get_connection() as conn:
        row = conn.execute("SELECT user_id FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    if dict(row)["user_id"] != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "권한 없음"}), 403

    data = request.get_json(force=True) or {}
    content = data.get("content", {})
    if not isinstance(content, dict):
        return jsonify({"ok": False, "error": "content는 JSON 객체여야 합니다"}), 400

    editor = session.get("username", "")
    save_step_override(case_id=case_id, step_key=step_key,
                       content=content, editor=editor)
    return jsonify({"ok": True, "edited_at": datetime.now().isoformat()})


@app.route("/api/step_content/<int:case_id>/<step_key>")
@login_required
def api_step_content(case_id, step_key):
    """스텝 수정 오버라이드 조회."""
    override = get_step_override(case_id, step_key)
    if not override:
        return jsonify({"ok": True, "override": None})
    return jsonify({"ok": True, "override": override})


@app.route("/api/step_overrides/<int:case_id>")
@login_required
def api_step_overrides(case_id):
    """케이스의 모든 스텝 수정 오버라이드 조회."""
    overrides = get_all_step_overrides(case_id)
    return jsonify({"ok": True, "overrides": overrides})


# ─────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────


# ── 나라장터 입찰 모니터링 ──────────────────────────────

@app.route("/nara")
@login_required
def nara_dashboard():
    page         = max(1, int(request.args.get("page", 1)))
    keyword      = request.args.get("keyword", "").strip()
    hide_expired = request.args.get("hide_expired", "0") == "1"
    search_nm    = request.args.get("search_nm", "").strip()
    search_instt = request.args.get("search_instt", "").strip()
    keywords     = get_nara_keywords()
    paged        = list_nara_bids_paged(keyword=keyword, page=page, per_page=50,
                                        hide_expired=hide_expired,
                                        search_nm=search_nm, search_instt=search_instt)
    settings     = get_nara_settings()
    candidate_nos = get_candidate_bid_nos()
    return render_template("nara.html", keywords=keywords,
                           bids=paged["items"], pagination=paged,
                           settings=settings, candidate_nos=candidate_nos,
                           kw_filter=keyword, hide_expired=hide_expired,
                           search_nm=search_nm, search_instt=search_instt)

@app.route("/nara/candidates")
@login_required
def nara_candidates_page():
    page   = max(1, int(request.args.get("page", 1)))
    paged  = list_nara_candidates(page=page, per_page=50)
    is_ops = session.get("role") in ("admin", "operator")
    is_adm = bool(session.get("is_admin"))
    pickup_cand_ids = get_pickup_candidate_ids()
    return render_template("nara_candidates.html", candidates=paged["items"],
                           pagination=paged, is_ops=is_ops, is_adm=is_adm,
                           pickup_cand_ids=pickup_cand_ids)

@app.route("/nara/pickups")
@login_required
def nara_pickups_page():
    page   = max(1, int(request.args.get("page", 1)))
    paged  = list_nara_pickups(page=page, per_page=50)
    is_ops = session.get("role") in ("admin", "operator")
    return render_template("nara_pickups.html", pickups=paged["items"],
                           pagination=paged, is_ops=is_ops)

@app.route("/nara/confirmed")
@login_required
def nara_confirmed_page():
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 20
    offset   = (page - 1) * per_page
    is_ops   = session.get("role") in ("admin", "operator")

    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM nara_confirmed").fetchone()[0]
        rows = conn.execute("""
            SELECT cf.id, cf.candidate_id, cf.pickup_id, cf.confirmed_by,
                   cf.notes, cf.assignee, cf.created_at,
                   cf.completion_status, cf.final_result,
                   COALESCE(pk.bid_ntce_no, ca.bid_ntce_no) AS bid_ntce_no,
                   COALESCE(pk.bid_ntce_nm, ca.bid_ntce_nm) AS bid_ntce_nm,
                   COALESCE(pk.ntce_instt_nm, ca.ntce_instt_nm) AS ntce_instt_nm,
                   COALESCE(pk.presmpt_prce, ca.presmpt_prce) AS presmpt_prce,
                   COALESCE(pk.bid_clse_dt, ca.bid_clse_dt) AS bid_clse_dt,
                   COALESCE(pk.ntce_url, ca.ntce_url) AS ntce_url,
                   COALESCE(rfp.cnt, 0) AS rfp_count,
                   bi.submit_deadline, bi.submit_method,
                   bi.pt_date, bi.pt_location, bi.price_bid_date,
                   COALESCE(res.status, 'pending') AS research_status,
                   n.content AS narrative_content,
                   COALESCE(sch_bm.due_date, '') AS sched_bid,
                   COALESCE(sch_ps.due_date, '') AS sched_proposal,
                   COALESCE(sch_pt.due_date, '') AS sched_pt,
                   COALESCE(sch_pb.due_date, '') AS sched_price
            FROM nara_confirmed cf
            LEFT JOIN nara_pickups pk    ON pk.id = cf.pickup_id    AND cf.pickup_id > 0
            LEFT JOIN nara_candidates ca ON ca.id = cf.candidate_id AND cf.pickup_id = 0
            LEFT JOIN (SELECT confirmed_id, COUNT(*) AS cnt
                       FROM confirmed_rfp_files GROUP BY confirmed_id) rfp
                   ON rfp.confirmed_id = cf.id
            LEFT JOIN confirmed_bid_info bi ON bi.confirmed_id = cf.id
            LEFT JOIN confirmed_narratives n ON n.confirmed_id = cf.id
            LEFT JOIN confirmed_research res ON res.confirmed_id = cf.id
            LEFT JOIN (SELECT confirmed_id, MAX(due_date) AS due_date
                       FROM confirmed_schedule WHERE task_name LIKE '입찰 마감%'
                       GROUP BY confirmed_id) sch_bm ON sch_bm.confirmed_id = cf.id
            LEFT JOIN (SELECT confirmed_id, MAX(due_date) AS due_date
                       FROM confirmed_schedule WHERE task_name LIKE '제안서 제출%'
                       GROUP BY confirmed_id) sch_ps ON sch_ps.confirmed_id = cf.id
            LEFT JOIN (SELECT confirmed_id, MAX(due_date) AS due_date
                       FROM confirmed_schedule WHERE task_name LIKE 'PT 발표%'
                       GROUP BY confirmed_id) sch_pt ON sch_pt.confirmed_id = cf.id
            LEFT JOIN (SELECT confirmed_id, MAX(due_date) AS due_date
                       FROM confirmed_schedule WHERE task_name LIKE '가격투찰%'
                       GROUP BY confirmed_id) sch_pb ON sch_pb.confirmed_id = cf.id
            ORDER BY cf.created_at DESC LIMIT ? OFFSET ?
        """, (per_page, offset)).fetchall()

    confirmed_items = [dict(r) for r in rows]
    pagination = {
        "items":    confirmed_items,
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, -(-total // per_page)),
    }
    return render_template("nara_confirmed.html",
                           confirmed=confirmed_items,
                           pagination=pagination,
                           is_ops=is_ops)


@app.route("/nara/archive")
@login_required
def nara_archive():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT cf.id, cf.final_result, cf.completion_approved_by, cf.completion_approved_at,
                   cf.assignee, cf.created_at,
                   COALESCE(pk.bid_ntce_no, ca.bid_ntce_no) AS bid_ntce_no,
                   COALESCE(pk.bid_ntce_nm, ca.bid_ntce_nm) AS bid_ntce_nm,
                   COALESCE(pk.ntce_instt_nm, ca.ntce_instt_nm) AS ntce_instt_nm,
                   COALESCE(pk.presmpt_prce, ca.presmpt_prce) AS presmpt_prce,
                   COALESCE(pk.bid_clse_dt, ca.bid_clse_dt) AS bid_clse_dt
            FROM nara_confirmed cf
            LEFT JOIN nara_pickups pk    ON pk.id = cf.pickup_id    AND cf.pickup_id > 0
            LEFT JOIN nara_candidates ca ON ca.id = cf.candidate_id AND cf.pickup_id = 0
            WHERE cf.final_result IN ('won', 'lost')
            ORDER BY cf.completion_approved_at DESC
        """).fetchall()
    items = [dict(r) for r in rows]
    return render_template("nara_archive.html", items=items)


@app.route("/nara/confirmed/<int:confirmed_id>/request_completion", methods=["POST"])
@login_required
def request_completion_route(confirmed_id):
    username = session.get("username")
    request_completion(confirmed_id, username)
    c = get_confirmed_by_id(confirmed_id)
    nm = (c.get("bid_ntce_nm") or "-") if c else "-"
    settings = get_notification_settings()
    for uid in settings.get("completion_approval", []):
        create_notification(
            uid, "완료 승인 요청",
            f"{nm} — {username}님이 완료 승인을 요청했습니다.",
            f"/nara/confirmed/{confirmed_id}",
        )
    return jsonify({"ok": True})


@app.route("/nara/confirmed/<int:confirmed_id>/approve_completion", methods=["POST"])
@login_required
def approve_completion_route(confirmed_id):
    if not session.get("is_admin") and session.get("role") != "operator":
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    approve_completion(confirmed_id, session.get("username"))
    return jsonify({"ok": True})


@app.route("/nara/confirmed/<int:confirmed_id>/set_result", methods=["POST"])
@login_required
def set_result_route(confirmed_id):
    if not session.get("is_admin") and session.get("role") != "operator":
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    data = request.get_json(force=True) or {}
    result = data.get("result", "")
    if result not in ("won", "lost"):
        return jsonify({"ok": False, "error": "결과값 오류"}), 400
    set_final_result(confirmed_id, result, session.get("username"))
    return jsonify({"ok": True})


@app.route("/nara/confirmed/<int:confirmed_id>")
@login_required
def nara_confirmed_detail(confirmed_id):
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return "Not found", 404
    narrative  = get_confirmed_narrative(confirmed_id)
    comments   = list_confirmed_comments(confirmed_id)
    schedule   = list_confirmed_schedule(confirmed_id)
    if not schedule:
        raw_clse  = c.get("bid_clse_dt") or ""
        due_date  = raw_clse[:10] if raw_clse else ""
        add_confirmed_schedule(
            confirmed_id=confirmed_id,
            task_name="입찰 마감",
            assignee=c.get("assignee") or "",
            due_date=due_date,
            status="예정",
            sort_order=0,
        )
        schedule = list_confirmed_schedule(confirmed_id)
    bid_info   = get_confirmed_bid_info(confirmed_id)
    # 제안서 제출 일시가 있고 일정에 없으면 자동 추가
    if bid_info and bid_info.get("proposal_submit_date"):
        existing_tasks = [s["task_name"] for s in schedule]
        if not any("제안서 제출" in t for t in existing_tasks):
            ps_method = bid_info.get("proposal_submit_method") or "직접"
            ps_short  = "직" if ps_method == "직접" else "온"
            ps_due    = (bid_info.get("proposal_submit_date") or "")[:10]
            add_confirmed_schedule(
                confirmed_id=confirmed_id,
                task_name=f"제안서 제출 ({ps_short})",
                assignee=c.get("assignee") or "",
                due_date=ps_due,
                status="예정",
                sort_order=1,
            )
            schedule = list_confirmed_schedule(confirmed_id)
    # PT 일시가 있고 일정에 "PT 발표"가 없으면 자동 추가
    if bid_info and bid_info.get("pt_date") and bid_info.get("doc_pt"):
        existing_tasks = [s["task_name"] for s in schedule]
        if not any("PT 발표" in t for t in existing_tasks):
            pt_due      = (bid_info.get("pt_date") or "")[:10]
            pt_location = (bid_info.get("pt_location") or "").strip()
            pt_name     = f"PT 발표 @ {pt_location}" if pt_location else "PT 발표"
            add_confirmed_schedule(
                confirmed_id=confirmed_id,
                task_name=pt_name,
                assignee=c.get("assignee") or "",
                due_date=pt_due,
                status="예정",
                sort_order=2,
            )
            schedule = list_confirmed_schedule(confirmed_id)
    # 가격투찰 일시가 있고 일정에 "가격투찰"이 없으면 자동 추가
    if bid_info and bid_info.get("price_bid_date"):
        existing_tasks = [s["task_name"] for s in schedule]
        if not any("가격투찰" in t for t in existing_tasks):
            pb_due = (bid_info.get("price_bid_date") or "")[:10]
            add_confirmed_schedule(
                confirmed_id=confirmed_id,
                task_name="가격투찰",
                assignee=c.get("assignee") or "",
                due_date=pb_due,
                status="예정",
                sort_order=3,
            )
            schedule = list_confirmed_schedule(confirmed_id)
    from database.db import get_connection
    with get_connection() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT id, username FROM users ORDER BY username"
        ).fetchall()]
    is_ops      = session.get("role") in ("admin", "operator")
    is_assignee = session.get("username") == c.get("assignee")
    can_edit    = is_ops or is_assignee
    can_edit_narrative = bool(session.get("is_admin")) or is_assignee
    narrative_qa = None
    if narrative:
        try:
            parsed = json.loads(narrative["content"])
            if isinstance(parsed, dict) and any(parsed.values()):
                narrative_qa = parsed
        except Exception:
            pass
    rfp_files       = list_confirmed_rfp_files(confirmed_id)
    research        = get_confirmed_research(confirmed_id)
    proposal_design = get_proposal_design(confirmed_id)
    from datetime import datetime as _dt
    return render_template("nara_confirmed_detail.html",
                           c=c, narrative=narrative, narrative_qa=narrative_qa,
                           comments=comments,
                           schedule=schedule, bid_info=bid_info,
                           rfp_files=rfp_files, research=research,
                           proposal_design=proposal_design,
                           users=users, can_edit=can_edit, is_ops=is_ops,
                           can_edit_narrative=can_edit_narrative,
                           now=_dt.now().strftime("%Y-%m-%d %H:%M"))


@app.route("/nara/confirmed/<int:confirmed_id>/narrative", methods=["POST"])
@login_required
def nara_confirmed_narrative_save(confirmed_id):
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return jsonify({"ok": False, "error": "Not found"}), 404
    is_admin    = session.get("is_admin")
    is_assignee = session.get("username") == c.get("assignee")
    if not (is_admin or is_assignee):
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    data    = request.get_json(force=True) or {}
    content = (data.get("content") or "").strip()
    save_confirmed_narrative(confirmed_id, content, session["username"])
    return jsonify({"ok": True})


@app.route("/nara/confirmed/<int:confirmed_id>/proposal_design", methods=["GET", "POST"])
@login_required
def nara_proposal_design(confirmed_id):
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if request.method == "GET":
        pd = get_proposal_design(confirmed_id)
        return jsonify({"ok": True, "proposal_design": pd})
    data    = request.get_json(force=True) or {}
    content = (data.get("content") or "")
    save_proposal_design(confirmed_id, content)
    return jsonify({"ok": True})


@app.route("/nara/confirmed/<int:confirmed_id>/proposal_design/feedback", methods=["POST"])
@login_required
def nara_proposal_design_feedback(confirmed_id):
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return jsonify({"ok": False, "error": "Not found"}), 404
    is_ops      = session.get("role") in ("admin", "operator")
    is_assignee = session.get("username") == c.get("assignee")
    if not (is_ops or is_assignee):
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    pd = get_proposal_design(confirmed_id)
    content = (pd or {}).get("content", "").strip()
    if not content:
        return jsonify({"ok": False, "error": "제안서 설계 내용을 먼저 작성하세요"}), 400
    narrative = get_confirmed_narrative(confirmed_id)
    narrative_text = ""
    if narrative and narrative.get("content"):
        try:
            import json as _j
            nq = _j.loads(narrative["content"])
            if isinstance(nq, dict):
                narrative_text = "\n".join(f"- {v}" for v in nq.values() if v)
        except Exception:
            narrative_text = narrative["content"]
    research = get_confirmed_research(confirmed_id)
    rfp_analysis = (research or {}).get("rfp_analysis", "") or ""
    prompt = (
        f"당신은 공공입찰 제안서 전문 컨설턴트입니다.\n\n"
        f"과업명: {c.get('bid_ntce_nm','')}\n발주처: {c.get('ntce_instt_nm','')}\n\n"
        f"[제안서 설계안]\n{content}\n\n"
        + (f"[전략 내러티브]\n{narrative_text}\n\n" if narrative_text else "")
        + (f"[RFP 분석]\n{rfp_analysis[:2000]}\n\n" if rfp_analysis else "")
        + "다음 관점에서 구체적인 피드백을 작성하세요 (각 항목 3~5문장):\n"
        + "## 1. 평가위원 시각\n## 2. 경쟁력 분석\n## 3. 논리 흐름\n## 4. 보완점 및 제안"
    )
    try:
        import anthropic as _ant
        resp = _ant.Anthropic().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
            timeout=90,
        )
        feedback = resp.content[0].text.strip()
        save_proposal_design(confirmed_id, content, ai_feedback=feedback)
        return jsonify({"ok": True, "feedback": feedback})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/nara/confirmed/<int:confirmed_id>/comment", methods=["POST"])
@login_required
def nara_confirmed_comment_add(confirmed_id):
    if not get_confirmed_by_id(confirmed_id):
        return jsonify({"ok": False, "error": "Not found"}), 404
    data    = request.get_json(force=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": "내용을 입력하세요"})
    cid = add_confirmed_comment(confirmed_id, session["username"], content)
    try:
        c         = get_confirmed_by_id(confirmed_id)
        settings  = get_notification_settings()
        notif_ids = settings.get("comment", [])
        author    = session.get("username", "")
        related   = set()
        if c:
            from database.db import get_connection as _gc
            with _gc() as conn:
                for row in conn.execute("SELECT id, username FROM users WHERE username IN (?,?)",
                                        (c.get("confirmed_by",""), c.get("assignee",""))).fetchall():
                    related.add(row["id"])
        targets = set(notif_ids) | related
        targets.discard(session.get("user_id"))
        nm = (c or {}).get("bid_ntce_nm", "") or f"확정#{confirmed_id}"
        for uid in targets:
            create_notification(uid, "새 댓글", f"{author}: {content[:50]}", f"/nara/confirmed/{confirmed_id}")
    except Exception:
        pass
    return jsonify({"ok": True, "id": cid})


@app.route("/nara/confirmed/<int:confirmed_id>/schedule", methods=["POST"])
@login_required
def nara_confirmed_schedule_add(confirmed_id):
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return jsonify({"ok": False, "error": "Not found"}), 404
    is_ops      = session.get("role") in ("admin", "operator")
    is_assignee = session.get("username") == c.get("assignee")
    if not (is_ops or is_assignee):
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    data       = request.get_json(force=True) or {}
    task_name  = (data.get("task_name") or "").strip()
    if not task_name:
        return jsonify({"ok": False, "error": "업무명을 입력하세요"})
    assignee   = (data.get("assignee") or "").strip()
    due_date   = (data.get("due_date") or "").strip()
    status     = data.get("status", "예정")
    sort_order = int(data.get("sort_order", 0))
    sid = add_confirmed_schedule(confirmed_id, task_name, assignee, due_date, status, sort_order)
    return jsonify({"ok": True, "id": sid})


@app.route("/nara/confirmed/<int:confirmed_id>/schedule/<int:schedule_id>", methods=["PATCH"])
@login_required
def nara_confirmed_schedule_update(confirmed_id, schedule_id):
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return jsonify({"ok": False, "error": "Not found"}), 404
    is_ops      = session.get("role") in ("admin", "operator")
    is_assignee = session.get("username") == c.get("assignee")
    if not (is_ops or is_assignee):
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    data      = request.get_json(force=True) or {}
    task_name = (data.get("task_name") or "").strip()
    assignee  = (data.get("assignee") or "").strip()
    due_date  = (data.get("due_date") or "").strip()
    status    = data.get("status", "예정")
    update_confirmed_schedule(schedule_id, task_name, assignee, due_date, status)
    return jsonify({"ok": True})


@app.route("/nara/confirmed/<int:confirmed_id>/schedule/<int:schedule_id>", methods=["DELETE"])
@login_required
def nara_confirmed_schedule_delete(confirmed_id, schedule_id):
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return jsonify({"ok": False, "error": "Not found"}), 404
    is_ops      = session.get("role") in ("admin", "operator")
    is_assignee = session.get("username") == c.get("assignee")
    if not (is_ops or is_assignee):
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    delete_confirmed_schedule(schedule_id)
    return jsonify({"ok": True})


@app.route("/nara/confirmed/<int:confirmed_id>/bid_info", methods=["POST"])
@login_required
def nara_confirmed_bid_info_save(confirmed_id):
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return jsonify({"ok": False, "error": "Not found"}), 404
    is_ops      = session.get("role") in ("admin", "operator")
    is_assignee = session.get("username") == c.get("assignee")
    if not (is_ops or is_assignee):
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    data = request.get_json(force=True) or {}
    save_confirmed_bid_info(confirmed_id, data, session["username"])
    return jsonify({"ok": True})


@app.route("/narrative/summarize", methods=["POST"])
@login_required
def narrative_summarize():
    data = request.get_json() or {}
    answers = data.get("answers", [])

    if not answers:
        return jsonify({"ok": False})

    import anthropic
    client = anthropic.Anthropic()

    subtitles = []
    for answer in answers:
        if not answer.strip():
            subtitles.append("")
            continue
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{
                "role": "user",
                "content": f"다음 내용의 핵심을 10자 이내 명사형 소제목으로 뽑아줘. 소제목만 출력:\n\n{answer}"
            }]
        )
        subtitles.append(response.content[0].text.strip())

    return jsonify({"ok": True, "subtitles": subtitles})


@app.route("/nara/results")
@login_required
def nara_results_page():
    page  = max(1, int(request.args.get("page", 1)))
    paged = list_nara_results(page=page, per_page=50)
    return render_template("nara_results.html", results=paged["items"], pagination=paged)

@app.route("/nara/keyword", methods=["POST"])
@login_required
def nara_add_keyword():
    from database.db import get_connection
    data    = request.get_json(force=True) or {}
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"ok": False, "error": "키워드를 입력하세요"})
    if len(keyword) > 50:
        return jsonify({"ok": False, "error": "키워드는 50자 이내로 입력하세요"})
    try:
        with get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM nara_keywords").fetchone()[0]
            if count >= 30:
                return jsonify({"ok": False, "error": "키워드는 최대 30개까지 등록 가능합니다"})
            cur    = conn.execute("INSERT INTO nara_keywords (keyword) VALUES (?)", (keyword,))
            new_id = cur.lastrowid
        return jsonify({"ok": True, "id": new_id, "keyword": keyword})
    except Exception:
        return jsonify({"ok": False, "error": "이미 등록된 키워드입니다"})

@app.route("/nara/keyword/<int:keyword_id>", methods=["DELETE"])
@login_required
def nara_delete_keyword(keyword_id):
    delete_nara_keyword(keyword_id)
    return jsonify({"ok": True})

@app.route("/nara/reset_keywords", methods=["POST"])
@operator_or_admin_required
def nara_reset_keywords():
    from database.db import get_connection
    with get_connection() as conn:
        conn.execute("DELETE FROM nara_keywords")
    return jsonify({"ok": True})

@app.route("/nara/reset_all_bids", methods=["POST"])
@operator_or_admin_required
def nara_reset_all_bids():
    from database.db import get_connection
    with get_connection() as conn:
        conn.execute("DELETE FROM nara_bids")
    return jsonify({"ok": True, "message": "전체 공고 삭제 완료"})

@app.route("/nara/scan", methods=["POST"])
@login_required
def nara_manual_scan():
    import threading
    def _scan():
        with app.app_context():
            manual_scan()
    threading.Thread(target=_scan, daemon=True).start()
    return jsonify({"ok": True, "message": "스캔 시작! 잠시 후 목록을 확인하세요."})

@app.route("/nara/bids")
@login_required
def nara_list_bids():
    keyword      = request.args.get("keyword", "").strip()
    hide_expired = request.args.get("hide_expired", "0") == "1"
    search_nm    = request.args.get("search_nm", "").strip()
    search_instt = request.args.get("search_instt", "").strip()
    page         = max(1, int(request.args.get("page", 1)))
    paged        = list_nara_bids(keyword=keyword, search_nm=search_nm,
                                   search_instt=search_instt,
                                   hide_expired=hide_expired, page=page, per_page=50)
    return jsonify({"ok": True, **paged})

@app.route("/nara/settings", methods=["POST"])
@login_required
def nara_save_settings():
    data = request.get_json(force=True) or {}
    try:
        min_budget  = max(0,   int(data.get("min_budget",  0)))
        max_budget  = max(0,   int(data.get("max_budget",  999)))
        period_days = max(1,   int(data.get("period_days", 30)))
        regions     = str(data.get("regions", "전국")).strip() or "전국"
        save_nara_settings(min_budget, max_budget, period_days, regions)
        print(f"[nara 설정 저장] 기간={period_days}일, 예산={min_budget}~{max_budget}억, 지역={regions}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/nara/candidate/add", methods=["POST"])
@login_required
def nara_candidate_add():
    data = request.get_json(force=True) or {}
    try:
        new_id = add_nara_candidate(
            bid_ntce_no    = str(data.get("bid_ntce_no",    "")),
            bid_ntce_nm    = str(data.get("bid_ntce_nm",    "")),
            ntce_instt_nm  = str(data.get("ntce_instt_nm",  "")),
            presmpt_prce   = str(data.get("presmpt_prce",   "")),
            bid_clse_dt    = str(data.get("bid_clse_dt",    "")),
            ntce_url       = str(data.get("ntce_url",       "")),
            matched_keyword= str(data.get("matched_keyword","")),
            reason         = str(data.get("reason",         "")),
            registered_by  = session.get("username", ""),
        )
        if not new_id:
            return jsonify({"ok": False, "error": "이미 후보에 등록된 공고입니다"})
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/nara/candidate/delete/<int:candidate_id>", methods=["POST"])
@login_required
def nara_candidate_delete(candidate_id):
    try:
        delete_nara_candidate(candidate_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/nara/pickup/add", methods=["POST"])
@login_required
def nara_pickup_add():
    data = request.get_json(force=True) or {}
    try:
        bid_nm   = str(data.get("bid_ntce_nm",   ""))
        instt_nm = str(data.get("ntce_instt_nm", ""))
        prce     = str(data.get("presmpt_prce",  ""))
        new_id = add_nara_pickup(
            candidate_id   = int(data.get("candidate_id", 0)),
            bid_ntce_no    = str(data.get("bid_ntce_no",    "")),
            bid_ntce_nm    = bid_nm,
            ntce_instt_nm  = instt_nm,
            presmpt_prce   = prce,
            bid_clse_dt    = str(data.get("bid_clse_dt",    "")),
            ntce_url       = str(data.get("ntce_url",       "")),
            matched_keyword= str(data.get("matched_keyword","")),
            reason         = str(data.get("reason",         "")),
            registered_by  = session.get("username", ""),
        )
        try:
            settings  = get_notification_settings()
            notif_ids = settings.get("pickup_auto", [])
            prce_str  = f"{int(prce):,}원" if prce.isdigit() else (prce + "원" if prce else "-")
            notif_msg = f"공고명: {bid_nm}\n발주처: {instt_nm or '-'}\n추정가격: {prce_str}"
            for uid in notif_ids:
                create_notification(uid, "📌 픽업 공고 등록", notif_msg, "/nara/pickups")
        except Exception:
            pass
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/nara/pickup/delete/<int:pickup_id>", methods=["POST"])
@operator_or_admin_required
def nara_pickup_delete(pickup_id):
    try:
        delete_nara_pickup(pickup_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/nara/confirm/pickup/<int:pickup_id>", methods=["POST"])
@operator_or_admin_required
def nara_confirm_from_pickup(pickup_id):
    data = request.get_json(force=True) or {}
    assignee = str(data.get("assignee", ""))
    try:
        new_id = confirm_nara_pickup(
            pickup_id    = pickup_id,
            confirmed_by = session.get("username", ""),
            notes        = str(data.get("notes", "")),
            assignee     = assignee,
        )
        # 담당자에게 RFP 등록 요청 알림
        try:
            c = get_confirmed_by_id(new_id)
            nm = (c or {}).get("bid_ntce_nm", "") or f"확정#{new_id}"
            from database.db import get_connection as _gc
            with _gc() as conn:
                row = conn.execute("SELECT id FROM users WHERE username=?", (assignee,)).fetchone()
                if row:
                    create_notification(
                        row["id"],
                        "과업 확정",
                        f"[{nm}] 과업이 확정되었습니다. 나라장터에서 RFP 파일을 다운받아 프로인터즈에 등록해주세요.",
                        f"/nara/confirmed/{new_id}/workspace",
                    )
        except Exception:
            pass
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/nara/confirm/<int:candidate_id>", methods=["POST"])
@operator_or_admin_required
def nara_confirm(candidate_id):
    data = request.get_json(force=True) or {}
    try:
        new_id = confirm_nara_candidate(
            candidate_id = candidate_id,
            confirmed_by = session.get("username", ""),
            notes        = str(data.get("notes", "")),
            assignee     = str(data.get("assignee", "")),
        )
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/nara/confirmed/<int:confirmed_id>/upload_rfp", methods=["POST"])
@login_required
def upload_rfp(confirmed_id):
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return jsonify({"ok": False, "error": "Not found"}), 404
    is_ops      = session.get("role") in ("admin", "operator")
    is_assignee = session.get("username") == c.get("assignee")
    if not (is_ops or is_assignee):
        return jsonify({"ok": False, "error": "권한 없음"}), 403

    files = request.files.getlist("files") or request.files.getlist("file")
    if not files or not files[0].filename:
        return jsonify({"ok": False, "error": "파일을 선택하세요"})

    rfp_dir = UPLOAD_DIR / "rfp_uploads" / str(confirmed_id)
    rfp_dir.mkdir(parents=True, exist_ok=True)

    saved_paths = []
    for f in files:
        safe = str(confirmed_id) + "_" + uuid.uuid4().hex[:8] + "_" + f.filename
        dest = rfp_dir / safe
        f.save(str(dest))
        save_confirmed_rfp_file(confirmed_id, f.filename, str(dest), session["username"])
        saved_paths.append(str(dest))

    # 기존 리서치 상태 확인
    existing_research = get_confirmed_research(confirmed_id)
    current_status = existing_research.get("status") if existing_research else None
    if current_status == "running":
        return jsonify({"ok": False, "status": "already_running", "message": "이미 분석 중입니다"})
    if current_status == "done":
        return jsonify({"ok": True, "message": f"{len(saved_paths)}개 업로드 완료. (리서치 이미 완료 — 재분석은 🔄 버튼을 이용하세요)"})

    # 백그라운드 RFP 분석 + 리서치 트리거
    ctx = app.app_context()
    threading.Thread(
        target=run_rfp_research,
        args=(confirmed_id, saved_paths, ctx),
        daemon=True,
    ).start()

    return jsonify({"ok": True, "message": f"{len(saved_paths)}개 업로드 완료. 분석을 시작합니다."})


def run_rfp_research(confirmed_id: int, file_paths: list, app_context) -> None:
    """RFP 파일 분석 + 리서치 실행 후 DB 저장. 백그라운드 스레드에서 실행."""
    with app_context:
        # 중복 실행 방어: 이미 running 상태이면 중단
        existing = get_confirmed_research(confirmed_id)
        if existing and existing.get('status') == 'running':
            print(f"  [workspace] confirmed_id={confirmed_id} 이미 분석 중 — 중복 실행 방지")
            return

        rfp_analysis    = ""
        research_result = ""
        try:
            save_confirmed_research(confirmed_id, 'running', updated_by='system')
            c = get_confirmed_by_id(confirmed_id) or {}
            client_name  = c.get("ntce_instt_nm", "") or ""
            project_name = c.get("bid_ntce_nm", "")   or ""

            # ① RFP 텍스트 추출
            rfp_text = ""
            try:
                from agents.rfp_parser import extract_text as _et
                for fp in file_paths:
                    try:
                        rfp_text += _et(fp) + "\n\n"
                    except Exception as e:
                        print(f"  [workspace] 파일 추출 오류 {fp}: {e}")
            except Exception as e:
                print(f"  [workspace] rfp_parser import 오류: {e}")

            # ② Claude Haiku — RFP 분석 (timeout=60)
            rfp_analysis = ""
            if rfp_text.strip():
                import anthropic as _ant
                _client = _ant.Anthropic()
                analysis_prompt = (
                    f"아래는 입찰 공고 RFP 문서입니다. 다음 항목을 마크다운으로 정리해주세요.\n\n"
                    f"## 과업 개요\n## 주요 요구사항\n## 제출 마감일\n## 제안서 제출 방법\n"
                    f"## PT 일정 (있으면)\n## 가격투찰 일정 (있으면)\n## 평가 기준\n\n"
                    f"[RFP 내용]\n{rfp_text[:8000]}"
                )
                try:
                    resp = _client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=2000,
                        messages=[{"role": "user", "content": analysis_prompt}],
                        timeout=60,
                    )
                    rfp_analysis = resp.content[0].text.strip()
                except Exception as e:
                    rfp_analysis = f"분석 오류: {e}"
                    if "timeout" in str(e).lower() or "Timeout" in type(e).__name__:
                        save_confirmed_research(confirmed_id, 'error',
                                                rfp_analysis='', research_result='API 응답 시간 초과 (RFP 분석)',
                                                updated_by='system')
                        return

            # ② Claude Haiku — 날짜 추출 (timeout=30) → confirmed_bid_info + confirmed_schedule 자동 등재
            if rfp_text.strip():
                try:
                    import re as _re2
                    date_prompt = f"""다음 RFP 텍스트에서 아래 정보를 JSON으로 추출해줘.
없으면 null로.

{{
  "submit_deadline": "YYYY-MM-DD HH:MM",
  "submit_method": "온라인 또는 오프라인",
  "pt_date": "YYYY-MM-DD HH:MM",
  "pt_location": "장소명",
  "price_bid_date": "YYYY-MM-DD HH:MM"
}}

RFP 텍스트:
{rfp_text[:6000]}"""
                    import anthropic as _ant_d
                    _cd = _ant_d.Anthropic()
                    date_resp = _cd.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=400,
                        messages=[{"role": "user", "content": date_prompt}],
                        timeout=30,
                    )
                    date_text = date_resp.content[0].text.strip()
                    _dm = _re2.search(r'\{[\s\S]*\}', date_text)
                    extracted = json.loads(_dm.group()) if _dm else {}

                    existing_info = get_confirmed_bid_info(confirmed_id) or {}
                    merged_info = dict(existing_info)
                    for _key in ("submit_deadline", "submit_method", "pt_date", "pt_location", "price_bid_date"):
                        _val = extracted.get(_key)
                        if _val and not merged_info.get(_key):
                            merged_info[_key] = _val
                    save_confirmed_bid_info(confirmed_id, merged_info, updated_by='자동추출')

                    existing_sched = {s["task_name"]: s for s in list_confirmed_schedule(confirmed_id)}
                    bid_clse_dt = c.get("bid_clse_dt", "") or ""

                    def _upsert_schedule(task_name, due_date, assignee, sort_order):
                        if not due_date:
                            return
                        if task_name in existing_sched:
                            row = existing_sched[task_name]
                            update_confirmed_schedule(
                                row["id"], task_name,
                                row.get("assignee") or assignee,
                                due_date, row.get("status", "예정"),
                            )
                        else:
                            add_confirmed_schedule(confirmed_id, task_name, assignee, due_date, "예정", sort_order)

                    _upsert_schedule("입찰 마감",   bid_clse_dt, "", 1)
                    _upsert_schedule("제안서 제출", merged_info.get("submit_deadline") or "",
                                     merged_info.get("submit_method") or "", 2)
                    _upsert_schedule("PT 발표",    merged_info.get("pt_date") or "",
                                     merged_info.get("pt_location") or "", 3)
                    _upsert_schedule("가격투찰",   merged_info.get("price_bid_date") or "", "", 4)

                    print(f"[RFP분석] 일정 자동 등재 완료: {confirmed_id}")
                except Exception as _de:
                    print(f"  [workspace] 날짜 추출 오류: {_de}")

            # ③ Perplexity 병렬 검색 + Claude Sonnet 리서치 (timeout=120)
            research_result = ""
            try:
                import os, requests as _req, concurrent.futures as _cfut
                perplexity_key = os.environ.get("PERPLEXITY_API_KEY", "")
                queries = [
                    f"{client_name} 2025 최신 현황 주요 정책 사업",
                    f"{client_name} 2024 2025 최근 이슈 뉴스",
                ]
                px_parts = []
                if perplexity_key:
                    _px_headers = {"Authorization": f"Bearer {perplexity_key}", "Content-Type": "application/json"}

                    def _fetch_px(q):
                        try:
                            r = _req.post(
                                "https://api.perplexity.ai/chat/completions",
                                headers=_px_headers,
                                json={"model": "sonar-pro",
                                      "messages": [{"role": "user", "content": q}],
                                      "max_tokens": 800},
                                timeout=25,
                            )
                            r.raise_for_status()
                            answer = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                            return f"[{q}]\n{answer}" if answer else None
                        except Exception:
                            return None

                    with _cfut.ThreadPoolExecutor(max_workers=2) as _ex:
                        for result in _ex.map(_fetch_px, queries):
                            if result:
                                px_parts.append(result)

                px_block = "\n\n".join(px_parts) if px_parts else "(Perplexity 검색 결과 없음)"

                import anthropic as _ant2
                _c2 = _ant2.Anthropic()
                research_prompt = (
                    f"당신은 공공입찰 전략 전문가입니다.\n"
                    f"발주처: {client_name}\n과업명: {project_name}\n\n"
                    f"[검색 결과]\n{px_block}\n\n"
                    f"아래 항목을 마크다운으로 작성하세요 (각 항목 최소 200자):\n"
                    f"## ① 기관 특성/정책 분석\n## ② 과업 맥락 및 배경\n"
                    f"## ③ 유사사례 및 시장 트렌드\n## ④ 경쟁 환경 분석\n## ⑤ 전략 방향 제언"
                )
                try:
                    resp2 = _c2.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=4000,
                        messages=[{"role": "user", "content": research_prompt}],
                        timeout=120,
                    )
                    research_result = resp2.content[0].text.strip()
                except Exception as e:
                    research_result = f"리서치 오류: {e}"
                    if "timeout" in str(e).lower() or "Timeout" in type(e).__name__:
                        save_confirmed_research(confirmed_id, 'error',
                                                rfp_analysis=rfp_analysis,
                                                research_result='API 응답 시간 초과 (리서치 종합)',
                                                updated_by='system')
                        return
            except Exception as e:
                research_result = f"리서치 오류: {e}"

            save_confirmed_research(
                confirmed_id,
                status='done',
                rfp_analysis=rfp_analysis,
                research_result=research_result,
                updated_by='system',
            )
            print(f"  [workspace] confirmed_id={confirmed_id} 리서치 완료")
        except Exception as e:
            print(f"  [workspace] run_rfp_research 오류: {e}")
            try:
                save_confirmed_research(confirmed_id, 'error',
                                        rfp_analysis=rfp_analysis,
                                        research_result=research_result or str(e),
                                        updated_by='system')
            except Exception:
                pass


@app.route("/nara/confirmed/<int:confirmed_id>/research_status")
@login_required
def research_status(confirmed_id):
    return jsonify({"ok": True, "research": get_confirmed_research(confirmed_id)})


@app.route("/nara/confirmed/<int:confirmed_id>/research_fix", methods=["POST"])
@login_required
def research_fix(confirmed_id):
    """running 상태로 고착된 리서치를 done으로 수정"""
    research = get_confirmed_research(confirmed_id)
    if research and research.get("status") == "running" and research.get("research_result"):
        save_confirmed_research(
            confirmed_id,
            status="done",
            rfp_analysis=research.get("rfp_analysis", ""),
            research_result=research.get("research_result", ""),
            updated_by=session.get("username", "system"),
        )
        return jsonify({"ok": True, "message": "상태 수정 완료"})
    return jsonify({"ok": False, "error": "수정 불필요"})


@app.route("/nara/confirmed/<int:confirmed_id>/rerun_research", methods=["POST"])
@login_required
def rerun_research(confirmed_id):
    """기존 리서치 상태와 무관하게 강제 재실행 (담당자/admin/operator만)"""
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return jsonify({"ok": False, "error": "Not found"}), 404
    is_ops      = session.get("role") in ("admin", "operator")
    is_assignee = session.get("username") == c.get("assignee")
    if not (is_ops or is_assignee):
        return jsonify({"ok": False, "error": "권한 없음"}), 403
    rfp_files = list_confirmed_rfp_files(confirmed_id)
    if not rfp_files:
        return jsonify({"ok": False, "error": "업로드된 RFP 파일이 없습니다"})
    file_paths = [f["filepath"] for f in rfp_files if f.get("filepath")]
    if not file_paths:
        return jsonify({"ok": False, "error": "파일 경로를 찾을 수 없습니다"})
    ctx = app.app_context()
    threading.Thread(
        target=run_rfp_research,
        args=(confirmed_id, file_paths, ctx),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/nara/confirmed/<int:confirmed_id>/workspace")
@login_required
def confirmed_workspace(confirmed_id):
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return "Not found", 404
    research  = get_confirmed_research(confirmed_id)
    narrative = get_confirmed_narrative(confirmed_id)
    rfp_files = list_confirmed_rfp_files(confirmed_id)
    is_ops      = session.get("role") in ("admin", "operator")
    is_assignee = session.get("username") == c.get("assignee")
    can_edit    = is_ops or is_assignee
    narrative_qa = None
    if narrative:
        try:
            parsed = json.loads(narrative["content"])
            if isinstance(parsed, dict) and any(parsed.values()):
                narrative_qa = parsed
        except Exception:
            pass
    bid_info = get_confirmed_bid_info(confirmed_id) or {}
    schedule = get_or_create_default_schedules(
        confirmed_id, c.get("bid_clse_dt", "") or ""
    )
    users = list_users()
    return render_template(
        "confirmed_workspace.html",
        c=c, research=research, narrative=narrative,
        narrative_qa=narrative_qa, rfp_files=rfp_files,
        can_edit=can_edit, is_ops=is_ops,
        bid_info=bid_info, schedule=schedule, users=users,
    )


@app.route("/nara/confirmed/<int:confirmed_id>/narrative_share")
def narrative_share(confirmed_id):
    c = get_confirmed_by_id(confirmed_id)
    if not c:
        return "Not found", 404
    narrative = get_confirmed_narrative(confirmed_id)
    narrative_qa = None
    if narrative:
        try:
            parsed = json.loads(narrative["content"])
            if isinstance(parsed, dict) and any(parsed.values()):
                narrative_qa = parsed
        except Exception:
            pass
    nq_labels = [
        ("q1", "우리 회사가 본 과업을 하는데 내세울 어떤 경쟁력이 있을까요?"),
        ("q2", "본 과업은 기존에 어떤 문제나 위기를 갖고 있나요?"),
        ("q3", "문제를 타개하고 좋은 성과를 이루어내기 위해 어떤 전략이 필요할까요?"),
        ("q4", "추구하는 전략을 사람들에게 각인시킬 직관적인 컨셉은 무엇인가요?"),
        ("q5", "컨셉에 맞게 실행할 콘텐츠 아이디어는 어떤 것인가요?"),
        ("q6", "운영이나 관리 부문에 내세울만한 특별한 장점은 어떤 것인가요?"),
        ("q7", "추가로 기재할 중요한 내용이 있다면 무엇인가요?"),
    ]
    return render_template(
        "narrative_share.html",
        c=c, narrative=narrative,
        narrative_qa=narrative_qa, nq_labels=nq_labels,
    )


@app.route("/nara/candidate/<int:candidate_id>/comment", methods=["POST"])
@login_required
def nara_add_comment(candidate_id):
    data    = request.get_json(force=True) or {}
    content = str(data.get("content", "")).strip()
    if not content:
        return jsonify({"ok": False, "error": "내용을 입력하세요"})
    try:
        new_id = add_candidate_comment(
            candidate_id = candidate_id,
            author       = session.get("username", ""),
            content      = content,
        )
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/nara/candidate/<int:candidate_id>/comments")
@login_required
def nara_list_comments(candidate_id):
    comments = list_candidate_comments(candidate_id)
    return jsonify({"ok": True, "comments": comments})

@app.route("/nara/users")
@operator_or_admin_required
def nara_users():
    from database.db import get_connection
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, username FROM users ORDER BY username"
        ).fetchall()
    return jsonify({"ok": True, "users": [dict(r) for r in rows]})

@app.route("/nara/result/<int:confirmed_id>", methods=["POST"])
@login_required
def nara_result_add(confirmed_id):
    data = request.get_json(force=True) or {}
    try:
        add_nara_result(
            confirmed_id = confirmed_id,
            result       = str(data.get("result", "미정")),
            notes        = str(data.get("notes", "")),
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/notifications")
@login_required
def notifications_page():
    uid    = session.get("user_id")
    notifs = list_notifications(uid)
    if request.args.get("json") == "1":
        return jsonify({"notifications": notifs})
    return render_template("notifications.html", notifications=notifs)

@app.route("/notifications/unread_count")
@login_required
def notifications_unread_count():
    return jsonify({"count": count_unread(session.get("user_id"))})

@app.route("/notifications/<int:nid>/read", methods=["POST"])
@login_required
def notification_read(nid):
    mark_notification_read(nid, session.get("user_id"))
    return jsonify({"ok": True})

@app.route("/notifications/read_all", methods=["POST"])
@login_required
def notifications_read_all():
    mark_all_read(session.get("user_id"))
    return jsonify({"ok": True})

@app.route("/notification_settings", methods=["GET"])
@operator_or_admin_required
def notification_settings_page():
    settings = get_notification_settings()
    from database.db import get_connection as _gc
    with _gc() as conn:
        users = [dict(r) for r in conn.execute("SELECT id, username FROM users ORDER BY username").fetchall()]
    return render_template("notification_settings.html", settings=settings, users=users)

@app.route("/notification_settings", methods=["POST"])
@operator_or_admin_required
def notification_settings_save():
    data = request.get_json(force=True) or {}
    for trigger_type in ['candidate_manual', 'pickup_auto', 'deadline', 'comment', 'completion_approval']:
        user_ids = [int(x) for x in data.get(trigger_type, []) if str(x).isdigit()]
        save_notification_settings(trigger_type, user_ids)
    return jsonify({"ok": True})

@app.route("/nara/notify_candidates", methods=["POST"])
@operator_or_admin_required
def nara_notify_candidates():
    from database.db import get_connection as _gc
    last_sent = get_last_notification_time('candidate_manual')
    with _gc() as conn:
        if last_sent:
            candidates = conn.execute(
                """SELECT bid_ntce_no, bid_ntce_nm, ntce_instt_nm
                   FROM nara_candidates
                   WHERE created_at > ?
                   ORDER BY created_at DESC""",
                (last_sent,),
            ).fetchall()
        else:
            candidates = conn.execute(
                """SELECT bid_ntce_no, bid_ntce_nm, ntce_instt_nm
                   FROM nara_candidates
                   WHERE date(created_at) = date('now', 'localtime')
                   ORDER BY created_at DESC"""
            ).fetchall()
    if not candidates:
        return jsonify({"ok": False, "error": "이전 알림 이후 신규 등록된 후보가 없습니다"})
    count   = len(candidates)
    lines   = [f"• {r['bid_ntce_nm'] or r['bid_ntce_no']}" for r in candidates[:5]]
    message = "\n".join(lines)
    if count > 5:
        message += f"\n외 {count - 5}건"
    settings = get_notification_settings()
    targets  = settings.get("candidate_manual", [])
    for uid in targets:
        create_notification(uid, f"📋 신규 후보 {count}건", message, "/nara/candidates")
    record_notification_sent('candidate_manual')
    return jsonify({"ok": True, "sent": len(targets)})


@app.route("/nara/search_by_no", methods=["POST"])
@login_required
def nara_search_by_no():
    import threading
    data   = request.get_json(force=True) or {}
    bid_no = str(data.get("bid_no", "")).strip()
    bid_no = bid_no.split("-")[0].strip()  # R26BK01514926-000 → R26BK01514926
    if not bid_no:
        return jsonify({"ok": False, "error": "공고번호를 입력하세요"})

    result_box = [None]
    def _search():
        result_box[0] = fetch_bid_by_no(bid_no)
    t = threading.Thread(target=_search, daemon=True)
    t.start()
    t.join(timeout=30)
    if t.is_alive():
        return jsonify({"ok": False, "error": "검색 시간이 초과되었습니다 (20초). 잠시 후 다시 시도하세요."})
    if result_box[0]:
        return jsonify({"ok": True, "bid": result_box[0]})
    return jsonify({"ok": False, "error": "공고를 찾을 수 없습니다"})

@app.route("/nara/add_to_candidates", methods=["POST"])
@login_required
def nara_add_to_candidates():
    data = request.get_json(force=True) or {}
    try:
        new_id = add_nara_candidate(
            bid_ntce_no    = str(data.get("bid_ntce_no",    "")),
            bid_ntce_nm    = str(data.get("bid_ntce_nm",    "")),
            ntce_instt_nm  = str(data.get("ntce_instt_nm",  "")),
            presmpt_prce   = str(data.get("presmpt_prce",   "")),
            bid_clse_dt    = str(data.get("bid_clse_dt",    "")),
            ntce_url       = str(data.get("ntce_url",       "")),
            matched_keyword= str(data.get("matched_keyword","직접검색")),
            reason         = str(data.get("reason",         "")),
            registered_by  = session.get("username", ""),
        )
        if not new_id:
            return jsonify({"ok": False, "error": "이미 후보에 등록된 공고입니다"})
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    init_db()
    init_users()
    _ensure_worker()
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_ENV", "production") != "production"
    app.run(host="0.0.0.0", port=port, threaded=True, debug=debug)

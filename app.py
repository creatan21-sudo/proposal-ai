# app.py
# Flask 웹 서비스 — 다중 사용자 + 작업 큐 지원

import os
print(f"[startup] ANTHROPIC_API_KEY: {'SET' if os.environ.get('ANTHROPIC_API_KEY') else 'NOT SET'}", flush=True)

import dataclasses
import json
import os
import threading
import uuid
from collections import deque
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, Response, abort, flash, jsonify, redirect,
    render_template, request, send_file, session, url_for,
)
from werkzeug.utils import secure_filename

from core.dna import create_dna
from database.db import (
    change_password, create_user, delete_user, get_case_detail, get_connection,
    get_telegram_chat_id, get_user_by_id, init_db, init_users, list_users,
    save_case, set_telegram_chat_id, verify_user,
    hide_case, unhide_case,
    save_learning_case, list_learning_cases, delete_learning_case,
)
from output.txt_writer import write_txt
from utils.telegram_notify import send_telegram

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "proposal-ai-web-secret-2024")

_IS_PRODUCTION = os.environ.get("FLASK_ENV", "production") == "production"
_default_upload = "/tmp/uploads" if _IS_PRODUCTION else str(Path(__file__).parent / "uploads")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", _default_upload))

# 시작 시 필요한 폴더 자동 생성
for _d in [UPLOAD_DIR, Path(__file__).parent / "database", Path(__file__).parent / "output" / "proposals"]:
    _d.mkdir(parents=True, exist_ok=True)

# gunicorn 등 외부 서버 기동 시에도 DB/테이블이 반드시 존재하도록 초기화
with app.app_context():
    init_db()
    init_users()

VIDEO_TYPES = ["홍보영상", "다큐멘터리", "교육영상", "캠페인영상", "뉴스형영상"]
ALLOWED_EXT     = {".hwp", ".hwpx", ".pdf", ".txt"}
ALLOWED_REF_EXT = {".hwp", ".hwpx", ".pdf", ".txt", ".docx", ".pptx"}


def _safe_upload_name(original_filename: str, forced_ext: str) -> str:
    """업로드 파일명을 안전하게 변환하되 원본 확장자를 반드시 보존.

    - secure_filename()은 한글 등 비ASCII 문자를 제거해 확장자가 사라질 수 있음.
    - 확장자를 forced_ext(소문자)로 강제 통일하고, base만 secure_filename으로 처리.
    """
    # base 부분(확장자 제외)만 안전하게 처리
    stem = Path(original_filename).stem  # 확장자 없는 파일명
    safe_stem = secure_filename(stem)
    if not safe_stem:
        safe_stem = "upload"
    return safe_stem + forced_ext  # forced_ext는 항상 소문자

# ── 인메모리 파이프라인 세션
_sessions: dict = {}
_sessions_lock = threading.Lock()

# ── 작업 큐 (FIFO, 메모리 전용 — 서버 재시작 시 자동 초기화)
_job_queue: deque = deque()   # session_id 목록 (queued 순서 보존)
_queue_lock = threading.Lock()
_queue_notify = threading.Event()
_worker_started = False

# ── 동시 실행 제어
_MAX_CONCURRENT = 3           # 동시 실행 최대 작업 수
_active_sids: set = set()     # 현재 실행 중인 sid 집합
_active_lock = threading.Lock()

# ── 작업 타임아웃
_JOB_TIMEOUT_SEC = 600        # 10분 이상 running 상태면 강제 종료

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

        threading.Thread(target=_run_job, args=(sid,), daemon=True).start()


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
        _push(sid, {"type": "pipeline_error", "message": str(e)})
        with _sessions_lock:
            s = _sessions.get(sid)
            if s:
                s["status"] = "error"
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
    """10분 이상 running 상태인 작업을 자동 타임아웃 처리."""
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
            if (sess.get("status") == "running"
                    and (now - sess.get("started_at", now)) > _JOB_TIMEOUT_SEC):
                print(f"  [타임아웃] {sid} {_JOB_TIMEOUT_SEC}초 초과 → 강제 종료")
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
            if s:
                s["status"] = "waiting_confirm"
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

    try:
        results = wp_run(
            dna, push, wait_confirm,
            rfp_file=rfp_file, concept=concept,
            start_step_key=sess.get("retry_from"),
            prior_results=sess.get("results") or {},
            notify_fn=notify_fn,
        )

        txt_path = None
        if "__aborted_at__" not in results:
            try:
                txt_path = write_txt(dna, results)
            except Exception as e:
                push({"type": "log", "message": f"TXT 생성 오류: {e}"})
            # 파이프라인 완료 후 케이스 DNA/결과 업데이트
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

        # case_saved 먼저 → pipeline_done 나중 (PPT 버튼 활성화 타이밍 보장)
        if saved_case_id:
            push({"type": "case_saved", "case_id": saved_case_id})
        push({"type": "pipeline_done"})

        with _sessions_lock:
            s = _sessions.get(sid)
            if s:
                s["status"]   = "done"
                s["txt_path"] = txt_path
                s["results"]  = results
                s["sse_event"].set()

    except Exception as e:
        push({"type": "pipeline_error", "message": str(e)})
        with _sessions_lock:
            s = _sessions.get(sid)
            if s:
                s["status"] = "error"
                s["sse_event"].set()


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


# ─────────────────────────────────────────────
@app.route("/health")
def health():
    return "OK", 200


# 인증 라우트
# ─────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = verify_user(username, password)
        if user:
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            session["is_admin"] = bool(user["is_admin"])
            return redirect(url_for("index"))
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
    return render_template("index.html", video_types=VIDEO_TYPES)


# ─────────────────────────────────────────────
# 파이프라인 시작
# ─────────────────────────────────────────────

@app.route("/start", methods=["POST"])
@login_required
def start():
    client        = request.form.get("client", "").strip()
    project       = request.form.get("project", "").strip()
    video_type    = request.form.get("video_type", "홍보영상")
    quantity      = int(request.form.get("quantity") or 1)
    duration      = request.form.get("duration", "3분").strip() or "3분"
    pages         = int(request.form.get("pages") or 30)
    concept       = request.form.get("concept", "").strip() or None
    budget        = request.form.get("budget", "").strip()
    deadline      = request.form.get("deadline", "").strip()
    target        = request.form.get("target", "").strip()
    key_message   = request.form.get("key_message", "").strip()
    user_direction = request.form.get("user_direction", "").strip()
    reference_case_id = int(request.form.get("reference_case_id") or 0)

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

    # 참고 제안서 처리
    ref_structure = ""
    ref_f = request.files.get("ref_proposal_file")
    if ref_f and ref_f.filename:
        orig_ref = ref_f.filename
        ext = Path(orig_ref).suffix.lower()
        if ext in ALLOWED_REF_EXT:
            safe_ref = "ref_" + _safe_upload_name(orig_ref, ext)
            ref_path = str(UPLOAD_DIR / safe_ref)
            ref_f.save(ref_path)
            print(f"  [업로드] 참고 제안서: {orig_ref!r} → 저장: {ref_path}")
            try:
                from agents.rfp_parser import parse_reference_proposal
                ref_structure = parse_reference_proposal(ref_path)
            except Exception as e:
                print(f"[경고] 참고 제안서 분석 실패: {e}")

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
    if ref_structure:
        dna.reference_structure = ref_structure

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

    sid = str(uuid.uuid4())
    with _sessions_lock:
        _sessions[sid] = {
            "status":           "queued",
            "user_id":          session["user_id"],
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
        while True:
            with _sessions_lock:
                sess = _sessions.get(sid)
            if not sess:
                payload = json.dumps({"type": "server_restart"}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                break
            events = sess["events"]
            while idx < len(events):
                data = json.dumps(events[idx], ensure_ascii=False)
                yield f"data: {data}\n\n"
                idx += 1
            if sess["status"] in ("done", "error"):
                break
            fired = sess["sse_event"].wait(timeout=30)
            sess["sse_event"].clear()
            # Railway 60초 타임아웃 방지: 새 이벤트 없으면 keepalive 핑
            if not fired:
                yield "data: {\"type\": \"ping\"}\n\n"

    return Response(
        generate(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/confirm/<sid>", methods=["POST"])
@login_required
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


@app.route("/rfp_analyze", methods=["POST"])
@login_required
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
@login_required
def retry_pipeline(sid):
    """실패/중단된 파이프라인을 재시작. 중단 지점부터 재개."""
    with _sessions_lock:
        sess = _sessions.get(sid)
    if not sess:
        return jsonify({"ok": False, "error": "세션 없음"}), 404
    if sess["user_id"] != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False}), 403
    if sess["status"] not in ("error", "done"):
        return jsonify({"ok": False, "error": "재시도 불가 상태"}), 400

    with _sessions_lock:
        prior = dict(sess.get("results", {}))
        aborted_at = prior.pop("__aborted_at__", None)
        sess["results"]       = prior
        sess["retry_from"]    = aborted_at   # None 이면 처음부터
        sess["status"]        = "queued"
        sess["sse_event"]     = threading.Event()
        sess["confirm_event"] = threading.Event()
        sess["user_input"]    = None
        sess["txt_path"]      = None
        # 기존 이벤트 유지 — 재시도 구분자 추가
        sess["events"].append({"type": "retry_start", "step": aborted_at})
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
# 이력 페이지
# ─────────────────────────────────────────────

@app.route("/history")
@login_required
def history():
    q           = request.args.get("q", "").strip()
    view_all    = request.args.get("all") == "1"
    show_hidden = request.args.get("show_hidden") == "1"

    with get_connection() as conn:
        _step_subq = (
            "("
            "(CASE WHEN EXISTS(SELECT 1 FROM rfp_analyses      WHERE client_name=r.client_name AND project_name=r.project_name) THEN 1 ELSE 0 END)+"
            "(CASE WHEN EXISTS(SELECT 1 FROM research_results  WHERE client_name=r.client_name AND project_name=r.project_name) THEN 1 ELSE 0 END)+"
            "(CASE WHEN EXISTS(SELECT 1 FROM strategy_results  WHERE client_name=r.client_name AND project_name=r.project_name) THEN 1 ELSE 0 END)+"
            "(CASE WHEN EXISTS(SELECT 1 FROM creative_results  WHERE client_name=r.client_name AND project_name=r.project_name) THEN 1 ELSE 0 END)+"
            "(CASE WHEN EXISTS(SELECT 1 FROM plan_results      WHERE client_name=r.client_name AND project_name=r.project_name) THEN 1 ELSE 0 END)+"
            "(CASE WHEN EXISTS(SELECT 1 FROM script_results    WHERE client_name=r.client_name AND project_name=r.project_name) THEN 1 ELSE 0 END)+"
            "(CASE WHEN EXISTS(SELECT 1 FROM marketing_results WHERE client_name=r.client_name AND project_name=r.project_name) THEN 1 ELSE 0 END)+"
            "(CASE WHEN EXISTS(SELECT 1 FROM final_proposals   WHERE client_name=r.client_name AND project_name=r.project_name) THEN 1 ELSE 0 END)"
            ") AS step_count"
        )
        base = (
            f"SELECT r.id, r.created_at, r.client_name, r.project_name, "
            f"r.video_type, r.budget, r.agency_type, r.user_id, u.username, "
            f"{_step_subq} "
            "FROM rfp_cases r LEFT JOIN users u ON r.user_id=u.id "
        )
        conditions = []
        params     = []

        # 관리자이면서 전체 보기가 아닐 때 or 일반 사용자 → 내 것만
        if not (session.get("is_admin") and view_all):
            conditions.append("r.user_id=?")
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

    cases = [dict(r) for r in rows]
    return render_template(
        "history.html", cases=cases, q=q,
        view_all=view_all,
        show_hidden=show_hidden,
        is_admin=session.get("is_admin", False),
    )


@app.route("/history/<int:case_id>")
@login_required
def history_detail(case_id):
    """제안서 상세 페이지."""
    detail = get_case_detail(case_id)
    if not detail:
        abort(404)
    case = detail["case"]
    if case.get("user_id") != session["user_id"] and not session.get("is_admin"):
        abort(403)
    return render_template("detail.html", detail=detail, case_id=case_id)


@app.route("/history/<int:case_id>/download")
@login_required
def history_download(case_id):
    """이력에서 TXT 다운로드 — dna_json 기반 재생성."""
    import io
    detail = get_case_detail(case_id)
    if not detail:
        abort(404)
    case = detail["case"]
    if case.get("user_id") != session["user_id"] and not session.get("is_admin"):
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
        "rfp_analysis":  "STEP 0  RFP 분석",
        "research":      "STEP 1  리서치",
        "narrative":     "STEP 1.5  내러티브",
        "strategy":      "STEP 2  전략 수립",
        "creative":      "STEP 3  컨셉 개발",
        "plan":          "STEP 4  실행 기획",
        "script":        "STEP 5  대본",
        "marketing":     "STEP 6  마케팅 전략",
        "final_proposal":"STEP 7  최종 제안서",
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
@login_required
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
@login_required
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
@login_required
def unhide_case_route(case_id):
    with get_connection() as conn:
        row = conn.execute("SELECT user_id FROM rfp_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        abort(404)
    if row["user_id"] != session["user_id"] and not session.get("is_admin"):
        abort(403)
    unhide_case(case_id)
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
        ("marketing",      "marketing_results"),
        ("final_proposal", "final_proposals"),
    ]
    PIPELINE_ORDER = [
        "rfp_analysis", "research", "narrative", "strategy",
        "creative", "plan", "script", "marketing", "final_proposal",
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
    if dna.narrative:
        completed.add("narrative")

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
def queue_status():
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


@app.route("/admin")
@admin_required
def admin():
    users = list_users()
    return render_template("admin.html", users=users)


@app.route("/admin/add-user", methods=["POST"])
@admin_required
def admin_add_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    is_admin = request.form.get("is_admin") == "1"
    error    = None

    if not username or not password:
        error = "아이디와 비밀번호를 입력하세요."
    elif len(password) < 4:
        error = "비밀번호는 4자 이상이어야 합니다."
    else:
        try:
            create_user(username, password, is_admin)
        except Exception as e:
            error = f"계정 생성 실패: {e}"

    if error:
        users = list_users()
        return render_template("admin.html", users=users, error=error)
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
            ok = send_telegram(chat_id, f"✅ ProposalAI 텔레그램 알림이 연결되었습니다.\n계정: {session['username']}")
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


@app.route("/ppt/start", methods=["POST"])
@login_required
def ppt_start():
    data    = request.get_json(force=True) or {}
    case_id = int(data.get("case_id", 0))
    pages   = max(10, min(60, int(data.get("pages", 30))))

    if not case_id:
        return jsonify({"ok": False, "error": "case_id 필요"}), 400

    detail = get_case_detail(case_id)
    if not detail:
        return jsonify({"ok": False, "error": "케이스 없음"}), 404
    if detail["case"].get("user_id") != session["user_id"] and not session.get("is_admin"):
        return jsonify({"ok": False}), 403

    job_id  = str(uuid.uuid4())
    case    = detail["case"]
    user_id = session["user_id"]
    fname   = f"{case['client_name']}_{case['project_name'][:20]}_제안서.pptx"

    with _ppt_jobs_lock:
        _ppt_jobs[job_id] = {
            "status":     "running",
            "events":     [],
            "pptx_bytes": None,
            "filename":   fname,
            "sse_event":  threading.Event(),
            "user_id":    user_id,
        }

    def _worker():
        try:
            def progress_cb(message, current, total):
                _ppt_push(job_id, {
                    "type":    "ppt_progress",
                    "message": message,
                    "current": current,
                    "total":   total,
                })

            from agents import ppt_generator
            pptx_bytes = ppt_generator.run(detail, pages, progress_cb)

            with _ppt_jobs_lock:
                job = _ppt_jobs.get(job_id)
                if job:
                    job["status"]     = "done"
                    job["pptx_bytes"] = pptx_bytes

            _ppt_push(job_id, {
                "type":         "ppt_done",
                "download_url": f"/ppt/download/{job_id}",
            })

            # 텔레그램 알림
            chat_id = get_telegram_chat_id(user_id)
            if chat_id:
                try:
                    send_telegram(chat_id,
                        f"📊 <b>{case['project_name']}</b> PPT 생성 완료!\n"
                        f"다운로드: /history/{case_id}")
                except Exception:
                    pass

        except Exception as e:
            _ppt_push(job_id, {"type": "ppt_error", "message": str(e)})
            with _ppt_jobs_lock:
                job = _ppt_jobs.get(job_id)
                if job:
                    job["status"] = "error"

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


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
    return send_file(
        buf,
        as_attachment=True,
        download_name=job["filename"],
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


# ─────────────────────────────────────────────
# 큐 상태 API (폴링 폴백용)
# ─────────────────────────────────────────────

@app.route("/learning", methods=["GET", "POST"])
@login_required
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
# 실행
# ─────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    init_users()
    _ensure_worker()
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_ENV", "production") != "production"
    app.run(host="0.0.0.0", port=port, threaded=True, debug=debug)

# database/db.py
# 역할: SQLite DB 연결 및 케이스 누적 관리
# - 생성된 제안서를 케이스로 저장 (쓸수록 DB 고도화)
# - 유사 발주처/사업 케이스 조회
# - 테이블 초기화 및 마이그레이션

import sqlite3
from datetime import datetime
from pathlib import Path
from config import DB_PATH


def get_connection() -> sqlite3.Connection:
    """DB 연결 반환. WAL 모드 + 30초 쓰기 타임아웃으로 동시 접근 충돌 방지."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL: 읽기/쓰기 동시 접근 허용, 쓰기 충돌 최소화
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL에서 안전하면서 빠른 설정
    return conn


def init_db() -> None:
    """테이블 초기화. 최초 실행 시 스키마 생성."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS rfp_cases (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT    NOT NULL,
                client_name TEXT    NOT NULL,
                project_name TEXT   NOT NULL,
                video_type  TEXT    NOT NULL,
                agency_type TEXT    DEFAULT '',
                budget      TEXT    DEFAULT '',
                deadline    TEXT    DEFAULT '',
                dna_json    TEXT    NOT NULL,
                result_json TEXT    DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS rfp_analyses (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT    NOT NULL,
                client_name     TEXT    NOT NULL,
                project_name    TEXT    NOT NULL,
                agency_type     TEXT    DEFAULT '',
                evaluation_items_json TEXT DEFAULT '[]',
                top_keywords_json     TEXT DEFAULT '[]',
                core_tasks_json       TEXT DEFAULT '[]',
                forbidden_notes_json  TEXT DEFAULT '[]',
                agency_tone_hint      TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS final_proposals (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at               TEXT    NOT NULL,
                client_name              TEXT    NOT NULL,
                project_name             TEXT    NOT NULL,
                consistency_score        REAL    DEFAULT 0.0,
                evaluation_coverage_json TEXT    DEFAULT '{}',
                issues_json              TEXT    DEFAULT '[]',
                company_profile_json     TEXT    DEFAULT '{}',
                pt_script_json           TEXT    DEFAULT '{}',
                qa_prep_json             TEXT    DEFAULT '[]',
                final_proposal_json      TEXT    DEFAULT '{}',
                dna_snapshot_json        TEXT    DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS platform_results (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at            TEXT    NOT NULL,
                client_name           TEXT    NOT NULL,
                project_name          TEXT    NOT NULL,
                case_id               INTEGER DEFAULT 0,
                platforms_json        TEXT    DEFAULT '[]',
                youtube_strategy      TEXT    DEFAULT '',
                sns_strategy          TEXT    DEFAULT '',
                edit_versions_json    TEXT    DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS marketing_results (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at               TEXT    NOT NULL,
                client_name              TEXT    NOT NULL,
                project_name             TEXT    NOT NULL,
                platforms_json           TEXT    DEFAULT '[]',
                youtube_strategy_json    TEXT    DEFAULT '{}',
                shortform_strategy_json  TEXT    DEFAULT '{}',
                sns_strategy_json        TEXT    DEFAULT '{}',
                influencer_strategy_json TEXT    DEFAULT '{}',
                kpi_json                 TEXT    DEFAULT '{}',
                reporting_system         TEXT    DEFAULT '',
                marketing_budget_json    TEXT    DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS script_results (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at       TEXT    NOT NULL,
                client_name      TEXT    NOT NULL,
                project_name     TEXT    NOT NULL,
                episode_number   INTEGER DEFAULT 0,
                episode_title    TEXT    DEFAULT '',
                format           TEXT    DEFAULT 'longform',
                duration         TEXT    DEFAULT '',
                script_json      TEXT    DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS plan_results (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at               TEXT    NOT NULL,
                client_name              TEXT    NOT NULL,
                project_name             TEXT    NOT NULL,
                is_youtube_channel       INTEGER DEFAULT 0,
                episodes_json            TEXT    DEFAULT '[]',
                production_schedule_json TEXT    DEFAULT '[]',
                team_composition_json    TEXT    DEFAULT '{}',
                budget_plan_json         TEXT    DEFAULT '{}',
                series_plan_json         TEXT    DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS creative_results (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at              TEXT    NOT NULL,
                client_name             TEXT    NOT NULL,
                project_name            TEXT    NOT NULL,
                agency_type             TEXT    DEFAULT '',
                concept                 TEXT    DEFAULT '',
                concept_description     TEXT    DEFAULT '',
                confirmed_slogan        TEXT    DEFAULT '',
                slogans_json            TEXT    DEFAULT '[]',
                tone_keywords_json      TEXT    DEFAULT '[]',
                tone_description        TEXT    DEFAULT '',
                forbidden_json          TEXT    DEFAULT '[]',
                visual_direction        TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS strategy_results (
                id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at                TEXT    NOT NULL,
                client_name               TEXT    NOT NULL,
                project_name              TEXT    NOT NULL,
                core_problem              TEXT    DEFAULT '',
                crisis_statement          TEXT    DEFAULT '',
                current_situation         TEXT    DEFAULT '',
                solution_direction        TEXT    DEFAULT '',
                expected_effects_json     TEXT    DEFAULT '[]',
                persuasion_structure_json TEXT    DEFAULT '[]',
                high_priority_eval_json   TEXT    DEFAULT '[]',
                keyword_map_json          TEXT    DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS research_results (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at               TEXT    NOT NULL,
                client_name              TEXT    NOT NULL,
                project_name             TEXT    NOT NULL,
                agency_type              TEXT    DEFAULT '',
                agency_characteristics   TEXT    DEFAULT '',
                recent_issues_json       TEXT    DEFAULT '[]',
                similar_cases_json       TEXT    DEFAULT '[]',
                target_audience          TEXT    DEFAULT '',
                preferred_message_style  TEXT    DEFAULT '',
                raw_search_json          TEXT    DEFAULT '{}',
                result_json              TEXT    DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS bid_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT    NOT NULL,
                client_name     TEXT    NOT NULL,
                project_name    TEXT    NOT NULL,
                pt_file_path    TEXT    DEFAULT '',
                qa_content      TEXT    DEFAULT '',
                bid_result      TEXT    DEFAULT '미정',
                bid_score       REAL    DEFAULT 0.0,
                notes           TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS learning_cases (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT    NOT NULL,
                user_id         INTEGER DEFAULT 0,
                data_type       TEXT    NOT NULL,
                client_name     TEXT    DEFAULT '',
                project_name    TEXT    DEFAULT '',
                content         TEXT    DEFAULT '',
                file_name       TEXT    DEFAULT '',
                bid_result      TEXT    DEFAULT '미정',
                eval_score      REAL    DEFAULT 0.0,
                notes           TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS proposal_shares (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id     INTEGER NOT NULL,
                shared_by   INTEGER NOT NULL,
                shared_with INTEGER NOT NULL,
                created_at  TEXT    NOT NULL,
                UNIQUE(case_id, shared_with)
            );

            CREATE TABLE IF NOT EXISTS research_cache (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at   TEXT    NOT NULL,
                client_name  TEXT    NOT NULL,
                project_name TEXT    NOT NULL DEFAULT '',
                data_json    TEXT    NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS delete_requests (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id       INTEGER NOT NULL,
                requested_by  TEXT    NOT NULL,
                requested_at  TEXT    NOT NULL,
                status        TEXT    DEFAULT 'pending',
                handled_by    TEXT,
                handled_at    TEXT,
                reject_reason TEXT,
                client_name   TEXT    DEFAULT '',
                project_name  TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS ppt_versions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id      INTEGER NOT NULL,
                version      INTEGER NOT NULL,
                ppt_data     BLOB,
                ppt_filename TEXT    DEFAULT '',
                pt_script    TEXT    DEFAULT '{}',
                created_at   TEXT    NOT NULL,
                created_by   TEXT    DEFAULT '',
                memo         TEXT    DEFAULT '',
                is_pdf       INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS storyboard_results (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id          INTEGER NOT NULL,
                scene_num        INTEGER NOT NULL,
                image_path       TEXT    DEFAULT '',
                image_url        TEXT    DEFAULT '',
                scene_description TEXT   DEFAULT '',
                style            TEXT    DEFAULT 'line',
                created_at       TEXT    NOT NULL,
                client_name      TEXT    DEFAULT '',
                project_name     TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS step_content_overrides (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id      INTEGER NOT NULL,
                step_key     TEXT    NOT NULL,
                content_json TEXT    NOT NULL DEFAULT '{}',
                edited_at    TEXT    NOT NULL,
                editor       TEXT    DEFAULT '',
                UNIQUE(case_id, step_key)
            );

            CREATE TABLE IF NOT EXISTS ppt_jobs (
                task_id     TEXT    PRIMARY KEY,
                case_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL DEFAULT 0,
                status      TEXT    DEFAULT 'pending',
                ppt_type    TEXT    DEFAULT '',
                gamma_url   TEXT    DEFAULT '',
                error_msg   TEXT    DEFAULT '',
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ppt_narratives (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id        INTEGER NOT NULL UNIQUE,
                target_slides  INTEGER DEFAULT 30,
                slides_json    TEXT    DEFAULT '[]',
                rfp_coverage_json TEXT DEFAULT '{}',
                content_chars  INTEGER DEFAULT 0,
                created_at     TEXT    NOT NULL,
                updated_at     TEXT    NOT NULL
            );
        """)
        # ── 마이그레이션: 기존 DB에 누락된 컬럼 추가 ──
        for migration in [
            "ALTER TABLE research_results ADD COLUMN result_json TEXT DEFAULT '{}'",
            "ALTER TABLE rfp_cases ADD COLUMN user_id INTEGER DEFAULT 0",
            "ALTER TABLE rfp_cases ADD COLUMN hidden INTEGER DEFAULT 0",
            "ALTER TABLE rfp_cases ADD COLUMN stopped INTEGER DEFAULT 0",
            "ALTER TABLE script_results ADD COLUMN case_id INTEGER DEFAULT 0",
            "ALTER TABLE marketing_results ADD COLUMN case_id INTEGER DEFAULT 0",
            "ALTER TABLE rfp_analyses ADD COLUMN case_id INTEGER DEFAULT 0",
            "ALTER TABLE research_results ADD COLUMN case_id INTEGER DEFAULT 0",
            "ALTER TABLE strategy_results ADD COLUMN case_id INTEGER DEFAULT 0",
            "ALTER TABLE creative_results ADD COLUMN case_id INTEGER DEFAULT 0",
            "ALTER TABLE plan_results ADD COLUMN case_id INTEGER DEFAULT 0",
            "ALTER TABLE final_proposals ADD COLUMN case_id INTEGER DEFAULT 0",
            "ALTER TABLE research_cache ADD COLUMN project_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE storyboard_results ADD COLUMN client_name TEXT DEFAULT ''",
            "ALTER TABLE storyboard_results ADD COLUMN project_name TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(migration)
            except Exception:
                pass  # 이미 존재하는 컬럼 → 무시


def save_case(client_name: str, project_name: str, video_type: str,
              dna_json: str, result_json: str = "{}",
              agency_type: str = "", budget: str = "", deadline: str = "",
              user_id: int = 0) -> int:
    """신규 케이스 저장.

    Returns:
        생성된 케이스 ID
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO rfp_cases
               (created_at, client_name, project_name, video_type,
                agency_type, budget, deadline, dna_json, result_json, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(), client_name, project_name, video_type,
             agency_type, budget, deadline, dna_json, result_json, user_id),
        )
        return cursor.lastrowid


def update_case(case_id: int, dna_json: str = None, result_json: str = None) -> None:
    """케이스 DNA/결과 업데이트 (파이프라인 완료 후 호출)."""
    if dna_json is None and result_json is None:
        return
    fields = []
    params = []
    if dna_json is not None:
        fields.append("dna_json=?")
        params.append(dna_json)
    if result_json is not None:
        fields.append("result_json=?")
        params.append(result_json)
    params.append(case_id)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE rfp_cases SET {', '.join(fields)} WHERE id=?",
            params,
        )


def mark_case_stopped(case_id: int, dna_json: str = None) -> None:
    """사용자 강제 중지: stopped=1 + 현재까지의 DNA 스냅샷 저장."""
    fields = ["stopped=1"]
    params: list = []
    if dna_json is not None:
        fields.append("dna_json=?")
        params.append(dna_json)
    params.append(case_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE rfp_cases SET {', '.join(fields)} WHERE id=?", params)


def hide_case(case_id: int) -> None:
    """케이스를 숨김 처리."""
    with get_connection() as conn:
        conn.execute("UPDATE rfp_cases SET hidden=1 WHERE id=?", (case_id,))


def unhide_case(case_id: int) -> None:
    """케이스 숨김 해제."""
    with get_connection() as conn:
        conn.execute("UPDATE rfp_cases SET hidden=0 WHERE id=?", (case_id,))


def save_rfp_analysis(client_name: str, project_name: str, analysis: dict,
                      case_id: int = 0) -> int:
    """RFP 분석 결과 저장."""
    import json
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO rfp_analyses
               (created_at, client_name, project_name, agency_type,
                evaluation_items_json, top_keywords_json,
                core_tasks_json, forbidden_notes_json, agency_tone_hint, case_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                client_name,
                project_name,
                analysis.get("agency_type", ""),
                json.dumps(analysis.get("evaluation_items", []), ensure_ascii=False),
                json.dumps(analysis.get("top_keywords", []), ensure_ascii=False),
                json.dumps(analysis.get("core_tasks", []), ensure_ascii=False),
                json.dumps(analysis.get("forbidden_notes", []), ensure_ascii=False),
                analysis.get("agency_tone_hint", ""),
                case_id,
            ),
        )
        return cursor.lastrowid


def save_final_proposal(client_name: str, project_name: str, result: dict,
                        case_id: int = 0) -> int:
    """최종 확정 제안서 저장."""
    import json
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO final_proposals
               (created_at, client_name, project_name,
                consistency_score, evaluation_coverage_json, issues_json,
                company_profile_json, pt_script_json, qa_prep_json,
                final_proposal_json, dna_snapshot_json, case_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                client_name, project_name,
                result.get("consistency_score", 0.0),
                json.dumps(result.get("evaluation_coverage", {}), ensure_ascii=False),
                json.dumps(result.get("issues", []), ensure_ascii=False),
                json.dumps(result.get("company_profile", {}), ensure_ascii=False),
                json.dumps(result.get("pt_script", {}), ensure_ascii=False),
                json.dumps(result.get("qa_prep", []), ensure_ascii=False),
                json.dumps(result.get("final_proposal", {}), ensure_ascii=False),
                json.dumps(result.get("dna_snapshot", {}), ensure_ascii=False),
                case_id,
            ),
        )
        return cursor.lastrowid


def save_platform(client_name: str, project_name: str, result: dict,
                  case_id: int = 0) -> int:
    """플랫폼 운영전략 결과 저장."""
    import json
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO platform_results
               (created_at, client_name, project_name, case_id,
                platforms_json, youtube_strategy, sns_strategy, edit_versions_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                client_name, project_name, case_id,
                json.dumps(result.get("platforms", []), ensure_ascii=False),
                result.get("youtube_strategy", ""),
                result.get("sns_strategy", ""),
                json.dumps(result.get("edit_versions", []), ensure_ascii=False),
            ),
        )
        return cursor.lastrowid


def save_marketing(client_name: str, project_name: str, result: dict,
                   case_id: int = 0) -> int:
    """마케팅 전략 결과 저장."""
    import json
    with get_connection() as conn:
        def _mkt_val(v):
            """텍스트면 그대로, dict/list면 JSON 직렬화."""
            if isinstance(v, str):
                return v
            return json.dumps(v, ensure_ascii=False)

        cursor = conn.execute(
            """INSERT INTO marketing_results
               (created_at, client_name, project_name,
                platforms_json, youtube_strategy_json, shortform_strategy_json,
                sns_strategy_json, influencer_strategy_json,
                kpi_json, reporting_system, marketing_budget_json, case_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                client_name,
                project_name,
                _mkt_val(result.get("platforms", [])),
                _mkt_val(result.get("youtube_strategy", result.get("youtube_seo", {}))),
                _mkt_val(result.get("shortform_strategy", {})),
                _mkt_val(result.get("sns_strategy", result.get("sns_channels", {}))),
                _mkt_val(result.get("influencer_strategy", {})),
                _mkt_val(result.get("kpi_targets", result.get("kpi", {}))),
                result.get("reporting_system", ""),
                _mkt_val(result.get("marketing_budget", {})),
                case_id,
            ),
        )
        return cursor.lastrowid


def save_script(client_name: str, project_name: str, script: dict,
                case_id: int = 0) -> int:
    """편별 대본 저장. 편마다 한 행씩 저장."""
    import json

    def _scalar(v, default=""):
        """dict/list → JSON 문자열로 변환, 나머지는 원본 반환."""
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return v if v is not None else default

    ep_raw = script.get("episode", 0)
    ep_num = ep_raw if isinstance(ep_raw, int) else (
        int(ep_raw) if isinstance(ep_raw, str) and ep_raw.isdigit() else 0
    )

    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO script_results
               (created_at, client_name, project_name,
                episode_number, episode_title, format, duration, script_json, case_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                client_name,
                project_name,
                ep_num,
                _scalar(script.get("title", "")),
                _scalar(script.get("format", "longform")),
                _scalar(script.get("duration", "")),
                json.dumps(script, ensure_ascii=False),
                case_id,
            ),
        )
        return cursor.lastrowid


def save_plan(client_name: str, project_name: str, result: dict,
              case_id: int = 0) -> int:
    """제작 계획 결과 저장."""
    import json
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO plan_results
               (created_at, client_name, project_name, is_youtube_channel,
                episodes_json, production_schedule_json,
                team_composition_json, budget_plan_json, series_plan_json, case_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                client_name,
                project_name,
                int(result.get("is_youtube_channel", False)),
                json.dumps(result.get("episodes", []), ensure_ascii=False),
                json.dumps(result.get("production_schedule", []), ensure_ascii=False),
                json.dumps(result.get("team_composition", {}), ensure_ascii=False),
                json.dumps(result.get("budget_plan", {}), ensure_ascii=False),
                json.dumps(result.get("series_plan", {}), ensure_ascii=False),
                case_id,
            ),
        )
        return cursor.lastrowid


def save_creative(client_name: str, project_name: str, result: dict,
                  case_id: int = 0) -> int:
    """크리에이티브 결과 저장."""
    import json
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO creative_results
               (created_at, client_name, project_name, agency_type,
                concept, concept_description, confirmed_slogan,
                slogans_json, tone_keywords_json, tone_description,
                forbidden_json, visual_direction, case_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                client_name,
                project_name,
                result.get("agency_type", ""),
                result.get("concept", ""),
                result.get("concept_description", ""),
                result.get("confirmed_slogan", ""),
                json.dumps(result.get("slogans", []), ensure_ascii=False),
                json.dumps(result.get("tone_keywords", []), ensure_ascii=False),
                result.get("tone_description", ""),
                json.dumps(result.get("forbidden_expressions", []), ensure_ascii=False),
                result.get("visual_direction", ""),
                case_id,
            ),
        )
        return cursor.lastrowid


def save_strategy(client_name: str, project_name: str, result: dict,
                  case_id: int = 0) -> int:
    """전략 수립 결과 저장."""
    import json
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO strategy_results
               (created_at, client_name, project_name,
                core_problem, crisis_statement, current_situation, solution_direction,
                expected_effects_json, persuasion_structure_json,
                high_priority_eval_json, keyword_map_json, case_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                client_name,
                project_name,
                result.get("core_problem", ""),
                result.get("crisis_statement", ""),
                result.get("current_situation", ""),
                result.get("solution_direction", ""),
                json.dumps(result.get("expected_effects", []), ensure_ascii=False),
                json.dumps(result.get("persuasion_structure", []), ensure_ascii=False),
                json.dumps(result.get("high_priority_eval_items", []), ensure_ascii=False),
                json.dumps(result.get("keyword_integration_map", {}), ensure_ascii=False),
                case_id,
            ),
        )
        return cursor.lastrowid


def save_research(client_name: str, project_name: str, result: dict,
                  case_id: int = 0) -> int:
    """리서치 결과 저장."""
    import json
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO research_results
               (created_at, client_name, project_name, agency_type,
                agency_characteristics, recent_issues_json, similar_cases_json,
                target_audience, preferred_message_style, raw_search_json,
                result_json, case_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                client_name,
                project_name,
                result.get("agency_type", ""),
                result.get("agency_characteristics", ""),
                json.dumps(result.get("recent_issues", []), ensure_ascii=False),
                json.dumps(result.get("similar_cases", []), ensure_ascii=False),
                result.get("target_audience", ""),
                result.get("preferred_message_style", ""),
                json.dumps(result.get("raw_searches", {}), ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                case_id,
            ),
        )
        return cursor.lastrowid


def get_case_detail(case_id: int) -> "dict | None":
    """케이스 ID로 rfp_cases + 모든 스텝 테이블 통합 조회.

    Returns:
        {
          "case":   rfp_cases row (dna_json/result_json 파싱 포함),
          "steps":  { step_key: data_dict, ... }
        }
        케이스 없으면 None
    """
    import json

    with get_connection() as conn:
        case_row = conn.execute(
            "SELECT * FROM rfp_cases WHERE id=?", (case_id,)
        ).fetchone()
        if not case_row:
            return None
        case = dict(case_row)

        client      = case["client_name"]
        project     = case["project_name"]
        case_ts     = case["created_at"]  # 이 케이스 생성 시각 — 스텝 조회 범위 기준점

        # JSON 필드 파싱
        try:
            case["dna"] = json.loads(case.pop("dna_json", "{}"))
        except Exception:
            case["dna"] = {}
        try:
            case["final_result"] = json.loads(case.pop("result_json", "{}"))
        except Exception:
            case["final_result"] = {}

        def _latest(table):
            """스텝 결과 조회: case_id 직접 매칭 → timestamp 이후 → timestamp 이전 최근 순."""
            case_id_val = case.get("id", 0)
            # 1순위: case_id 직접 매칭 (신규 파이프라인)
            if case_id_val:
                row = conn.execute(
                    f"SELECT * FROM {table} WHERE case_id=?"
                    f" ORDER BY created_at ASC LIMIT 1",
                    (case_id_val,),
                ).fetchone()
                if row:
                    return dict(row)
            # 2순위: case_ts 이후 첫 번째 (구버전 호환)
            row = conn.execute(
                f"SELECT * FROM {table} WHERE client_name=? AND project_name=?"
                f" AND created_at >= ? ORDER BY created_at ASC LIMIT 1",
                (client, project, case_ts),
            ).fetchone()
            if row:
                return dict(row)
            # 3순위: case_ts 이전 가장 최근 (구버그 — save_case가 파이프라인 완료 후 저장됨)
            row = conn.execute(
                f"SELECT * FROM {table} WHERE client_name=? AND project_name=?"
                f" AND created_at < ? ORDER BY created_at DESC LIMIT 1",
                (client, project, case_ts),
            ).fetchone()
            return dict(row) if row else None

        def _parse_json_cols(d: dict, cols: list[str], text_fallback_cols: set = None) -> dict:
            if text_fallback_cols is None:
                text_fallback_cols = set()
            for col in cols:
                val = d.pop(col, None)
                key = col.replace("_json", "")
                try:
                    d[key] = json.loads(val or "[]") if val else []
                except Exception:
                    # 텍스트 형태 값은 원본 문자열로, 그 외는 빈 리스트
                    d[key] = val if (col in text_fallback_cols and val) else []
            return d

        steps = {}

        # STEP 1 RFP 분석
        row = _latest("rfp_analyses")
        if row:
            _parse_json_cols(row, ["evaluation_items_json", "top_keywords_json",
                                   "core_tasks_json", "forbidden_notes_json"])
            steps["rfp_analysis"] = row

        # STEP 2 리서치
        row = _latest("research_results")
        if row:
            # result_json에 전체 결과가 있으면 그것을 베이스로, 없으면 개별 컬럼 사용
            result_json_raw = row.pop("result_json", None)
            try:
                full_result = json.loads(result_json_raw or "{}") if result_json_raw else {}
            except Exception:
                full_result = {}
            if full_result:
                # 전체 결과를 기반으로 하되 DB 메타 필드(id, created_at 등)는 보존
                merged = {k: row[k] for k in ("id", "created_at", "client_name", "project_name", "agency_type")}
                merged.update(full_result)
                row = merged
            else:
                # 구버전 DB: 개별 컬럼에서 복원
                _parse_json_cols(row, ["recent_issues_json", "similar_cases_json"])
                try:
                    row["raw_search"] = json.loads(row.pop("raw_search_json", "{}") or "{}")
                except Exception:
                    row["raw_search"] = {}
            steps["research"] = row

        # STEP 3 내러티브 (DNA 필드에서)
        narrative = case["dna"].get("narrative", "")
        if narrative:
            steps["narrative"] = {"narrative": narrative}

        # STEP 4 전략
        row = _latest("strategy_results")
        if row:
            _parse_json_cols(row, ["expected_effects_json", "persuasion_structure_json",
                                   "high_priority_eval_json", "keyword_map_json"])
            steps["strategy"] = row

        # STEP 5 컨셉
        row = _latest("creative_results")
        if row:
            _parse_json_cols(row, ["slogans_json", "tone_keywords_json", "forbidden_json"])
            steps["creative"] = row
        else:
            # fallback: DNA 스냅샷에서 컨셉 복원 (concept 주입 스킵 케이스 등)
            dna_snap = case.get("dna", {})
            concept_val = dna_snap.get("concept", "")
            if concept_val:
                steps["creative"] = {
                    "concept":               concept_val,
                    "concept_description":   dna_snap.get("concept_description", ""),
                    "confirmed_slogan":      dna_snap.get("slogan", ""),
                    "slogans":               dna_snap.get("slogans", []),
                    "tone_keywords":         dna_snap.get("tone_keywords", []),
                    "tone_description":      dna_snap.get("tone_and_manner", ""),
                    "forbidden_expressions": dna_snap.get("forbidden_expressions", []),
                    "visual_direction":      dna_snap.get("visual_direction", ""),
                    "_from_dna":             True,  # DNA에서 복원됨을 표시
                }

        # STEP 6 기획
        row = _latest("plan_results")
        if row:
            _parse_json_cols(row, ["episodes_json", "production_schedule_json",
                                   "team_composition_json", "budget_plan_json",
                                   "series_plan_json"])
            steps["plan"] = row

        # STEP 7 대본 (편별 복수)
        # 1순위: case_id 직접 매칭 (신규 파이프라인)
        # 2순위: case_ts 이후 저장된 것 (과거 구버그 호환)
        # 3순위: case_ts 이전 가장 최근 실행 (구 버전 데이터 — save_case가 마지막이었던 케이스)
        case_id_val = case.get("id", 0)
        script_rows = conn.execute(
            "SELECT * FROM script_results WHERE case_id=? ORDER BY episode_number, created_at",
            (case_id_val,),
        ).fetchall() if case_id_val else []

        if not script_rows:
            # Fallback 1: timestamp 이후 — episode_number별 가장 이른 것 (첫 번째 런)
            all_after = conn.execute(
                "SELECT * FROM script_results WHERE client_name=? AND project_name=?"
                " AND created_at >= ? ORDER BY episode_number, created_at ASC",
                (client, project, case_ts),
            ).fetchall()
            if all_after:
                seen_ep = set()
                deduped = []
                for row_a in all_after:
                    ep_num = dict(row_a).get("episode_number", 0)
                    if ep_num not in seen_ep:
                        seen_ep.add(ep_num)
                        deduped.append(row_a)
                script_rows = sorted(deduped, key=lambda r: dict(r).get("episode_number", 0))

        if not script_rows:
            # Fallback 2: case_ts 이전 가장 최근 실행 (구버그 — case가 파이프라인 완료 후 저장됨)
            latest_ts_row = conn.execute(
                "SELECT MAX(created_at) FROM script_results"
                " WHERE client_name=? AND project_name=? AND created_at < ?",
                (client, project, case_ts),
            ).fetchone()
            latest_before = latest_ts_row[0] if latest_ts_row else None
            if latest_before:
                # 해당 타임스탬프 기준 1시간 이내 스크립트를 같은 "런"으로 간주
                # episode_number별 가장 최신 것만 보존 (중복 런 dedup)
                all_before = conn.execute(
                    "SELECT * FROM script_results WHERE client_name=? AND project_name=?"
                    " AND created_at <= ? ORDER BY episode_number, created_at DESC",
                    (client, project, latest_before),
                ).fetchall()
                seen_ep = set()
                deduped = []
                for row_b in all_before:
                    ep_num = dict(row_b).get("episode_number", 0)
                    if ep_num not in seen_ep:
                        seen_ep.add(ep_num)
                        deduped.append(row_b)
                script_rows = sorted(deduped, key=lambda r: dict(r).get("episode_number", 0))

        if script_rows:
            scripts = []
            for sr in script_rows:
                d = dict(sr)
                try:
                    d["script"] = json.loads(d.pop("script_json", "{}") or "{}")
                except Exception:
                    d["script"] = {}
                scripts.append(d)
            steps["script"] = scripts

        # STEP 9 플랫폼 운영전략
        plt_row = _latest("platform_results")
        if plt_row:
            try:
                plt_row["platforms"] = json.loads(plt_row.pop("platforms_json", "[]") or "[]")
            except Exception:
                plt_row["platforms"] = []
            try:
                plt_row["edit_versions"] = json.loads(plt_row.pop("edit_versions_json", "[]") or "[]")
            except Exception:
                plt_row["edit_versions"] = []
            steps["platform"] = plt_row

        # STEP 10 마케팅
        # 1순위: case_id 매칭, 2순위: timestamp 이후, 3순위: timestamp 이전 최근
        mkt_row = None
        if case_id_val:
            mkt_row_raw = conn.execute(
                "SELECT * FROM marketing_results WHERE case_id=? ORDER BY created_at DESC LIMIT 1",
                (case_id_val,),
            ).fetchone()
            mkt_row = dict(mkt_row_raw) if mkt_row_raw else None

        if not mkt_row:
            mkt_row = _latest("marketing_results")  # timestamp >= case_ts

        if not mkt_row:
            mkt_row_raw = conn.execute(
                "SELECT * FROM marketing_results WHERE client_name=? AND project_name=?"
                " AND created_at < ? ORDER BY created_at DESC LIMIT 1",
                (client, project, case_ts),
            ).fetchone()
            mkt_row = dict(mkt_row_raw) if mkt_row_raw else None

        if mkt_row:
            _parse_json_cols(mkt_row, ["platforms_json", "youtube_strategy_json",
                                       "shortform_strategy_json", "sns_strategy_json",
                                       "influencer_strategy_json", "kpi_json",
                                       "marketing_budget_json"],
                             text_fallback_cols={"youtube_strategy_json",
                                                 "sns_strategy_json",
                                                 "influencer_strategy_json",
                                                 "kpi_json"})
            steps["marketing"] = mkt_row

        # STEP 8 스토리보드
        sb_rows = conn.execute(
            "SELECT * FROM storyboard_results WHERE case_id=? ORDER BY scene_num",
            (case_id_val,),
        ).fetchall() if case_id_val else []
        if sb_rows:
            steps["storyboard"] = {
                "frames": [dict(r) for r in sb_rows],
                "total_scenes": len(sb_rows),
                "style": dict(sb_rows[0]).get("style", "line") if sb_rows else "line",
            }
        else:
            # 스토리보드 탭은 항상 활성화 (동적 로딩)
            steps["storyboard"] = {"frames": [], "total_scenes": 0, "style": "line"}

        # STEP 11 PT/Q&A (최종 제안서)
        row = _latest("final_proposals")
        if row:
            _parse_json_cols(row, ["evaluation_coverage_json", "issues_json",
                                   "company_profile_json", "pt_script_json",
                                   "qa_prep_json", "final_proposal_json",
                                   "dna_snapshot_json"])
            steps["final_proposal"] = row

        # PT/Q&A 및 크리틱 탭: final_proposal에서 파생
        if "final_proposal" in steps:
            fp = steps["final_proposal"]
            steps["pt_qa"] = {
                "pt_script": fp.get("pt_script", {}),
                "qa_prep":   fp.get("qa_prep", []),
            }
            steps["critic"] = {
                "consistency_score":    fp.get("consistency_score", 0),
                "evaluation_coverage":  fp.get("evaluation_coverage", {}),
                "issues":               fp.get("issues", []),
                "predicted_scores":     fp.get("predicted_scores", []),
                "competitive_analysis": fp.get("competitive_analysis", {}),
                "company_profile":      fp.get("company_profile", {}),
                "final_proposal":       fp.get("final_proposal", {}),
            }

        return {"case": case, "steps": steps}


def save_bid_result(
    client_name: str,
    project_name: str,
    pt_file_path: str = "",
    qa_content: str = "",
    bid_result: str = "미정",
    bid_score: float = 0.0,
    notes: str = "",
) -> int:
    """PT 파일 경로·Q&A 내용·입찰 결과를 저장."""
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO bid_results
               (created_at, client_name, project_name,
                pt_file_path, qa_content, bid_result, bid_score, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(), client_name, project_name,
             pt_file_path, qa_content, bid_result, bid_score, notes),
        )
        return cursor.lastrowid


def get_bid_results(limit: int = 20) -> list:
    """입찰 결과 목록 조회 (최신순)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM bid_results ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def count_bid_results() -> int:
    """누적 입찰 결과 건수 반환."""
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM bid_results").fetchone()
        return row[0] if row else 0


def analyze_bid_patterns() -> dict:
    """누적 입찰 데이터 패턴 분석 (3건 이상일 때 Claude 분석 활성화).

    Returns:
        분석 결과 dict. 3건 미만이면 count만 반환.
    """
    results = get_bid_results(limit=100)
    count = len(results)
    if count < 3:
        return {"count": count, "analysis": None,
                "message": f"누적 {count}건 — 3건 이상 필요"}

    from core.claude_client import call_json

    rows_text = "\n".join(
        f"[{r['created_at'][:10]}] {r['client_name']} / {r['project_name']} "
        f"| 결과: {r['bid_result']} | 점수: {r['bid_score']} | 비고: {r['notes']}"
        for r in results
    )

    prompt = f"""당신은 정부 입찰 전략 분석가입니다.
아래 입찰 실적 데이터({count}건)를 분석하여 패턴을 도출하세요.

[입찰 실적]
{rows_text}

다음 JSON 형식으로 응답하세요:
{{
  "win_rate": 수주율(0.0~1.0 소수),
  "total_count": 전체건수,
  "win_count": 수주건수,
  "loss_count": 탈락건수,
  "pending_count": 미정건수,
  "agency_type_pattern": "수주가 많은 기관 유형 경향 (2~3문장)",
  "success_factors": ["성공 패턴 요인 1", "성공 패턴 요인 2", "성공 패턴 요인 3"],
  "failure_factors": ["실패 패턴 요인 1", "실패 패턴 요인 2"],
  "strategic_recommendations": ["다음 입찰을 위한 전략 제언 1", "전략 제언 2", "전략 제언 3"],
  "summary": "전체 패턴 요약 (3~5문장)"
}}"""

    try:
        analysis = call_json(prompt)
        analysis["count"] = count
        return analysis
    except Exception as e:
        return {"count": count, "analysis": None, "message": f"분석 실패: {e}"}


def find_past_research(client_name: str, agency_type: str = "", limit: int = 3) -> list:
    """과거 리서치 결과 조회 (researcher.py가 재활용).

    Args:
        client_name: 발주처명 (LIKE 검색)
        agency_type: 기관 유형 (빈 문자열이면 무시)
        limit: 최대 반환 건수

    Returns:
        research_results dict 목록 (recent_issues, similar_cases 역직렬화 포함)
    """
    import json
    with get_connection() as conn:
        if agency_type:
            rows = conn.execute(
                """SELECT * FROM research_results
                   WHERE client_name LIKE ? OR agency_type = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (f"%{client_name}%", agency_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM research_results
                   WHERE client_name LIKE ?
                   ORDER BY created_at DESC LIMIT ?""",
                (f"%{client_name}%", limit),
            ).fetchall()

        results = []
        for row in rows:
            d = dict(row)
            for field in ("recent_issues_json", "similar_cases_json"):
                key = field.replace("_json", "")
                try:
                    d[key] = json.loads(d.pop(field, "[]"))
                except Exception:
                    d[key] = []
            d.pop("raw_search_json", None)  # 원문은 제외 (용량)
            results.append(d)
        return results


def find_similar_cases(client_name: str, video_type: str, limit: int = 3) -> list:
    """유사 발주처 또는 영상 종류 기준으로 과거 케이스 조회.

    Args:
        client_name: 발주처명 (LIKE 검색)
        video_type: 영상 종류
        limit: 최대 반환 건수

    Returns:
        케이스 dict 목록
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM rfp_cases
               WHERE client_name LIKE ? OR video_type = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (f"%{client_name}%", video_type, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def find_similar_analyses(client_name: str, agency_type: str = "", limit: int = 3) -> list:
    """유사 발주처 RFP 분석 이력 조회.

    Args:
        client_name: 발주처명 (LIKE 검색)
        agency_type: 기관 유형 (빈 문자열이면 무시)
        limit: 최대 반환 건수

    Returns:
        분석 dict 목록
    """
    import json
    with get_connection() as conn:
        if agency_type:
            rows = conn.execute(
                """SELECT * FROM rfp_analyses
                   WHERE client_name LIKE ? OR agency_type = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (f"%{client_name}%", agency_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM rfp_analyses
                   WHERE client_name LIKE ?
                   ORDER BY created_at DESC LIMIT ?""",
                (f"%{client_name}%", limit),
            ).fetchall()

        results = []
        for row in rows:
            d = dict(row)
            # JSON 필드 역직렬화
            for field in ("evaluation_items_json", "top_keywords_json",
                          "core_tasks_json", "forbidden_notes_json"):
                key = field.replace("_json", "")
                try:
                    d[key] = json.loads(d.pop(field, "[]"))
                except Exception:
                    d[key] = []
            results.append(d)
        return results


# ─────────────────────────────────────────────
# 사용자 관리
# ─────────────────────────────────────────────

def init_users() -> None:
    """users 테이블 생성 + rfp_cases user_id 마이그레이션 + admin 초기 계정."""
    import os
    from werkzeug.security import generate_password_hash

    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL,
                is_admin      INTEGER DEFAULT 0,
                created_at    TEXT    NOT NULL
            )
        """)
        # rfp_cases에 user_id 컬럼 추가 (최초 1회)
        try:
            conn.execute("ALTER TABLE rfp_cases ADD COLUMN user_id INTEGER DEFAULT 0")
        except Exception:
            pass  # 이미 존재하면 무시

        # users에 telegram_chat_id 컬럼 추가 (최초 1회)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN telegram_chat_id TEXT DEFAULT ''")
        except Exception:
            pass  # 이미 존재하면 무시

        # users에 role 컬럼 추가 (최초 1회) + 기존 계정 역할 설정
        try:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
            # 기존 팀원 → operator, 기존 관리자 → admin (기존 권한 유지)
            conn.execute("UPDATE users SET role='operator' WHERE is_admin=0")
            conn.execute("UPDATE users SET role='admin' WHERE is_admin=1")
        except Exception:
            pass  # 이미 존재하면 무시
        # viewer 등 비표준 role 정리 → user로 통합
        try:
            conn.execute(
                "UPDATE users SET role='user' WHERE role NOT IN ('admin','operator','user')"
            )
        except Exception:
            pass

        # admin 계정이 없으면 생성
        count = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]
        if count == 0:
            admin_pw = os.environ.get("ADMIN_PASSWORD", "admin1234")
            conn.execute(
                "INSERT OR IGNORE INTO users (username, password_hash, is_admin, role, created_at) VALUES (?,?,1,'admin',?)",
                ("admin", generate_password_hash(admin_pw), datetime.now().isoformat()),
            )
            print(f"  [초기화] admin 계정 생성 (비밀번호: {admin_pw})")


def verify_user(username: str, password: str) -> "dict | None":
    """username/password 검증. 성공 시 user dict, 실패 시 None."""
    from werkzeug.security import check_password_hash
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if row and check_password_hash(dict(row)["password_hash"], password):
        return dict(row)
    return None


def get_user_by_id(user_id: int) -> "dict | None":
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, username, is_admin, role, created_at FROM users ORDER BY id"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # role 컬럼이 없는 구버전 DB 대비 폴백
            if not d.get("role"):
                d["role"] = "admin" if d.get("is_admin") else "operator"
            result.append(d)
        return result


def create_user(username: str, password: str, is_admin: bool = False,
                role: str = "") -> int:
    from werkzeug.security import generate_password_hash
    effective_role = role or ("admin" if is_admin else "user")
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?)",
            (username, generate_password_hash(password), int(is_admin),
             effective_role, datetime.now().isoformat()),
        )
        return cursor.lastrowid


def update_user_role(user_id: int, role: str) -> None:
    """사용자 역할 변경 (admin/operator/user). is_admin 플래그도 동기화."""
    is_admin = 1 if role == "admin" else 0
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET role=?, is_admin=? WHERE id=?",
            (role, is_admin, user_id),
        )


def share_proposal(case_id: int, shared_by: int, shared_with: int) -> bool:
    """케이스를 특정 사용자에게 공유."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO proposal_shares "
                "(case_id, shared_by, shared_with, created_at) VALUES (?,?,?,?)",
                (case_id, shared_by, shared_with, datetime.now().isoformat()),
            )
        return True
    except Exception:
        return False


def unshare_proposal(case_id: int, shared_by: int, shared_with: int) -> bool:
    """공유 취소."""
    try:
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM proposal_shares WHERE case_id=? AND shared_by=? AND shared_with=?",
                (case_id, shared_by, shared_with),
            )
        return True
    except Exception:
        return False


def get_shared_cases(user_id: int) -> list:
    """이 user_id에게 공유된 케이스 목록 반환."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT r.id, r.created_at, r.client_name, r.project_name,
                      r.video_type, r.budget, r.agency_type, r.user_id,
                      u.username, ps.shared_by, su.username AS shared_by_name
               FROM rfp_cases r
               JOIN proposal_shares ps ON ps.case_id = r.id
               LEFT JOIN users u ON r.user_id = u.id
               LEFT JOIN users su ON ps.shared_by = su.id
               WHERE ps.shared_with = ? AND (r.hidden IS NULL OR r.hidden = 0)
               ORDER BY r.created_at DESC""",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_case_shares(case_id: int) -> list:
    """특정 케이스의 공유 대상 목록."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT ps.shared_with, u.username
               FROM proposal_shares ps
               JOIN users u ON ps.shared_with = u.id
               WHERE ps.case_id = ?""",
            (case_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_all_cases(user_id_filter: int = 0) -> list:
    """관리자용: 전체 케이스 목록 조회."""
    with get_connection() as conn:
        if user_id_filter:
            rows = conn.execute(
                "SELECT r.id, r.created_at, r.client_name, r.project_name, "
                "r.video_type, r.budget, r.agency_type, r.user_id, r.hidden, "
                "u.username FROM rfp_cases r LEFT JOIN users u ON r.user_id=u.id "
                "WHERE r.user_id=? ORDER BY r.created_at DESC LIMIT 200",
                (user_id_filter,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT r.id, r.created_at, r.client_name, r.project_name, "
                "r.video_type, r.budget, r.agency_type, r.user_id, r.hidden, "
                "u.username FROM rfp_cases r LEFT JOIN users u ON r.user_id=u.id "
                "ORDER BY r.created_at DESC LIMIT 200"
            ).fetchall()
        return [dict(row) for row in rows]


def delete_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def change_password(user_id: int, new_password: str) -> None:
    from werkzeug.security import generate_password_hash
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (generate_password_hash(new_password), user_id),
        )


def set_telegram_chat_id(user_id: int, chat_id: str) -> None:
    """사용자의 텔레그램 chat_id 저장."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET telegram_chat_id=? WHERE id=?",
            (chat_id.strip(), user_id),
        )


def get_telegram_chat_id(user_id: int) -> str:
    """사용자의 텔레그램 chat_id 조회. 없으면 빈 문자열."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT telegram_chat_id FROM users WHERE id=?", (user_id,)
        ).fetchone()
    return (dict(row).get("telegram_chat_id") or "") if row else ""


# ─────────────────────────────────────────────
# 학습 데이터
# ─────────────────────────────────────────────

def save_learning_case(
    user_id: int,
    data_type: str,
    client_name: str = "",
    project_name: str = "",
    content: str = "",
    file_name: str = "",
    bid_result: str = "미정",
    eval_score: float = 0.0,
    notes: str = "",
) -> int:
    """학습 데이터 저장.

    Returns:
        생성된 행 ID
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO learning_cases
               (created_at, user_id, data_type, client_name, project_name,
                content, file_name, bid_result, eval_score, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                user_id, data_type, client_name, project_name,
                content, file_name, bid_result, eval_score, notes,
            ),
        )
        return cursor.lastrowid


def list_learning_cases(user_id: int, data_type: str = "") -> list:
    """학습 데이터 목록 조회.

    Args:
        user_id: 사용자 ID (0이면 전체)
        data_type: 필터링할 데이터 종류 (빈 문자열이면 전체)

    Returns:
        행 목록 (dict 리스트)
    """
    with get_connection() as conn:
        if user_id and data_type:
            rows = conn.execute(
                "SELECT * FROM learning_cases WHERE user_id=? AND data_type=? ORDER BY created_at DESC",
                (user_id, data_type),
            ).fetchall()
        elif user_id:
            rows = conn.execute(
                "SELECT * FROM learning_cases WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        elif data_type:
            rows = conn.execute(
                "SELECT * FROM learning_cases WHERE data_type=? ORDER BY created_at DESC",
                (data_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM learning_cases ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def delete_learning_case(case_id: int, user_id: int) -> bool:
    """학습 데이터 삭제 (자신의 데이터만).

    Returns:
        삭제 성공 여부
    """
    with get_connection() as conn:
        result = conn.execute(
            "DELETE FROM learning_cases WHERE id=? AND user_id=?",
            (case_id, user_id),
        )
        return result.rowcount > 0


def get_learning_cases_for_researcher(client_name: str, limit: int = 5) -> list:
    """researcher.py용 — 유사 발주처 학습 데이터 조회.

    Args:
        client_name: 발주처명 (부분 매치)
        limit: 최대 반환 건수

    Returns:
        관련 학습 케이스 목록
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT data_type, client_name, project_name, content, bid_result, eval_score
               FROM learning_cases
               WHERE client_name LIKE ?
               ORDER BY created_at DESC LIMIT ?""",
            (f"%{client_name}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_winning_patterns(limit: int = 10) -> list:
    """orchestrator.py용 — 낙찰 케이스 패턴 조회.

    Returns:
        낙찰 케이스 목록 (content 포함)
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT data_type, client_name, project_name, content, eval_score, notes
               FROM learning_cases
               WHERE bid_result='낙찰'
               ORDER BY eval_score DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 리서치 캐시 (7일 재활용)
# ─────────────────────────────────────────────

def get_research_cache(client_name: str, project_name: str = "") -> "dict | None":
    """7일 이내 리서치 캐시 조회. client_name + project_name 완전 일치.

    Args:
        client_name: 발주처명 (완전 일치)
        project_name: 과업명 (완전 일치, 빈 문자열이면 client_name만 매칭)

    Returns:
        캐시된 리서치 결과 dict, 없으면 None
    """
    import json as _json
    with get_connection() as conn:
        if project_name:
            row = conn.execute(
                """SELECT data_json FROM research_cache
                   WHERE client_name = ?
                     AND project_name = ?
                     AND datetime(created_at) >= datetime('now', '-7 days')
                   ORDER BY created_at DESC LIMIT 1""",
                (client_name, project_name),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT data_json FROM research_cache
                   WHERE client_name = ?
                     AND datetime(created_at) >= datetime('now', '-7 days')
                   ORDER BY created_at DESC LIMIT 1""",
                (client_name,),
            ).fetchone()
    if row:
        try:
            return _json.loads(dict(row)["data_json"])
        except Exception:
            return None
    return None


def save_research_cache(client_name: str, project_name: str, data: dict) -> None:
    """리서치 결과를 캐시에 저장.

    Args:
        client_name: 발주처명
        project_name: 과업명
        data: 저장할 리서치 결과 dict
    """
    import json as _json
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO research_cache (created_at, client_name, project_name, data_json)
               VALUES (?, ?, ?, ?)""",
            (datetime.now().isoformat(), client_name, project_name,
             _json.dumps(data, ensure_ascii=False)),
        )


# ─────────────────────────────────────────────
# 삭제 요청 관리
# ─────────────────────────────────────────────

def create_delete_request(case_id: int, requested_by: str,
                          client_name: str = "", project_name: str = "") -> int:
    """삭제 요청 생성."""
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO delete_requests
               (case_id, requested_by, requested_at, client_name, project_name)
               VALUES (?,?,?,?,?)""",
            (case_id, requested_by, datetime.now().isoformat(), client_name, project_name),
        )
        return cursor.lastrowid


def list_delete_requests(status: str = "pending") -> list:
    """삭제 요청 목록 조회."""
    with get_connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM delete_requests WHERE status=? ORDER BY requested_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM delete_requests ORDER BY requested_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def approve_delete_request(req_id: int, handled_by: str) -> bool:
    """삭제 요청 승인 — 케이스 실제 삭제."""
    with get_connection() as conn:
        req = conn.execute(
            "SELECT * FROM delete_requests WHERE id=?", (req_id,)
        ).fetchone()
        if not req:
            return False
        req = dict(req)
        conn.execute("DELETE FROM rfp_cases WHERE id=?", (req["case_id"],))
        conn.execute(
            "UPDATE delete_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
            (handled_by, datetime.now().isoformat(), req_id),
        )
        return True


def reject_delete_request(req_id: int, handled_by: str, reject_reason: str = "") -> None:
    """삭제 요청 거절."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE delete_requests
               SET status='rejected', handled_by=?, handled_at=?, reject_reason=?
               WHERE id=?""",
            (handled_by, datetime.now().isoformat(), reject_reason, req_id),
        )


def delete_case(case_id: int) -> None:
    """케이스 즉시 삭제 (관리자용)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM rfp_cases WHERE id=?", (case_id,))


def get_admin_telegram_ids() -> list:
    """관리자 계정의 텔레그램 chat_id 목록 반환 (알림 전송용)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT telegram_chat_id FROM users WHERE is_admin=1 AND telegram_chat_id != ''"
        ).fetchall()
        return [dict(r)["telegram_chat_id"] for r in rows if dict(r).get("telegram_chat_id")]


# ─────────────────────────────────────────────
# PPT 버전 관리
# ─────────────────────────────────────────────

def save_ppt_version(case_id: int, ppt_data: bytes, ppt_filename: str,
                     pt_script: str = "{}", created_by: str = "",
                     memo: str = "", is_pdf: bool = False) -> "tuple[int, int]":
    """PPT 버전 저장.

    Returns:
        (version_id, version_number)
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(version) FROM ppt_versions WHERE case_id=?", (case_id,)
        ).fetchone()
        version = (row[0] or 0) + 1
        cursor = conn.execute(
            """INSERT INTO ppt_versions
               (case_id, version, ppt_data, ppt_filename, pt_script,
                created_at, created_by, memo, is_pdf)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (case_id, version, ppt_data, ppt_filename,
             pt_script if isinstance(pt_script, str) else __import__("json").dumps(pt_script, ensure_ascii=False),
             datetime.now().isoformat(), created_by, memo, int(is_pdf)),
        )
        return cursor.lastrowid, version


def get_ppt_versions(case_id: int) -> list:
    """PPT 버전 목록 조회 (ppt_data 제외 — 용량)."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, case_id, version, ppt_filename, pt_script,
                      created_at, created_by, memo, is_pdf
               FROM ppt_versions WHERE case_id=? ORDER BY version DESC""",
            (case_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_ppt_version_data(version_id: int) -> "dict | None":
    """특정 PPT 버전 전체 조회 (ppt_data 포함)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM ppt_versions WHERE id=?", (version_id,)
        ).fetchone()
        return dict(row) if row else None


def update_ppt_version_memo(version_id: int, memo: str) -> None:
    """PPT 버전 메모 수정."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE ppt_versions SET memo=? WHERE id=?", (memo, version_id)
        )


# ─────────────────────────────────────────────
# 스토리보드
# ─────────────────────────────────────────────

def save_storyboard(case_id: int, frames: list, style: str = "line") -> None:
    """스토리보드 프레임 일괄 저장 (기존 레코드 먼저 삭제)."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("DELETE FROM storyboard_results WHERE case_id=?", (case_id,))
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            conn.execute(
                """INSERT INTO storyboard_results
                   (case_id, scene_num, image_path, image_url,
                    scene_description, style, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    case_id,
                    frame.get("scene_num", 0),
                    frame.get("image_path", ""),
                    frame.get("image_url", ""),
                    frame.get("scene_description", ""),
                    style,
                    now,
                ),
            )


def get_storyboards(case_id: int) -> list:
    """케이스의 스토리보드 프레임 조회 (씬 번호 순)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM storyboard_results WHERE case_id=? ORDER BY scene_num",
            (case_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 스텝 내용 수정 (오버라이드)
# ─────────────────────────────────────────────

def save_step_override(case_id: int, step_key: str,
                       content: dict, editor: str = "") -> None:
    """스텝 내용 수정본 저장 (UPSERT)."""
    import json as _json
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO step_content_overrides
               (case_id, step_key, content_json, edited_at, editor)
               VALUES (?,?,?,?,?)
               ON CONFLICT(case_id, step_key)
               DO UPDATE SET content_json=excluded.content_json,
                             edited_at=excluded.edited_at,
                             editor=excluded.editor""",
            (case_id, step_key, _json.dumps(content, ensure_ascii=False), now, editor),
        )


def get_step_override(case_id: int, step_key: str) -> "dict | None":
    """스텝 수정본 조회. 없으면 None."""
    import json as _json
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM step_content_overrides WHERE case_id=? AND step_key=?",
            (case_id, step_key),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["content"] = _json.loads(d.get("content_json", "{}"))
        except Exception:
            d["content"] = {}
        return d


def get_all_step_overrides(case_id: int) -> dict:
    """케이스의 모든 스텝 수정본 조회. { step_key: content_dict } 형태."""
    import json as _json
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT step_key, content_json, edited_at FROM step_content_overrides WHERE case_id=?",
            (case_id,),
        ).fetchall()
        result = {}
        for row in rows:
            step_key = row["step_key"]
            try:
                result[step_key] = {
                    "content": _json.loads(row["content_json"] or "{}"),
                    "edited_at": row["edited_at"],
                }
            except Exception:
                result[step_key] = {"content": {}, "edited_at": row["edited_at"]}
        return result


# ─────────────────────────────────────────────
# PPT 작업 DB 영속화
# ─────────────────────────────────────────────

def save_ppt_narrative(case_id: int, slides: list, rfp_coverage: dict,
                       target_slides: int, content_chars: int) -> None:
    """PPT 설계안 저장 (case_id당 1건, UPSERT)."""
    import json
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO ppt_narratives
               (case_id, target_slides, slides_json, rfp_coverage_json, content_chars, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(case_id) DO UPDATE SET
                 target_slides=excluded.target_slides,
                 slides_json=excluded.slides_json,
                 rfp_coverage_json=excluded.rfp_coverage_json,
                 content_chars=excluded.content_chars,
                 updated_at=excluded.updated_at""",
            (case_id,
             target_slides,
             json.dumps(slides, ensure_ascii=False),
             json.dumps(rfp_coverage, ensure_ascii=False),
             content_chars,
             now, now),
        )


def get_ppt_narrative(case_id: int) -> "dict | None":
    """PPT 설계안 조회. 없으면 None."""
    import json
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM ppt_narratives WHERE case_id=?", (case_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["slides"] = json.loads(d.pop("slides_json", "[]") or "[]")
        except Exception:
            d["slides"] = []
        try:
            d["rfp_coverage"] = json.loads(d.pop("rfp_coverage_json", "{}") or "{}")
        except Exception:
            d["rfp_coverage"] = {}
        return d


def save_ppt_job(task_id: str, case_id: int, user_id: int, ppt_type: str = "") -> None:
    """PPT 작업 생성 시 DB에 저장."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO ppt_jobs
               (task_id, case_id, user_id, status, ppt_type, created_at, updated_at)
               VALUES (?, ?, ?, 'running', ?, ?, ?)""",
            (task_id, case_id, user_id, ppt_type, now, now),
        )


def update_ppt_job(task_id: str, status: str,
                   gamma_url: str = "", error_msg: str = "") -> None:
    """PPT 작업 상태/결과 업데이트."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """UPDATE ppt_jobs
               SET status=?, gamma_url=?, error_msg=?, updated_at=?
               WHERE task_id=?""",
            (status, gamma_url, error_msg, now, task_id),
        )


def get_ppt_job(task_id: str) -> dict | None:
    """task_id로 PPT 작업 조회. 없으면 None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM ppt_jobs WHERE task_id=?", (task_id,)
        ).fetchone()
    return dict(row) if row else None

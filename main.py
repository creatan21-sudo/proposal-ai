# main.py
# 역할: 실행 진입점
# - 사용자 입력 수집 (CLI)
# - ConceptDNA 초기화
# - 파이프라인 실행 (인터랙티브)
# - TXT 출력 + DB 저장
# - 완료 후 재실행 메뉴 / PT·Q&A·입찰결과 DB 누적

import dataclasses
import json

import click

from core.dna import create_dna
from core.pipeline import run_pipeline, STEP_NUM_TO_KEY
from database.db import (
    init_db, save_case,
    save_bid_result, count_bid_results, analyze_bid_patterns,
)
from output.txt_writer import write_txt

VIDEO_TYPES = ["홍보영상", "다큐멘터리", "교육영상", "캠페인영상", "뉴스형영상"]


@click.command()
@click.option("--client",      prompt="발주처",       help="예: 행정안전부")
@click.option("--project",     prompt="사업명",       help="예: 2025년 재난안전 홍보영상 제작")
@click.option("--video-type",  prompt="영상 종류",
              type=click.Choice(VIDEO_TYPES), help="영상 종류 선택")
@click.option("--quantity",    prompt="납품 수량(편)", type=int, help="예: 3")
@click.option("--duration",    prompt="편당 러닝타임", default="3분", help="예: 3분")
@click.option("--budget",      default=None, help="예산 규모 (선택)")
@click.option("--deadline",    default=None, help="납품 기한 (선택)")
@click.option("--target",      default=None, help="타겟 시청자 (선택)")
@click.option("--key-message", default=None, help="핵심 전달 메시지 (선택)")
@click.option("--rfp-text",    default=None, help="RFP 원문 텍스트 (선택, 직접 붙여넣기)")
@click.option("--rfp-file",    default=None,
              type=click.Path(exists=True, dir_okay=False),
              help="RFP 파일 경로 (HWP/HWPX/PDF/TXT, 선택)")
@click.option("--output-dir",  default=None, help="TXT 저장 경로")
@click.option("--pages",       default=30, type=int, show_default=True,
              help="목표 제안서 페이지 수")
@click.option("--concept",     default=None,
              help="미리 정해진 컨셉 문자열 (STEP 3 스킵)")
def main(client, project, video_type, quantity, duration,
         budget, deadline, target, key_message, rfp_text, rfp_file,
         output_dir, pages, concept):
    """정부 입찰용 영상콘텐츠 제안서 자동 생성 (멀티에이전트 파이프라인)"""

    # DB 초기화 (최초 실행 시 테이블 생성)
    init_db()

    # 사용자 입력 → ConceptDNA 초기화
    raw_input = dict(
        client_name=client,
        project_name=project,
        video_type=video_type,
        quantity=quantity,
        duration=duration,
        budget=budget or "",
        deadline=deadline or "",
        target_audience=target or "일반 국민",
        key_message=key_message or "",
        rfp_text=rfp_text or "",
    )
    dna = create_dna(raw_input)

    # ── 최초 파이프라인 실행 ───────────────────
    results = run_pipeline(
        dna,
        rfp_file=rfp_file,
        concept=concept,
        pages=pages,
    )

    if "__aborted_at__" in results:
        click.echo(f"\n[오류] 파이프라인이 {results['__aborted_at__']} 단계에서 중단되었습니다.")
        return

    # ── TXT 파일 생성 ──────────────────────────
    txt_path = _save_txt(dna, results, output_dir)

    # ── DB 케이스 저장 ──────────────────────────
    case_id = _save_case_to_db(dna, results)

    # ── 완료 후 메뉴 루프 ──────────────────────
    _post_pipeline_loop(dna, results, rfp_file, output_dir, txt_path, case_id)


# ─────────────────────────────────────────────
# 완료 후 대화형 루프
# ─────────────────────────────────────────────

def _post_pipeline_loop(dna, results, rfp_file, output_dir, txt_path, case_id):
    """파이프라인 완료 후 재실행 메뉴 + PT/QA/입찰결과 누적."""
    click.echo(f"\n{'═' * 60}")
    click.echo(f"  파이프라인 완료!")
    click.echo(f"  TXT 파일: {txt_path}")
    click.echo(f"{'═' * 60}\n")

    # PT·Q&A·입찰 결과 누적 질문
    _accumulate_bid_info(dna)

    # 재실행 루프
    while True:
        click.echo("\n어느 스텝부터 다시 실행할까요?")
        click.echo("  0=RFP분석  0.5=전략내러티브  1=리서치  2=전략  3=컨셉  "
                   "4=기획  5=대본  6=마케팅  7=최종  n=종료")
        step_input = input("  > ").strip().lower()

        if step_input in ("n", "no", "종료", "끝", ""):
            click.echo("종료합니다.")
            break

        start_key = STEP_NUM_TO_KEY.get(step_input)
        if not start_key:
            click.echo(f"  [오류] 유효한 스텝 번호가 아닙니다: {step_input}")
            continue

        click.echo(f"\n  {step_input}번 스텝부터 재실행합니다...\n")
        results = run_pipeline(
            dna,
            rfp_file=rfp_file,
            concept=None,  # 재실행 시 concept 강제 없음
            pages=dna.pages,
            start_step_key=start_key,
            prior_results=results,
        )

        if "__aborted_at__" not in results:
            txt_path = _save_txt(dna, results, output_dir)
            _save_case_to_db(dna, results)
            click.echo(f"\n  재실행 완료. TXT 저장: {txt_path}")


# ─────────────────────────────────────────────
# PT / Q&A / 입찰 결과 누적
# ─────────────────────────────────────────────

def _accumulate_bid_info(dna):
    """PT 파일, Q&A, 입찰 결과를 입력받아 DB에 저장."""
    click.echo("\n─────────────────────────────────────────")
    click.echo("  [PT·Q&A·입찰결과 누적]")
    click.echo("  (나중에 입력하려면 모두 Enter로 건너뜁니다)")
    click.echo("─────────────────────────────────────────")

    try:
        pt_path = input("  PT 파일 경로 (없으면 Enter): ").strip()
        qa_content = input("  Q&A 내용 요약 (없으면 Enter): ").strip()
        bid_result_input = input("  입찰 결과 [수주/탈락/미정] (없으면 Enter): ").strip()
        bid_score_input  = input("  입찰 점수 (숫자, 없으면 Enter): ").strip()
        notes = input("  비고 (없으면 Enter): ").strip()
    except EOFError:
        return

    if not any([pt_path, qa_content, bid_result_input, bid_score_input, notes]):
        click.echo("  → 입찰 정보 입력 없음. 건너뜁니다.")
        return

    bid_result_val = bid_result_input if bid_result_input in ("수주", "탈락", "미정") else "미정"
    try:
        bid_score_val = float(bid_score_input) if bid_score_input else 0.0
    except ValueError:
        bid_score_val = 0.0

    try:
        save_bid_result(
            client_name=dna.client_name,
            project_name=dna.project_name,
            pt_file_path=pt_path,
            qa_content=qa_content,
            bid_result=bid_result_val,
            bid_score=bid_score_val,
            notes=notes,
        )
        click.echo("  → 입찰 정보 DB 저장 완료.")
    except Exception as e:
        click.echo(f"  → [경고] DB 저장 실패: {e}")

    # 3건 이상이면 패턴 분석 제안
    total = count_bid_results()
    if total >= 3:
        click.echo(f"\n  누적 입찰 데이터 {total}건 — 패턴 분석을 실행할까요? [y/n]")
        try:
            ans = input("  > ").strip().lower()
        except EOFError:
            ans = "n"
        if ans in ("y", "yes", "예"):
            click.echo("\n  패턴 분석 중...")
            analysis = analyze_bid_patterns()
            _print_bid_analysis(analysis)


def _print_bid_analysis(analysis: dict):
    """입찰 패턴 분석 결과 출력."""
    click.echo("\n─────────────────────────────────────────")
    click.echo("  [입찰 패턴 분석]")
    click.echo("─────────────────────────────────────────")

    if not analysis.get("analysis") and analysis.get("message"):
        click.echo(f"  {analysis['message']}")
        return

    win_rate = analysis.get("win_rate", 0)
    click.echo(f"  수주율: {win_rate:.0%}  "
               f"(전체 {analysis.get('total_count',0)}건 / "
               f"수주 {analysis.get('win_count',0)}건 / "
               f"탈락 {analysis.get('loss_count',0)}건)")

    for key, label in [
        ("success_factors",          "성공 패턴"),
        ("failure_factors",          "실패 패턴"),
        ("strategic_recommendations","전략 제언"),
    ]:
        items = analysis.get(key, [])
        if items:
            click.echo(f"\n  [{label}]")
            for item in items:
                click.echo(f"    • {item}")

    summary = analysis.get("summary", "")
    if summary:
        click.echo(f"\n  [총평]\n  {summary}")


# ─────────────────────────────────────────────
# TXT 저장 / DB 저장 헬퍼
# ─────────────────────────────────────────────

def _save_txt(dna, results, output_dir) -> str:
    try:
        path = write_txt(dna, results, output_dir)
        click.echo(f"  TXT 저장 완료: {path}")
        return path
    except Exception as e:
        click.echo(f"  [경고] TXT 생성 실패: {e}")
        import traceback; traceback.print_exc()
        return ""


def _save_case_to_db(dna, results) -> int:
    try:
        dna_json    = json.dumps(dataclasses.asdict(dna), ensure_ascii=False)
        result_json = json.dumps(results.get("final_proposal", {}), ensure_ascii=False)
        case_id = save_case(
            client_name=dna.client_name,
            project_name=dna.project_name,
            video_type=dna.video_type,
            dna_json=dna_json,
            result_json=result_json,
            agency_type=dna.agency_type,
            budget=dna.budget,
            deadline=dna.deadline,
        )
        click.echo(f"  케이스 DB 저장 완료 (ID: {case_id})")
        return case_id
    except Exception as e:
        click.echo(f"  [경고] DB 저장 실패 (계속 진행): {e}")
        return -1


if __name__ == "__main__":
    main()

# ProposalAI TODO

## 🔴 1순위: 긴급 버그

### 1. marketer.py 마케팅 전략 내용 미생성
- [x] run() 함수 3분할 API 호출로 리팩터 (part1: 유통/SEO/인플루언서, part3: SNS채널, part2: KPI)
- [x] 오류 조용히 삼키는 코드 → try/except + print 오류 로그로 교체
- [x] 테스트 통과 (youtube_seo 6키, influencer_strategy 3키, sns_channels 4키)

### 2. 이력 페이지 표시 불일치
- [x] _latest() 쿼리를 case_created_at >= 기준으로 수정 (다른 실행의 데이터 오염 방지)
- [x] script_results 조회도 case_ts 기준 필터 추가
- [x] research_results에 result_json 컬럼 추가 (전체 결과 저장)
- [x] save_research() → result_json에 전체 dict 저장
- [x] detail.html 대본 렌더링: 아웃라인 포맷 씬별 카드로 표시
- [x] detail.html 리서치 렌더링: 12개 필드 + 미정의 필드 추가 렌더

### 3. 기대효과 빈칸
- [x] strategist.py: expected_effects 부족 시 자동 재시도 로직 구현

---

## 🟡 2순위: 기능 개선

### 4. RFP 업로드 시 입력 폼 자동 채우기
- [x] /rfp_analyze POST 엔드포인트 구현
- [x] rfp_quick_extract() 함수 (rfp_parser.py)
- [x] index.html autoFillFromRfp() + applyAutofill() JS 구현
- [x] 드래그&드롭 파일 선택 시 자동 분석 트리거

### 5. 스텝 시작 전 사전 지시 입력창
- [x] run.html 컨펌 패널에 "다음 스텝 특별 지시" textarea 추가
- [x] /confirm 라우트 instruction 파라미터 처리
- [x] wait_confirm() → dna.step_instruction 주입
- [x] web_pipeline.py: 스텝 성공 후 step_instruction 초기화

### 6. 스텝 스킵 기능 확인 및 수정
- [x] 스킵 시 results[step_key] = {} → result로 수정 (결과 버림 버그 수정)
- [x] 웹 UI 스킵 버튼 작동 확인

### 7. 대본 속도 개선
- [x] 제안서용 아웃라인 모드 구현 (_generate_longform_outline)
- [x] 1편: 최대 7씬 + 나레이션 방향 (is_sample=True)
- [x] 나머지: 최대 5씬 + 핵심 포인트만 (is_sample=False)
- [x] 결과: 3편 66초 (22초/편, 목표 30초 이내 달성)

### 8. 진행 중인 작업으로 돌아가기 메뉴
- [x] base.html 상단 네비에 🔄 진행 중 메뉴 추가
- [x] 빨간 점 표시 (nav-active-dot)
- [x] /active_run 엔드포인트 (현재 사용자의 진행 중 세션 반환)

### 9. 동시 접속 지원
- [x] 작업 큐 시스템 (_job_queue deque + _queue_worker 스레드)
- [x] 대기 중 위치 SSE 브로드캐스트

### 10. PPT 슬라이드 구조 개선
- [x] 7가지 유형 구현: cover / toc / content / process / compare / number / message
- [x] 흑백 도식화 (모노크롬 디자인)

### 11. 참고 제안서 업로드
- [x] index.html 참고 제안서 파일 업로드 UI (드래그&드롭)
- [x] parse_reference_proposal() 구현 (rfp_parser.py)
- [x] dna.reference_structure 필드
- [x] dna_to_context_string() → 모든 에이전트에 자동 주입

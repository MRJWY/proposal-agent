# Proposal Agent

정부 R&D 사업공고(IRIS · 중기부 · NIPA)를 자동 수집·분석하고,  
Streamlit 통합 대시보드와 Slack 연동으로 의사결정을 지원하는 AI 에이전트입니다.

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| 📄 **사업공고 분석 및 요약** | Google Sheets 공고 데이터를 LLM으로 자동 요약, 핵심 포인트·적합도·리스크 분석 |
| 📊 **통합 대시보드** | IRIS·MSS·NIPA 전체 공고를 KPI·차트·목록으로 한눈에 확인 (Streamlit) |
| 🔔 **Slack 연동** | 일일 브리핑·신규 공고 알림 자동 발송, 버튼 클릭으로 승인/기각/검토 의사결정 |

---

## 아키텍처

```
proposal_agent/
├── schemas.py          # 데이터 스키마 (ProposalSummary, DashboardStats)
├── analyzer.py         # 공고 로딩 + LLM 분석 + 필터/정렬 로직
├── slack_agent.py      # Slack Block Kit 빌더 + Webhook/Bot API 발송
├── dashboard.py        # Streamlit 대시보드 (5개 탭)
├── run_daily.py        # GitHub Actions / cron 진입점
└── run_slack_server.py # Interactive 버튼 수신 Flask 서버
proposal_dashboard_app.py  # Streamlit 실행 진입점
.github/workflows/
└── proposal_agent.yml  # 매일 07:05 / 20:05 KST 자동 실행
```

---

## 대시보드 탭 구성

| 탭 | 내용 |
|----|------|
| 📊 **Overview** | KPI 카드(전체·추천·마감임박·고우선미결) + 출처/추천/의사결정 분포 차트 |
| 📋 **공고 목록** | 필터(출처·추천·의사결정·마감임박·키워드) + 정렬 + 클릭으로 상세 이동 |
| 🔍 **상세 분석** | AI 요약 · 핵심 포인트 · 적합도 근거 · 즉시 조치 · 리스크 플래그 |
| ✅ **의사결정** | 승인/기각/검토중 처리 + 담당자·의견 입력 + Google Sheets 저장 + 이력 |
| 🔔 **Slack 발송** | 일일 브리핑·개별 공고 알림·마감 임박 알림 직접 발송 |

---

## 빠른 시작

### 1. 환경 설정

```bash
git clone https://github.com/MRJWY/proposal-agent.git
cd proposal-agent
pip install -r requirements.txt
cp .env.example .env
# .env 파일에 필요한 값 입력
```

### 2. 대시보드 실행

```bash
streamlit run proposal_dashboard_app.py
```

### 3. 일일 알림 수동 실행

```bash
# 일일 브리핑 발송
python proposal_agent/run_daily.py

# 신규 추천 공고만 발송
PROPOSAL_SLACK_MODE=new python proposal_agent/run_daily.py

# 마감 임박 공고 발송
PROPOSAL_SLACK_MODE=due python proposal_agent/run_daily.py
```

### 4. Slack Interactive 서버 실행 (의사결정 버튼)

```bash
python proposal_agent/run_slack_server.py
# → http://localhost:3000/slack/actions
```

---

## 환경 변수

| 변수명 | 필수 | 설명 |
|--------|------|------|
| `GOOGLE_SHEET_ID` | ✅ | Google Sheets 문서 ID |
| `GOOGLE_CREDENTIALS_JSON` | ✅ | 서비스 계정 credentials.json 경로 |
| `OPENAI_API_KEY` | ✅ | OpenAI API Key (LLM 분석) |
| `SLACK_WEBHOOK_URL` | ✅ | Slack Incoming Webhook URL |
| `OPENAI_MODEL` | - | 기본값: `gpt-4o-mini` |
| `APP_URL` | - | 대시보드 URL (Slack 링크용) |
| `SLACK_BOT_TOKEN` | - | Interactive 버튼 사용 시 필요 |
| `SLACK_SIGNING_SECRET` | - | Interactive 버튼 보안 검증 |
| `SLACK_SERVER_PORT` | - | Interactive 서버 포트 (기본 3000) |
| `IRIS_OPPORTUNITY_MASTER_SHEET` | - | 기본값: `OPPORTUNITY_MASTER` |
| `MSS_OPPORTUNITY_MASTER_SHEET` | - | 기본값: `MSS_OPPORTUNITY_MASTER` |
| `NIPA_OPPORTUNITY_MASTER_SHEET` | - | 기본값: `NIPA_OPPORTUNITY_MASTER` |
| `PROPOSAL_SLACK_MODE` | - | `brief` / `new` / `due` (기본 `brief`) |
| `PROPOSAL_LLM_MAX_ITEMS` | - | LLM 분석 최대 건수 (기본 20) |
| `SLACK_DUE_SOON_DAYS` | - | 마감 임박 기준 일수 (기본 7) |

---

## GitHub Secrets 설정

```
GOOGLE_SHEET_ID          → Google Sheets 문서 ID
GOOGLE_CREDENTIALS_JSON_B64 → credentials.json을 base64 인코딩한 값
OPENAI_API_KEY           → OpenAI API Key
SLACK_WEBHOOK_URL        → Slack Webhook URL
```

GitHub Variables:
```
APP_URL                  → 배포된 Streamlit 대시보드 URL
```

---

## Google Sheets 연동 구조

| 시트명 | 역할 |
|--------|------|
| `OPPORTUNITY_MASTER` | IRIS RFP-level 공고 마스터 |
| `MSS_OPPORTUNITY_MASTER` | 중기부 공고 마스터 |
| `NIPA_OPPORTUNITY_MASTER` | NIPA 공고 마스터 |
| `PROPOSAL_DECISIONS` | 의사결정 결과 저장 (Agent 자동 생성) |

---

## Slack 연동 설정

### Incoming Webhook (기본 알림)
1. [api.slack.com/apps](https://api.slack.com/apps) → Create New App
2. Features → Incoming Webhooks → ON
3. Webhook URL을 `SLACK_WEBHOOK_URL`에 저장

### Interactive Components (의사결정 버튼)
1. Features → Interactivity & Shortcuts → ON
2. Request URL: `https://<your-server>/slack/actions`
3. Slash Commands → `/proposal` → `https://<your-server>/slack/slash`

### 슬래시 커맨드
```
/proposal list              → 추천 공고 목록
/proposal status            → 오늘 현황 통계
/proposal approve <id>      → 공고 승인
/proposal reject <id>       → 공고 기각
/proposal reviewing <id>    → 검토 중으로 변경
```

---

## GitHub Actions 자동 스케줄

| 스케줄 | 실행 시각 (KST) |
|--------|-----------------|
| 일일 브리핑 | 매일 07:05, 20:05 |

`workflow_dispatch`로 수동 실행 및 모드 선택 가능

---

## 기존 IRIS 파이프라인 연동

이 레포는 [iris_auto_crawling](https://github.com/MRJWY/iris_auto_crawling) 파이프라인이 생성한 Google Sheets 데이터를 읽어 작동합니다.  
IRIS → Google Sheets 적재가 선행되어야 합니다.

```
iris_auto_crawling (크롤링·분석·적재)
        ↓  Google Sheets
proposal-agent (분석·요약·의사결정·Slack 알림)
```

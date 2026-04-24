"""
proposal_agent/run_daily.py

GitHub Actions / cron 스케줄 진입점
- Google Sheets에서 공고 데이터 로드
- LLM 분석 (선택)
- Slack 일일 브리핑 또는 신규 공고 알림 발송
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv()


def clean(v) -> str:
    return str(v or "").strip()


def safe_int(v, default=0) -> int:
    try:
        return int(float(str(v).strip()))
    except Exception:
        return default


def main():
    sheet_id   = clean(os.getenv("GOOGLE_SHEET_ID"))
    creds_path = clean(os.getenv("GOOGLE_CREDENTIALS_JSON"))
    webhook    = clean(os.getenv("SLACK_WEBHOOK_URL"))
    api_key    = clean(os.getenv("OPENAI_API_KEY"))
    model      = clean(os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    app_url    = clean(os.getenv("APP_URL", ""))
    due_days   = safe_int(os.getenv("SLACK_DUE_SOON_DAYS", "7"), 7)
    max_llm    = safe_int(os.getenv("PROPOSAL_LLM_MAX_ITEMS", "20"), 20)
    mode       = clean(os.getenv("PROPOSAL_SLACK_MODE", "brief")).lower()  # brief | new | due

    iris_master = clean(os.getenv("IRIS_OPPORTUNITY_MASTER_SHEET", "OPPORTUNITY_MASTER"))
    mss_master  = clean(os.getenv("MSS_OPPORTUNITY_MASTER_SHEET",  "MSS_OPPORTUNITY_MASTER"))
    nipa_master = clean(os.getenv("NIPA_OPPORTUNITY_MASTER_SHEET", "NIPA_OPPORTUNITY_MASTER"))

    # ── 필수값 체크 ──
    if not sheet_id or not creds_path:
        raise SystemExit("[ERROR] GOOGLE_SHEET_ID / GOOGLE_CREDENTIALS_JSON 환경변수 필요")
    if not webhook:
        raise SystemExit("[ERROR] SLACK_WEBHOOK_URL 환경변수 필요")

    # ── import ──
    from proposal_agent.analyzer import (
        get_gc,
        load_all_notices,
        enrich_with_llm,
        compute_stats,
        filter_summaries,
    )
    from proposal_agent.slack_agent import (
        post_daily_brief,
        post_notice_alert,
        post_to_slack,
        build_notice_blocks,
    )

    # ── 데이터 로딩 ──
    print(f"[INFO] mode={mode}, due_days={due_days}, llm_max={max_llm}")
    gc = get_gc(creds_path)
    sh = gc.open_by_key(sheet_id)

    summaries = load_all_notices(
        sh,
        iris_master_sheet=iris_master,
        mss_master_sheet=mss_master,
        nipa_master_sheet=nipa_master,
    )
    print(f"[INFO] loaded {len(summaries)} proposals")

    # ── LLM 분석 ──
    if api_key and max_llm > 0:
        summaries = enrich_with_llm(
            summaries, api_key=api_key, model=model, max_items=max_llm
        )

    stats = compute_stats(summaries, due_days=due_days)

    # ── Slack 발송 ──
    if mode == "new":
        # 신규 추천 공고만 개별 카드 발송
        new_recommended = [
            s for s in summaries
            if s.recommendation == "추천" and s.decision in ("pending", "")
        ]
        sent = post_notice_alert(
            webhook, new_recommended, app_url=app_url, max_notices=5
        )
        print(f"[OK] new-notice alert: {sent} sent")

    elif mode == "due":
        # 마감 임박 공고 발송
        due_soon = filter_summaries(summaries, due_within_days=due_days)
        sent = 0
        for s in due_soon[:5]:
            try:
                blocks = build_notice_blocks(s, app_url=app_url)
                post_to_slack(webhook, blocks=blocks)
                sent += 1
            except Exception as e:
                print(f"[WARN] slack post failed: {e}")
        print(f"[OK] due-soon alert: {sent} sent")

    else:
        # 기본: 일일 브리핑
        post_daily_brief(
            webhook, summaries, stats,
            app_url=app_url, due_days=due_days,
        )
        print("[OK] daily brief sent")

    print("[OK] proposal_agent run_daily completed")


if __name__ == "__main__":
    main()

"""
proposal_agent/run_slack_server.py

Slack Interactive Component / Slash Command 수신 서버
- Flask 앱 실행
- 의사결정 버튼 클릭 → Google Sheets PROPOSAL_DECISIONS 시트 upsert
- /proposal 슬래시 커맨드 처리
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 프로젝트 루트를 path에 추가
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv()


def clean(v) -> str:
    return str(v or "").strip()


def main():
    sheet_id    = clean(os.getenv("GOOGLE_SHEET_ID"))
    creds_path  = clean(os.getenv("GOOGLE_CREDENTIALS_JSON"))
    bot_token   = clean(os.getenv("SLACK_BOT_TOKEN", ""))
    signing_sec = clean(os.getenv("SLACK_SIGNING_SECRET", ""))
    port        = int(clean(os.getenv("SLACK_SERVER_PORT", "3000")) or "3000")

    if not sheet_id or not creds_path:
        raise SystemExit("GOOGLE_SHEET_ID / GOOGLE_CREDENTIALS_JSON 환경변수 필요")

    from proposal_agent.analyzer import get_gc, upsert_decision_sheet
    from proposal_agent.slack_agent import create_slack_app

    gc = get_gc(creds_path)
    sh = gc.open_by_key(sheet_id)

    def on_decision(notice_id: str, source_site: str, decision: str, user: str):
        """Slack 버튼 클릭 시 호출되는 콜백"""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        SEOUL_TZ = ZoneInfo("Asia/Seoul")
        now_str = datetime.now(SEOUL_TZ).strftime("%Y-%m-%d %H:%M:%S")

        dec_data = {
            "notice_id":        notice_id,
            "source_site":      source_site,
            "notice_title":     "",   # 버튼 payload에서 title을 추가로 전달하면 채울 수 있음
            "decision":         decision,
            "decision_by":      user,
            "decision_at":      now_str,
            "decision_comment": "",
        }
        upsert_decision_sheet(sh, [dec_data])
        print(f"[OK] decision saved: {notice_id} / {decision} by {user}")

    flask_app, _port = create_slack_app(
        signing_secret=signing_sec,
        bot_token=bot_token,
        on_decision_callback=on_decision,
        port=port,
    )

    print(f"[OK] Slack interaction server running on port {port}")
    print(f"[INFO] POST /slack/actions  — Interactive Components")
    print(f"[INFO] POST /slack/slash    — Slash Commands")
    print(f"[INFO] GET  /health         — Health Check")
    flask_app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()

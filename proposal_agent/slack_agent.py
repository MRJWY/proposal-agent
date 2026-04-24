"""
proposal_agent/slack_agent.py

Slack 연동 모듈
- 공고 알림 발송 (신규/마감임박/추천공고)
- Interactive Message (Block Kit) 으로 의사결정 버튼 제공
- Slash Command / Webhook 수신 → 의사결정 처리
- Flask 기반 Slack Event/Action 수신 서버 (선택 실행)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from proposal_agent.schemas import (
    ProposalSummary,
    DashboardStats,
    DECISION_LABELS,
    RECOMMENDATION_EMOJI,
    SOURCE_EMOJI,
)

SEOUL_TZ = ZoneInfo("Asia/Seoul")

# ──────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────

def clean(v) -> str:
    return str(v or "").strip()


def _post(url: str, payload: dict, timeout: int = 20) -> dict:
    if not HAS_REQUESTS:
        raise RuntimeError("requests 패키지 필요")
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {"ok": True, "raw": resp.text}


def _post_with_token(url: str, payload: dict, bot_token: str, timeout: int = 20) -> dict:
    if not HAS_REQUESTS:
        raise RuntimeError("requests 패키지 필요")
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ──────────────────────────────────────────────
# Block Kit 빌더
# ──────────────────────────────────────────────

def _header_block(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def _section_block(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _divider() -> dict:
    return {"type": "divider"}


def _fields_block(fields: list[tuple[str, str]]) -> dict:
    return {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*{k}*\n{v}"}
            for k, v in fields
        ],
    }


def _action_block(notice_id: str, source_site: str, notice_title_short: str) -> dict:
    """승인/기각/검토중 버튼"""
    payload_base = json.dumps({"notice_id": notice_id, "source_site": source_site})
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ 제안 승인", "emoji": True},
                "style": "primary",
                "action_id": "proposal_approve",
                "value": payload_base,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "❌ 제안 기각", "emoji": True},
                "style": "danger",
                "action_id": "proposal_reject",
                "value": payload_base,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔍 검토 중으로 변경", "emoji": True},
                "action_id": "proposal_reviewing",
                "value": payload_base,
            },
        ],
    }


def _context_block(text: str) -> dict:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text}],
    }


# ──────────────────────────────────────────────
# 공고 알림 메시지 빌드
# ──────────────────────────────────────────────

def build_notice_blocks(s: ProposalSummary, app_url: str = "") -> list[dict]:
    """단건 공고 상세 Block Kit 메시지"""
    src_emoji = SOURCE_EMOJI.get(s.source_site, "📋")
    rec_emoji = RECOMMENDATION_EMOJI.get(s.recommendation, "")
    title_short = (s.notice_title[:50] + "…") if len(s.notice_title) > 50 else s.notice_title

    blocks: list[dict] = [
        _header_block(f"{src_emoji} [{s.source_site}] 신규 사업공고"),
        _section_block(f"*{rec_emoji} {s.notice_title}*"),
        _divider(),
    ]

    # 기본 정보
    fields = [
        ("담당기관", s.agency or s.ministry or "-"),
        ("접수기간", f"{s.period or '-'} ({s.d_day or '-'})"),
    ]
    if s.total_budget_text:
        fields.append(("총사업비", s.total_budget_text))
    if s.per_project_budget_text:
        fields.append(("과제당 예산", s.per_project_budget_text))
    if s.rfp_score:
        fields.append(("적합도 점수", f"{s.rfp_score}점"))
    if s.recommendation:
        fields.append(("추천 여부", f"{rec_emoji} {s.recommendation}"))
    blocks.append(_fields_block(fields))

    # AI 요약
    if s.summary:
        blocks.append(_divider())
        blocks.append(_section_block(f"*🤖 AI 분석 요약*\n{s.summary}"))

    # 핵심 포인트
    if s.key_points:
        kp_text = "\n".join(f"• {p}" for p in s.key_points[:5])
        blocks.append(_section_block(f"*📌 핵심 포인트*\n{kp_text}"))

    # 적합도 근거
    if s.fit_reason:
        blocks.append(_section_block(f"*💡 참여 적합도*\n{s.fit_reason}"))

    # 즉시 조치 필요
    if s.action_required:
        blocks.append(_section_block(f"*⚡ 즉시 조치 필요*\n{s.action_required}"))

    # 리스크
    if s.risk_flags:
        risk_text = "\n".join(f"⚠️ {r}" for r in s.risk_flags[:3])
        blocks.append(_section_block(f"*리스크 요인*\n{risk_text}"))

    # 의사결정 버튼
    blocks.append(_divider())
    blocks.append(_action_block(s.notice_id, s.source_site, title_short))

    # 링크 / 앱 URL
    context_parts = []
    if s.detail_link:
        context_parts.append(f"<{s.detail_link}|공고 원문 보기>")
    if app_url:
        context_parts.append(f"<{app_url}|대시보드 열기>")
    if context_parts:
        blocks.append(_context_block(" | ".join(context_parts)))

    return blocks


def build_daily_brief_blocks(
    summaries: list[ProposalSummary],
    stats: DashboardStats,
    app_url: str = "",
    due_days: int = 7,
) -> list[dict]:
    """일일 브리핑 메시지 (요약 통계 + 최우선 공고 목록)"""
    today_str = datetime.now(SEOUL_TZ).strftime("%Y-%m-%d")
    blocks: list[dict] = [
        _header_block(f"📊 Proposal Agent 일일 브리핑 ({today_str})"),
        _divider(),
    ]

    # 전체 통계
    stat_lines = [
        f"*📋 전체 공고:* {stats.total_notices}건",
        f"*⭐ 추천 공고:* {stats.by_recommendation.get('추천', 0)}건",
        f"*⏰ 마감 임박 ({due_days}일):* {stats.due_soon_count}건",
        f"*🆕 오늘 신규:* {stats.new_today_count}건",
        f"*🔥 고우선 미결:* {stats.high_priority_count}건",
    ]
    blocks.append(_section_block("\n".join(stat_lines)))

    # 출처별 분포
    source_parts = [f"{SOURCE_EMOJI.get(k,'📋')} {k}: {v}건" for k, v in stats.by_source.items()]
    if source_parts:
        blocks.append(_context_block("  |  ".join(source_parts)))

    blocks.append(_divider())

    # 최우선 공고 (추천 + pending, D-day 가까운 순, 최대 5건)
    priority = [
        s for s in summaries
        if s.recommendation == "추천" and s.decision in ("pending", "reviewing")
    ]
    priority = sorted(priority, key=lambda s: (s.d_day or "Z"))[:5]

    if priority:
        blocks.append(_section_block("*🔥 즉시 검토 필요 공고*"))
        for s in priority:
            rec_emoji = RECOMMENDATION_EMOJI.get(s.recommendation, "")
            src_emoji = SOURCE_EMOJI.get(s.source_site, "📋")
            title_short = (s.notice_title[:45] + "…") if len(s.notice_title) > 45 else s.notice_title
            summary_short = (s.summary[:80] + "…") if s.summary and len(s.summary) > 80 else s.summary
            line = f"{src_emoji} {rec_emoji} *{title_short}*"
            if s.d_day:
                line += f"  |  {s.d_day}"
            if s.total_budget_text:
                line += f"  |  💰 {s.total_budget_text}"
            blocks.append(_section_block(line))
            if summary_short:
                blocks.append(_context_block(f"🤖 {summary_short}"))
            blocks.append(_action_block(s.notice_id, s.source_site, title_short))

    else:
        blocks.append(_section_block("_현재 즉시 검토 필요한 추천 공고가 없습니다._"))

    # 앱 링크
    if app_url:
        blocks.append(_divider())
        blocks.append(_context_block(f"<{app_url}|📊 전체 대시보드 열기>"))

    return blocks


def build_decision_ack_blocks(
    notice_id: str,
    source_site: str,
    notice_title: str,
    decision: str,
    user: str = "",
) -> list[dict]:
    """의사결정 완료 확인 메시지"""
    label = DECISION_LABELS.get(decision, decision)
    src_emoji = SOURCE_EMOJI.get(source_site, "📋")
    who = f" by {user}" if user else ""
    return [
        _section_block(
            f"{src_emoji} *[{source_site}] 의사결정 완료*\n"
            f"공고: {notice_title[:60]}\n"
            f"결정: *{label}*{who}\n"
            f"처리 시각: {datetime.now(SEOUL_TZ).strftime('%Y-%m-%d %H:%M')}"
        )
    ]


# ──────────────────────────────────────────────
# Webhook 발송
# ──────────────────────────────────────────────

def post_to_slack(webhook_url: str, text: str = "", blocks: list[dict] | None = None) -> None:
    payload: dict = {}
    if text:
        payload["text"] = text
    if blocks:
        payload["blocks"] = blocks
        if not text:
            payload["text"] = "Proposal Agent 알림"  # fallback
    _post(webhook_url, payload)


def post_notice_alert(
    webhook_url: str,
    summaries: list[ProposalSummary],
    app_url: str = "",
    max_notices: int = 5,
    only_recommended: bool = True,
) -> int:
    """추천 신규 공고 알림 발송 (최대 max_notices 건)"""
    targets = [
        s for s in summaries
        if (not only_recommended or s.recommendation == "추천")
    ][:max_notices]

    sent = 0
    for s in targets:
        blocks = build_notice_blocks(s, app_url=app_url)
        try:
            post_to_slack(webhook_url, blocks=blocks)
            sent += 1
            time.sleep(0.5)
        except Exception as e:
            print(f"[WARN] slack post failed for {s.notice_id}: {e}")

    print(f"[OK] sent {sent} notice alerts to Slack")
    return sent


def post_daily_brief(
    webhook_url: str,
    summaries: list[ProposalSummary],
    stats: DashboardStats,
    app_url: str = "",
    due_days: int = 7,
) -> None:
    """일일 브리핑 발송"""
    blocks = build_daily_brief_blocks(summaries, stats, app_url=app_url, due_days=due_days)
    post_to_slack(webhook_url, blocks=blocks)
    print("[OK] sent daily brief to Slack")


# ──────────────────────────────────────────────
# Slack Bot API (채널에 메시지 발송 / 응답)
# ──────────────────────────────────────────────

def post_message_to_channel(
    bot_token: str,
    channel: str,
    text: str = "",
    blocks: list[dict] | None = None,
    thread_ts: str = "",
) -> dict:
    payload: dict = {"channel": channel}
    if text:
        payload["text"] = text
    if blocks:
        payload["blocks"] = blocks
        if not text:
            payload["text"] = "Proposal Agent 알림"
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return _post_with_token("https://slack.com/api/chat.postMessage", payload, bot_token)


def update_message(
    bot_token: str,
    channel: str,
    ts: str,
    text: str = "",
    blocks: list[dict] | None = None,
) -> dict:
    """기존 메시지 업데이트 (버튼 클릭 후 결과 반영)"""
    payload: dict = {"channel": channel, "ts": ts}
    if text:
        payload["text"] = text
    if blocks:
        payload["blocks"] = blocks
    return _post_with_token("https://slack.com/api/chat.update", payload, bot_token)


# ──────────────────────────────────────────────
# Slack Signing Secret 검증
# ──────────────────────────────────────────────

def verify_slack_signature(
    signing_secret: str,
    request_body: bytes,
    timestamp: str,
    signature: str,
) -> bool:
    basestring = f"v0:{timestamp}:{request_body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    # 5분 이상 된 요청 거부
    if abs(time.time() - int(timestamp)) > 300:
        return False
    return hmac.compare_digest(expected, signature)


# ──────────────────────────────────────────────
# Flask Slack Interaction 서버
# ──────────────────────────────────────────────

def create_slack_app(
    signing_secret: str,
    bot_token: str,
    on_decision_callback,
    port: int = 3000,
):
    """
    Slack Interactive Component 수신 Flask 앱 생성
    on_decision_callback(notice_id, source_site, decision, user_name) → None
    """
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        raise RuntimeError("flask 패키지 필요: pip install flask")

    app = Flask(__name__)

    @app.route("/slack/actions", methods=["POST"])
    def slack_actions():
        # 서명 검증
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if signing_secret:
            if not verify_slack_signature(signing_secret, request.get_data(), timestamp, signature):
                return jsonify({"error": "invalid signature"}), 403

        payload_raw = request.form.get("payload", "")
        try:
            payload = json.loads(payload_raw)
        except Exception:
            return jsonify({"error": "bad payload"}), 400

        actions = payload.get("actions", [])
        if not actions:
            return jsonify({"ok": True})

        action = actions[0]
        action_id = action.get("action_id", "")
        value_raw = action.get("value", "{}")
        user = payload.get("user", {}).get("name", "unknown")
        channel = payload.get("channel", {}).get("id", "")
        message_ts = payload.get("message", {}).get("ts", "")

        try:
            value = json.loads(value_raw)
        except Exception:
            value = {}

        notice_id = clean(value.get("notice_id"))
        source_site = clean(value.get("source_site"))

        decision_map = {
            "proposal_approve": "approved",
            "proposal_reject": "rejected",
            "proposal_reviewing": "reviewing",
        }
        decision = decision_map.get(action_id, "pending")

        # 콜백 호출
        try:
            on_decision_callback(notice_id, source_site, decision, user)
        except Exception as e:
            print(f"[WARN] on_decision_callback error: {e}")

        # 메시지 업데이트 (버튼 제거)
        if channel and message_ts and bot_token:
            ack_blocks = build_decision_ack_blocks(
                notice_id, source_site,
                notice_title=clean(payload.get("message", {}).get("text", "")),
                decision=decision,
                user=user,
            )
            try:
                update_message(bot_token, channel, message_ts, blocks=ack_blocks)
            except Exception as e:
                print(f"[WARN] message update failed: {e}")

        return jsonify({"ok": True})

    @app.route("/slack/slash", methods=["POST"])
    def slack_slash():
        """
        /proposal list        → 추천 공고 목록
        /proposal status      → 오늘 현황 통계
        /proposal approve <id>
        /proposal reject <id>
        """
        text = clean(request.form.get("text", ""))
        parts = text.split()
        cmd = parts[0].lower() if parts else "list"
        response_url = clean(request.form.get("response_url", ""))
        user = clean(request.form.get("user_name", ""))

        if cmd == "status":
            msg = "📊 Proposal Agent — 상태 조회 중입니다. 잠시 후 대시보드를 확인하세요."
        elif cmd in ("approve", "승인") and len(parts) > 1:
            notice_id = parts[1]
            try:
                on_decision_callback(notice_id, "", "approved", user)
                msg = f"✅ {notice_id} — 제안 승인 처리되었습니다."
            except Exception as e:
                msg = f"오류: {e}"
        elif cmd in ("reject", "기각") and len(parts) > 1:
            notice_id = parts[1]
            try:
                on_decision_callback(notice_id, "", "rejected", user)
                msg = f"❌ {notice_id} — 제안 기각 처리되었습니다."
            except Exception as e:
                msg = f"오류: {e}"
        elif cmd == "reviewing" and len(parts) > 1:
            notice_id = parts[1]
            try:
                on_decision_callback(notice_id, "", "reviewing", user)
                msg = f"🔍 {notice_id} — 검토 중으로 변경되었습니다."
            except Exception as e:
                msg = f"오류: {e}"
        else:
            msg = (
                "*Proposal Agent 명령어*\n"
                "• `/proposal list` — 추천 공고 목록\n"
                "• `/proposal status` — 오늘 현황\n"
                "• `/proposal approve <notice_id>` — 공고 승인\n"
                "• `/proposal reject <notice_id>` — 공고 기각\n"
                "• `/proposal reviewing <notice_id>` — 검토 중 변경"
            )

        # 응답
        if response_url:
            try:
                _post(response_url, {"response_type": "in_channel", "text": msg})
            except Exception:
                pass
        return jsonify({"response_type": "in_channel", "text": msg})

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "service": "proposal-agent-slack"})

    return app, port


# ──────────────────────────────────────────────
# CLI 진입점
# ──────────────────────────────────────────────

def main():
    """일일 브리핑 및 신규 추천 공고 알림 발송"""
    load_dotenv()

    webhook_url = clean(os.getenv("SLACK_WEBHOOK_URL"))
    app_url = clean(os.getenv("APP_URL", ""))
    sheet_id = clean(os.getenv("GOOGLE_SHEET_ID"))
    creds_path = clean(os.getenv("GOOGLE_CREDENTIALS_JSON"))
    api_key = clean(os.getenv("OPENAI_API_KEY"))
    model = clean(os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    due_days = int(clean(os.getenv("SLACK_DUE_SOON_DAYS", "7")) or "7")
    max_llm = int(clean(os.getenv("PROPOSAL_LLM_MAX_ITEMS", "20")) or "20")
    max_alerts = int(clean(os.getenv("PROPOSAL_SLACK_MAX_ALERTS", "5")) or "5")
    alert_mode = clean(os.getenv("PROPOSAL_SLACK_MODE", "brief")).lower()  # brief | new | all

    if not webhook_url:
        raise SystemExit("SLACK_WEBHOOK_URL 환경변수 필요")
    if not sheet_id or not creds_path:
        raise SystemExit("GOOGLE_SHEET_ID / GOOGLE_CREDENTIALS_JSON 환경변수 필요")

    from proposal_agent.analyzer import (
        get_gc, load_all_notices, enrich_with_llm, compute_stats,
    )

    gc = get_gc(creds_path)
    sh = gc.open_by_key(sheet_id)

    summaries = load_all_notices(
        sh,
        iris_master_sheet=clean(os.getenv("IRIS_OPPORTUNITY_MASTER_SHEET", "OPPORTUNITY_MASTER")),
        mss_master_sheet=clean(os.getenv("MSS_OPPORTUNITY_MASTER_SHEET", "MSS_OPPORTUNITY_MASTER")),
        nipa_master_sheet=clean(os.getenv("NIPA_OPPORTUNITY_MASTER_SHEET", "NIPA_OPPORTUNITY_MASTER")),
    )

    if api_key:
        summaries = enrich_with_llm(summaries, api_key=api_key, model=model, max_items=max_llm)

    stats = compute_stats(summaries, due_days=due_days)

    if alert_mode == "new":
        # 신규 추천 공고만 개별 발송
        post_notice_alert(webhook_url, summaries, app_url=app_url, max_notices=max_alerts)
    else:
        # 기본: 일일 브리핑
        post_daily_brief(webhook_url, summaries, stats, app_url=app_url, due_days=due_days)

    print("[OK] Slack notification complete")


if __name__ == "__main__":
    main()

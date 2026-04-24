"""
proposal_agent/dashboard.py

통합 대시보드 Streamlit 앱
- 탭 1: 📊 Overview  — KPI 카드 + 출처/추천/의사결정 분포 차트
- 탭 2: 📋 공고 목록 — 필터/검색/정렬 테이블
- 탭 3: 🔍 상세 분석 — 선택 공고 AI 요약 + 핵심 포인트 + 리스크
- 탭 4: ✅ 의사결정  — 승인/기각/검토중 처리 + 이력 관리
- 탭 5: 🔔 Slack 발송 — 브리핑 / 개별 공고 알림 직접 발송
"""
from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st
from dotenv import load_dotenv

from proposal_agent.schemas import (
    ProposalSummary,
    DashboardStats,
    DECISION_LABELS,
    RECOMMENDATION_EMOJI,
    SOURCE_EMOJI,
)
from proposal_agent.analyzer import (
    get_gc,
    load_all_notices,
    enrich_with_llm,
    compute_stats,
    filter_summaries,
    sort_summaries,
    upsert_decision_sheet,
    extract_period_end,
)

load_dotenv()
SEOUL_TZ = ZoneInfo("Asia/Seoul")


# ──────────────────────────────────────────────
# 페이지 설정
# ──────────────────────────────────────────────

st.set_page_config(
    page_title="Proposal Agent Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ──────────────────────────────────────────────
# CSS 스타일
# ──────────────────────────────────────────────

st.markdown("""
<style>
/* KPI 카드 */
.kpi-card {
    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
    border: 1px solid #334155;
    border-radius: 12px;
    padding: 20px 24px;
    text-align: center;
    margin-bottom: 8px;
}
.kpi-label {
    font-size: 0.78rem;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 6px;
}
.kpi-value {
    font-size: 2.2rem;
    font-weight: 700;
    color: #f1f5f9;
    line-height: 1;
}
.kpi-sub {
    font-size: 0.75rem;
    color: #64748b;
    margin-top: 4px;
}
/* 공고 카드 */
.notice-card {
    background: #1e293b;
    border: 1px solid #334155;
    border-left: 4px solid #3b82f6;
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 10px;
}
.notice-card.recommend {
    border-left-color: #10b981;
}
.notice-card.normal {
    border-left-color: #f59e0b;
}
.notice-card.not-recommend {
    border-left-color: #6b7280;
}
/* 배지 */
.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 0.72rem;
    font-weight: 600;
    margin-right: 4px;
}
.badge-green  { background: #064e3b; color: #6ee7b7; }
.badge-yellow { background: #451a03; color: #fcd34d; }
.badge-gray   { background: #1f2937; color: #9ca3af; }
.badge-blue   { background: #1e3a5f; color: #93c5fd; }
.badge-red    { background: #450a0a; color: #fca5a5; }
/* 섹션 헤더 */
.section-header {
    font-size: 1.05rem;
    font-weight: 600;
    color: #e2e8f0;
    border-bottom: 1px solid #334155;
    padding-bottom: 8px;
    margin-bottom: 14px;
}
/* 결정 버튼 영역 */
.decision-box {
    background: #0f172a;
    border: 1px solid #1e3a5f;
    border-radius: 10px;
    padding: 16px 20px;
    margin-top: 12px;
}
</style>
""", unsafe_allow_html=True)


# ──────────────────────────────────────────────
# 환경 변수 / 설정
# ──────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return str(os.getenv(key) or default).strip()


SHEET_ID       = _env("GOOGLE_SHEET_ID")
CREDS_PATH     = _env("GOOGLE_CREDENTIALS_JSON")
OPENAI_KEY     = _env("OPENAI_API_KEY")
OPENAI_MODEL   = _env("OPENAI_MODEL", "gpt-4o-mini")
WEBHOOK_URL    = _env("SLACK_WEBHOOK_URL")
APP_URL        = _env("APP_URL", "")
IRIS_MASTER    = _env("IRIS_OPPORTUNITY_MASTER_SHEET", "OPPORTUNITY_MASTER")
MSS_MASTER     = _env("MSS_OPPORTUNITY_MASTER_SHEET",  "MSS_OPPORTUNITY_MASTER")
NIPA_MASTER    = _env("NIPA_OPPORTUNITY_MASTER_SHEET", "NIPA_OPPORTUNITY_MASTER")
DUE_DAYS       = int(_env("SLACK_DUE_SOON_DAYS", "7") or "7")
LLM_MAX        = int(_env("PROPOSAL_LLM_MAX_ITEMS", "20") or "20")


# ──────────────────────────────────────────────
# 데이터 로딩 (캐시)
# ──────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def _get_sheet_client():
    if not SHEET_ID or not CREDS_PATH:
        return None, None
    try:
        gc = get_gc(CREDS_PATH)
        sh = gc.open_by_key(SHEET_ID)
        return gc, sh
    except Exception as e:
        st.error(f"Google Sheets 연결 실패: {e}")
        return None, None


def _load_data(force_reload: bool = False) -> list[ProposalSummary]:
    cache_key = "proposal_summaries"
    if not force_reload and cache_key in st.session_state:
        return st.session_state[cache_key]

    _, sh = _get_sheet_client()
    if sh is None:
        return []

    with st.spinner("📡 Google Sheets에서 공고 데이터 로딩 중..."):
        summaries = load_all_notices(
            sh,
            iris_master_sheet=IRIS_MASTER,
            mss_master_sheet=MSS_MASTER,
            nipa_master_sheet=NIPA_MASTER,
        )

    st.session_state[cache_key] = summaries
    st.session_state["last_loaded"] = datetime.now(SEOUL_TZ).strftime("%H:%M:%S")
    return summaries


def _enrich_data(summaries: list[ProposalSummary]) -> list[ProposalSummary]:
    if not OPENAI_KEY:
        return summaries
    already = sum(1 for s in summaries if s.summary)
    if already >= len(summaries):
        return summaries
    with st.spinner(f"🤖 LLM 분석 중... (최대 {LLM_MAX}건)"):
        summaries = enrich_with_llm(
            summaries, api_key=OPENAI_KEY, model=OPENAI_MODEL, max_items=LLM_MAX
        )
    st.session_state["proposal_summaries"] = summaries
    return summaries


# ──────────────────────────────────────────────
# 유틸 렌더링 헬퍼
# ──────────────────────────────────────────────

def _badge_html(text: str, style: str = "gray") -> str:
    return f'<span class="badge badge-{style}">{text}</span>'


def _rec_badge(rec: str) -> str:
    if rec == "추천":   return _badge_html(f"⭐ {rec}", "green")
    if rec == "보통":   return _badge_html(f"🔹 {rec}", "yellow")
    if rec == "비추천": return _badge_html(f"🔸 {rec}", "gray")
    return _badge_html(rec or "미평가", "gray")


def _dec_badge(dec: str) -> str:
    mapping = {
        "approved":  ("green",  "✅ 승인"),
        "rejected":  ("red",    "❌ 기각"),
        "reviewing": ("blue",   "🔍 검토중"),
        "pending":   ("yellow", "⏳ 대기"),
    }
    style, label = mapping.get(dec, ("gray", dec or "대기"))
    return _badge_html(label, style)


def _src_badge(src: str) -> str:
    emoji = SOURCE_EMOJI.get(src, "📋")
    return _badge_html(f"{emoji} {src}", "blue")


def _kpi_card(label: str, value, sub: str = "") -> str:
    return f"""
<div class="kpi-card">
  <div class="kpi-label">{label}</div>
  <div class="kpi-value">{value}</div>
  {"<div class='kpi-sub'>" + sub + "</div>" if sub else ""}
</div>"""


def _dday_color(d_day: str) -> str:
    if not d_day or d_day == "마감":
        return "#6b7280"
    if d_day == "D-Day":
        return "#ef4444"
    try:
        n = int(d_day.replace("D-", ""))
        if n <= 3:  return "#ef4444"
        if n <= 7:  return "#f59e0b"
        return "#10b981"
    except Exception:
        return "#94a3b8"


# ──────────────────────────────────────────────
# 사이드바
# ──────────────────────────────────────────────

def render_sidebar(summaries: list[ProposalSummary]) -> dict:
    st.sidebar.markdown("## 🎛️ 필터 & 설정")

    # 새로고침
    col1, col2 = st.sidebar.columns(2)
    if col1.button("🔄 새로고침", use_container_width=True):
        st.session_state.pop("proposal_summaries", None)
        st.rerun()
    if col2.button("🤖 LLM 분석", use_container_width=True):
        if OPENAI_KEY:
            summaries_enriched = _enrich_data(summaries)
            st.session_state["proposal_summaries"] = summaries_enriched
            st.success("LLM 분석 완료!")
            st.rerun()
        else:
            st.sidebar.error("OPENAI_API_KEY 없음")

    last = st.session_state.get("last_loaded", "-")
    st.sidebar.caption(f"마지막 로딩: {last}")

    st.sidebar.divider()

    # 출처 필터
    sources = ["전체"] + sorted({s.source_site for s in summaries if s.source_site})
    source_filter = st.sidebar.selectbox("📡 출처", sources)

    # 추천 필터
    recs = ["전체", "추천", "보통", "비추천", "미평가"]
    rec_filter = st.sidebar.selectbox("⭐ 추천 여부", recs)

    # 의사결정 필터
    decs = ["전체", "pending", "reviewing", "approved", "rejected"]
    dec_labels_map = {
        "전체": "전체",
        "pending":   "⏳ 검토 대기",
        "reviewing": "🔍 검토 중",
        "approved":  "✅ 승인",
        "rejected":  "❌ 기각",
    }
    dec_filter_label = st.sidebar.selectbox("✅ 의사결정", list(dec_labels_map.values()))
    dec_filter = next(k for k, v in dec_labels_map.items() if v == dec_filter_label)

    # 마감 임박
    due_filter = st.sidebar.checkbox(f"⏰ {DUE_DAYS}일 이내 마감만")

    # 키워드 검색
    keyword = st.sidebar.text_input("🔍 키워드 검색", placeholder="예: AI, 플랫폼, IITP")

    # 정렬
    sort_options = {"적합도 점수": "score", "마감일 순": "dday", "등록일 최신": "registered"}
    sort_label = st.sidebar.selectbox("정렬 기준", list(sort_options.keys()))
    sort_by = sort_options[sort_label]

    st.sidebar.divider()
    st.sidebar.markdown("### 📊 요약")
    total = len(summaries)
    rec_cnt = sum(1 for s in summaries if s.recommendation == "추천")
    pending_cnt = sum(1 for s in summaries if s.decision in ("pending", ""))
    st.sidebar.metric("전체 공고", total)
    st.sidebar.metric("추천 공고", rec_cnt)
    st.sidebar.metric("미결 공고", pending_cnt)

    return {
        "source": None if source_filter == "전체" else source_filter,
        "recommendation": None if rec_filter == "전체" else rec_filter,
        "decision": None if dec_filter == "전체" else dec_filter,
        "due_within_days": DUE_DAYS if due_filter else None,
        "keyword": keyword.strip() if keyword.strip() else None,
        "sort_by": sort_by,
    }


# ──────────────────────────────────────────────
# 탭 1: Overview
# ──────────────────────────────────────────────

def render_overview(summaries: list[ProposalSummary], stats: DashboardStats) -> None:
    st.markdown("### 📊 전체 현황")

    # KPI 행
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    kpis = [
        (c1, "전체 공고",    stats.total_notices,                    ""),
        (c2, "추천 공고",    stats.by_recommendation.get("추천", 0), "⭐ 참여 검토 필요"),
        (c3, "마감 임박",    stats.due_soon_count,                   f"{DUE_DAYS}일 이내"),
        (c4, "오늘 신규",    stats.new_today_count,                  ""),
        (c5, "고우선 미결",  stats.high_priority_count,              "추천 + 미결정"),
        (c6, "의사결정 완료",
            stats.by_decision.get("approved", 0) + stats.by_decision.get("rejected", 0),
            "승인+기각"),
    ]
    for col, label, val, sub in kpis:
        col.markdown(_kpi_card(label, val, sub), unsafe_allow_html=True)

    st.markdown("---")

    # 차트 행
    left, mid, right = st.columns(3)

    # 출처별 분포
    with left:
        st.markdown('<div class="section-header">출처별 공고 수</div>', unsafe_allow_html=True)
        if stats.by_source:
            try:
                import plotly.graph_objects as go
                fig = go.Figure(go.Pie(
                    labels=list(stats.by_source.keys()),
                    values=list(stats.by_source.values()),
                    hole=0.55,
                    marker_colors=["#3b82f6", "#10b981", "#f59e0b"],
                    textinfo="label+value",
                ))
                fig.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font_color="#e2e8f0",
                    showlegend=False,
                    margin=dict(t=10, b=10, l=10, r=10),
                    height=230,
                )
                st.plotly_chart(fig, use_container_width=True)
            except ImportError:
                for k, v in stats.by_source.items():
                    st.metric(k, v)

    # 추천 분포
    with mid:
        st.markdown('<div class="section-header">추천 여부 분포</div>', unsafe_allow_html=True)
        if stats.by_recommendation:
            try:
                import plotly.graph_objects as go
                colors = {"추천": "#10b981", "보통": "#f59e0b", "비추천": "#6b7280", "미평가": "#334155"}
                labels = list(stats.by_recommendation.keys())
                values = list(stats.by_recommendation.values())
                bar_colors = [colors.get(l, "#3b82f6") for l in labels]
                fig = go.Figure(go.Bar(
                    x=labels, y=values,
                    marker_color=bar_colors,
                    text=values, textposition="outside",
                ))
                fig.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font_color="#e2e8f0",
                    margin=dict(t=10, b=10, l=10, r=10),
                    height=230,
                    yaxis=dict(gridcolor="#334155"),
                    xaxis=dict(gridcolor="#334155"),
                )
                st.plotly_chart(fig, use_container_width=True)
            except ImportError:
                for k, v in stats.by_recommendation.items():
                    st.metric(k, v)

    # 의사결정 현황
    with right:
        st.markdown('<div class="section-header">의사결정 현황</div>', unsafe_allow_html=True)
        if stats.by_decision:
            try:
                import plotly.graph_objects as go
                dec_colors = {
                    "approved":  "#10b981",
                    "rejected":  "#ef4444",
                    "reviewing": "#3b82f6",
                    "pending":   "#f59e0b",
                }
                dec_names = {
                    "approved": "✅ 승인", "rejected": "❌ 기각",
                    "reviewing": "🔍 검토중", "pending": "⏳ 대기",
                }
                labels = [dec_names.get(k, k) for k in stats.by_decision.keys()]
                values = list(stats.by_decision.values())
                bar_colors = [dec_colors.get(k, "#334155") for k in stats.by_decision.keys()]
                fig = go.Figure(go.Bar(
                    x=labels, y=values,
                    marker_color=bar_colors,
                    text=values, textposition="outside",
                ))
                fig.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font_color="#e2e8f0",
                    margin=dict(t=10, b=10, l=10, r=10),
                    height=230,
                    yaxis=dict(gridcolor="#334155"),
                    xaxis=dict(gridcolor="#334155"),
                )
                st.plotly_chart(fig, use_container_width=True)
            except ImportError:
                for k, v in stats.by_decision.items():
                    st.metric(DECISION_LABELS.get(k, k), v)

    st.markdown("---")
    st.markdown("### 🔥 즉시 검토 필요 공고 (추천 + 미결정)")

    priority = [
        s for s in summaries
        if s.recommendation == "추천" and s.decision in ("pending", "reviewing", "")
    ]
    priority = sorted(priority, key=lambda s: (s.d_day or "Z"))[:8]

    if not priority:
        st.info("현재 즉시 검토 필요한 추천 공고가 없습니다.")
        return

    for s in priority:
        _render_notice_card_compact(s)


def _render_notice_card_compact(s: ProposalSummary) -> None:
    rec_map = {"추천": "recommend", "보통": "normal", "비추천": "not-recommend"}
    card_cls = rec_map.get(s.recommendation, "")
    dday_color = _dday_color(s.d_day)
    title_short = s.notice_title[:70] + "…" if len(s.notice_title) > 70 else s.notice_title
    budget_str = s.total_budget_text or s.per_project_budget_text or "-"
    kw_str = ", ".join(s.keywords[:5]) if s.keywords else "-"

    badges = (
        _src_badge(s.source_site) +
        _rec_badge(s.recommendation) +
        _dec_badge(s.decision)
    )

    summary_html = f"<p style='color:#94a3b8;font-size:0.82rem;margin:6px 0 0 0;'>{s.summary[:120]}…</p>" if s.summary else ""

    st.markdown(f"""
<div class="notice-card {card_cls}">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;">
    <div style="flex:1;">
      <div style="margin-bottom:6px;">{badges}</div>
      <div style="color:#f1f5f9;font-size:0.95rem;font-weight:600;">{title_short}</div>
      {summary_html}
    </div>
    <div style="text-align:right;min-width:90px;margin-left:12px;">
      <div style="color:{dday_color};font-size:1.1rem;font-weight:700;">{s.d_day or '-'}</div>
      <div style="color:#64748b;font-size:0.72rem;">💰 {budget_str[:30]}</div>
    </div>
  </div>
  <div style="margin-top:8px;color:#64748b;font-size:0.75rem;">
    🏢 {s.agency or s.ministry or '-'} &nbsp;|&nbsp; 🏷️ {kw_str}
  </div>
</div>
""", unsafe_allow_html=True)


# ──────────────────────────────────────────────
# 탭 2: 공고 목록
# ──────────────────────────────────────────────

def render_notice_list(
    summaries: list[ProposalSummary],
    filters: dict,
) -> ProposalSummary | None:
    filtered = filter_summaries(
        summaries,
        source_site=filters.get("source"),
        recommendation=filters.get("recommendation"),
        decision=filters.get("decision"),
        due_within_days=filters.get("due_within_days"),
        keyword=filters.get("keyword"),
    )
    filtered = sort_summaries(filtered, by=filters.get("sort_by", "score"))

    st.markdown(f"### 📋 공고 목록 &nbsp;<span style='color:#64748b;font-size:0.85rem;'>({len(filtered)}건 / 전체 {len(summaries)}건)</span>", unsafe_allow_html=True)

    if not filtered:
        st.warning("필터 조건에 맞는 공고가 없습니다.")
        return None

    # 테이블 헤더
    hdr = st.columns([0.5, 3.5, 1.0, 1.0, 1.2, 1.2, 0.8])
    for col, txt in zip(hdr, ["#", "공고명", "출처", "추천", "의사결정", "D-Day", "점수"]):
        col.markdown(f"<span style='color:#94a3b8;font-size:0.8rem;font-weight:600;'>{txt}</span>", unsafe_allow_html=True)
    st.markdown("<hr style='margin:4px 0 8px 0;border-color:#334155;'>", unsafe_allow_html=True)

    selected: ProposalSummary | None = None

    for i, s in enumerate(filtered):
        cols = st.columns([0.5, 3.5, 1.0, 1.0, 1.2, 1.2, 0.8])
        cols[0].markdown(f"<span style='color:#475569;font-size:0.8rem;'>{i+1}</span>", unsafe_allow_html=True)

        title_disp = s.notice_title[:55] + "…" if len(s.notice_title) > 55 else s.notice_title
        if cols[1].button(title_disp, key=f"notice_{s.notice_id}_{i}", use_container_width=True):
            st.session_state["selected_notice_id"] = s.notice_id
            st.session_state["selected_notice_site"] = s.source_site
            selected = s

        cols[2].markdown(_src_badge(s.source_site), unsafe_allow_html=True)
        cols[3].markdown(_rec_badge(s.recommendation), unsafe_allow_html=True)
        cols[4].markdown(_dec_badge(s.decision), unsafe_allow_html=True)
        dday_color = _dday_color(s.d_day)
        cols[5].markdown(
            f"<span style='color:{dday_color};font-size:0.85rem;font-weight:600;'>{s.d_day or '-'}</span>",
            unsafe_allow_html=True,
        )
        score_color = "#10b981" if s.rfp_score >= 70 else ("#f59e0b" if s.rfp_score >= 50 else "#6b7280")
        cols[6].markdown(
            f"<span style='color:{score_color};font-size:0.85rem;font-weight:600;'>{s.rfp_score}</span>",
            unsafe_allow_html=True,
        )

    return selected


# ──────────────────────────────────────────────
# 탭 3: 상세 분석
# ──────────────────────────────────────────────

def render_detail(summaries: list[ProposalSummary]) -> None:
    # 선택된 공고 찾기
    sel_id   = st.session_state.get("selected_notice_id")
    sel_site = st.session_state.get("selected_notice_site")

    # 드롭다운으로도 선택 가능
    options = {f"[{s.source_site}] {s.notice_title[:60]}": s for s in summaries}
    selected_label = st.selectbox(
        "공고 선택",
        ["— 공고를 선택하세요 —"] + list(options.keys()),
        index=0,
    )

    s: ProposalSummary | None = None
    if selected_label != "— 공고를 선택하세요 —":
        s = options.get(selected_label)
    elif sel_id:
        for _s in summaries:
            if _s.notice_id == sel_id and (not sel_site or _s.source_site == sel_site):
                s = _s
                break

    if not s:
        st.info("위에서 공고를 선택하면 상세 분석 결과가 표시됩니다.")
        return

    # ── 헤더 ──
    src_emoji = SOURCE_EMOJI.get(s.source_site, "📋")
    rec_emoji = RECOMMENDATION_EMOJI.get(s.recommendation, "")
    st.markdown(
        f"<h3 style='color:#f1f5f9;'>{src_emoji} {s.notice_title}</h3>",
        unsafe_allow_html=True,
    )

    badges = _src_badge(s.source_site) + _rec_badge(s.recommendation) + _dec_badge(s.decision)
    st.markdown(badges, unsafe_allow_html=True)

    # ── 기본 정보 ──
    st.markdown("---")
    st.markdown('<div class="section-header">📌 기본 정보</div>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("담당기관", s.agency or s.ministry or "-")
        st.metric("접수기간", s.period or "-")
    with c2:
        st.metric("총사업비", s.total_budget_text or "-")
        st.metric("과제당 예산", s.per_project_budget_text or "-")
    with c3:
        dday_color = _dday_color(s.d_day)
        st.markdown(
            f"<div style='padding:10px;'><div style='color:#94a3b8;font-size:0.8rem;'>D-Day</div>"
            f"<div style='color:{dday_color};font-size:2rem;font-weight:700;'>{s.d_day or '-'}</div></div>",
            unsafe_allow_html=True,
        )
        st.metric("적합도 점수", f"{s.rfp_score}점")

    if s.keywords:
        st.markdown("**🏷️ 기술 키워드**")
        kw_html = " ".join([
            f"<span style='background:#1e3a5f;color:#93c5fd;padding:3px 10px;border-radius:12px;font-size:0.8rem;margin:2px;display:inline-block;'>{k}</span>"
            for k in s.keywords[:10]
        ])
        st.markdown(kw_html, unsafe_allow_html=True)

    if s.detail_link:
        st.markdown(f"[🔗 공고 원문 바로가기]({s.detail_link})")

    # ── AI 요약 ──
    st.markdown("---")
    st.markdown('<div class="section-header">🤖 AI 분석</div>', unsafe_allow_html=True)

    if not s.summary:
        if st.button("🤖 지금 LLM 분석 실행", key=f"llm_single_{s.notice_id}"):
            if not OPENAI_KEY:
                st.error("OPENAI_API_KEY가 없습니다.")
            else:
                with st.spinner("LLM 분석 중..."):
                    enriched = enrich_with_llm(
                        [s], api_key=OPENAI_KEY, model=OPENAI_MODEL, max_items=1,
                        target_recommendations=("추천", "보통", "비추천"),
                    )
                    if enriched:
                        s_new = enriched[0]
                        # 세션 캐시 업데이트
                        all_s = st.session_state.get("proposal_summaries", [])
                        for idx, _s in enumerate(all_s):
                            if _s.notice_id == s.notice_id and _s.source_site == s.source_site:
                                all_s[idx] = s_new
                                break
                        st.session_state["proposal_summaries"] = all_s
                        st.rerun()
    else:
        # 요약
        st.markdown(f"""
<div style="background:#0f172a;border-left:3px solid #3b82f6;border-radius:6px;padding:14px 18px;margin-bottom:12px;">
  <div style="color:#94a3b8;font-size:0.78rem;margin-bottom:6px;">📝 요약</div>
  <div style="color:#e2e8f0;line-height:1.7;">{s.summary}</div>
</div>""", unsafe_allow_html=True)

        # 핵심 포인트
        if s.key_points:
            st.markdown("**📌 핵심 포인트**")
            for pt in s.key_points:
                st.markdown(f"- {pt}")

        # 2컬럼: 적합도 근거 | 즉시 조치
        col_l, col_r = st.columns(2)
        with col_l:
            if s.fit_reason:
                st.markdown(f"""
<div style="background:#0f172a;border-left:3px solid #10b981;border-radius:6px;padding:12px 16px;">
  <div style="color:#6ee7b7;font-size:0.78rem;margin-bottom:4px;">💡 참여 적합도</div>
  <div style="color:#d1fae5;font-size:0.88rem;line-height:1.6;">{s.fit_reason}</div>
</div>""", unsafe_allow_html=True)
        with col_r:
            if s.action_required:
                st.markdown(f"""
<div style="background:#0f172a;border-left:3px solid #f59e0b;border-radius:6px;padding:12px 16px;">
  <div style="color:#fcd34d;font-size:0.78rem;margin-bottom:4px;">⚡ 즉시 조치 필요</div>
  <div style="color:#fef3c7;font-size:0.88rem;line-height:1.6;">{s.action_required}</div>
</div>""", unsafe_allow_html=True)

        # 리스크
        if s.risk_flags:
            st.markdown("**⚠️ 리스크 요인**")
            for r in s.risk_flags:
                st.warning(r)

        st.caption(f"분석 시각: {s.analyzed_at or '-'}")


# ──────────────────────────────────────────────
# 탭 4: 의사결정
# ──────────────────────────────────────────────

def render_decision(summaries: list[ProposalSummary]) -> None:
    st.markdown("### ✅ 의사결정 관리")

    _, sh = _get_sheet_client()

    # 미결 공고 목록
    pending = [s for s in summaries if s.decision in ("pending", "reviewing", "")]
    pending = sort_summaries(pending, "score")

    st.markdown(f"**미결 공고: {len(pending)}건** (추천 {sum(1 for s in pending if s.recommendation=='추천')}건 포함)")
    st.markdown("---")

    if not pending:
        st.success("모든 공고에 대한 의사결정이 완료되었습니다! 🎉")

    for s in pending:
        with st.expander(
            f"{SOURCE_EMOJI.get(s.source_site,'📋')} "
            f"{RECOMMENDATION_EMOJI.get(s.recommendation,'')} "
            f"{s.notice_title[:65]}  |  {s.d_day or '-'}",
            expanded=s.recommendation == "추천",
        ):
            col_info, col_action = st.columns([3, 2])

            with col_info:
                st.markdown(f"**기관:** {s.agency or s.ministry or '-'}")
                st.markdown(f"**기간:** {s.period or '-'}")
                st.markdown(f"**예산:** {s.total_budget_text or '-'} / 과제당 {s.per_project_budget_text or '-'}")
                if s.keywords:
                    st.markdown(f"**키워드:** {', '.join(s.keywords[:6])}")
                if s.summary:
                    st.markdown(f"> {s.summary[:180]}…" if len(s.summary) > 180 else f"> {s.summary}")
                if s.fit_reason:
                    st.caption(f"💡 {s.fit_reason}")
                if s.detail_link:
                    st.markdown(f"[공고 원문]({s.detail_link})")

            with col_action:
                st.markdown('<div class="decision-box">', unsafe_allow_html=True)
                st.markdown("**의사결정**")

                decision_by = st.text_input(
                    "담당자", key=f"by_{s.notice_id}_{s.source_site}",
                    placeholder="담당자명 입력"
                )
                comment = st.text_area(
                    "의견", key=f"comment_{s.notice_id}_{s.source_site}",
                    placeholder="결정 이유 또는 메모 입력",
                    height=80,
                )

                btn_cols = st.columns(3)
                decision_result = None

                if btn_cols[0].button("✅ 승인", key=f"approve_{s.notice_id}_{s.source_site}", use_container_width=True):
                    decision_result = "approved"
                if btn_cols[1].button("❌ 기각", key=f"reject_{s.notice_id}_{s.source_site}", use_container_width=True):
                    decision_result = "rejected"
                if btn_cols[2].button("🔍 검토중", key=f"review_{s.notice_id}_{s.source_site}", use_container_width=True):
                    decision_result = "reviewing"

                st.markdown("</div>", unsafe_allow_html=True)

                if decision_result:
                    now_str = datetime.now(SEOUL_TZ).strftime("%Y-%m-%d %H:%M:%S")
                    dec_data = {
                        "notice_id":       s.notice_id,
                        "source_site":     s.source_site,
                        "notice_title":    s.notice_title,
                        "decision":        decision_result,
                        "decision_by":     decision_by,
                        "decision_at":     now_str,
                        "decision_comment": comment,
                    }
                    # Google Sheets upsert
                    if sh:
                        try:
                            upsert_decision_sheet(sh, [dec_data])
                            st.success(f"✅ {DECISION_LABELS.get(decision_result, decision_result)} 처리 완료!")
                        except Exception as e:
                            st.error(f"저장 실패: {e}")
                    # 로컬 캐시 업데이트
                    all_s = st.session_state.get("proposal_summaries", [])
                    for idx, _s in enumerate(all_s):
                        if _s.notice_id == s.notice_id and _s.source_site == s.source_site:
                            all_s[idx].decision = decision_result
                            all_s[idx].decision_by = decision_by
                            all_s[idx].decision_at = now_str
                            all_s[idx].decision_comment = comment
                            break
                    st.session_state["proposal_summaries"] = all_s
                    st.rerun()

    # ── 결정 이력 ──
    st.markdown("---")
    st.markdown("### 📜 결정 이력")
    decided = [s for s in summaries if s.decision in ("approved", "rejected")]
    decided = sort_summaries(decided, "registered")

    if not decided:
        st.info("아직 결정된 공고가 없습니다.")
        return

    hdr = st.columns([3.5, 1.0, 1.0, 1.2, 2.0])
    for col, txt in zip(hdr, ["공고명", "출처", "결정", "처리자", "결정 시각"]):
        col.markdown(f"<span style='color:#94a3b8;font-size:0.8rem;font-weight:600;'>{txt}</span>", unsafe_allow_html=True)
    st.markdown("<hr style='margin:4px 0 8px 0;border-color:#334155;'>", unsafe_allow_html=True)

    for s in decided:
        cols = st.columns([3.5, 1.0, 1.0, 1.2, 2.0])
        title_short = s.notice_title[:50] + "…" if len(s.notice_title) > 50 else s.notice_title
        cols[0].markdown(f"<span style='font-size:0.85rem;'>{title_short}</span>", unsafe_allow_html=True)
        cols[1].markdown(_src_badge(s.source_site), unsafe_allow_html=True)
        cols[2].markdown(_dec_badge(s.decision), unsafe_allow_html=True)
        cols[3].markdown(f"<span style='color:#94a3b8;font-size:0.82rem;'>{s.decision_by or '-'}</span>", unsafe_allow_html=True)
        cols[4].markdown(f"<span style='color:#64748b;font-size:0.8rem;'>{s.decision_at or '-'}</span>", unsafe_allow_html=True)


# ──────────────────────────────────────────────
# 탭 5: Slack 발송
# ──────────────────────────────────────────────

def render_slack_panel(summaries: list[ProposalSummary], stats: DashboardStats) -> None:
    st.markdown("### 🔔 Slack 알림 발송")

    webhook = WEBHOOK_URL or st.text_input("Slack Webhook URL", type="password", placeholder="https://hooks.slack.com/...")
    app_url_input = APP_URL or st.text_input("대시보드 URL (선택)", placeholder="https://your-app.streamlit.app")

    if not webhook:
        st.warning("SLACK_WEBHOOK_URL을 설정하거나 위에 입력하세요.")
        return

    st.markdown("---")

    # 일일 브리핑
    st.markdown("#### 📊 일일 브리핑 발송")
    st.markdown("전체 통계 + 즉시 검토 필요 추천 공고 목록을 Slack에 발송합니다.")
    if st.button("📤 일일 브리핑 발송", use_container_width=True, key="send_brief"):
        try:
            from proposal_agent.slack_agent import post_daily_brief
            post_daily_brief(webhook, summaries, stats, app_url=app_url_input, due_days=DUE_DAYS)
            st.success("✅ 일일 브리핑 발송 완료!")
        except Exception as e:
            st.error(f"발송 실패: {e}")

    st.markdown("---")

    # 개별 공고 알림
    st.markdown("#### 📋 개별 공고 알림 발송")

    target_options = {
        f"[{s.source_site}] {s.notice_title[:60]}": s
        for s in summaries
    }
    selected_labels = st.multiselect(
        "발송할 공고 선택 (다중 선택 가능)",
        list(target_options.keys()),
        default=[
            lbl for lbl, s in target_options.items()
            if s.recommendation == "추천" and s.decision == "pending"
        ][:3],
    )

    include_buttons = st.checkbox("의사결정 버튼 포함 (Interactive 메시지)", value=True)

    if st.button("📤 선택 공고 알림 발송", use_container_width=True, key="send_notices"):
        if not selected_labels:
            st.warning("발송할 공고를 선택하세요.")
        else:
            targets = [target_options[lbl] for lbl in selected_labels]
            from proposal_agent.slack_agent import post_to_slack, build_notice_blocks
            sent = 0
            for s in targets:
                try:
                    blocks = build_notice_blocks(s, app_url=app_url_input) if include_buttons else None
                    text = f"[{s.source_site}] {s.notice_title}"
                    post_to_slack(webhook, text=text, blocks=blocks)
                    sent += 1
                except Exception as e:
                    st.error(f"{s.notice_title[:30]} 발송 실패: {e}")
            if sent:
                st.success(f"✅ {sent}건 발송 완료!")

    st.markdown("---")

    # 마감 임박 공고 알림
    st.markdown("#### ⏰ 마감 임박 공고 알림")
    due_filter_days = st.slider("마감 임박 기준 (일)", 1, 14, DUE_DAYS)
    from proposal_agent.analyzer import filter_summaries as _filter
    due_soon = _filter(summaries, due_within_days=due_filter_days)
    st.caption(f"현재 {due_filter_days}일 이내 마감 공고: {len(due_soon)}건")

    if st.button(f"⏰ 마감 임박 공고 {len(due_soon)}건 발송", use_container_width=True, key="send_due"):
        if not due_soon:
            st.info("해당하는 공고가 없습니다.")
        else:
            from proposal_agent.slack_agent import post_to_slack, build_notice_blocks
            sent = 0
            for s in due_soon[:5]:
                try:
                    blocks = build_notice_blocks(s, app_url=app_url_input)
                    post_to_slack(webhook, blocks=blocks)
                    sent += 1
                except Exception as e:
                    st.error(f"발송 실패: {e}")
            if sent:
                st.success(f"✅ {sent}건 발송 완료!")

    st.markdown("---")

    # Slack 연동 설정 가이드
    with st.expander("⚙️ Slack Bot 설정 가이드"):
        st.markdown("""
**1. Slack App 생성 (api.slack.com/apps)**
- Create New App → From Scratch
- App Name: `Proposal Agent`

**2. Incoming Webhooks 활성화**
- Features → Incoming Webhooks → ON
- Add New Webhook to Workspace → 채널 선택
- Webhook URL을 `SLACK_WEBHOOK_URL` 환경변수에 저장

**3. Interactive Components (의사결정 버튼)**
- Features → Interactivity & Shortcuts → ON
- Request URL: `https://<your-server>/slack/actions`
- Proposal Agent Slack 서버 실행 필요:
  ```bash
  python proposal_agent/run_slack_server.py
  ```

**4. Slash Commands**
- Features → Slash Commands → Create New Command
- Command: `/proposal`
- Request URL: `https://<your-server>/slack/slash`

**5. Bot Token (Optional)**
- OAuth & Permissions → Bot Token Scopes: `chat:write`, `channels:read`
- Bot User OAuth Token → `SLACK_BOT_TOKEN` 환경변수

**필수 환경변수:**
```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SLACK_BOT_TOKEN=xoxb-...           # Interactive 사용 시
SLACK_SIGNING_SECRET=...           # 보안 서명 검증
```
""")


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def main() -> None:
    # ── 제목 ──
    st.markdown("""
<div style="background:linear-gradient(135deg,#1e3a5f 0%,#0f172a 100%);
            border-radius:12px;padding:24px 32px;margin-bottom:24px;
            border:1px solid #1e3a5f;">
  <h1 style="color:#f1f5f9;margin:0;font-size:1.8rem;">📊 Proposal Agent Dashboard</h1>
  <p style="color:#64748b;margin:6px 0 0 0;font-size:0.9rem;">
    IRIS · 중기부 · NIPA 사업공고 분석 | AI 요약 | Slack 의사결정 연동
  </p>
</div>
""", unsafe_allow_html=True)

    # ── 환경 체크 ──
    if not SHEET_ID or not CREDS_PATH:
        st.error("⚠️ `GOOGLE_SHEET_ID` 또는 `GOOGLE_CREDENTIALS_JSON` 환경변수가 설정되지 않았습니다.")
        with st.expander("설정 방법"):
            st.code("""
# .env 파일 또는 환경변수 설정
GOOGLE_SHEET_ID=your_sheet_id
GOOGLE_CREDENTIALS_JSON=./credentials.json
OPENAI_API_KEY=sk-...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
APP_URL=https://your-dashboard.streamlit.app
""")
        st.stop()

    # ── 데이터 로딩 ──
    summaries = _load_data()
    if not summaries:
        st.warning("공고 데이터가 없습니다. Google Sheets 연결 및 시트 이름을 확인하세요.")
        st.stop()

    stats = compute_stats(summaries, due_days=DUE_DAYS)

    # ── 사이드바 ──
    filters = render_sidebar(summaries)

    # ── 탭 ──
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 Overview",
        "📋 공고 목록",
        "🔍 상세 분석",
        "✅ 의사결정",
        "🔔 Slack 발송",
    ])

    with tab1:
        render_overview(summaries, stats)

    with tab2:
        selected = render_notice_list(summaries, filters)
        if selected:
            st.session_state["selected_notice_id"]   = selected.notice_id
            st.session_state["selected_notice_site"] = selected.source_site

    with tab3:
        render_detail(summaries)

    with tab4:
        render_decision(summaries)

    with tab5:
        render_slack_panel(summaries, stats)


if __name__ == "__main__":
    main()

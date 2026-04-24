"""
proposal_agent/schemas.py
공고 분석 결과 스키마 정의
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProposalSummary:
    """사업공고 분석/요약 결과"""
    # 식별
    notice_id: str = ""
    source_site: str = ""          # IRIS / MSS / NIPA
    notice_title: str = ""
    notice_no: str = ""
    detail_link: str = ""

    # 공고 메타
    ministry: str = ""
    agency: str = ""
    registered_at: str = ""
    period: str = ""
    status: str = ""
    d_day: str = ""

    # RFP 분석
    project_name: str = ""
    keywords: list[str] = field(default_factory=list)
    total_budget_text: str = ""
    per_project_budget_text: str = ""
    recommendation: str = ""       # 추천 / 보통 / 비추천
    rfp_score: int = 0

    # LLM 요약 필드
    summary: str = ""              # 1~3줄 요약 (Agent 생성)
    key_points: list[str] = field(default_factory=list)   # 핵심 포인트 bullet
    fit_reason: str = ""           # 적합도 판단 근거
    action_required: str = ""      # 즉시 검토 필요 여부 & 이유
    risk_flags: list[str] = field(default_factory=list)   # 리스크 플래그

    # 의사결정 상태
    decision: str = ""             # approved / rejected / pending / reviewing
    decision_by: str = ""
    decision_at: str = ""
    decision_comment: str = ""

    # 내부
    analyzed_at: str = ""
    llm_enriched: str = ""


@dataclass
class DashboardStats:
    """대시보드 통계"""
    total_notices: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    by_recommendation: dict[str, int] = field(default_factory=dict)
    by_decision: dict[str, int] = field(default_factory=dict)
    due_soon_count: int = 0        # 7일 이내 마감
    new_today_count: int = 0
    high_priority_count: int = 0   # 추천 + pending


DECISION_LABELS = {
    "approved": "✅ 제안 승인",
    "rejected": "❌ 제안 기각",
    "pending": "⏳ 검토 대기",
    "reviewing": "🔍 검토 중",
}

RECOMMENDATION_EMOJI = {
    "추천": "⭐",
    "보통": "🔹",
    "비추천": "🔸",
}

SOURCE_EMOJI = {
    "IRIS": "🔬",
    "MSS": "🏢",
    "NIPA": "💻",
}

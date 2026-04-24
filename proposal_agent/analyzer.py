"""
proposal_agent/analyzer.py

사업공고 분석 및 요약 Agent
- Google Sheets에서 OPPORTUNITY_MASTER / MSS / NIPA 공고 데이터 로드
- LLM을 사용해 각 공고를 1~3줄 요약 + 핵심 포인트 + 적합도 분석
- 결과를 ProposalSummary 리스트로 반환
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

try:
    import gspread
    from google.oauth2.service_account import Credentials
    HAS_GSPREAD = True
except ImportError:
    HAS_GSPREAD = False

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

from proposal_agent.schemas import ProposalSummary, DashboardStats

SEOUL_TZ = ZoneInfo("Asia/Seoul")
SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
PERIOD_RE = re.compile(r"(\d{4}[-.]\d{2}[-.]\d{2})\s*~\s*(\d{4}[-.]\d{2}[-.]\d{2})")

# 의사결정 상태를 저장하는 시트 이름
PROPOSAL_DECISION_SHEET = "PROPOSAL_DECISIONS"


# ──────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────

def clean(v) -> str:
    return str(v or "").strip()


def safe_int(v, default=0) -> int:
    try:
        return int(float(str(v).strip()))
    except Exception:
        return default


def parse_date(value: str) -> datetime | None:
    text = clean(value).replace(".", "-")
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def extract_period_end(value: str) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    m = PERIOD_RE.search(text)
    if m:
        return parse_date(m.group(2))
    parts = [x.strip() for x in text.split("~") if x.strip()]
    if len(parts) == 2:
        return parse_date(parts[1])
    return None


def compute_d_day(period: str) -> str:
    end = extract_period_end(period)
    if not end:
        return ""
    today = datetime.now(SEOUL_TZ).replace(tzinfo=None)
    delta = (end.date() - today.date()).days
    if delta < 0:
        return "마감"
    if delta == 0:
        return "D-Day"
    return f"D-{delta}"


# ──────────────────────────────────────────────
# Google Sheets 연결
# ──────────────────────────────────────────────

def get_gc(credentials_path: str):
    if not HAS_GSPREAD:
        raise RuntimeError("gspread not installed")
    creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPE)
    return gspread.authorize(creds)


def read_sheet(sh, sheet_name: str) -> list[dict]:
    if not sheet_name:
        return []
    try:
        ws = sh.worksheet(sheet_name)
        values = ws.get_all_values()
        if not values:
            return []
        header = [clean(x) for x in values[0]]
        rows = []
        for row in values[1:]:
            item = {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}
            rows.append(item)
        return rows
    except Exception as e:
        print(f"[WARN] read_sheet({sheet_name}): {e}")
        return []


def upsert_decision_sheet(sh, decisions: list[dict]) -> None:
    """PROPOSAL_DECISIONS 시트에 의사결정 결과를 upsert"""
    header = [
        "notice_id", "source_site", "notice_title",
        "decision", "decision_by", "decision_at", "decision_comment",
        "updated_at",
    ]
    try:
        try:
            ws = sh.worksheet(PROPOSAL_DECISION_SHEET)
        except Exception:
            ws = sh.add_worksheet(title=PROPOSAL_DECISION_SHEET, rows=1000, cols=len(header))
            ws.append_row(header)

        existing_values = ws.get_all_values()
        if not existing_values:
            ws.append_row(header)
            existing_map: dict[str, int] = {}
        else:
            existing_header = existing_values[0]
            try:
                id_col = existing_header.index("notice_id")
                site_col = existing_header.index("source_site")
            except ValueError:
                id_col, site_col = 0, 1
            existing_map = {}
            for row_idx, row in enumerate(existing_values[1:], start=2):
                key = f"{row[id_col]}||{row[site_col]}" if len(row) > site_col else ""
                if key:
                    existing_map[key] = row_idx

        now_str = datetime.now(SEOUL_TZ).strftime("%Y-%m-%d %H:%M:%S")
        for dec in decisions:
            key = f"{clean(dec.get('notice_id'))}||{clean(dec.get('source_site'))}"
            row_data = [
                clean(dec.get("notice_id")),
                clean(dec.get("source_site")),
                clean(dec.get("notice_title")),
                clean(dec.get("decision")),
                clean(dec.get("decision_by")),
                clean(dec.get("decision_at")),
                clean(dec.get("decision_comment")),
                now_str,
            ]
            if key in existing_map:
                row_num = existing_map[key]
                ws.update(f"A{row_num}:{chr(64+len(header))}{row_num}", [row_data])
            else:
                ws.append_row(row_data)
                existing_map[key] = len(existing_map) + 2

        print(f"[OK] upserted {len(decisions)} decisions to {PROPOSAL_DECISION_SHEET}")
    except Exception as e:
        print(f"[WARN] upsert_decision_sheet: {e}")


def load_decisions(sh) -> dict[str, dict]:
    """notice_id||source_site -> decision dict"""
    rows = read_sheet(sh, PROPOSAL_DECISION_SHEET)
    result = {}
    for row in rows:
        key = f"{clean(row.get('notice_id'))}||{clean(row.get('source_site'))}"
        if key:
            result[key] = row
    return result


# ──────────────────────────────────────────────
# 공고 데이터 → ProposalSummary 변환
# ──────────────────────────────────────────────

def _row_to_summary(row: dict, source_site: str, decisions: dict[str, dict]) -> ProposalSummary:
    notice_id = clean(row.get("notice_id"))
    s = ProposalSummary(
        notice_id=notice_id,
        source_site=clean(row.get("source_site") or source_site),
        notice_title=clean(row.get("notice_title") or row.get("title") or ""),
        notice_no=clean(row.get("notice_no") or row.get("ancm_no") or ""),
        detail_link=clean(row.get("detail_link") or ""),
        ministry=clean(row.get("ministry") or row.get("소관부처") or ""),
        agency=clean(row.get("agency") or row.get("전문기관") or ""),
        registered_at=clean(row.get("registered_at") or row.get("ancm_de") or ""),
        period=clean(row.get("period") or row.get("접수기간") or row.get("신청기간") or ""),
        status=clean(row.get("status") or row.get("공고상태") or ""),
        d_day=clean(row.get("d_day") or ""),
        project_name=clean(row.get("project_name") or row.get("llm_project_name") or ""),
        total_budget_text=clean(row.get("total_budget_text") or row.get("llm_total_budget_text") or ""),
        per_project_budget_text=clean(row.get("per_project_budget_text") or row.get("llm_per_project_budget_text") or ""),
        recommendation=clean(row.get("recommendation") or row.get("llm_recommendation") or ""),
        rfp_score=safe_int(row.get("rfp_score") or row.get("llm_fit_score") or 0),
        llm_enriched=clean(row.get("llm_enriched") or ""),
    )

    # 키워드 파싱
    kw_raw = row.get("keywords") or row.get("llm_keywords") or ""
    if isinstance(kw_raw, list):
        s.keywords = [clean(k) for k in kw_raw if clean(k)]
    elif clean(kw_raw):
        s.keywords = [k.strip() for k in clean(kw_raw).split(",") if k.strip()]

    # d_day 계산 (비어있으면)
    if not s.d_day and s.period:
        s.d_day = compute_d_day(s.period)

    # 의사결정 상태 로드
    dec_key = f"{notice_id}||{s.source_site}"
    dec = decisions.get(dec_key) or decisions.get(f"{notice_id}||")
    if dec:
        s.decision = clean(dec.get("decision") or "pending")
        s.decision_by = clean(dec.get("decision_by") or "")
        s.decision_at = clean(dec.get("decision_at") or "")
        s.decision_comment = clean(dec.get("decision_comment") or "")
    else:
        s.decision = "pending"

    return s


def load_all_notices(
    sh,
    iris_master_sheet: str = "OPPORTUNITY_MASTER",
    mss_master_sheet: str = "MSS_OPPORTUNITY_MASTER",
    nipa_master_sheet: str = "NIPA_OPPORTUNITY_MASTER",
    current_only: bool = True,
) -> list[ProposalSummary]:
    """
    IRIS / MSS / NIPA Opportunity Master에서 공고를 로드해 ProposalSummary 리스트로 반환
    """
    decisions = load_decisions(sh)

    summaries: list[ProposalSummary] = []

    for sheet_name, site in [
        (iris_master_sheet, "IRIS"),
        (mss_master_sheet, "MSS"),
        (nipa_master_sheet, "NIPA"),
    ]:
        if not sheet_name:
            continue
        rows = read_sheet(sh, sheet_name)
        for row in rows:
            if current_only:
                is_current = clean(row.get("is_current") or row.get("notice_is_current") or "Y")
                if is_current == "N":
                    continue
            summaries.append(_row_to_summary(row, site, decisions))

    print(f"[OK] loaded {len(summaries)} proposals from sheets")
    return summaries


# ──────────────────────────────────────────────
# LLM 분석 (요약 + 핵심 포인트 + 리스크)
# ──────────────────────────────────────────────

def _build_analysis_prompt(s: ProposalSummary) -> str:
    keywords_str = ", ".join(s.keywords[:8]) if s.keywords else "없음"
    return f"""
당신은 정부 R&D 사업 제안 전문가입니다.
아래 사업공고 정보를 분석하여 의사결정자를 위한 구조화된 분석을 작성하세요.

## 공고 정보
- 출처: {s.source_site}
- 공고명: {s.notice_title}
- 사업/과제명: {s.project_name or "미확인"}
- 담당기관: {s.agency or "미확인"} / {s.ministry or "미확인"}
- 접수기간: {s.period or "미확인"} ({s.d_day or ""})
- 총사업비: {s.total_budget_text or "미확인"}
- 과제당 예산: {s.per_project_budget_text or "미확인"}
- 기술 키워드: {keywords_str}
- 자동 추천 점수: {s.rfp_score}점 / 추천여부: {s.recommendation or "미평가"}

## 작성 지침
1. summary: 이 공고의 핵심을 2~3문장으로 요약 (누가, 무엇을, 얼마나)
2. key_points: 의사결정에 필요한 핵심 포인트 3~5개 (bullet)
3. fit_reason: 우리 회사가 참여해야 하는 이유 또는 하지 말아야 하는 이유 (1~2문장)
4. action_required: 즉시 취해야 할 행동이 있다면 명시 (마감일 촉박, 컨소시엄 모집 필요 등)
5. risk_flags: 리스크 요인 목록 (없으면 빈 배열)

## 출력 형식 (JSON only, markdown 없이)
{{
  "summary": "...",
  "key_points": ["...", "..."],
  "fit_reason": "...",
  "action_required": "...",
  "risk_flags": ["..."]
}}
""".strip()


def enrich_with_llm(
    summaries: list[ProposalSummary],
    api_key: str,
    model: str = "gpt-4o-mini",
    max_items: int = 30,
    target_recommendations: tuple[str, ...] = ("추천", "보통"),
    retry: int = 2,
) -> list[ProposalSummary]:
    """LLM으로 각 공고 요약/분석 보강"""
    if not HAS_OPENAI or not api_key:
        print("[WARN] OpenAI not available, skipping LLM enrichment")
        return summaries

    client = OpenAI(api_key=api_key)
    enriched = 0

    for s in summaries:
        # 이미 분석된 항목은 스킵
        if s.summary:
            continue
        # 추천 필터 (비추천은 LLM 사용 안 함)
        if target_recommendations and s.recommendation and s.recommendation not in target_recommendations:
            continue
        if enriched >= max_items > 0:
            break

        prompt = _build_analysis_prompt(s)

        for attempt in range(max(retry, 1)):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    response_format={"type": "json_object"},
                )
                raw = response.choices[0].message.content or ""
                parsed = json.loads(raw)

                s.summary = clean(parsed.get("summary") or "")
                kp = parsed.get("key_points") or []
                s.key_points = [clean(x) for x in kp if clean(x)]
                s.fit_reason = clean(parsed.get("fit_reason") or "")
                s.action_required = clean(parsed.get("action_required") or "")
                rf = parsed.get("risk_flags") or []
                s.risk_flags = [clean(x) for x in rf if clean(x)]
                s.analyzed_at = datetime.now(SEOUL_TZ).strftime("%Y-%m-%d %H:%M:%S")
                enriched += 1
                print(f"[OK] LLM enriched ({enriched}): {s.notice_title[:40]}")
                time.sleep(0.3)
                break
            except Exception as e:
                if attempt + 1 < retry:
                    time.sleep(1.5)
                else:
                    print(f"[WARN] LLM failed for {s.notice_title[:40]}: {e}")

    print(f"[OK] LLM enrichment done: {enriched} items")
    return summaries


# ──────────────────────────────────────────────
# 통계 계산
# ──────────────────────────────────────────────

def compute_stats(summaries: list[ProposalSummary], due_days: int = 7) -> DashboardStats:
    today = datetime.now(SEOUL_TZ)
    stats = DashboardStats(total_notices=len(summaries))

    for s in summaries:
        # 출처별
        stats.by_source[s.source_site] = stats.by_source.get(s.source_site, 0) + 1
        # 상태별
        status = s.status or "미분류"
        stats.by_status[status] = stats.by_status.get(status, 0) + 1
        # 추천별
        rec = s.recommendation or "미평가"
        stats.by_recommendation[rec] = stats.by_recommendation.get(rec, 0) + 1
        # 의사결정별
        dec = s.decision or "pending"
        stats.by_decision[dec] = stats.by_decision.get(dec, 0) + 1
        # 마감 임박
        end = extract_period_end(s.period)
        if end:
            delta = (end.date() - today.date()).days
            if 0 <= delta <= due_days:
                stats.due_soon_count += 1
        # 오늘 신규
        reg = parse_date(s.registered_at)
        if reg and reg.date() == today.date():
            stats.new_today_count += 1
        # 고우선순위 (추천 + 미결정)
        if s.recommendation == "추천" and s.decision in ("pending", "reviewing"):
            stats.high_priority_count += 1

    return stats


# ──────────────────────────────────────────────
# 필터링 / 정렬 헬퍼
# ──────────────────────────────────────────────

def filter_summaries(
    summaries: list[ProposalSummary],
    source_site: str | None = None,
    recommendation: str | None = None,
    decision: str | None = None,
    due_within_days: int | None = None,
    keyword: str | None = None,
) -> list[ProposalSummary]:
    today = datetime.now(SEOUL_TZ)
    result = []
    for s in summaries:
        if source_site and s.source_site != source_site:
            continue
        if recommendation and s.recommendation != recommendation:
            continue
        if decision and s.decision != decision:
            continue
        if due_within_days is not None:
            end = extract_period_end(s.period)
            if not end:
                continue
            delta = (end.date() - today.date()).days
            if not (0 <= delta <= due_within_days):
                continue
        if keyword:
            kw = keyword.lower()
            haystack = " ".join([
                s.notice_title, s.project_name, s.agency, s.ministry,
                " ".join(s.keywords), s.summary,
            ]).lower()
            if kw not in haystack:
                continue
        result.append(s)
    return result


def sort_summaries(summaries: list[ProposalSummary], by: str = "score") -> list[ProposalSummary]:
    """by: 'score' | 'dday' | 'registered'"""
    if by == "score":
        return sorted(summaries, key=lambda s: -s.rfp_score)
    if by == "dday":
        def dday_key(s: ProposalSummary):
            end = extract_period_end(s.period)
            if not end:
                return 9999
            today = datetime.now(SEOUL_TZ)
            return (end.date() - today.date()).days
        return sorted(summaries, key=dday_key)
    if by == "registered":
        return sorted(summaries, key=lambda s: s.registered_at or "", reverse=True)
    return summaries


# ──────────────────────────────────────────────
# CLI 진입점 (독립 실행 시 분석 결과 JSON 저장)
# ──────────────────────────────────────────────

def main():
    load_dotenv()
    sheet_id = clean(os.getenv("GOOGLE_SHEET_ID"))
    creds_path = clean(os.getenv("GOOGLE_CREDENTIALS_JSON"))
    api_key = clean(os.getenv("OPENAI_API_KEY"))
    model = clean(os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    max_items = safe_int(os.getenv("PROPOSAL_LLM_MAX_ITEMS", "30"), 30)

    if not sheet_id or not creds_path:
        raise SystemExit("GOOGLE_SHEET_ID / GOOGLE_CREDENTIALS_JSON 환경변수 필요")

    gc = get_gc(creds_path)
    sh = gc.open_by_key(sheet_id)

    summaries = load_all_notices(
        sh,
        iris_master_sheet=clean(os.getenv("IRIS_OPPORTUNITY_MASTER_SHEET", "OPPORTUNITY_MASTER")),
        mss_master_sheet=clean(os.getenv("MSS_OPPORTUNITY_MASTER_SHEET", "MSS_OPPORTUNITY_MASTER")),
        nipa_master_sheet=clean(os.getenv("NIPA_OPPORTUNITY_MASTER_SHEET", "NIPA_OPPORTUNITY_MASTER")),
    )

    summaries = enrich_with_llm(summaries, api_key=api_key, model=model, max_items=max_items)
    stats = compute_stats(summaries)

    import pathlib, dataclasses
    out_dir = pathlib.Path("analysis")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "proposal_summaries.json"

    def _to_dict(obj):
        return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else obj

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([dataclasses.asdict(s) for s in summaries], f, ensure_ascii=False, indent=2)
    print(f"[OK] saved {len(summaries)} summaries -> {out_path}")
    print(f"[STATS] total={stats.total_notices}, high_priority={stats.high_priority_count}, due_soon={stats.due_soon_count}")


if __name__ == "__main__":
    main()

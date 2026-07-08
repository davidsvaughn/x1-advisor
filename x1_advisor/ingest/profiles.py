"""Entity profile renderer: app rows → denormalized markdown profile documents.

Field lists follow the app's own chat-context renderer (ReportChatService.php —
recon 2026-07-08) so profiles say what the product already says about an entity,
plus cross-entity extras the plan calls for (latest eval score on the startup card,
resolved lookup labels for filter metadata).

NEVER-INDEX list (PLAN §0.2, applied here at ingest): contact/invite emails,
invitation/claim/share tokens, lat/long coordinates. These never enter markdown or
chunk metadata.

TipTap rich-text columns are stored as raw HTML → converted with markdownify.
"""

from __future__ import annotations

import json
from typing import Any

from markdownify import markdownify

from x1_advisor.ingest.bundles import SourceDoc

PROFILE_VISIBILITY = "x1"  # profiles are platform content for signed-in users


# ---------------------------------------------------------------------------
# field formatting helpers
# ---------------------------------------------------------------------------
def html_to_md(value: str | None) -> str | None:
    if not value:
        return None
    if "<" in value and ">" in value:
        return markdownify(value, heading_style="ATX").strip() or None
    return value.strip() or None


def money(value) -> str | None:
    try:
        return f"${float(value):,.0f}" if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return value if isinstance(value, list) else [value]


def labels(value) -> str | None:
    """JSON label arrays → comma list. Handles denormalized industry/skill objects
    ({display_name|category|subcategory|name}) and plain strings alike."""
    out = []
    for item in _as_list(value):
        if isinstance(item, dict):
            name = (item.get("display_name") or item.get("name")
                    or " / ".join(p for p in (item.get("category"),
                                              item.get("subcategory")) if p))
            if name:
                out.append(str(name))
        elif item:
            out.append(str(item))
    return ", ".join(dict.fromkeys(out)) or None


def label_list(value) -> list[str]:
    s = labels(value)
    return [x.strip() for x in s.split(",")] if s else []


def fields_block(pairs: list[tuple[str, Any]]) -> str:
    lines = [f"- {label}: {value}" for label, value in pairs
             if value not in (None, "", "[]")]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# renderers — one per entity type; each returns (markdown, filter_metadata)
# ---------------------------------------------------------------------------
def render_startup(conn, row: dict) -> tuple[str, dict]:
    parts = [f"# {row['name']} — Startup Profile"]
    if row.get("tagline"):
        parts.append(f"*{row['tagline']}*")

    with conn.cursor() as cur:
        cur.execute(
            """SELECT r.name FROM regions r
               JOIN startup_company_regions sr ON sr.region_id = r.id
               WHERE sr.startup_company_id = %s ORDER BY r.name""", (row["id"],))
        regions = [r["name"] for r in cur.fetchall()]
        cur.execute(
            """SELECT overall_score, evaluation_date FROM startup_company_evaluations
               WHERE startup_company_id = %s AND is_visible
               ORDER BY evaluation_date DESC LIMIT 1""", (row["id"],))
        latest_eval = cur.fetchone()
        cur.execute(
            """SELECT m.*, u.name AS user_name FROM startup_company_team_members m
               LEFT JOIN users u ON u.id = m.user_id
               WHERE m.startup_company_id = %s
                 AND (m.request_status IS NULL OR m.request_status = 'approved')
               ORDER BY m.is_founder DESC, m.id""", (row["id"],))
        members = cur.fetchall()

    # ReportChatService.php:134-156 field map (minus never-index: contact_email, socials kept)
    parts.append("## Company\n" + fields_block([
        ("Website", row.get("website_url")),
        ("LinkedIn", row.get("linkedin_url")),
        ("Industry Tags", labels(row.get("industry_tags"))),
        ("Industries", labels(row.get("industries"))),
        ("Target Sectors", labels(row.get("target_sectors"))),
        ("One Sentence Pitch", html_to_md(row.get("one_sentence_pitch"))),
        ("Description", html_to_md(row.get("description"))),
        ("Business Model", labels(row.get("business_model"))),
        ("Headquarters", row.get("headquarters_location")),
        ("Founded On", str(row["founded_on"]) if row.get("founded_on") else None),
        ("Regions", ", ".join(regions) or None),
        ("Fundraising Status", row.get("fundraising_status")),
        ("Fundraising Round", row.get("fundraising_round")),
        ("Fundraising Valuation", money(row.get("fundraising_valuation"))),
        ("Fundraising Amount", money(row.get("fundraising_amount"))),
        ("Fundraising Committed", money(row.get("fundraising_committed_amount"))),
        ("Min Investment", money(row.get("fundraising_min_investment"))),
    ]))

    full_desc = html_to_md(row.get("full_description"))
    if full_desc:
        parts.append(f"## About\n{full_desc}")

    if latest_eval:
        parts.append("## Latest X1 Evaluation\n" + fields_block([
            ("Overall Score", latest_eval["overall_score"]),
            ("Evaluation Date", str(latest_eval["evaluation_date"])[:10]),
        ]))

    if members:
        mparts = ["## Team"]
        for m in members:  # ReportChatService.php:181-193 field list (minus email)
            name = m.get("user_name") or "Team member"
            mparts.append(f"### {name}" + (" (Founder)" if m.get("is_founder") else ""))
            mparts.append(fields_block([
                ("Position", m.get("position")),
                ("Employment", m.get("employment_type")),
                ("Time Commitment", m.get("time_commitment")),
                ("Focus", html_to_md(m.get("focus_statement"))),
                ("Skillsets", html_to_md(m.get("skillsets"))),
                ("Summary", html_to_md(m.get("personal_summary"))),
                ("Achievements", html_to_md(m.get("achievements"))),
                ("Responsibilities", html_to_md(m.get("core_responsibilities"))),
            ]))
        parts.append("\n\n".join(mparts))

    meta = {
        "company_name": row["name"], "slug": row.get("slug"),
        "industries": label_list(row.get("industries")) or label_list(row.get("industry_tags")),
        "regions": regions,
        "business_model": label_list(row.get("business_model")),
        "fundraising_round": row.get("fundraising_round"),
        "fundraising_status": row.get("fundraising_status"),
        "eval_overall_score": latest_eval["overall_score"] if latest_eval else None,
        "is_claimed": row.get("is_claimed"),
    }
    return "\n\n".join(p for p in parts if p.strip()), meta


def render_investor(conn, row: dict) -> tuple[str, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM users WHERE id = %s", (row["user_id"],))
        u = cur.fetchone()
    name = row.get("display_name") or (u and u["name"]) or f"Investor {row['id']}"
    parts = [f"# {name} — Investor Profile"]
    parts.append(fields_block([
        ("Investor Type", row.get("investor_type")),
        ("LinkedIn", row.get("linkedin_url")),
        ("Investment Stages", labels(row.get("investment_stages"))),
        ("Industries", labels(row.get("industries"))),
        ("Sector Focus", labels(row.get("sector_focus"))),
        ("Regions", labels(row.get("regions"))),
        ("Country Focus", labels(row.get("country_focus"))),
        ("Business Model Preferences", labels(row.get("business_model_preferences"))),
        ("Investment Instrument", row.get("investment_instrument")),
        ("Check Size", row.get("check_size")),
        ("Investment Size", " – ".join(filter(None, (money(row.get("investment_size_min")),
                                                     money(row.get("investment_size_max"))))) or None),
        ("Deal Role", row.get("deal_role")),
        ("Deals Per Year", row.get("deals_per_year")),
        ("Investments", row.get("investments_count")),
        ("Exits", row.get("exits_count")),
        ("Active Investments", row.get("active_investments_count")),
    ]))
    for heading, col in (("Focus", "focus_statement"), ("Investment Thesis", "investment_thesis"),
                         ("Value Beyond Capital", "value_beyond_capital"),
                         ("How to Pitch", "pitch_instructions")):
        v = html_to_md(row.get(col))
        if v:
            parts.append(f"## {heading}\n{v}")
    meta = {
        "person_name": name, "slug": row.get("slug"),
        "investor_type": row.get("investor_type"),
        "industries": label_list(row.get("industries")),
        "investment_stages": label_list(row.get("investment_stages")),
        "regions": label_list(row.get("regions")),
    }
    return "\n\n".join(parts), meta


_COMPANYABLE_TABLE = {
    "App\\Models\\StartupCompany": "startup_companies",
    "App\\Models\\InvestmentCompany": "investment_companies",
    "App\\Models\\InvestmentFund": "investment_funds",
    "App\\Models\\Organization": "organizations",
}


def _companyable_name(conn, ctype: str | None, cid) -> str | None:
    table = _COMPANYABLE_TABLE.get(ctype or "")
    if not table or not cid:
        return None
    with conn.cursor() as cur:
        cur.execute(f"SELECT name FROM {table} WHERE id = %s", (cid,))  # noqa: S608 — table from fixed map
        r = cur.fetchone()
    return r["name"] if r else None


def render_cv(conn, row: dict) -> tuple[str, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM users WHERE id = %s", (row["user_id"],))
        u = cur.fetchone()
        cur.execute("""SELECT * FROM cv_experiences WHERE cv_id = %s
                       ORDER BY is_current DESC, start_date DESC NULLS LAST""", (row["id"],))
        experiences = cur.fetchall()
        cur.execute("""SELECT * FROM cv_education WHERE cv_id = %s
                       ORDER BY sort_order, start_date DESC NULLS LAST""", (row["id"],))
        education = cur.fetchall()

    name = (u and u["name"]) or f"CV {row['id']}"
    parts = [f"# {name} — CV"]
    if row.get("headline"):
        parts.append(f"*{row['headline']}*")
    parts.append(fields_block([
        ("Location", row.get("location")),
        ("Open To Work", row.get("open_to_work")),
        ("Roles", labels(row.get("role_badges")) or labels(row.get("custom_role_badges"))),
        ("Skills", labels(row.get("skills"))),
        ("Custom Skills", labels(row.get("custom_skills"))),
        ("Industries", labels(row.get("industries"))),
    ]))
    mission = html_to_md(row.get("mission_statement"))
    if mission:
        parts.append(f"## Mission\n{mission}")

    if experiences:
        eparts = ["## Experience"]
        for e in experiences:
            company = (_companyable_name(conn, e.get("companyable_type"), e.get("companyable_id"))
                       or e.get("custom_organization_name") or e.get("company_name") or "—")
            period = " – ".join(filter(None, (
                str(e["start_date"])[:10] if e.get("start_date") else None,
                "present" if e.get("is_current") else (str(e["end_date"])[:10] if e.get("end_date") else None),
            )))
            eparts.append(f"### {e.get('title') or 'Role'} at {company}" + (f" ({period})" if period else ""))
            eparts.append(fields_block([
                ("Employment", e.get("employment_type")),
                ("Description", html_to_md(e.get("description"))),
                ("Summary", html_to_md(e.get("personal_summary"))),
                ("Focus", html_to_md(e.get("focus_statement"))),
                ("Skillsets", html_to_md(e.get("skillsets"))),
                ("Responsibilities", html_to_md(e.get("core_responsibilities"))),
                ("Achievements", html_to_md(e.get("achievements"))),
            ]))
        parts.append("\n\n".join(eparts))

    if education:
        gparts = ["## Education"]
        for g in education:
            gparts.append(f"### {g.get('school') or 'School'}")
            gparts.append(fields_block([
                ("Degree", g.get("degree")),
                ("Field of Study", g.get("field_of_study")),
                ("Grade", g.get("grade")),
                ("Activities", g.get("activities_and_societies")),
                ("Description", html_to_md(g.get("description"))),
            ]))
        parts.append("\n\n".join(gparts))

    meta = {
        "person_name": name, "slug": row.get("slug"),
        "industries": label_list(row.get("industries")),
        "skills": label_list(row.get("skills")) + label_list(row.get("custom_skills")),
        "roles": label_list(row.get("role_badges")) + label_list(row.get("custom_role_badges")),
        "open_to_work": row.get("open_to_work"),
    }
    return "\n\n".join(p for p in parts if p.strip()), meta


def render_investment_company(conn, row: dict) -> tuple[str, dict]:
    parts = [f"# {row['name']} — Investment Company Profile"]
    if row.get("tagline"):
        parts.append(f"*{row['tagline']}*")
    parts.append(fields_block([
        ("Website", row.get("website")),
        ("Industries", labels(row.get("industries"))),
        ("Sector Focus", labels(row.get("sector_focus"))),
        ("Stage Focus", labels(row.get("stage_focus"))),
        ("Geographic Focus", labels(row.get("geographic_focus"))),
        ("Country Focus", labels(row.get("country_focus"))),
        ("Business Model Preferences", labels(row.get("business_model_preferences"))),
        ("Investment Size", " – ".join(filter(None, (money(row.get("investment_size_min")),
                                                     money(row.get("investment_size_max"))))) or None),
        ("Investments", row.get("investments_count")),
        ("Exits", row.get("exits_count")),
    ]))
    for heading, col in (("Vision", "vision"), ("Summary", "summary"),
                         ("Pitch Process", "pitch_process"),
                         ("Pitch Requirements", "pitch_requirements"),
                         ("Value Proposition", "value_proposition"),
                         ("Network Access", "network_access")):
        v = html_to_md(row.get(col))
        if v:
            parts.append(f"## {heading}\n{v}")
    meta = {"company_name": row["name"], "slug": row.get("slug"),
            "industries": label_list(row.get("industries")),
            "stage_focus": label_list(row.get("stage_focus"))}
    return "\n\n".join(parts), meta


def render_fund(conn, row: dict) -> tuple[str, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM investment_companies WHERE id = %s",
                    (row["investment_company_id"],))
        parent = cur.fetchone()
    parts = [f"# {row['name']} — Investment Fund Profile"]
    parts.append(fields_block([
        ("Managed By", parent and parent["name"]),
        ("Fund Size", money(row.get("fund_size"))),
        ("Fund Size Target", money(row.get("fund_size_target"))),
        ("Typical Check Size", " – ".join(filter(None, (money(row.get("typical_check_size_min")),
                                                        money(row.get("typical_check_size_max"))))) or None),
        ("Minimum Commitment", money(row.get("minimum_commitment"))),
        ("Fund Structure", row.get("fund_structure")),
        ("Fundraising Status", row.get("fundraising_status")),
        ("Industries", labels(row.get("industries"))),
        ("Sector Focus", labels(row.get("sector_focus"))),
        ("Stage Focus", labels(row.get("stage_focus"))),
        ("Geographic Focus", labels(row.get("geographic_focus"))),
    ]))
    for heading, col in (("Executive Summary", "executive_summary"),
                         ("Investment Thesis", "investment_thesis"),
                         ("Portfolio Construction", "portfolio_construction_strategy"),
                         ("How to Pitch", "how_to_pitch"),
                         ("Value Beyond Capital", "value_beyond_capital")):
        v = html_to_md(row.get(col))
        if v:
            parts.append(f"## {heading}\n{v}")
    meta = {"fund_name": row["name"], "slug": row.get("slug"),
            "industries": label_list(row.get("industries")),
            "stage_focus": label_list(row.get("stage_focus"))}
    return "\n\n".join(parts), meta


def render_organization(conn, row: dict) -> tuple[str, dict]:
    parts = [f"# {row['name']} — Organization Profile"]
    parts.append(fields_block([
        ("Website", row.get("website_url")),
        ("Type", row.get("organization_type")),
        ("Industries", labels(row.get("industries")) or labels(row.get("industry_tags"))),
        ("Business Model", labels(row.get("business_model"))),
        ("Headquarters", row.get("headquarters_location")),
        ("Founded On", str(row["founded_on"]) if row.get("founded_on") else None),
        ("Publicly Traded", row.get("is_public_traded")),
    ]))
    desc = html_to_md(row.get("description"))
    if desc:
        parts.append(f"## About\n{desc}")
    meta = {"company_name": row["name"], "slug": row.get("slug"),
            "industries": label_list(row.get("industries"))}
    return "\n\n".join(parts), meta


ENTITY_RENDERERS = {
    "startup_company": ("startup_companies", render_startup),
    "investor": ("investors", render_investor),
    "cv": ("cvs", render_cv),
    "investment_company": ("investment_companies", render_investment_company),
    "investment_fund": ("investment_funds", render_fund),
    "organization": ("organizations", render_organization),
}


def make_profile_doc(entity_type: str, row: dict, markdown: str,
                     meta: dict) -> SourceDoc:
    title = markdown.splitlines()[0].lstrip("# ").strip()
    return SourceDoc(
        source_type="profile", title=title, markdown=markdown,
        visibility=PROFILE_VISIBILITY,
        metadata={k: v for k, v in meta.items() if v not in (None, [], "")},
    )

"""
Market Intelligence Terminal — Streamlit dashboard.

Dark-mode Bloomberg-terminal aesthetic. Mobile friendly.
Run:  streamlit run app.py
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

import streamlit as st

from db import fetch_scan_logs, fetch_signals, fetch_top_picks

st.set_page_config(
    page_title="Market Intelligence Terminal",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

DOMAINS = [
    "All",
    "Advanced Compute",
    "Hyperscale Cloud",
    "Critical Energy",
    "Critical Minerals",
    "Physical AI & Robotics",
]

DOMAIN_ACCENT = {
    "Advanced Compute": "#00e5ff",
    "Hyperscale Cloud": "#7c4dff",
    "Critical Energy": "#00ff9c",
    "Critical Minerals": "#ffb300",
    "Physical AI & Robotics": "#ff4d6d",
}

CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=Inter:wght@400;600;800&display=swap');

html, body, [class*="css"], .stApp {
    background-color: #05070a;
    color: #d7dde5;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
}
#MainMenu, footer, header {visibility: hidden;}

.terminal-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.55rem;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: #e9eef5;
    text-transform: uppercase;
    border-left: 4px solid #00ff9c;
    padding-left: 0.7rem;
    margin-bottom: 0.15rem;
}
.terminal-sub {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.8rem;
    color: #5b6675;
    letter-spacing: 0.08em;
    margin-bottom: 1.4rem;
}

section[data-testid="stSidebar"] {
    background-color: #080b10;
    border-right: 1px solid #161d27;
}
section[data-testid="stSidebar"] * { font-family: 'IBM Plex Mono', monospace; }

.signal-card {
    background: linear-gradient(180deg, #0b0f16 0%, #080b11 100%);
    border: 1px solid #1b2430;
    border-left: 3px solid var(--accent, #00ff9c);
    border-radius: 10px;
    padding: 1.15rem 1.3rem 1.25rem 1.3rem;
    margin-bottom: 1.15rem;
    box-shadow: 0 0 22px rgba(0,0,0,0.55);
}
.card-top {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.55rem;
    margin-bottom: 0.6rem;
}
.badge {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    padding: 0.2rem 0.55rem;
    border-radius: 4px;
    border: 1px solid var(--accent, #00ff9c);
    color: var(--accent, #00ff9c);
    background: rgba(255,255,255,0.02);
}
.badge-company {
    color: #e9eef5;
    border-color: #2b3644;
}
.badge-date {
    color: #6b7686;
    border-color: #222c39;
}
.badge-score {
    background: var(--accent, #00ff9c);
    color: #05070a;
    border-color: var(--accent, #00ff9c);
}
.card-headline {
    font-family: 'Inter', sans-serif;
    font-size: 1.12rem;
    font-weight: 800;
    line-height: 1.35;
    color: #f4f7fb;
    margin: 0.35rem 0 0.55rem 0;
}
.card-analysis {
    font-size: 0.95rem;
    line-height: 1.55;
    color: #aab4c1;
    margin-bottom: 0.7rem;
}
.thesis {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.82rem;
    line-height: 1.5;
    color: #8b97a6;
    border-left: 2px solid #263141;
    padding-left: 0.7rem;
    margin-bottom: 0.9rem;
}
.thesis b { color: #aab4c1; letter-spacing: 0.06em; }
.analyst-row {
    display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.7rem;
}
.chip {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.7rem; font-weight: 600; letter-spacing: 0.06em;
    text-transform: uppercase; padding: 0.22rem 0.55rem; border-radius: 4px;
    border: 1px solid #2b3644; color: #c5cedb; background: rgba(255,255,255,0.02);
}
.chip-rec-buy   { color: #05070a; background: #00ff9c; border-color: #00ff9c; }
.chip-rec-hold  { color: #ffb300; border-color: #ffb300; }
.chip-rec-avoid { color: #ff4d6d; border-color: #ff4d6d; }
.chip-pt-up   { color: #00ff9c; border-color: #1f6f52; }
.chip-pt-down { color: #ff4d6d; border-color: #6f1f34; }
.chip-driver { color: var(--accent, #00ff9c); border-color: var(--accent, #00ff9c); }
.source-btn {
    display: inline-block;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.74rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    text-decoration: none;
    color: #05070a;
    background: var(--accent, #00ff9c);
    padding: 0.4rem 0.9rem;
    border-radius: 5px;
}
.source-btn:hover { filter: brightness(1.12); }

.empty-state {
    border: 1px dashed #263141;
    border-radius: 10px;
    padding: 2.4rem 1.5rem;
    text-align: center;
    color: #6b7686;
    font-family: 'IBM Plex Mono', monospace;
    letter-spacing: 0.05em;
    background: #080b11;
}
.empty-state .pulse { color: #00ff9c; font-weight: 700; }

.metric-row {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 1.4rem;
}
.metric {
    flex: 1 1 120px;
    background: #0a0e14;
    border: 1px solid #18212c;
    border-radius: 8px;
    padding: 0.7rem 0.9rem;
}
.metric .v {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.4rem;
    font-weight: 700;
    color: #00ff9c;
}
.metric .k {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.68rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #5b6675;
}

@media (max-width: 640px) {
    .terminal-title { font-size: 1.2rem; }
    .card-headline { font-size: 1.02rem; }
    .block-container { padding: 1rem 0.8rem 3rem 0.8rem; }
}
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


def _conviction_label(score: int) -> str:
    if score >= 9:
        return f"{score}/10 High Conviction"
    return f"{score}/10 Actionable"


def _fmt_date(value: str | None) -> str:
    if not value:
        return "—"
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return str(value)[:10]
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _rec_class(rec: str) -> str:
    r = (rec or "").upper()
    if "AVOID" in r:
        return "chip-rec-avoid"
    if "NEUTRAL" in r or "WATCH" in r or "HOLD" in r:
        return "chip-rec-hold"
    return "chip-rec-buy"


def _fmt_num(value, prefix: str = "", suffix: str = "", signed: bool = False) -> str | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:
        return None
    body = f"{num:+.1f}" if signed else f"{num:,.2f}".rstrip("0").rstrip(".")
    return f"{prefix}{body}{suffix}"


def render_card(sig: dict) -> None:
    domain = sig.get("domain", "—")
    accent = DOMAIN_ACCENT.get(domain, "#00ff9c")
    company = html.escape(str(sig.get("company", "—")))
    headline = html.escape(str(sig.get("headline", "")))
    analysis = html.escape(str(sig.get("analysis", "")))
    thesis = html.escape(str(sig.get("financial_impact_thesis", "") or ""))
    score = int(sig.get("conviction_score", 0))
    date_str = _fmt_date(sig.get("created_at"))
    url = sig.get("url") or ""

    rec = str(sig.get("recommendation", "") or "").strip()
    driver = str(sig.get("supply_chain_driver", "") or "").strip()
    delta = _fmt_num(sig.get("price_target_delta_pct"), suffix="%", signed=True)
    target = _fmt_num(sig.get("implied_price_target"), prefix="$")

    chips = []
    if rec:
        chips.append(f'<span class="chip {_rec_class(rec)}">{html.escape(rec)}</span>')
    if delta:
        cls = "chip-pt-up" if not delta.startswith("-") else "chip-pt-down"
        pt = f'PT {delta}' + (f' → {html.escape(target)}' if target else '')
        chips.append(f'<span class="chip {cls}">{pt}</span>')
    if driver:
        chips.append(f'<span class="chip chip-driver">{html.escape(driver)}</span>')
    chips_html = f'<div class="analyst-row">{"".join(chips)}</div>' if chips else ""

    thesis_html = (
        f'<div class="thesis"><b>Financial impact:</b> {thesis}</div>' if thesis else ""
    )
    source_html = (
        f'<a class="source-btn" href="{html.escape(url)}" target="_blank" '
        f'rel="noopener">↗ Source</a>'
        if url
        else '<span class="source-btn" style="opacity:0.4;">No source link</span>'
    )

    st.markdown(
        f"""
        <div class="signal-card" style="--accent:{accent};">
          <div class="card-top">
            <span class="badge">{html.escape(domain)}</span>
            <span class="badge badge-company">{company}</span>
            <span class="badge badge-date">{date_str}</span>
            <span class="badge badge-score">[{_conviction_label(score)}]</span>
          </div>
          <div class="card-headline">{headline}</div>
          {chips_html}
          <div class="card-analysis">{analysis}</div>
          {thesis_html}
          {source_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------- sidebar
st.sidebar.markdown("### ⌁ FILTERS")
domain_choice = st.sidebar.selectbox("Domain", DOMAINS, index=0)
min_score = st.sidebar.slider("Minimum conviction score", 1, 10, 8)
st.sidebar.markdown("---")
if st.sidebar.button("↻ Refresh feed", use_container_width=True):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption("Scanning loop active · data via SEC EDGAR + Google News + LLM grading")


@st.cache_data(ttl=60, show_spinner=False)
def load_signals(domain: str, score: int) -> list[dict]:
    try:
        return fetch_signals(domain=domain, min_score=score)
    except Exception as exc:  # surfaced in the UI instead of crashing
        st.error(f"Could not reach Supabase: {exc}")
        return []


@st.cache_data(ttl=60, show_spinner=False)
def load_scan_logs(limit: int = 50) -> list[dict]:
    try:
        return fetch_scan_logs(limit=limit)
    except Exception as exc:
        st.warning(f"Scan audit history unavailable: {exc}")
        return []


@st.cache_data(ttl=60, show_spinner=False)
def load_top_picks(limit: int = 6) -> list[dict]:
    try:
        return fetch_top_picks(limit=limit)
    except Exception as exc:
        st.warning(f"Top picks unavailable: {exc}")
        return []


def render_top_picks(picks: list[dict]) -> None:
    """Executive hero: aggregate metrics + one bordered container per pick."""
    st.markdown(
        '<div class="terminal-sub" style="margin-top:0.4rem;">'
        '🎯 EXECUTIVE TOP PICKS · HIGHEST CONVICTION, LAST 30 DAYS</div>',
        unsafe_allow_html=True,
    )
    if not picks:
        st.info("No qualifying high-conviction picks in the last 30 days. "
                "Scanning loop active.")
        st.markdown("---")
        return

    returns = [
        float(p["price_target_delta_pct"]) for p in picks
        if isinstance(p.get("price_target_delta_pct"), (int, float))
    ]
    with_targets = sum(1 for p in picks if _fmt_num(p.get("implied_price_target")))
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Top picks", len(picks))
    m2.metric("Avg expected return",
              f"{sum(returns) / len(returns):+.1f}%" if returns else "—")
    m3.metric("Best upside", f"{max(returns):+.1f}%" if returns else "—")
    m4.metric("With price target", f"{with_targets}/{len(picks)}")

    st.markdown("")
    for p in picks:
        company = p.get("company", "—")
        domain = str(p.get("domain") or "").strip()
        headline = p.get("headline", "")
        rec = str(p.get("recommendation") or "").strip()
        driver = str(p.get("supply_chain_driver") or "").strip() or "—"
        score = int(p.get("conviction_score", 0) or 0)
        delta_str = _fmt_num(p.get("price_target_delta_pct"), suffix="%", signed=True)
        target_str = _fmt_num(p.get("implied_price_target"), prefix="$")

        with st.container(border=True):
            head, c_ret, c_pt = st.columns([4, 1.4, 1.4])
            with head:
                st.markdown(f"**{company}**" + (f"  ·  {domain}" if domain else ""))
                st.markdown(f"##### {headline}")
                tags = [f"⚙ {driver}"]
                if rec:
                    tags.append(f"**{rec}**")
                if score:
                    tags.append(f"Conviction {score}/10")
                st.caption("  ·  ".join(tags))
            c_ret.metric("Expected return", delta_str or "—")
            c_pt.metric("12M price target", target_str or "n/a")

            with st.expander("Financial impact brief"):
                thesis = str(p.get("financial_impact_thesis") or "").strip()
                analysis = str(p.get("analysis") or "").strip()
                if thesis:
                    st.markdown(f"**Supply / earnings impact** — {thesis}")
                if analysis:
                    st.markdown(f"**Analyst view** — {analysis}")
                if not (thesis or analysis):
                    st.caption("No written brief was captured for this signal.")
                meta = _fmt_date(p.get("created_at"))
                capex = _fmt_num(p.get("cited_capex_usd_m"), prefix="$", suffix="M")
                if capex:
                    meta += f"  ·  cited capex {capex}"
                st.caption(meta)
                url = p.get("url")
                if url:
                    st.link_button("↗ Open source", url)
    st.markdown("---")


# ---------------------------------------------------------------- header
st.markdown('<div class="terminal-title">Market Intelligence Terminal</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="terminal-sub">STRATEGIC LEVERAGE · PHYSICAL CAPEX · SUPPLY-CHAIN CHOKEPOINTS</div>',
    unsafe_allow_html=True,
)


@st.fragment(run_every=60)
def live_dashboard(domain: str, score: int) -> None:
    """Metrics, Executive Top Picks and the live feed — reruns itself every 60s.

    Cached loaders use a 60s TTL, so each fragment tick pulls fresh Supabase
    data without a full-page rerun (sidebar filters and scan audit stay put).
    """
    signals = load_signals(domain, score)

    domains_hit = len({s.get("domain") for s in signals})
    top_score = max((int(s.get("conviction_score", 0)) for s in signals), default=0)
    st.markdown(
        f"""
        <div class="metric-row">
          <div class="metric"><div class="v">{len(signals)}</div><div class="k">Live signals</div></div>
          <div class="metric"><div class="v">{domains_hit}</div><div class="k">Domains active</div></div>
          <div class="metric"><div class="v">{top_score or '—'}</div><div class="k">Peak conviction</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ------------------------------------------------------------ Executive Top Picks
    render_top_picks(load_top_picks(6))

    # ------------------------------------------------------------ Main Feed
    updated = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    st.markdown("### 📡 ALL LIVE SIGNALS")
    st.caption(f"Auto-refreshing every 60s · last updated {updated}")

    if not signals:
        st.markdown(
            """
            <div class="empty-state">
              No high-conviction breakthroughs detected in recent scans.
              <br><span class="pulse">Scanning loop active.</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        for signal in signals:
            render_card(signal)


live_dashboard(domain_choice, min_score)


# ---------------------------------------------------------------- Scan Audit
st.markdown("---")
with st.expander("🗂  Scan Audit History  ·  last 50 company scans", expanded=False):
    logs = load_scan_logs(50)
    if not logs:
        st.caption("No scans recorded yet. Run `python run_scan.py` to populate the audit trail.")
    else:
        table = [
            {
                "Time": _fmt_date(row.get("scanned_at")),
                "Symbol": row.get("company_symbol") or "—",
                "Company": row.get("company_name") or "—",
                "Domain": row.get("domain") or "—",
                "Collected": int(row.get("raw_collected") or 0),
                "Graded": int(row.get("raw_items_count") or 0),
                "Signals": int(row.get("signals_found") or 0),
                "Drop: unresolved": int(row.get("dropped_unresolved") or 0),
                "Drop: paywall": int(row.get("dropped_paywall") or 0),
                "Drop: short": int(row.get("dropped_short") or 0),
            }
            for row in logs
        ]
        _numcol = st.column_config.NumberColumn(format="%d")
        st.dataframe(
            table,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Collected": st.column_config.NumberColumn(
                    format="%d", help="Items found before URL de-duplication"
                ),
                "Graded": st.column_config.NumberColumn(
                    format="%d", help="New items actually sent to the LLM"
                ),
                "Signals": _numcol,
                "Drop: unresolved": st.column_config.NumberColumn(
                    format="%d", help="Google redirect could not be resolved"
                ),
                "Drop: paywall": st.column_config.NumberColumn(
                    format="%d", help="Body was a paywall / teaser"
                ),
                "Drop: short": st.column_config.NumberColumn(
                    format="%d", help="Extracted body under the minimum length"
                ),
            },
        )
        total_collected = sum(r["Collected"] for r in table)
        total_graded = sum(r["Graded"] for r in table)
        total_sig = sum(r["Signals"] for r in table)
        total_drop = sum(
            r["Drop: unresolved"] + r["Drop: paywall"] + r["Drop: short"] for r in table
        )
        saved_pct = 1 - total_graded / total_collected if total_collected else 0.0
        st.caption(
            f"{len(table)} scans · {total_collected} collected · {total_graded} graded · "
            f"{total_sig} signals · {total_drop} dropped · "
            f"{saved_pct:.0%} of LLM calls saved by URL dedup"
        )
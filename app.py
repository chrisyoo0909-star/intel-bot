"""
Market Intelligence Terminal — Streamlit dashboard.

Dark-mode Bloomberg-terminal aesthetic. Mobile friendly.
Run:  streamlit run app.py
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

import streamlit as st

from db import fetch_scan_logs, fetch_signals

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
    margin-bottom: 0.9rem;
}
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


def render_card(sig: dict) -> None:
    domain = sig.get("domain", "—")
    accent = DOMAIN_ACCENT.get(domain, "#00ff9c")
    company = html.escape(str(sig.get("company", "—")))
    headline = html.escape(str(sig.get("headline", "")))
    analysis = html.escape(str(sig.get("analysis", "")))
    score = int(sig.get("conviction_score", 0))
    date_str = _fmt_date(sig.get("created_at"))
    url = sig.get("url") or ""

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
          <div class="card-analysis">{analysis}</div>
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
st.sidebar.caption("Scanning loop active · data via SEC EDGAR + Google News + Gemini")


@st.cache_data(ttl=120, show_spinner=False)
def load_signals(domain: str, score: int) -> list[dict]:
    try:
        return fetch_signals(domain=domain, min_score=score)
    except Exception as exc:  # surfaced in the UI instead of crashing
        st.error(f"Could not reach Supabase: {exc}")
        return []


@st.cache_data(ttl=120, show_spinner=False)
def load_scan_logs(limit: int = 50) -> list[dict]:
    try:
        return fetch_scan_logs(limit=limit)
    except Exception as exc:
        st.warning(f"Scan audit history unavailable: {exc}")
        return []


# ---------------------------------------------------------------- header
st.markdown('<div class="terminal-title">Market Intelligence Terminal</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="terminal-sub">STRATEGIC LEVERAGE · PHYSICAL CAPEX · SUPPLY-CHAIN CHOKEPOINTS</div>',
    unsafe_allow_html=True,
)

signals = load_signals(domain_choice, min_score)

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


# ---------------------------------------------------------------- scan audit
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
                "Raw Items": int(row.get("raw_items_count") or 0),
                "Signals Saved": int(row.get("signals_found") or 0),
            }
            for row in logs
        ]
        st.dataframe(
            table,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Raw Items": st.column_config.NumberColumn(format="%d"),
                "Signals Saved": st.column_config.NumberColumn(format="%d"),
            },
        )
        total_raw = sum(r["Raw Items"] for r in table)
        total_sig = sum(r["Signals Saved"] for r in table)
        st.caption(
            f"{len(table)} scans shown · {total_raw} raw items processed · "
            f"{total_sig} signals saved"
        )

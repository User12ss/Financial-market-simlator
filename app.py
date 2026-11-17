"""Streamlit web UI for the financial market simulation framework."""

from __future__ import annotations
import streamlit as st
from orchestrator import Orchestrator
from utils.llm_client import LLMError, PROVIDER_NAMES
import config
from config import AGENT_ROSTER


def build_orchestrator(provider: str, model: str, api_key: str) -> Orchestrator:
    """Create the orchestrator, or stop the page with a readable error."""
    try:
        orch = Orchestrator(provider=provider, model=model or None, api_key=api_key or None)
    except LLMError as e:
        st.error(str(e))
        st.stop()
    problems = [line for line in orch.status_lines() if line.startswith("LLM PROBLEM")]
    if problems:
        st.error("\n\n".join(problems))
        st.stop()
    return orch

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Financial Market Simulator",
    page_icon="📈",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ Configuration")
    provider = st.selectbox(
        "LLM provider",
        PROVIDER_NAMES,
        index=PROVIDER_NAMES.index(config.LLM_PROVIDER) if config.LLM_PROVIDER in PROVIDER_NAMES else 0,
        help="ollama runs open-source models on this machine. The default comes from LLM_PROVIDER in .env.",
    )
    default_model = config.LLM_MODEL if provider == config.LLM_PROVIDER else config.provider_default(provider, "model")
    model = st.text_input("Model", value=default_model, help="Model name as the provider knows it")
    api_key = ""
    if provider != "ollama":
        api_key = st.text_input(
            "API key", type="password",
            help="Leave empty to use the key from .env. Not needed for local models.",
        )
    economy = st.selectbox("Base Economy", ["US", "EU", "UK", "China", "Japan", "India", "Brazil", "Global"])

    st.divider()
    st.subheader("Agents")
    for a in AGENT_ROSTER:
        with st.expander(a["name"]):
            st.caption(a["domain"])

    st.divider()
    st.caption("Multi-Agent Simulation • model provider is configurable")

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("📈 Financial Market Simulation Engine")
st.markdown("Simulate how markets react to macroeconomic, monetary, and fiscal policy changes using a multi-agent AI system.")

# ---------------------------------------------------------------------------
# Mode selector
# ---------------------------------------------------------------------------
tab_single, tab_compare = st.tabs(["🔬 Single Scenario", "⚖️ Compare Scenarios"])

# ---------------------------------------------------------------------------
# Single scenario tab
# ---------------------------------------------------------------------------
with tab_single:
    scenario_input = st.text_area(
        "Describe a policy scenario",
        placeholder="e.g., The Fed raises interest rates by 100bps unexpectedly while inflation is running at 6%",
        height=100,
    )

    example_scenarios = [
        "The Fed cuts rates by 50bps in an emergency meeting",
        "Congress passes a $2 trillion infrastructure bill funded by corporate tax hikes",
        "China devalues the yuan by 10% in response to new trade tariffs",
        "Oil prices spike to $150/barrel due to Middle East supply disruption",
        "The ECB begins quantitative tightening while the US is cutting rates",
    ]
    st.caption("**Quick examples:**")
    cols = st.columns(3)
    for i, ex in enumerate(example_scenarios):
        col = cols[i % 3]
        if col.button(ex[:50] + "...", key=f"ex_{i}", use_container_width=True):
            scenario_input = ex

    if st.button("🚀 Run Simulation", type="primary", use_container_width=True):
        if not scenario_input:
            st.warning("Please enter a scenario.")
        else:
            orch = build_orchestrator(provider, model, api_key)

            progress_bar = st.progress(0, text="Parsing & routing scenario (fast model)...")
            status_container = st.empty()
            completed = {"n": 0}

            def on_start(name):
                status_container.info(f"🔄 Running **{name}**...")

            def on_done(name):
                # The orchestrator decides how many agents to consult, so there
                # is no fixed total. The bar advances per finished agent and
                # waits below 100% until the report is built.
                completed["n"] += 1
                pct = min(10 + completed["n"] * 8, 85)
                progress_bar.progress(pct, text=f"✅ {name} complete ({completed['n']} agents done)")

            try:
                report = orch.run(
                    scenario_input,
                    base_economy=economy,
                    parallel=True,
                    on_agent_start=on_start,
                    on_agent_done=on_done,
                )
                progress_bar.progress(100, text="✅ Simulation complete!")
                status_container.empty()
                st.caption(orch.llm.usage.summary())

                # --- Display Report ---
                st.divider()
                st.header("📊 Simulation Report")

                for warning in report.quality_warnings:
                    st.warning(warning)

                # Summary
                st.subheader("Policy Scenario Summary")
                st.info(report.scenario_summary)

                # Synthesis
                if report.synthesis:
                    st.subheader("🎯 Key Takeaways")
                    st.success(report.synthesis)

                # Timeline tabs
                t1, t2, t3 = st.tabs(["⚡ Immediate (0-1 week)", "📅 Short-Term (1-3 months)", "🔭 Medium-Term (3-12 months)"])
                with t1:
                    st.markdown(report.immediate_reaction)
                with t2:
                    st.markdown(report.short_term_transmission)
                with t3:
                    st.markdown(report.medium_term_outlook)

                # Scenario matrix
                st.subheader("🎲 Scenario Matrix")
                if report.scenario_matrix:
                    matrix_cols = st.columns(len(report.scenario_matrix))
                    colors = {"Base case": "blue", "Bull case": "green", "Bear case": "red"}
                    for i, s in enumerate(report.scenario_matrix):
                        with matrix_cols[i]:
                            color = colors.get(s.label, "gray")
                            st.markdown(f"**:{color}[{s.label}]** — {s.probability}")
                            st.markdown(f"**Driver:** {s.key_driver}")
                            st.markdown(f"**Impact:** {s.market_impact}")

                # Cross-domain map
                st.subheader("🔗 Cross-Domain Dependency Map")
                st.markdown(report.cross_domain_map)

                # Agent details
                st.subheader("🤖 Individual Agent Analyses")
                for a in report.agent_analyses:
                    with st.expander(f"{a.agent_name} — Confidence: {a.confidence}"):
                        col1, col2, col3 = st.columns(3)
                        with col1:
                            st.markdown("**⚡ Immediate**")
                            st.markdown(a.immediate_impact)
                        with col2:
                            st.markdown("**📅 Short-Term**")
                            st.markdown(a.short_term_impact)
                        with col3:
                            st.markdown("**🔭 Medium-Term**")
                            st.markdown(a.medium_term_impact)
                        st.markdown("**🔗 Cross-Domain Effects**")
                        st.markdown(a.cross_domain_effects)

                # Risk factors
                st.subheader("⚠️ Risk Factors & Assumptions")
                st.warning(report.risk_factors)

                # Download
                st.divider()
                st.download_button(
                    "📥 Download Full Report (Markdown)",
                    report.to_markdown(),
                    file_name="simulation_report.md",
                    mime="text/markdown",
                )

            except Exception as e:
                progress_bar.empty()
                status_container.empty()
                st.error(f"Simulation failed: {e}")

# ---------------------------------------------------------------------------
# Compare tab
# ---------------------------------------------------------------------------
with tab_compare:
    col_a, col_b = st.columns(2)
    with col_a:
        scenario_a = st.text_area("Scenario A", placeholder="e.g., Fed raises rates 25bps", height=100, key="cmp_a")
    with col_b:
        scenario_b = st.text_area("Scenario B", placeholder="e.g., Fed raises rates 100bps", height=100, key="cmp_b")

    if st.button("⚖️ Compare Scenarios", type="primary", use_container_width=True):
        if not scenario_a or not scenario_b:
            st.warning("Please enter both scenarios.")
        else:
            orch = build_orchestrator(provider, model, api_key)

            with st.spinner("Running Scenario A..."):
                report_a = orch.run(scenario_a, base_economy=economy, parallel=True)
            with st.spinner("Running Scenario B..."):
                report_b = orch.run(scenario_b, base_economy=economy, parallel=True)
            with st.spinner("Generating comparative analysis..."):
                comparison = orch._compare_reports(report_a, report_b)

            st.divider()
            st.header("⚖️ Comparative Analysis")
            st.markdown(comparison)

            st.divider()
            st.subheader("Individual Reports")
            r1, r2 = st.tabs(["Scenario A Report", "Scenario B Report"])
            with r1:
                st.markdown(report_a.to_markdown())
            with r2:
                st.markdown(report_b.to_markdown())

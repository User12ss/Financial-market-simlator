"""Data models for simulation outputs and reports."""

from __future__ import annotations
from pydantic import BaseModel, Field


class AgentAnalysis(BaseModel):
    """Output from a single agent's analysis of a scenario."""

    agent_id: str
    agent_name: str
    domain: str
    immediate_impact: str = Field(description="0-1 week reaction")
    short_term_impact: str = Field(description="1-3 month transmission")
    medium_term_impact: str = Field(description="3-12 month outlook")
    cross_domain_effects: str = Field(description="How this agent's output affects other domains")
    confidence: str = Field(default="moderate", description="Agent's confidence in its analysis")


class ScenarioOutcome(BaseModel):
    """A single scenario in the probability-weighted matrix."""

    label: str  # base / bull / bear
    probability: str  # e.g. "55%"
    key_driver: str
    market_impact: str


class SimulationReport(BaseModel):
    """Complete simulation output combining all agent analyses."""

    scenario_summary: str
    agent_analyses: list[AgentAnalysis] = Field(default_factory=list)
    immediate_reaction: str = ""
    short_term_transmission: str = ""
    medium_term_outlook: str = ""
    cross_domain_map: str = ""
    scenario_matrix: list[ScenarioOutcome] = Field(default_factory=list)
    risk_factors: str = ""
    synthesis: str = ""
    data_sources: list[str] = Field(default_factory=list, description="External data and memory used")
    quality_warnings: list[str] = Field(
        default_factory=list, description="Problems the code detected in the agents' output"
    )

    def to_markdown(self) -> str:
        """Render the full report as Markdown."""
        lines = []
        lines.append("# Financial Market Simulation Report\n")

        if self.data_sources:
            lines.append("## Data Sources Used\n")
            for src in self.data_sources:
                lines.append(f"- {src}")
            lines.append("")

        if self.quality_warnings:
            lines.append("## Quality Warnings\n")
            for warning in self.quality_warnings:
                lines.append(f"- {warning}")
            lines.append("")

        lines.append("## Policy Scenario Summary\n")
        lines.append(self.scenario_summary + "\n")

        lines.append("---\n")
        lines.append("## Agent Analyses\n")
        for a in self.agent_analyses:
            lines.append(f"### {a.agent_name} ({a.domain})\n")
            lines.append(f"**Immediate (0-1 week):** {a.immediate_impact}\n")
            lines.append(f"**Short-term (1-3 months):** {a.short_term_impact}\n")
            lines.append(f"**Medium-term (3-12 months):** {a.medium_term_impact}\n")
            lines.append(f"**Cross-domain effects:** {a.cross_domain_effects}\n")
            lines.append("")

        lines.append("---\n")
        lines.append("## Immediate Market Reaction (0-1 week)\n")
        lines.append(self.immediate_reaction + "\n")

        lines.append("## Short-Term Transmission (1-3 months)\n")
        lines.append(self.short_term_transmission + "\n")

        lines.append("## Medium-Term Outlook (3-12 months)\n")
        lines.append(self.medium_term_outlook + "\n")

        lines.append("---\n")
        lines.append("## Cross-Domain Dependency Map\n")
        lines.append(self.cross_domain_map + "\n")

        lines.append("---\n")
        lines.append("## Scenario Matrix\n")
        lines.append("| Scenario | Probability | Key Driver | Market Impact |")
        lines.append("|----------|------------|------------|---------------|")
        for s in self.scenario_matrix:
            lines.append(f"| {s.label} | {s.probability} | {s.key_driver} | {s.market_impact} |")
        lines.append("")

        lines.append("---\n")
        lines.append("## Risk Factors & Assumptions\n")
        lines.append(self.risk_factors + "\n")

        if self.synthesis:
            lines.append("---\n")
            lines.append("## Synthesis & Key Takeaways\n")
            lines.append(self.synthesis + "\n")

        return "\n".join(lines)

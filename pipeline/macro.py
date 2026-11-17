"""The macro layer: seven agents read the current state, code combines their readings.

No model writes the desk's macro view. Each agent gives a stance, a
confidence and optional sector calls for its own domain, and the view is
computed from those by the rules below. The same assessments always give the
same view, and it can be traced back to who said what.
"""

from __future__ import annotations

import math
from datetime import date

from models.signals import DomainAssessment, MacroRegime, SectorTilt

# With fewer assessments than this there is no macro view, and the pipeline
# opens no new positions.
MIN_ASSESSMENTS = 4
# A sector is tilted only when the confidence behind one side outweighs the
# other by this much: more than any single agent can give, so one agent
# listing every sector tilts nothing.
MIN_TILT_WEIGHT = 1.0
# Sums of confidences that differ by less than this are treated as equal.
_TOLERANCE = 1e-9

_APPETITE = {"supportive": "risk_on", "neutral": "neutral", "restrictive": "risk_off"}


def combine(assessments: list[DomainAssessment], session: date) -> MacroRegime | None:
    """The desk's macro view, or None when too few agents produced a usable assessment.

    Risk appetite is the stance with the most confidence behind it, and the
    view's own confidence is that stance's share of all the confidence given.
    A tie comes out neutral. Agents who all gave zero confidence have not
    formed a view, and none is returned.
    """
    if len(assessments) < MIN_ASSESSMENTS:
        return None

    weight = {"supportive": 0.0, "neutral": 0.0, "restrictive": 0.0}
    for item in assessments:
        weight[item.stance] += item.confidence
    total = sum(weight.values())
    if total <= _TOLERANCE:
        return None
    best = max(weight.values())
    leaders = [stance for stance, value in weight.items() if math.isclose(value, best, abs_tol=_TOLERANCE)]
    if len(leaders) > 1:
        stance, confidence = "neutral", weight["neutral"] / total
    else:
        stance, confidence = leaders[0], best / total

    tilts = []
    sectors = dict.fromkeys(call.sector for item in assessments for call in item.sector_tilts)
    for sector in sectors:
        over = [a for a in assessments if any(c.sector == sector and c.tilt == "overweight" for c in a.sector_tilts)]
        under = [a for a in assessments if any(c.sector == sector and c.tilt == "underweight" for c in a.sector_tilts)]
        net = sum(a.confidence for a in over) - sum(a.confidence for a in under)
        if abs(net) >= MIN_TILT_WEIGHT - _TOLERANCE:
            side = over if net > 0 else under
            tilts.append(SectorTilt(
                sector=sector, tilt="overweight" if net > 0 else "underweight",
                supporters=[a.agent_id for a in side],
            ))

    return MacroRegime(
        session_date=session, risk_appetite=_APPETITE[stance], confidence=round(confidence, 2),
        sector_tilts=tilts, key_risks=[f"{a.agent_name}: {a.key_risk}" for a in assessments],
        assessments=assessments,
    )


def render_regime(regime: MacroRegime | None) -> str:
    """The macro view as the text other agents are shown."""
    if regime is None:
        return "MACRO VIEW: not available today."
    lines = [
        f"MACRO VIEW (the desk's macro team, formed on {regime.session_date}): "
        f"risk appetite {regime.risk_appetite.replace('_', '-')}, confidence {regime.confidence:.2f}"
    ]
    over = [t.sector for t in regime.sector_tilts if t.tilt == "overweight"]
    under = [t.sector for t in regime.sector_tilts if t.tilt == "underweight"]
    if over:
        lines.append(f"- Favoured sectors: {', '.join(over)}")
    if under:
        lines.append(f"- Sectors to avoid: {', '.join(under)}")
    lines.append("- Main risks:")
    lines.extend(f"  * {risk}" for risk in regime.key_risks)
    return "\n".join(lines)

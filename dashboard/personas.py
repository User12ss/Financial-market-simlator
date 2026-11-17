"""Who stands in the meeting room.

Fourteen figures: twelve are models and two are code. The risk officer and
the trader are rules, not models, and the page says so on their badge. A
figure has a position (its job), not a personal name: that was the user's
choice. The titles are in Chinese because the page is; what a figure says is
shown exactly as it was recorded, in the language it was written in.

Seats run clockwise from the top of the circle. The bull and the bear stand
directly opposite each other, with the judge between them at the bottom and
the portfolio manager facing them from the top.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

# Who speaks at each stage of the pipeline, for the page's legend.
STAGES = [
    ("settle", "结算"), ("macro", "宏观"), ("views", "观点"), ("debate", "辩论"),
    ("proposals", "提案"), ("checks", "检查"), ("risk", "风控"), ("orders", "下单"),
]


@dataclass(frozen=True)
class Persona:
    id: str                 # the speaker id the journal records
    title: str              # the job, as shown on the page
    kind: Literal["llm", "rule"]
    group: str
    seat: int               # 0 is the top of the circle, counted clockwise
    does: str               # one line on what this figure is for
    tone: int               # which of the page's figure colours it wears
    hair: Literal["short", "bob", "bun", "long", "crop", "cap"]
    extra: Literal["none", "glasses", "tie", "headset", "badge", "scarf"] = "none"


ROSTER: tuple[Persona, ...] = (
    Persona("portfolio_manager", "组合经理", "llm", "决策", 0,
            "听完所有人的意见，提出今天买、卖、持有或减仓哪些标的。", 0, "short", "tie"),
    Persona("ticker_analyst", "标的分析师", "llm", "标的组", 1,
            "对每只候选和持仓，综合价格、基本面和新闻给出一条观点。", 1, "bob", "glasses"),
    Persona("central_bank", "央行分析师", "llm", "宏观组", 2,
            "看货币政策、利率路径和流动性。", 2, "crop"),
    Persona("bull", "多方", "llm", "辩论", 3,
            "在辩论中为买入或继续持有找最强的理由。", 3, "long"),
    Persona("fiscal_policy", "财政分析师", "llm", "宏观组", 4,
            "看财政支出、税收和国债供给。", 2, "bun", "glasses"),
    Persona("equity_market", "股市分析师", "llm", "宏观组", 5,
            "看板块轮动、估值和风险偏好。", 2, "short"),
    Persona("fixed_income", "债市分析师", "llm", "宏观组", 6,
            "看收益率曲线和信用利差。", 2, "bob"),
    Persona("judge", "裁判", "llm", "辩论", 7,
            "听完多空双方，裁定这只标的该不该持有。", 4, "crop", "scarf"),
    Persona("forex", "外汇分析师", "llm", "宏观组", 8,
            "看汇率、利差和资本流动。", 2, "long", "glasses"),
    Persona("commodities_inflation", "商品与通胀分析师", "llm", "宏观组", 9,
            "看商品价格和通胀走向。", 2, "short"),
    Persona("bear", "空方", "llm", "辩论", 10,
            "在辩论中为不买或卖出找最强的理由。", 5, "bun"),
    Persona("sentiment", "情绪分析师", "llm", "宏观组", 11,
            "看市场情绪、仓位和波动预期。", 2, "bob"),
    Persona("risk_officer", "风控官", "rule", "执行", 12,
            "按写死的规则检查每条提案和每张订单，批准、削减或拒绝。它是代码，不是模型。", 6, "cap", "badge"),
    Persona("trader", "交易员", "rule", "执行", 13,
            "把通过风控的订单下到模拟盘，并报告成交。它是代码，不是模型。", 6, "cap", "headset"),
)

# Lines the pipeline itself records (the macro view it computed, say) are not said by any figure.
NARRATOR = {"id": "pipeline", "title": "流程播报"}
EVERYONE = {"id": "all", "title": "所有人"}

_BY_ID = {persona.id: persona for persona in ROSTER}


def title_of(speaker: str) -> str:
    """The Chinese title for a speaker or listener id; the id itself when it is not one of ours."""
    if speaker in _BY_ID:
        return _BY_ID[speaker].title
    for other in (NARRATOR, EVERYONE):
        if speaker == other["id"]:
            return other["title"]
    return speaker


def roster() -> list[dict]:
    return [asdict(persona) for persona in ROSTER]

"""Who stands in the meeting room.

Seventeen figures: thirteen are models and four are code. The risk officer,
the trader, the news desk and the quant engineer are code, not models, and
the page says so on their badge. A figure has a position (its job), not a
personal name: that was the user's choice. The titles are in Chinese because
the page is; what a figure says is shown exactly as it was recorded, in the
language it was written in.

Seats run clockwise from the top of the circle, where the portfolio manager
stands, so seat n and seat 17 - n are at the same height on the two sides of
the room. The first seats follow a meeting's own order: the news desk, the
ticker analyst, the quant engineer, then the bull. The bull and the bear
stand at the two ends of the table, facing each other down its length. The
judge and the scenario analyst, the last of the models to speak, stand
nearest the reader, either side of the middle.

Who stands where is also a matter of room. The two at each end of the room
carry their names beside them and have the least space for one, so the short
names go there; the longest title has a seat with its name under it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

# Who speaks at each stage of the pipeline, for the page's legend.
STAGES = [
    ("briefing", "情报"),       # only at the desk's check-ins: what has come in since the last meeting
    ("settle", "结算"), ("macro", "宏观"), ("views", "观点"),
    ("forecast", "预测"),       # only at the desk's close meeting: the forecast engine's distributions
    ("debate", "辩论"),
    ("proposals", "提案"), ("checks", "检查"), ("risk", "风控"),
    ("plans", "计划"),          # only at the desk's close meeting too: the plan for each trade, what held, and what was done about it
    ("scenario", "推演"),      # only there too, once every decision is made: events a seat proposes, and the paths read backwards with them in
    ("orders", "下单"),
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
    emblem: str = ""        # the mark of its field, drawn by the page
    craft: tuple[str, ...] = ()     # what it is a specialist in, as a few tags
    # The recorded series that belong to its field, by the snapshot's own ids. "sectors" stands for
    # the sector funds as a group. This is how the page sorts the data, not what the agent was
    # given: every macro analyst is shown the same whole table.
    reads: tuple[str, ...] = ()


ROSTER: tuple[Persona, ...] = (
    Persona("portfolio_manager", "组合经理", "llm", "决策", 0,
            "听完所有人的意见，提出今天买、卖、持有或减仓哪些标的。", 0, "short", "tie",
            "pie", ("组合构建", "仓位取舍", "综合判断")),
    Persona("news_desk", "情报员", "rule", "情报", 1,
            "碰头会一开始报告：上一场会议之后，账上的股票进来了多少条新消息、多少份交给美国证监会的新公告。对有新情况的每一只，"
            "说出各有几条、现在的价格在不在平常的日波动之内。收盘会上，在标的分析师给出观点之前，逐只说出它的新公告。"
            "没取到的数据会说明；计划里的条件成立、该开会时，由它提出来。它是代码，不是模型。",
            6, "cap", "none",
            "wire", ("新消息", "新公告", "价格异动", "数据缺口")),
    Persona("ticker_analyst", "标的分析师", "llm", "标的组", 2,
            "对每只候选和持仓，综合价格、基本面和新闻给出一条观点。", 1, "bob", "glasses",
            "scope", ("价格走势", "基本面", "技术指标", "止损位")),
    Persona("quant_engineer", "量化工程师", "rule", "量化", 3,
            "收盘会上读出预测引擎算出的结果：市场处在哪种状态，讨论到的股票 1、5、10、20 个交易日后的价格分布；"
            "再把交易倒推一遍，并把情景推演师提出的事件放进去推演。到期的预测和实际结果对账。它是代码，不是模型。",
            6, "cap", "glasses",
            "bell", ("市场状态", "价格分布", "倒推与推演", "到期对账")),
    Persona("bull", "多方", "llm", "辩论", 4,
            "在辩论中为买入或继续持有找最强的理由。", 3, "long", "none",
            "bull", ("上行逻辑", "催化剂", "最强证据")),
    Persona("fiscal_policy", "财政分析师", "llm", "宏观组", 5,
            "看财政支出、税收和国债供给。", 2, "bun", "glasses",
            "ledger", ("财政支出", "税收", "国债供给"),
            ("^TNX", "^TYX", "T10Y2Y", "PAYEMS", "UNRATE")),
    Persona("equity_market", "股市分析师", "llm", "宏观组", 6,
            "看板块轮动、估值和风险偏好。", 2, "short", "none",
            "candles", ("板块轮动", "估值", "风险偏好"),
            ("^GSPC", "^IXIC", "^VIX", "sectors")),
    Persona("fixed_income", "债市分析师", "llm", "宏观组", 7,
            "看收益率曲线和信用利差。", 2, "bob", "none",
            "curve", ("收益率曲线", "期限利差", "信用利差"),
            ("^IRX", "^FVX", "^TNX", "^TYX", "T10Y2Y", "BAMLH0A0HYM2")),
    Persona("judge", "裁判", "llm", "辩论", 8,
            "听完多空双方，裁定这只标的该不该持有。", 4, "crop", "scarf",
            "scales", ("权衡论据", "裁定", "反转条件")),
    Persona("scenario_analyst", "情景推演师", "llm", "标的组", 9,
            "所有决定做完之后，挑有新闻可依的几只股票，看新闻有没有指向一件模拟里没有的事。"
            "有，才提出来，并估计它发生的概率；没有，是常见的回答。概率是它的判断，不是量出来的。",
            1, "long", "tie",
            "fork", ("新闻里的事件", "发生的概率", "事后怎么验证")),
    Persona("forex", "外汇分析师", "llm", "宏观组", 10,
            "看汇率、利差和资本流动。", 2, "long", "glasses",
            "exchange", ("汇率", "利差", "资本流动"),
            ("DX-Y.NYB", "DGS2", "DGS10", "GC=F")),
    Persona("commodities_inflation", "商品与通胀分析师", "llm", "宏观组", 11,
            "看商品价格和通胀走向。", 2, "short", "none",
            "barrel", ("原油", "黄金", "通胀"),
            ("CL=F", "DCOILWTICO", "GC=F", "CPIAUCSL", "CPILFESL")),
    Persona("central_bank", "央行分析师", "llm", "宏观组", 12,
            "看货币政策、利率路径和流动性。", 2, "crop", "none",
            "bank", ("货币政策", "利率路径", "流动性"),
            ("FEDFUNDS", "^IRX", "DGS2", "CPIAUCSL", "CPILFESL", "UNRATE")),
    Persona("bear", "空方", "llm", "辩论", 13,
            "在辩论中为不买或卖出找最强的理由。", 5, "bun", "none",
            "bear", ("下行风险", "反方论据", "最强证据")),
    Persona("sentiment", "情绪分析师", "llm", "宏观组", 14,
            "看市场情绪、仓位和波动预期。", 2, "bob", "none",
            "pulse", ("市场情绪", "波动预期", "信用压力"),
            ("^VIX", "BAMLH0A0HYM2", "^GSPC", "^IXIC")),
    Persona("risk_officer", "风控官", "rule", "执行", 15,
            "按写死的规则检查每条提案和每张订单，批准、削减或拒绝。它是代码，不是模型。", 6, "cap", "badge",
            "shield", ("仓位上限", "止损距离", "熔断")),
    Persona("trader", "交易员", "rule", "执行", 16,
            "把通过风控的订单下到模拟盘，并报告成交。它是代码，不是模型。", 6, "cap", "headset",
            "ticket", ("下单", "成交回报", "撤单")),
)

# Lines the pipeline itself records (the macro view it computed, say) are not said by any figure: the page lets
# them speak through the screen in the middle of the room.
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

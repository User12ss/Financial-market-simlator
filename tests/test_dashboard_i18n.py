"""The dashboard's two languages.

The page is written in Chinese; every Chinese string of its scripts goes through ``t``, which looks it up in
``en.js`` when the reader has chosen English. Two things would quietly break that: a new string written
without ``t`` (it would stay Chinese on the English page), and a string with no English on record. Both are
checked here against the scripts themselves, so nobody has to remember.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "dashboard" / "static" / "js"
NODE = shutil.which("node")
HAN = re.compile(r"[㐀-鿿]")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "`": "`", "$": "$"}
REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
# The switch names the language it leads to in that language: these two are Chinese on the English page on purpose.
IN_CHINESE_ON_PURPOSE = {"中文", "切换到中文界面"}


def cooked(raw: str) -> str:
    out, i = [], 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            if raw[i + 1] == "u":
                out.append(chr(int(raw[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append(ESCAPES.get(raw[i + 1], raw[i + 1]))
            i += 2
            continue
        out.append(raw[i])
        i += 1
    return "".join(out)


class Reader:
    """Reads a script far enough to tell its strings apart: which are handed to ``t``, and which are not."""

    def __init__(self, src: str):
        self.src = src
        self.keys: list[str] = []          # what t() is handed: a string as it is, a template as its fixed parts joined by {}
        self.bare: list[str] = []          # strings with Chinese in them that are not handed to t()

    def _last(self, out: list[str]) -> str:
        text = "".join(out[-40:]).rstrip()
        word = re.search(r"([A-Za-z_$][\w$]*)$", text)
        return word.group(1) if word else text[-1:]

    def code(self, i: int = 0, in_expr: bool = False) -> int:
        src, out, depth = self.src, [], 0
        while i < len(src):
            ch = src[i]
            if src.startswith("//", i):
                end = src.find("\n", i)
                i = len(src) if end < 0 else end
            elif src.startswith("/*", i):
                i = src.index("*/", i) + 2
            elif ch in "\"'":
                end = i + 1
                while src[end] != ch:
                    end += 2 if src[end] == "\\" else 1
                value = cooked(src[i + 1:end])
                if "".join(out[-2:]).endswith("t(") and re.search(r"(^|[^\w$.])t\($", "".join(out[-4:])):
                    self.keys.append(value)
                elif HAN.search(value):
                    self.bare.append(value)
                out.append('""')
                i = end + 1
            elif ch == "`":
                tag = re.search(r"(^|[^\w$.])t$", "".join(out[-3:]))
                i, parts = self.template(i)
                if tag:
                    self.keys.append("{}".join(parts))
                elif any(HAN.search(part) for part in parts):
                    self.bare.append("{}".join(parts))
                out.append("``")
            elif ch == "/" and (self._last(out) in REGEX_AFTER or self._last(out) in ("return", "typeof", "")):
                end, in_class = i + 1, False
                while src[end] != "/" or in_class:
                    if src[end] == "\\":
                        end += 1
                    in_class = (in_class or src[end] == "[") and src[end] != "]"
                    end += 1
                i = end + 1
                out.append("/x/")
            elif ch == "{":
                depth += 1
                out.append(ch)
                i += 1
            elif ch == "}":
                if in_expr and depth == 0:
                    return i
                depth -= 1
                out.append(ch)
                i += 1
            else:
                out.append(ch)
                i += 1
        return i

    def template(self, i: int) -> tuple[int, list[str]]:
        src, parts, part = self.src, [], []
        i += 1
        while True:
            if src[i] == "\\":
                part.append(src[i:i + 2])
                i += 2
            elif src.startswith("${", i):
                parts.append(cooked("".join(part)))
                part = []
                i = self.code(i + 2, True) + 1
            elif src[i] == "`":
                parts.append(cooked("".join(part)))
                return i + 1, parts
            else:
                part.append(src[i])
                i += 1


def read_scripts() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    keys, bare = {}, {}
    for path in sorted(SCRIPTS.glob("*.js")):
        if path.name in ("en.js", "i18n.js"):
            continue
        reader = Reader(path.read_text(encoding="utf-8"))
        assert reader.code() == len(reader.src), path.name
        keys[path.name], bare[path.name] = reader.keys, reader.bare
    return keys, bare


def english() -> dict[str, str]:
    """en.js is one object of strings, written as JSON between its braces."""
    text = (SCRIPTS / "en.js").read_text(encoding="utf-8")
    body = text[text.index("export const EN = {") + len("export const EN = "):text.rindex("};") + 1]
    return json.loads(re.sub(r",\s*}$", "}", body))


def test_every_chinese_string_of_the_page_goes_through_the_lookup():
    keys, bare = read_scripts()
    assert sum(len(found) for found in keys.values()) > 900            # the page's words are all here to be read
    loose = {name: sorted(set(found) - IN_CHINESE_ON_PURPOSE) for name, found in bare.items()}
    assert {name: found for name, found in loose.items() if found} == {}, "write these as t(\"…\") or t`…`"
    # The reader can tell the two apart: a made-up script with one of each.
    made_up = Reader('const a = t("会议室"); const b = "回放"; const c = t`共 ${n > 1 ? `${n}` : "一"} 条`; const d = /条"记录/;')
    made_up.code()
    assert (made_up.keys, made_up.bare) == (["会议室", "共 {} 条"], ["回放", "一"])


def test_every_string_the_page_looks_up_has_its_english():
    keys, _ = read_scripts()
    table = english()
    wanted = {key for found in keys.values() for key in found if HAN.search(key)}
    assert sorted(wanted - set(table)) == [], "add these to dashboard/static/js/en.js"
    for key in wanted:
        places = [int(place) for place in re.findall(r"\{(\d+)\}", table[key])]
        assert table[key].strip() != "" or key.strip() == "", key
        assert all(place < key.count("{}") for place in places), (key, table[key])
    # No English entry is left in Chinese by mistake.
    assert [key for key, text in table.items() if HAN.search(text)] == []


def test_the_names_the_server_sends_have_their_english_too():
    from dashboard import personas

    table = english()
    sent = {personas.NARRATOR["title"], personas.EVERYONE["title"]} | {title for _, title in personas.STAGES}
    for seat in personas.ROSTER:
        sent |= {seat.title, seat.group, seat.does, *seat.craft}
    assert sorted(sent - set(table)) == []
    # Two seats never share an English name: a reader must be able to tell them apart.
    names = [table[seat.title] for seat in personas.ROSTER]
    assert len(set(names)) == len(names) == 17


@pytest.mark.skipif(NODE is None, reason="node is not installed, so the page's own logic cannot be run here")
def test_the_sentences_the_server_composes_come_out_in_english(tmp_path):
    """The tallies the reader writes for each seat are matched on the page by pattern. Each kind is put through
    the page's own translator here, so a change of wording on the server cannot leave one in Chinese unnoticed."""
    sentences = [
        "12 条有方向的观点已评分：方向对了 7 条，跑赢基准 6 条；另有 30 条还没到期",
        "写过 10 条观点，还没有一条评分（10 条没到期）",
        "提出的交易里已平仓 3 笔（赢 2，亏 1，其中止损出场 1），还开着 8 笔",
        "SPY 买入持有", "候选池等权", "模拟账户", "立场：中性，把握度 0.75", "沿用 2026-11-23 的宏观结论",
        "10 条观点：看多 8，中性 2，看空 0", "在 2 场辩论中发言 2 次", "2 条裁决：持有 2，不持有 0",
        "10 条提案：买入 2，卖出 0，减仓 0，持有 8", "提案：接受 10，拒绝 0；订单：批准 0，削减 2，拒绝 0",
        "成交 2 笔，下单 2 张", "3 条更新：不变 2，加强 1，削弱 0，不成立 0", "3 条决定：保留 3，减仓 0，卖出 0，撤回买单 0",
        "留给收盘会 1 条，撤回买单 0 张，下单 0 张", "账本读不了：database is locked", "账本里没有这套模型配置的记录。",
        "2 只股票有新情况，数据缺口 1 处，计划条件成立 1 条", "预测 10 只，倒推 3 只，推演 1 只",
        "看了 7 只股票，提出事件 2 个，另有 1 个被退回",
        "还没有账本：每日流程还没有运行过。",
    ]
    script = tmp_path / "say.mjs"
    script.write_text(
        'globalThis.localStorage = { getItem: () => "en", setItem() {} };\n'
        f'const {{ fromServer }} = await import({json.dumps((SCRIPTS / "i18n.js").as_uri())});\n'
        f"console.log(JSON.stringify({json.dumps(sentences, ensure_ascii=False)}.map(fromServer)));\n", encoding="utf-8")
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    said = json.loads(result.stdout)
    assert [text for text in said if HAN.search(text)] == []
    assert said[8] == "10 views: bullish 8, neutral 2, bearish 0" and said[6] == "Stance: neutral, confidence 0.75"

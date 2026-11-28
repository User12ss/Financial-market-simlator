"""Company filings from the SEC's EDGAR system. Official, free, and read only.

Three kinds are read, each about one company:

    8-K            something the company had to report at once: an officer
                   leaving, results, a restatement, a bankruptcy
    Form 4         an officer or director traded the company's shares
    Schedule 13D   someone holds more than five per cent of it and may seek
                   to influence it

What the SEC asks of a program that reads EDGAR (checked against sec.gov on
2026-11-27): say who is asking, in the User-Agent, with an address it can
write to; and make no more than ten requests a second. The address is the
user's to choose (EDGAR_CONTACT). Without one nothing is asked. It is sent to
sec.gov and nowhere else, and is never written into an error or a file.

A company's own list of filings (data.sec.gov/submissions) also holds what
the company filed about OTHER companies: a Form 4 or a Schedule 13D in which
it is the holder. Those have to be opened to be told apart, and are left out.
Which ones were left out is kept in a small file beside the number the SEC
knows each ticker by, so that they are not opened again at every reading.

The price an insider traded at is on every Form 4. It is not kept: one seat
of the desk reads these filings and must see no price (docs/UPGRADE_PLAN.md,
11.8). The words of an 8-K are kept as written, which may state amounts.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Protocol
from xml.etree import ElementTree
from xml.parsers import expat

import httpx
from pydantic import BaseModel, Field

import config
from data.models import Filing
from data.providers import DataError

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
LIST_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{document}"

# The forms read, by what they tell. An amendment ("/A") is read like the form it amends.
KINDS: dict[str, str] = {
    "8-K": "event", "8-K/A": "event", "4": "insider", "4/A": "insider",
    "SCHEDULE 13D": "holder", "SCHEDULE 13D/A": "holder",
}
# How long the list of tickers is kept before it is fetched afresh.
TICKERS_KEPT_DAYS = 30
# No answer larger than this is taken in. The largest that is expected, the list of every ticker, is under a megabyte.
MAX_BYTES = 20_000_000
# How much of a page is gone through for its words. A real 8-K is some tens of thousands of characters.
PARSED_CHARS = 2_000_000

# The items of Form 8-K by number, with the SEC's own titles (sec.gov, Exchange Act Form 8-K, 2026-11-27).
ITEMS: dict[str, str] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety - Reporting of Shutdowns and Patterns of Violations",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation under an Off-Balance Sheet Arrangement of a Registrant",
    "2.04": "Triggering Events That Accelerate or Increase a Direct Financial Obligation or an Obligation under an "
            "Off-Balance Sheet Arrangement",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard; Transfer of Listing",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements or a Related Audit Report or Completed Interim Review",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain Officers; "
            "Compensatory Arrangements of Certain Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "5.04": "Temporary Suspension of Trading Under Registrant's Employee Benefit Plans",
    "5.05": "Amendments to the Registrant's Code of Ethics, or Waiver of a Provision of the Code of Ethics",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "6.01": "ABS Information and Computational Material",
    "6.02": "Change of Servicer or Trustee",
    "6.03": "Change in Credit Enhancement or Other External Support",
    "6.04": "Failure to Make a Required Distribution",
    "6.05": "Securities Act Updating Disclosure",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}
EXHIBITS = "9.01"

# What a line of a Form 4 did, by the form's own transaction code (sec.gov, Ownership Form Codes, 2026-11-27).
# `{n}` is the number of shares. A code not listed here is said by its letter.
_ACQUIRED = {
    "P": "bought {n} shares (open market or private purchase)",
    "A": "was granted {n} shares",
    "M": "acquired {n} shares by exercising or converting a derivative security",
    "C": "acquired {n} shares by converting a derivative security",
    "X": "acquired {n} shares by exercising a derivative security",
    "G": "received {n} shares as a gift",
    "W": "received {n} shares by will or inheritance",
}
_DISPOSED = {
    "S": "sold {n} shares (open market or private sale)",
    "F": "{n} shares withheld to pay an exercise price or tax",
    "D": "returned {n} shares to the company",
    "G": "gave {n} shares as a gift",
    "U": "tendered {n} shares in a change of control",
    "W": "passed on {n} shares by will or inheritance",
}


class NotListed(DataError):
    """The SEC's list of tickers does not hold this one.

    For the desk that is nearly always a fund: a fund is not a company with
    officers and holders of five per cent, and files none of these forms. It
    is told apart from a failure because nothing is missing that could have
    been read.
    """


class Unavailable(DataError):
    """EDGAR is not answering, or is turning this program away (too many requests, forbidden, a server error).

    The next request would meet the same, and asking on is neither useful nor
    polite: whoever is reading stops there, and tries again at the next reading.
    """


class FilingsRead(BaseModel):
    """One reading of a company's filings."""

    filings: list[Filing] = Field(default_factory=list, description="Those not held before, newest first")
    problems: list[str] = Field(default_factory=list, description="Filings that could not be opened or read this time")
    waiting: int = Field(default=0, description="Filings left unopened: the reading had opened its share, or stopped")
    unavailable: str = Field(default="", description="Why the reading stopped early, when EDGAR stopped answering")


@dataclass
class Budget:
    """How many documents a reading may still open. Shared by the tickers of one reading."""

    left: int


class FilingsProvider(Protocol):
    def filings(
        self, ticker: str, since: dict[str, date], known: set[str] | frozenset[str] = frozenset(),
        budget: Budget | None = None,
    ) -> FilingsRead:
        """The company's filings of each kind filed from `since[kind]` on that are not among `known`."""


# ── Reading a page ───────────────────────────────────────────────────────────

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_UNSEEN = {"head", "script", "style", "ix:header"}
_BREAKS = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
_CELLS = {"td", "th"}


class _Text(HTMLParser):
    """The words of a page that someone reading it would see: nothing hidden, no script, no style sheet."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.unseen = 0                       # how deep inside something not shown

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            self.handle_startendtag(tag, attrs)
            return
        style = (dict(attrs).get("style") or "").replace(" ", "").lower()
        if self.unseen or tag in _UNSEEN or "display:none" in style:
            self.unseen += 1
        elif tag in _BREAKS:
            self.parts.append("\n")
        elif tag in _CELLS:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if not self.unseen and tag in _BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if self.unseen:
            self.unseen -= 1
        elif tag in _BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.unseen:
            self.parts.append(data)


def document_text(page: str) -> str:
    """A filing's page as plain text, a line for each paragraph or row of a table."""
    reader = _Text()
    reader.feed(page)
    reader.close()
    lines = (" ".join(line.replace("\xa0", " ").split()) for line in "".join(reader.parts).splitlines())
    return "\n".join(line for line in lines if line)


_ITEM = re.compile(r"^Item\s+(\d\.\d\d)\b", re.IGNORECASE | re.MULTILINE)
_SIGNED = re.compile(r"^SIGNATURES?\b", re.MULTILINE)


def _cut(text: str, limit: int) -> str:
    """No longer than `limit`, cut at a word, saying so when it was cut."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit + 1].rsplit(None, 1)[0].rstrip(" ,;:") + " ..."


def event_excerpt(text: str, limit: int) -> str:
    """What an 8-K says: from its first item (its cover page is the same on every one) to its list of exhibits
    or its signatures, whichever comes first."""
    first = _ITEM.search(text)
    if first is None:
        return ""
    ends = [found.start() for found in _ITEM.finditer(text, first.end())
            if found.group(1) == EXHIBITS and first.group(1) != EXHIBITS]
    signed = _SIGNED.search(text, first.end())
    if signed:
        ends.append(signed.start())
    return _cut(text[first.start():min(ends)] if ends else text[first.start():], limit)


def event_page_excerpt(page: bytes, limit: int) -> str:
    """What an 8-K's page says, from its first item. Only the start of a very long page is gone through: what
    is kept of it is far shorter, and an item that begins beyond that is not quoted."""
    return event_excerpt(document_text(page.decode("utf-8", "replace")[:PARSED_CHARS]), limit)


def event_title(items: list[str], amended: bool = False) -> str:
    """An 8-K in one line: its items by the SEC's own titles. Its exhibits are named only when it has nothing else."""
    form = "8-K/A (an amendment)" if amended else "8-K"
    said = [item for item in items if item != EXHIBITS] or items
    names = [ITEMS.get(item, f"Item {item}") for item in said]
    return f"{form}: {'; '.join(names)}" if names else form


# ── Reading a form ───────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _Declares(Exception):
    """A form declares a document type or entities of its own."""


def _tree(text: bytes | str, root: str, what: str) -> ElementTree.Element:
    """A form as a tree. One that declares a document type is refused before it is read.

    The SEC's forms never carry one. A document that does can define entities
    that expand as they are read, a few lines into gigabytes, and the standard
    reader would expand them. So the form is first gone through by a reader
    that builds nothing and stops at the first such declaration.
    """
    def refuse(*args):
        raise _Declares()

    checker = expat.ParserCreate()
    checker.StartDoctypeDeclHandler = refuse
    checker.EntityDeclHandler = refuse
    try:
        checker.Parse(text, True)
    except _Declares:
        raise DataError(f"is not a readable {what}: it declares a document type, which these forms never do") from None
    except (expat.ExpatError, ValueError, TypeError) as e:
        raise DataError(f"is not a readable {what}: {e}") from e
    try:
        tree = ElementTree.fromstring(text)
    except (ElementTree.ParseError, ValueError) as e:
        raise DataError(f"is not a readable {what}: {e}") from e
    if _local(tree.tag) != root:
        raise DataError(f"is not a readable {what}: it is a <{_local(tree.tag)}>, not a <{root}>")
    return tree


def _first(node: ElementTree.Element, name: str) -> str:
    """The text of the first element of that name under a node, whatever lies between. Empty when there is none."""
    for found in node.iter():
        if _local(found.tag) == name:
            value = found.find("{*}value") if found.find("{*}value") is not None else found.find("value")
            return " ".join(((value if value is not None else found).text or "").split())
    return ""


def _all(node: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [found for found in node.iter() if _local(found.tag) == name]


def _yes(text: str) -> bool:
    return text.strip().lower() in ("1", "true", "y", "yes")


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _count(shares: float) -> str:
    return f"{shares:,.0f}" if float(shares).is_integer() else f"{shares:,.4f}".rstrip("0").rstrip(".")


def _names(names: list[str]) -> str:
    if len(names) > 3:
        return f"{', '.join(names[:3])} and {len(names) - 3} more"
    return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)


def insider_fields(page: bytes | str, cik: int) -> dict | None:
    """What a Form 4 says, without its prices. None when it is about another company's shares."""
    form = _tree(page, "ownershipDocument", "ownership form")
    issuer = _number(_first(form, "issuerCik"))
    if issuer is None:
        raise DataError("is not a readable ownership form: it names no issuer")
    if int(issuer) != cik:
        return None
    owners = _all(form, "reportingOwner")
    owner = _names([_first(one, "rptOwnerName") for one in owners if _first(one, "rptOwnerName")]) or "An insider"
    roles: list[str] = []
    for one in owners:
        if _yes(_first(one, "isDirector")):
            roles.append("Director")
        if _yes(_first(one, "isOfficer")):
            roles.append(_first(one, "officerTitle") or "Officer")
        if _yes(_first(one, "isTenPercentOwner")):
            roles.append("10% owner")
    trades = []
    for table, derivative in (("nonDerivativeTransaction", False), ("derivativeTransaction", True)):
        for line in _all(form, table):
            shares = _number(_first(line, "transactionShares"))
            if shares is None:
                continue
            trades.append({
                "code": _first(line, "transactionCode").upper(),
                "acquired": _first(line, "transactionAcquiredDisposedCode").upper() == "A",
                "shares": shares, "security": _first(line, "securityTitle"),
                "held_after": _number(_first(line, "sharesOwnedFollowingTransaction")), "derivative": derivative,
            })
    return {
        "owner": owner, "roles": list(dict.fromkeys(roles)), "planned": _yes(_first(form, "aff10b5One")),
        "report_date": _first(form, "periodOfReport"), "trades": trades,
    }


def insider_title(fields: dict, amended: bool = False) -> str:
    """A Form 4 in one line: who, in what office, did what with how many shares, and whether it was planned."""
    who = fields["owner"] + (f" ({', '.join(fields['roles'])})" if fields["roles"] else "")
    form = "Form 4/A (an amendment)" if amended else "Form 4"
    shares = [trade for trade in fields["trades"] if not trade["derivative"]]
    if not shares:
        return (f"{form}: {who} reported a change in derivative holdings and no trade in shares." if fields["trades"]
                else f"{form}: {who} reported no trade.")
    totals: dict[tuple[str, bool], float] = {}
    for trade in shares:                                         # in the order filed, the same kind of line added up
        totals[(trade["code"], trade["acquired"])] = totals.get((trade["code"], trade["acquired"]), 0.0) + trade["shares"]
    parts = []
    for (code, acquired), total in totals.items():
        wording = (_ACQUIRED if acquired else _DISPOSED).get(code)
        parts.append(wording.format(n=_count(total)) if wording else
                     f"{'acquired' if acquired else 'disposed of'} {_count(total)} shares (transaction code {code or '?'})")
    planned = " under a Rule 10b5-1 trading plan" if fields["planned"] else ""
    held = shares[-1]["held_after"]
    after = f"; {_count(held)} reported held after" if held is not None else ""
    return f"{form}: {who} {'; '.join(parts)}{planned}{after}."


def holder_fields(page: bytes | str, cik: int) -> dict | None:
    """What a Schedule 13D says of a holding. None when it is about another company's shares."""
    form = _tree(page, "edgarSubmission", "Schedule 13D")
    issuer = _number(_first(form, "issuerCIK"))
    if issuer is None:
        raise DataError("is not a readable Schedule 13D: it names no issuer")
    if int(issuer) != cik:
        return None
    persons = list(dict.fromkeys(_first(one, "reportingPersonName") for one in _all(form, "reportingPersonInfo")))
    percents = [value for value in (_number(node.text or "") for node in _all(form, "percentOfClass")) if value is not None]
    event = _first(form, "dateOfEvent")
    try:
        event = datetime.strptime(event, "%m/%d/%Y").date().isoformat()
    except ValueError:
        pass                                                     # kept as the form wrote it
    amendment = _number(_first(form, "amendmentNo"))
    return {
        "persons": [person for person in persons if person], "percent": max(percents) if percents else None,
        "amendment": int(amendment) if amendment is not None else None, "class": _first(form, "securitiesClassTitle"),
        "event_date": event, "issuer": _first(form, "issuerName"), "purpose": _first(form, "transactionPurpose"),
    }


def holder_title(fields: dict, company: str) -> str:
    form = "Schedule 13D" if fields["amendment"] is None else f"Schedule 13D/A (amendment {fields['amendment']})"
    who = _names(fields["persons"]) or "A holder"
    verb = "report" if len(fields["persons"]) > 1 else "reports"
    share = f"holding {fields['percent']:g}% of" if fields["percent"] is not None else "a holding in"
    return f"{form}: {who} {verb} {share} {(fields['issuer'] or company).rstrip('.')}."


# ── The provider ─────────────────────────────────────────────────────────────

class EdgarProvider:
    """A company's recent filings, read from sec.gov with the contact the SEC asks every program to give."""

    def __init__(
        self,
        contact: str,
        client: httpx.Client | None = None,
        memory: str | Path | None = None,
        pause: float | None = None,
        excerpt_chars: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self._max_bytes = max_bytes
        words = contact.split()
        address = any("@" in word and "." in word for word in words)
        name = any("@" not in word for word in words)
        if not (address and name):
            raise ValueError(
                "EDGAR_CONTACT must be a name and an address the SEC can write to, such as "
                "'research-desk someone@example.com'. Without it no filing is read."
            )
        self._headers = {"User-Agent": " ".join(words), "Accept-Encoding": "gzip, deflate"}
        self._http = client or httpx.Client(timeout=30, follow_redirects=True)
        self._memory = Path(memory or config.EDGAR_MEMORY_PATH)
        self._pause = config.EDGAR_PAUSE_SECONDS if pause is None else pause
        self._excerpt = config.EDGAR_EXCERPT_CHARS if excerpt_chars is None else excerpt_chars
        self._sleep, self._today = sleep, today
        self._asked = 0

    # What is kept between runs: each ticker's number, and the filings found to be about another company.

    def _kept(self) -> dict:
        try:
            kept = json.loads(self._memory.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            kept = {}
        return {"fetched": kept.get("fetched", ""), "ciks": kept.get("ciks", {}), "left_out": kept.get("left_out", {})}

    def _keep(self, kept: dict) -> None:
        """Written to one side and moved into place, so that another process reading it (a second desk's watch,
        a close meeting) never finds half a file. Two writing at once can still lose one's note of a filing to
        leave out: that filing is opened once more at a later reading, and noted again."""
        self._memory.parent.mkdir(parents=True, exist_ok=True)
        aside = self._memory.with_name(f"{self._memory.name}.{os.getpid()}.tmp")
        aside.write_text(json.dumps(kept, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(aside, self._memory)

    def _get(self, url: str, what: str) -> bytes:
        """One request, and what it answered. The message of a failure names what was asked for, never who asked."""
        if self._asked:
            self._sleep(self._pause)
        self._asked += 1
        try:
            with self._http.stream("GET", url, headers=self._headers) as response:
                status = response.status_code
                if status in (403, 429) or status >= 500:   # turned away, or not working: the same for the next request
                    raise Unavailable(f"EDGAR answered {status} for {what}.")
                if status != 200:
                    raise DataError(f"EDGAR answered {status} for {what}.")
                taken = bytearray()
                for piece in response.iter_bytes():
                    taken += piece
                    if len(taken) > self._max_bytes:        # not read to its end: nothing here expects one this large
                        raise DataError(f"EDGAR's answer for {what} is larger than {self._max_bytes:,} bytes and was not read.")
                return bytes(taken)
        except httpx.HTTPError as e:
            raise Unavailable(f"EDGAR could not be reached for {what}: {type(e).__name__}: {e}") from e

    def _cik(self, ticker: str) -> int:
        kept = self._kept()
        names = [ticker.upper(), ticker.upper().replace(".", "-")]
        fresh = False
        try:
            fresh = (self._today() - date.fromisoformat(kept["fetched"])).days <= TICKERS_KEPT_DAYS
        except ValueError:
            pass
        if not fresh or not any(name in kept["ciks"] for name in names):
            try:
                table = json.loads(self._get(TICKERS_URL, "the list of tickers"))
                kept["ciks"] = {str(row["ticker"]).upper(): int(row["cik_str"]) for row in table.values()}
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                raise DataError(f"EDGAR's list of tickers is not laid out as expected: {type(e).__name__}: {e}") from e
            kept["fetched"] = self._today().isoformat()
            self._keep(kept)
        for name in names:
            if name in kept["ciks"]:
                return kept["ciks"][name]
        raise NotListed(f"{ticker} is not in the SEC's list of tickers, so its filings cannot be looked up.")

    def _rows(self, ticker: str, cik: int) -> tuple[str, list[dict]]:
        answer = self._get(LIST_URL.format(cik=cik), f"the list of {ticker}'s filings")
        try:
            data = json.loads(answer)
            recent = data["filings"]["recent"]
            wanted = ("accessionNumber", "form", "filingDate", "acceptanceDateTime", "items", "primaryDocument", "reportDate")
            rows = [dict(zip(wanted, values)) for values in zip(*(recent[key] for key in wanted))]
            for row in rows:
                row["filed"] = date.fromisoformat(row["filingDate"])
                accepted = datetime.fromisoformat(row["acceptanceDateTime"].replace("Z", "+00:00"))
                if accepted.tzinfo is None:
                    # The SEC writes these as UTC. One without a zone is not guessed at: a few hours either
                    # way would put a filing on the wrong side of a meeting's cut-off.
                    raise ValueError(f"the time {row['acceptanceDateTime']!r} carries no zone")
                row["accepted"] = accepted.astimezone(timezone.utc)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise DataError(f"{ticker}: EDGAR's list of filings is not laid out as expected: {type(e).__name__}: {e}") from e
        return str(data.get("name") or ticker), rows

    def filings(
        self, ticker: str, since: dict[str, date], known: set[str] | frozenset[str] = frozenset(),
        budget: Budget | None = None,
    ) -> FilingsRead:
        cik = self._cik(ticker)
        company, rows = self._rows(ticker, cik)
        kept = self._kept()
        left_out = set(kept["left_out"].get(ticker.upper(), []))
        read = FilingsRead()
        newly_left_out = []
        wanted = [
            row for row in rows
            if row["form"] in KINDS and row["filed"] >= since[KINDS[row["form"]]]
            and row["accessionNumber"] not in known and row["accessionNumber"] not in left_out
        ]
        for row in sorted(wanted, key=lambda row: row["accepted"], reverse=True):
            if read.unavailable or (budget is not None and budget.left <= 0):
                read.waiting += 1
                continue
            if budget is not None:
                budget.left -= 1
            try:
                filing = self._filing(cik, company, row)
            except Unavailable as e:
                # EDGAR has stopped answering: nothing more is asked of it at this reading. What was read stands.
                read.problems.append(f"{row['form']} {row['accessionNumber']}: {e}")
                read.unavailable = str(e)
                continue
            except DataError as e:
                read.problems.append(f"{row['form']} {row['accessionNumber']}: {_named(str(e), row)}")
                continue
            if filing is None:
                newly_left_out.append(row["accessionNumber"])
            else:
                read.filings.append(filing)
        if newly_left_out:
            kept = self._kept()
            kept["left_out"][ticker.upper()] = sorted(left_out | set(newly_left_out))
            self._keep(kept)
        return read

    def _filing(self, cik: int, company: str, row: dict) -> Filing | None:
        """One filing, opened and read. None when it turns out to be about another company."""
        accession, form, kind = row["accessionNumber"], row["form"], KINDS[row["form"]]
        shown = str(row["primaryDocument"])
        raw = shown.rsplit("/", 1)[-1]                           # the form itself, not the page drawn from it
        folder = accession.replace("-", "")
        link = DOCUMENT_URL.format(cik=cik, folder=folder, document=shown)
        page = self._get(DOCUMENT_URL.format(cik=cik, folder=folder, document=raw), raw)
        amended = form.endswith("/A")
        if kind == "event":
            items = [item.strip() for item in str(row["items"]).split(",") if item.strip()]
            return Filing(
                accession=accession, form=form, kind=kind, accepted=row["accepted"], link=link,
                title=event_title(items, amended), excerpt=event_page_excerpt(page, self._excerpt),
                detail={"items": items, "report_date": row["reportDate"]},
            )
        if kind == "insider":
            fields = insider_fields(page, cik)
            if fields is None:
                return None
            return Filing(accession=accession, form=form, kind=kind, accepted=row["accepted"], link=link,
                          title=insider_title(fields, amended), detail=fields)
        fields = holder_fields(page, cik)
        if fields is None:
            return None
        purpose, issuer = fields.pop("purpose"), fields.pop("issuer")
        return Filing(
            accession=accession, form=form, kind=kind, accepted=row["accepted"], link=link,
            title=holder_title({**fields, "issuer": issuer}, company), excerpt=_cut(purpose, self._excerpt), detail=fields,
        )


def _named(message: str, row: dict) -> str:
    """A failure to read a form, said with the name of the document it was."""
    document = str(row["primaryDocument"]).rsplit("/", 1)[-1]
    return f"{document} {message}" if message.startswith("is not") else message

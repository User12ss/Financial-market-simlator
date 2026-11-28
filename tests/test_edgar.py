"""Company filings from the SEC's EDGAR: what is asked for, what is kept of an answer, and what is left out.

Nothing here reaches the network. The site is a stand-in that hands back files kept in tests/fixtures/edgar:
made-up companies and people, laid out exactly as real answers were on 2026-11-27 (the list of a company's
filings, an 8-K, three Forms 4, two Schedules 13D).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from data.edgar import (
    Budget, EdgarProvider, NotListed, Unavailable, document_text, event_excerpt, event_page_excerpt, event_title,
    holder_fields, holder_title, insider_fields, insider_title,
)
from data.providers import DataError

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"
CONTACT = "research-desk desk@example.test"
BASE = "https://www.sec.gov/Archives/edgar/data/999001"
LIST = "https://data.sec.gov/submissions/CIK0000999001.json"
TICKERS = "https://www.sec.gov/files/company_tickers.json"
SALE, RETIRES, OTHERS_FORM4 = f"{BASE}/000099900126000050/form4.xml", f"{BASE}/000099900126000049/acme-20261123.htm", f"{BASE}/000077700026000011/wk-form4_1.xml"
HOLDER, OWN_13D = f"{BASE}/000088800026000007/primary_doc.xml", f"{BASE}/000099900126000040/primary_doc.xml"
RESULTS, GRANT = f"{BASE}/000099900126000038/acme-20261102.htm", f"{BASE}/000099900126000020/form4.xml"
PAGES = {
    TICKERS: "company_tickers.json", LIST: "submissions_ACME.json", SALE: "form4_sale.xml", RETIRES: "8k_502.htm",
    OTHERS_FORM4: "form4_other_issuer.xml", HOLDER: "13d_about_acme.xml", OWN_13D: "13d_by_acme.xml",
    RESULTS: "8k_202.htm", GRANT: "form4_grant.xml",
}
# As of 2026-11-27: events and large holders 90 days back, insiders' trades 180.
SINCE = {"event": date(2026, 8, 29), "holder": date(2026, 8, 29), "insider": date(2026, 5, 31)}
KEPT = ["0000999001-26-000050", "0000999001-26-000049", "0000888000-26-000007", "0000999001-26-000038", "0000999001-26-000020"]


class Site:
    """sec.gov, as far as these tests go: it notes every request and hands back a kept file, or a failure."""

    def __init__(self) -> None:
        self.asked: list[httpx.Request] = []
        self.broken: dict[str, int] = {}
        self.pages = dict(PAGES)
        self.bodies: dict[str, bytes] = {}                       # answers written out here, not kept in a file

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        url = str(request.url)
        if url in self.broken:
            return httpx.Response(self.broken[url], text="Request Rate Threshold Exceeded")
        if url in self.bodies:
            return httpx.Response(200, content=self.bodies[url])
        name = self.pages.get(url)
        if name is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=(FIXTURES / name).read_bytes())

    def urls(self) -> list[str]:
        return [str(request.url) for request in self.asked]


def reader(tmp_path, site: Site | None = None, naps: list | None = None, **more) -> tuple[EdgarProvider, Site]:
    site = site or Site()
    naps = naps if naps is not None else []
    more.setdefault("today", lambda: date(2026, 11, 27))
    provider = EdgarProvider(
        CONTACT, client=httpx.Client(transport=httpx.MockTransport(site)), memory=tmp_path / "edgar.json",
        pause=0.2, sleep=naps.append, **more,
    )
    return provider, site


def test_every_request_says_who_is_asking_and_nothing_is_asked_without_saying():
    for contact in ("", "   ", "research-desk", "desk@example.test", "research desk"):
        with pytest.raises(ValueError, match="EDGAR_CONTACT"):
            EdgarProvider(contact)


def test_requests_carry_the_contact_and_are_spaced_apart(tmp_path):
    naps: list[float] = []
    provider, site = reader(tmp_path, naps=naps)
    provider.filings("ACME", SINCE)
    assert len(site.asked) == 9                                   # the list of tickers, the company's list, seven documents
    for request in site.asked:
        assert request.headers["user-agent"] == CONTACT
        assert request.headers["accept-encoding"] == "gzip, deflate"
        assert request.method == "GET"
    # A pause before every request but the first: well under the ten a second the SEC allows.
    assert naps == [0.2] * 8


def test_a_ticker_is_found_by_its_number_and_the_list_of_them_is_kept(tmp_path):
    provider, site = reader(tmp_path)
    provider.filings("ACME", SINCE)
    assert site.urls()[:2] == [TICKERS, LIST]
    # The next process does not download the list of every company again: it was kept.
    again, later = reader(tmp_path)
    again.filings("ACME", SINCE, known=set(KEPT))
    assert TICKERS not in later.urls() and LIST in later.urls()
    # A share class is written with a dot here and a dash there.
    dotted, third = reader(tmp_path)
    third.pages["https://data.sec.gov/submissions/CIK0000999003.json"] = "submissions_ACME.json"
    dotted.filings("BRK.B", SINCE, budget=Budget(0))
    assert "https://data.sec.gov/submissions/CIK0000999003.json" in third.urls()
    # A ticker the SEC does not list is said to be missing, after one fresh look at the list.
    # (Said as its own kind of failure: for the desk it is a fund, which files none of these forms.)
    with pytest.raises(NotListed, match="NOPE is not in the SEC's list of tickers"):
        dotted.filings("NOPE", SINCE)
    assert third.urls().count(TICKERS) == 1
    # A list kept for longer than a month is fetched afresh.
    old, fourth = reader(tmp_path, today=lambda: date(2027, 1, 15))
    old.filings("ACME", SINCE, budget=Budget(0))
    assert fourth.urls()[0] == TICKERS


def test_what_is_kept_are_the_three_kinds_inside_their_windows_and_about_this_company(tmp_path):
    provider, site = reader(tmp_path)
    read = provider.filings("ACME", SINCE)
    assert [filing.accession for filing in read.filings] == KEPT                  # newest first
    assert [(filing.form, filing.kind) for filing in read.filings] == [
        ("4", "insider"), ("8-K", "event"), ("SCHEDULE 13D", "holder"), ("8-K", "event"), ("4", "insider")]
    assert (read.problems, read.waiting) == ([], 0)
    # Left out: a Form 4 and a Schedule 13D in which the company is the holder of someone else's shares (they
    # have to be opened to tell), a quarterly report, a notice of sale, and what lies outside its window: an
    # 8-K of July, a Form 4 of March. The last four are never opened.
    assert set(site.urls()) == {TICKERS, LIST, SALE, RETIRES, OTHERS_FORM4, HOLDER, OWN_13D, RESULTS, GRANT}
    # The moment the SEC accepted a filing is its date here, as UTC.
    assert read.filings[1].accepted == datetime(2026, 11, 23, 21, 22, 30, tzinfo=timezone.utc)
    assert read.filings[1].link == RETIRES
    assert read.filings[0].link == f"{BASE}/000099900126000050/xslF345X06/form4.xml"      # the page a person reads


def test_an_8k_is_named_by_its_official_items_and_quotes_what_it_says(tmp_path):
    provider, _ = reader(tmp_path)
    by = {filing.accession: filing for filing in provider.filings("ACME", SINCE).filings}
    retires, results = by["0000999001-26-000049"], by["0000999001-26-000038"]
    assert retires.title == ("8-K: Departure of Directors or Certain Officers; Election of Directors; Appointment of "
                             "Certain Officers; Compensatory Arrangements of Certain Officers")
    assert retires.detail == {"items": ["5.02", "9.01"], "report_date": "2026-11-23"}
    # What it says begins at its first item: the cover page before it is the same on every 8-K.
    assert retires.excerpt.startswith("Item 5.02 Departure of Directors or Certain Officers;")
    assert ("On November 23, 2026, Alex B. Sample, Chief Financial Officer of Acme Robotics Inc. (the “Company”), "
            "notified the Company of his decision to resign, effective December 31, 2026.") in retires.excerpt
    assert "Dana C. Proxy, age 54, will begin serving as the Company’s Chief Financial Officer." in retires.excerpt
    # Nothing a reader of the page would not see: hidden data, a hidden note, a script, a style sheet.
    for unseen in ("HIDDEN", "A SCRIPT", "color: red", "0000999001", "UNITED STATES"):
        assert unseen not in retires.excerpt, unseen
    # The signature block says nothing: the excerpt stops before it.
    assert "SIGNATURES" not in retires.excerpt and "Pursuant to the requirements" not in retires.excerpt
    assert results.title == "8-K: Results of Operations and Financial Condition"
    assert results.excerpt.startswith("Item 2.02\nResults of Operations and Financial Condition.\nOn November 2, 2026,")

    # It is cut to a length, at a word, and says that it was cut.
    short, _ = reader(tmp_path, excerpt_chars=120)
    cut = {filing.accession: filing for filing in short.filings("ACME", SINCE).filings}["0000999001-26-000049"].excerpt
    assert len(cut) <= 124 and cut.endswith(" ...") and cut.startswith("Item 5.02 Departure of Directors")

    # With no list of exhibits to stop at, it stops at the signatures; with neither, at its length.
    signed = "FORM 8-K\nItem 8.01 Other Events.\nThe Company announced a thing.\nSIGNATURES\nPursuant to the requirements of the Act."
    assert event_excerpt(signed, 500) == "Item 8.01 Other Events.\nThe Company announced a thing."
    assert event_excerpt("Item 9.01 Financial Statements and Exhibits.\n(d) Exhibits.\nSIGNATURE\nBy: someone", 500) == (
        "Item 9.01 Financial Statements and Exhibits.\n(d) Exhibits.")
    assert event_excerpt("A cover page and no item at all.", 500) == ""

    # Exhibits are named only when a filing has nothing else; an item this list does not know keeps its number.
    assert event_title(["9.01"]) == "8-K: Financial Statements and Exhibits"
    assert event_title(["5.99", "1.03"]) == "8-K: Item 5.99; Bankruptcy or Receivership"
    assert event_title([]) == "8-K"
    assert event_title(["4.02"], amended=True) == ("8-K/A (an amendment): Non-Reliance on Previously Issued Financial "
                                                   "Statements or a Related Audit Report or Completed Interim Review")


def test_an_insiders_filing_says_who_did_what_and_never_at_what_price(tmp_path):
    provider, _ = reader(tmp_path)
    by = {filing.accession: filing for filing in provider.filings("ACME", SINCE).filings}
    sale, grant = by["0000999001-26-000050"], by["0000999001-26-000020"]
    assert sale.title == ("Form 4: Example Jane (SVP, General Counsel) sold 2,398 shares (open market or private sale) "
                          "under a Rule 10b5-1 trading plan; 22,802 reported held after.")
    assert sale.detail == {
        "owner": "Example Jane", "roles": ["SVP, General Counsel"], "planned": True, "report_date": "2026-11-24",
        "trades": [
            {"code": "S", "acquired": False, "shares": 2000.0, "security": "Common Stock", "held_after": 23200.0, "derivative": False},
            {"code": "S", "acquired": False, "shares": 398.0, "security": "Common Stock", "held_after": 22802.0, "derivative": False},
        ],
    }
    assert sale.excerpt == ""
    # The form states a price for each line, and its footnotes may too. Neither is kept: one seat of the desk
    # is to read these and must see no price.
    def names(value) -> list[str]:
        """Every field name anywhere in what was kept."""
        if isinstance(value, dict):
            return [str(key) for key in value] + [name for inner in value.values() for name in names(inner)]
        return [name for inner in value for name in names(inner)] if isinstance(value, list) else []

    for filing in (sale, grant):
        kept = filing.model_dump_json()
        for figure in ("347", "348", "301.25"):
            assert figure not in kept, (filing.accession, figure)
        assert not [name for name in names(filing.model_dump()) if "price" in name.lower()]

    # Several kinds of line in one filing are each said, in the order filed. A line of the derivatives table
    # (the units that were converted) is kept and not said twice.
    assert grant.title == ("Form 4: SAMPLE ROBERT A (Director, Chief Executive Officer) acquired 10,000 shares by exercising "
                           "or converting a derivative security; 4,321.5 shares withheld to pay an exercise price or tax; "
                           "145,678.5 reported held after.")
    assert grant.detail["planned"] is False and grant.detail["roles"] == ["Director", "Chief Executive Officer"]
    assert [(trade["code"], trade["derivative"]) for trade in grant.detail["trades"]] == [("M", False), ("F", False), ("M", True)]


def test_a_large_holders_filing_is_kept_only_when_it_is_about_the_company(tmp_path):
    provider, _ = reader(tmp_path)
    read = provider.filings("ACME", SINCE)
    holder = {filing.accession: filing for filing in read.filings}["0000888000-26-000007"]
    assert holder.title == ("Schedule 13D: Example Capital LP and Example Capital GP LLC report holding 6.2% of "
                            "Acme Robotics Inc.")
    assert holder.detail == {
        "persons": ["Example Capital LP", "Example Capital GP LLC"], "percent": 6.2, "amendment": None,
        "class": "Common Stock, par value $0.01 per share", "event_date": "2026-11-16",
    }
    assert holder.excerpt.startswith("The Reporting Persons acquired the shares because they believe")
    # The company's own filing about its stake in another company is in the same list. It is not about the company.
    assert "0000999001-26-000040" not in {filing.accession for filing in read.filings}


def test_what_is_already_held_or_was_found_to_be_about_another_company_is_not_opened_again(tmp_path):
    provider, site = reader(tmp_path)
    provider.filings("ACME", SINCE)
    again, later = reader(tmp_path)
    read = again.filings("ACME", SINCE, known=set(KEPT))
    assert (read.filings, read.problems, read.waiting) == ([], [], 0)
    assert later.urls() == [LIST]                                 # the company's list, and not one document
    # Without being told what is held, it opens the five kept ones again, and still not the two left out.
    fresh, third = reader(tmp_path)
    assert [filing.accession for filing in fresh.filings("ACME", SINCE).filings] == KEPT
    assert OTHERS_FORM4 not in third.urls() and OWN_13D not in third.urls()


def test_a_document_that_cannot_be_read_costs_that_filing_and_no_other(tmp_path):
    provider, site = reader(tmp_path)
    site.broken[RETIRES] = 404                                    # that one document is not there
    site.pages[GRANT] = "8k_202.htm"                              # not the form it should be
    read = provider.filings("ACME", SINCE)
    assert [filing.accession for filing in read.filings] == [KEPT[0], KEPT[2], KEPT[3]]
    assert len(read.problems) == 2 and read.unavailable == ""
    assert read.problems[0] == "8-K 0000999001-26-000049: EDGAR answered 404 for acme-20261123.htm."
    assert read.problems[1].startswith("4 0000999001-26-000020: form4.xml is not a readable ownership form")
    # Neither is taken for something to leave out: the next reading tries them again.
    site.broken.clear()
    site.pages[GRANT] = "form4_grant.xml"
    again, _ = reader(tmp_path, site=site)
    retry = again.filings("ACME", SINCE, known={KEPT[0], KEPT[2], KEPT[3]})
    assert ([filing.accession for filing in retry.filings], retry.problems) == ([KEPT[1], KEPT[4]], [])


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_once_edgar_turns_the_reader_away_nothing_more_is_asked_at_that_reading(tmp_path, status):
    provider, site = reader(tmp_path)
    site.broken[RETIRES] = status                                 # too many requests, forbidden, or the site is down
    read = provider.filings("ACME", SINCE)
    assert site.urls() == [TICKERS, LIST, SALE, RETIRES], "the next request would only meet the same"
    assert [filing.accession for filing in read.filings] == [KEPT[0]], "what had been read stands"
    assert read.problems == [f"8-K 0000999001-26-000049: EDGAR answered {status} for acme-20261123.htm."]
    assert read.unavailable == f"EDGAR answered {status} for acme-20261123.htm."
    assert read.waiting == 5
    # Turned away at the company's list, the whole reading of it fails, in the same way.
    site.broken[LIST] = status
    with pytest.raises(Unavailable, match=f"EDGAR answered {status} for the list of ACME's filings"):
        provider.filings("ACME", SINCE)
    # A list that is simply not there is a failure of that name, and no reason to stop asking about others.
    site.broken[LIST] = 404
    with pytest.raises(DataError) as missing:
        provider.filings("ACME", SINCE)
    assert not isinstance(missing.value, Unavailable)


def test_one_reading_opens_no_more_documents_than_it_is_allowed(tmp_path):
    provider, site = reader(tmp_path)
    budget = Budget(2)
    read = provider.filings("ACME", SINCE, budget=budget)
    assert [filing.accession for filing in read.filings] == KEPT[:2]              # the newest come first
    assert (read.waiting, budget.left, read.problems) == (5, 0, [])
    assert site.urls() == [TICKERS, LIST, SALE, RETIRES]
    # The next reading goes on from there.
    rest = provider.filings("ACME", SINCE, known=set(KEPT[:2]))
    assert [filing.accession for filing in rest.filings] == KEPT[2:]


def test_a_list_that_cannot_be_read_fails_the_reading_and_the_contact_is_never_in_the_message(tmp_path):
    for status in (403, 429, 500):
        provider, site = reader(tmp_path)
        site.broken[LIST] = status
        with pytest.raises(DataError) as failure:
            provider.filings("ACME", SINCE)
        assert f"EDGAR answered {status}" in str(failure.value) and "ACME" in str(failure.value)
        assert CONTACT not in str(failure.value) and "example.test" not in str(failure.value)

    def down(request):
        raise httpx.ConnectError("no route to host")

    provider = EdgarProvider(CONTACT, client=httpx.Client(transport=httpx.MockTransport(down)), memory=tmp_path / "none.json",
                             pause=0, sleep=lambda seconds: None)
    with pytest.raises(Unavailable, match="EDGAR could not be reached") as failure:
        provider.filings("ACME", SINCE)
    assert "example.test" not in str(failure.value)
    # An answer that is not what the SEC documents is a failure too, not an empty list.
    provider, site = reader(tmp_path)
    site.pages[LIST] = "company_tickers.json"
    with pytest.raises(DataError, match="ACME: EDGAR's list of filings is not laid out as expected"):
        provider.filings("ACME", SINCE)


BOMB = ('<?xml version="1.0"?><!DOCTYPE {root} [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]><{root}><issuer><issuerCik>0000999001</issuerCik>'
        "<issuerCIK>0000999001</issuerCIK></issuer><reportingOwner><reportingOwnerId><rptOwnerName>&c;</rptOwnerName>"
        "</reportingOwnerId></reportingOwner></{root}>")


def test_a_form_that_declares_its_own_entities_is_refused_before_it_is_read():
    # These forms never carry a document type. One that does could be made to swell to any size as it is read
    # (a few lines that expand into gigabytes), so it is turned away unread, in whatever encoding it comes.
    for read, root, what in ((insider_fields, "ownershipDocument", "ownership form"), (holder_fields, "edgarSubmission", "Schedule 13D")):
        page = BOMB.format(root=root)
        for raw in (page, page.encode(), page.replace('<?xml version="1.0"?>', '<?xml version="1.0" encoding="UTF-16"?>').encode("utf-16")):
            with pytest.raises(DataError, match=f"is not a readable {what}: it declares a document type"):
                read(raw, 999001)
    # A form that is plainly not XML is said to be unreadable, not passed over.
    with pytest.raises(DataError, match="is not a readable ownership form"):
        insider_fields(b"\x00\x01 not a form", 999001)


def test_a_form_with_little_in_it_is_still_said_plainly():
    quiet = ("<ownershipDocument><issuer><issuerCik>999001</issuerCik></issuer><reportingOwner><reportingOwnerId>"
             "<rptOwnerName>Example Jane</rptOwnerName></reportingOwnerId></reportingOwner><derivativeTable><derivativeTransaction>"
             "<transactionAmounts><transactionShares><value>100</value></transactionShares></transactionAmounts>"
             "</derivativeTransaction></derivativeTable></ownershipDocument>")
    fields = insider_fields(quiet, 999001)
    assert insider_title(fields) == "Form 4: Example Jane reported a change in derivative holdings and no trade in shares."
    assert insider_title(insider_fields("<ownershipDocument><issuerCik>999001</issuerCik></ownershipDocument>", 999001)) == (
        "Form 4: An insider reported no trade.")
    with pytest.raises(DataError, match="it names no issuer"):
        insider_fields("<ownershipDocument></ownershipDocument>", 999001)
    # A Schedule 13D that states no percentage says a holding, not a number that is not there.
    bare = ("<edgarSubmission><issuerCIK>999001</issuerCIK><issuerName>Acme Robotics Inc.</issuerName><reportingPersonInfo>"
            "<reportingPersonName>Example Capital LP</reportingPersonName></reportingPersonInfo><dateOfEvent>some day</dateOfEvent>"
            "</edgarSubmission>")
    fields = holder_fields(bare, 999001)
    assert holder_title(fields, "the company") == "Schedule 13D: Example Capital LP reports a holding in Acme Robotics Inc."
    assert (fields["percent"], fields["event_date"]) == (None, "some day")       # a date it could not read is kept as written


def test_an_answer_that_is_too_large_is_not_taken_in(tmp_path):
    provider, site = reader(tmp_path, max_bytes=20_000)
    site.bodies[RETIRES] = b"<html><body>" + b"<p>filler</p>" * 5000 + b"</body></html>"          # 65,000 bytes
    read = provider.filings("ACME", SINCE)
    assert KEPT[1] not in [filing.accession for filing in read.filings] and len(read.filings) == 4
    assert read.problems == ["8-K 0000999001-26-000049: EDGAR's answer for acme-20261123.htm is larger than 20,000 bytes "
                             "and was not read."]
    assert read.unavailable == "", "one oversized document is that document's problem"


def test_only_the_start_of_a_very_long_page_is_gone_through(monkeypatch):
    page = "<html><body><p>Item 8.01 Other Events.</p><p>The Company announced a thing.</p>" + "<p>more</p>" * 200 + "</body></html>"
    assert event_excerpt(document_text(page), 200).startswith("Item 8.01 Other Events.\nThe Company announced a thing.")
    hidden = "<html><body><div style='display:none'>" + "x" * 3000 + "</div><p>Item 8.01 Other Events.</p></body></html>"
    monkeypatch.setattr("data.edgar.PARSED_CHARS", 1000)
    assert event_page_excerpt(hidden.encode(), 200) == "", "its first item lies beyond what is gone through: nothing is quoted"
    assert event_page_excerpt(page.encode(), 200).startswith("Item 8.01 Other Events.")


def test_a_filing_whose_time_carries_no_zone_is_not_guessed_at(tmp_path):
    provider, site = reader(tmp_path)
    listing = json.loads((FIXTURES / "submissions_ACME.json").read_text())
    listing["filings"]["recent"]["acceptanceDateTime"][0] = "2026-11-25T23:30:25"
    site.bodies[LIST] = json.dumps(listing).encode()
    with pytest.raises(DataError, match="ACME: EDGAR's list of filings is not laid out as expected"):
        provider.filings("ACME", SINCE)


def test_the_text_of_a_page_is_what_a_reader_of_it_would_see():
    page = ('<html><head><title>t</title><style>p { x: y }</style></head><body>'
            '<div style="display: none"><p>gone <br/> <img src="x"> <b>too</b></p></div>'
            '<p>Shown&#160;one &amp; <span style="display:none">not this</span>two</p><br>'
            '<table><tr><td>a</td><td>b</td></tr></table><ix:header><x>hidden</x></ix:header><p>last</p></body></html>')
    assert document_text(page) == "Shown one & two\na b\nlast"
    assert document_text("") == ""


def test_the_kept_file_holds_numbers_and_accessions_and_nothing_of_the_contact(tmp_path):
    provider, _ = reader(tmp_path)
    provider.filings("ACME", SINCE)
    kept = json.loads((tmp_path / "edgar.json").read_text())
    assert kept["ciks"]["ACME"] == 999001
    assert sorted(kept["left_out"]["ACME"]) == ["0000777000-26-000011", "0000999001-26-000040"]
    assert "example.test" not in (tmp_path / "edgar.json").read_text()
    # It is written whole or not at all (to one side, then moved into place), and nothing is left lying beside it.
    assert [path.name for path in tmp_path.iterdir()] == ["edgar.json"]
    # A kept file that cannot be read is done without: the list is fetched again.
    (tmp_path / "edgar.json").write_text('{"ciks": {"ACME": 9', encoding="utf-8")
    again, site = reader(tmp_path)
    again.filings("ACME", SINCE, budget=Budget(0))
    assert site.urls()[0] == TICKERS and json.loads((tmp_path / "edgar.json").read_text())["ciks"]["ACME"] == 999001

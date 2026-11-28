"""Shared by every test."""

import pytest

import config


@pytest.fixture(autouse=True)
def no_contact_for_the_sec(monkeypatch):
    """No test reads filings from the SEC.

    The desk's command line reads them from EDGAR whenever a contact is set
    (EDGAR_CONTACT), and this machine may well have one set. While a test runs
    there is none, so nothing a test starts can send a request to sec.gov, or
    send it that contact. A test that needs a contact sets a made-up one.
    """
    monkeypatch.setattr(config, "EDGAR_CONTACT", "")

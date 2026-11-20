"""The end-of-run Chat message built by sourcing.pipeline.summary_message."""
from sourcing.models import LlmUsage
from sourcing.pipeline import CHAT_MAX_LISTED_ROWS, RunSummary, summary_message


def _summary(not_done=()):
    s = RunSummary("run-1")
    s.usage = LlmUsage(calls=6, input_tokens=113964, output_tokens=12043, cost_usd=0.348358)
    v = s.vendors["proponent"]
    v.parts, v.offers, v.mode, v.usage = 52, 78, "bulk search, up to 25 per search", s.usage
    v.counts.update({"DONE": 51, "NOT_FOUND": 1})
    s.not_done = list(not_done)
    return s


def _result(counts):
    return {"rows_total": sum(counts.values()), "counts": counts, "seconds": 349.3}


def test_message_has_totals_vendor_and_llm():
    text = summary_message(_summary([(46, "0684-12-28", "proponent", "NOT_FOUND", None)]),
                           _result({"DONE": 51, "NOT_FOUND": 1}), "1V-S8J4bFqw1w_aF7CVqak",
                           "anthropic claude-sonnet-5-5, effort medium")
    assert text.startswith("*SourcingAI run finished*")
    assert "52 part(s) in 5m 49s (6.7s per part)" in text
    assert "<https://docs.google.com/spreadsheets/d/1V-S8J4bFqw1w_aF7CVqak|" in text
    assert "DONE 51 · NOT_FOUND 1" in text and "Offers written to Results: 78" in text
    assert "• Proponent: 52 part(s), bulk search, up to 25 per search" in text
    assert "• Model: anthropic claude-sonnet-5-5, effort medium" in text
    assert "• Calls: 6" in text and "113,964 in + 12,043 out (avg 2,191 + 231 per part)" in text
    assert "• Cost: $0.3484 total, $0.0067 per part" in text
    assert "• Row 46 `0684-12-28` (Proponent): NOT_FOUND" in text


def test_problems_in_title_and_long_lists_cut():
    rows = [(i, f"P{i}", "proponent", "FAILED", f"Search timed out after {300 + i}s")
            for i in range(15)]
    text = summary_message(_summary(rows), _result({"DONE": 37, "FAILED": 15}), "test.xlsx", "")
    assert text.startswith("*SourcingAI run finished, 15 row(s) need attention*")
    assert "`test.xlsx` (local file)" in text
    assert "• Model:" not in text
    assert f"… and {15 - CHAT_MAX_LISTED_ROWS} more" in text


def test_page_actions_are_in_the_summary():
    summary = _summary([])
    summary.vendors["proponent"].parts = 11
    summary.vendors["proponent"].page_actions = [
        "proponent_warehouse_pick: done with the LLM (2 click(s)) (x6)"]
    text = summary_message(summary, _result({"DONE": 11}), "test.xlsx", "")
    assert "Page actions the code couldn't do alone" in text
    assert "proponent_warehouse_pick: done with the LLM" in text


def test_who_asked_and_the_sheet_name_are_in_the_summary():
    """Team, 2026-11-19: who used the service and on which sheet, at a glance."""
    summary = _summary([])
    summary.requesters.update({"ann@skylinkintl.com": 3, "bob@skylinkintl.com": 1})
    text = summary_message(summary, _result({"DONE": 4}), "1V-S8J4bFqw1w_aF7CVqak", "",
                           sheet_title="RFQ <Nov> | Textron")
    assert "Requested by: ann@skylinkintl.com (3 rows), bob@skylinkintl.com (1 row)" in text
    assert "|RFQ  Nov    Textron>" in text      # the name, without Chat's link characters


def test_missing_rfq_headings_are_warned():
    """2026-11-19: RFQ A1 overwritten with "CERTS"; the vendor name went to a new
    column at the end and nobody saw it."""
    text = summary_message(_summary([]), _result({"DONE": 1}), "test.xlsx", "",
                           missing_headings={"RFQ": ["Vendor Reference"]})
    assert "Column headings missing or doubled in the sheet" in text and '• RFQ: "Vendor Reference"' in text


def test_rows_with_the_same_reason_are_one_line_with_a_count():
    """Team, 2026-11-20: AAR's "price on request only" once per part made the message long."""
    note = "price on request only ($RFQ): contact AAR by email"
    rows = [(42, "20138-1000-0101", "aar", "NOT_SOLD", note),
            (43, "35-0F5-1000", "aar", "NOT_SOLD", note),
            (44, "MS20995C32", "proponent", "NOT_FOUND", None)]
    text = summary_message(_summary(rows), _result({"DONE": 49, "NOT_SOLD": 2, "NOT_FOUND": 1}),
                           "test.xlsx", "")
    assert f"2 parts NOT_SOLD: {note} (rows 42, 43)" in text
    assert "Row 42" not in text and "20138-1000-0101" not in text
    assert "• Row 44 `MS20995C32` (Proponent): NOT_FOUND" in text
    assert "*Not DONE* (3)" in text


def test_a_long_group_shows_the_first_rows_and_how_many_more():
    rows = [(i, f"P{i}", "aar", "NOT_SOLD", "price on request only") for i in range(10, 22)]
    text = summary_message(_summary(rows), _result({"NOT_SOLD": 12}), "test.xlsx", "")
    assert "12 parts NOT_SOLD: price on request only (rows 10, 11, 12, 13, 14, 15, 16, 17, … (+4 more))" in text

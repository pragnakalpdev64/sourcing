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
    rows = [(i, f"P{i}", "proponent", "FAILED", "Search timed out after 300s") for i in range(15)]
    text = summary_message(_summary(rows), _result({"DONE": 37, "FAILED": 15}), "test.xlsx", "")
    assert text.startswith("*SourcingAI run finished, 15 row(s) need attention*")
    assert "`test.xlsx` (local file)" in text
    assert "• Model:" not in text
    assert f"… and {15 - CHAT_MAX_LISTED_ROWS} more" in text

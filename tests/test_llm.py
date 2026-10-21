from types import SimpleNamespace
from unittest.mock import MagicMock

import openai
import pytest

from po_monitor.llm import POAnalysis, POData, LLMError, analyze_email, build_user_input
from tests.helpers import make_email, non_po_analysis, po_analysis


def client_returning(parsed):
    client = MagicMock()
    client.responses.parse.return_value = SimpleNamespace(output_parsed=parsed)
    return client


def test_po_email_parsed():
    client = client_returning(po_analysis())
    result = analyze_email(make_email(body="Butuh 5 security"), client, "gpt-6-luna", 12000)
    assert result.is_po and result.po.positions[0].quantity == 5
    kwargs = client.responses.parse.call_args.kwargs
    assert kwargs["model"] == "gpt-6-luna"
    assert kwargs["text_format"] is POAnalysis
    assert "Butuh 5 security" in kwargs["input"]


def test_non_po_drops_po_data():
    analysis = non_po_analysis()
    result = analyze_email(make_email(), client_returning(analysis), "gpt-6-luna", 12000)
    assert not result.is_po and result.po is None


def test_po_without_data_is_error():
    bad = POAnalysis(is_po=True, confidence=0.9, reason="x", po=None)
    with pytest.raises(LLMError):
        analyze_email(make_email(), client_returning(bad), "gpt-6-luna", 12000)


def test_refusal_or_unparsed_is_error():
    with pytest.raises(LLMError):
        analyze_email(make_email(), client_returning(None), "gpt-6-luna", 12000)


def test_api_error_wrapped():
    client = MagicMock()
    # Buat exception tanpa objek response HTTP sungguhan.
    err = openai.RateLimitError.__new__(openai.RateLimitError)
    Exception.__init__(err, "slow down")
    err.message = "slow down"
    client.responses.parse.side_effect = err
    with pytest.raises(LLMError, match="Rate limit"):
        analyze_email(make_email(), client, "gpt-6-luna", 12000)


def test_body_truncated():
    text = build_user_input(make_email(body="a" * 20000), 12000)
    assert text.count("a") <= 12000 + 10  # huruf 'a' juga muncul di header
    assert "[...dipotong...]" in text


def test_summary_is_last_po_field():
    # Urutan field = urutan generate; summary harus dibuat setelah ekstraksi selesai.
    assert list(POData.model_fields)[-1] == "summary"

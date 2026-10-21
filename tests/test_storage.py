import pytest

from po_monitor.storage import Storage
from tests.helpers import make_email, non_po_analysis, po_analysis, po_data, position


@pytest.fixture
def store(tmp_path):
    return Storage(tmp_path / "test.db")


def test_dedupe_existing_ids(store):
    store.save_email(make_email("m1"))
    store.save_email(make_email("m1"))  # diabaikan
    assert store.existing_message_ids(["m1", "m2"]) == {"m1"}
    assert len(store.list_emails()) == 1
    assert store.pending_message_ids() == ["m1"]


def test_data_version_changes(store):
    v0 = store.data_version()
    store.save_email(make_email("m1"))
    v1 = store.data_version()
    store.mark_error("m1", "boom")
    v2 = store.data_version()
    assert len({v0, v1, v2}) == 3
    assert store.data_version() == v2


def test_save_po_result_and_metrics(store):
    store.save_email(make_email("m1"))
    store.save_email(make_email("m2"))
    store.save_email(make_email("m3"))
    po = po_data(positions=[position("Security", 5), position("Cleaning Service", 3)])
    store.save_result("m1", po_analysis(po=po), "gpt-6-luna", 0.6)
    store.save_result("m2", non_po_analysis(), "gpt-6-luna", 0.6)
    store.mark_error("m3", "rate limit")

    m = store.metrics()
    assert m == {"total": 3, "po": 1, "review": 0, "error": 1, "headcount": 8}

    detail = store.get_email_detail("m1")
    assert detail["status"] == "processed"
    assert detail["po"].client_company == "PT Maju Jaya"
    assert store.get_email_detail("m2")["po"] is None


def test_needs_review_below_threshold(store):
    store.save_email(make_email("m1"))
    store.save_result("m1", po_analysis(confidence=0.45), "gpt-6-luna", 0.6)
    assert [r["message_id"] for r in store.list_emails("review")] == ["m1"]


def test_reprocess_overwrites(store):
    store.save_email(make_email("m1"))
    store.save_result("m1", po_analysis(), "gpt-6-luna", 0.6)
    store.save_result("m1", non_po_analysis(), "gpt-6-luna", 0.6)
    assert store.get_email_detail("m1")["po"] is None
    assert store.metrics()["po"] == 0
    assert store.list_emails("non_po")[0]["message_id"] == "m1"


def test_error_then_success_clears_error(store):
    store.save_email(make_email("m1"))
    store.mark_error("m1", "boom")
    store.save_result("m1", po_analysis(), "gpt-6-luna", 0.6)
    d = store.get_email_detail("m1")
    assert d["status"] == "processed" and d["error_message"] is None


def test_filters_and_search(store):
    store.save_email(make_email("m1", subject="PO Security", received_at=1))
    store.save_email(make_email("m2", subject="Newsletter", received_at=2))
    store.save_result("m1", po_analysis(), "gpt-6-luna", 0.6)
    store.save_result("m2", non_po_analysis(), "gpt-6-luna", 0.6)
    assert [r["message_id"] for r in store.list_emails("po")] == ["m1"]
    assert [r["message_id"] for r in store.list_emails("all")] == ["m2", "m1"]
    assert [r["message_id"] for r in store.list_emails("all", "news")] == ["m2"]


def test_list_emails_includes_summary(store):
    for mid in ("m1", "m2", "m3", "m4"):
        store.save_email(make_email(mid))
    store.save_result("m1", po_analysis(po=po_data(summary="5 Security di Cikarang")), "gpt-6-luna", 0.6)
    store.save_result("m2", non_po_analysis(), "gpt-6-luna", 0.6)
    store.mark_error("m3", "boom")  # m4 tetap pending

    summaries = {r["message_id"]: r["summary"] for r in store.list_emails()}
    assert summaries == {"m1": "5 Security di Cikarang", "m2": None, "m3": None, "m4": None}

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from po_monitor.gmail_client import GmailClient, HistoryExpired


def client_with(service):
    client = GmailClient.__new__(GmailClient)
    client._service = service
    return client


def test_list_added_since_filters_inbox_and_paginates():
    service = MagicMock()
    pages = [
        {
            "history": [
                {"messagesAdded": [{"message": {"id": "a", "labelIds": ["INBOX", "UNREAD"]}}]},
                {"messagesAdded": [{"message": {"id": "s", "labelIds": ["SENT"]}}]},
            ],
            "historyId": "110",
            "nextPageToken": "p2",
        },
        {
            "history": [{"messagesAdded": [{"message": {"id": "b", "labelIds": ["INBOX"]}}, {"message": {"id": "a", "labelIds": ["INBOX"]}}]}],
            "historyId": "120",
        },
    ]
    service.users().history().list().execute.side_effect = pages
    ids, latest = client_with(service).list_added_since("100")
    assert ids == ["a", "b"]
    assert latest == "120"


def test_list_added_since_404_raises_history_expired():
    service = MagicMock()
    service.users().history().list().execute.side_effect = HttpError(SimpleNamespace(status=404, reason="nf"), b"{}")
    with pytest.raises(HistoryExpired):
        client_with(service).list_added_since("1")


def test_current_history_id():
    service = MagicMock()
    service.users().getProfile().execute.return_value = {"emailAddress": "a@b", "historyId": 987}
    assert client_with(service).current_history_id() == "987"

import base64

from po_monitor.gmail_client import html_to_text, parse_message


def b64(text: str, encoding="utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(encoding)).decode().rstrip("=")


HEADERS = [
    {"name": "From", "value": "Budi <budi@client.co.id>"},
    {"name": "Subject", "value": "PO Tenaga Kerja"},
]


def test_plain_text_message():
    raw = {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1760000000123",
        "snippet": "Mohon disiapkan",
        "payload": {"mimeType": "text/plain", "headers": HEADERS, "body": {"data": b64("Mohon disiapkan 10 operator")}},
    }
    msg = parse_message(raw)
    assert msg.message_id == "m1"
    assert msg.sender == "Budi <budi@client.co.id>"
    assert msg.subject == "PO Tenaga Kerja"
    assert msg.received_at == 1760000000
    assert msg.body_text == "Mohon disiapkan 10 operator"
    assert msg.attachments == []


def test_html_only_message():
    html = "<html><head><style>p{}</style></head><body><p>Butuh <b>5 security</b></p><br>Terima kasih</body></html>"
    raw = {
        "id": "m2",
        "internalDate": "0",
        "payload": {"mimeType": "text/html", "headers": HEADERS, "body": {"data": b64(html)}},
    }
    text = parse_message(raw).body_text
    assert "<" not in text
    assert "5 security" in text and "Terima kasih" in text
    assert "p{}" not in text


def test_nested_multipart_with_attachment_and_charset():
    raw = {
        "id": "m3",
        "internalDate": "0",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": HEADERS,
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "headers": [{"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'}],
                            "body": {"data": b64("Café 3 orang", "iso-8859-1")},
                        },
                        {"mimeType": "text/html", "body": {"data": b64("<p>Café 3 orang</p>")}},
                    ],
                },
                {"mimeType": "application/pdf", "filename": "PO-123.pdf", "body": {"attachmentId": "x"}},
            ],
        },
    }
    msg = parse_message(raw)
    assert msg.body_text == "Café 3 orang"
    assert msg.attachments == ["PO-123.pdf"]


def test_html_to_text_collapses_blank_lines():
    assert html_to_text("a<br><br><br><br>b") == "a\n\nb"

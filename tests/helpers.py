from po_monitor.llm import POAnalysis, POData, PositionItem
from po_monitor.models import EmailMessage


def make_email(message_id="m1", received_at=1_760_000_000, subject="Test", sender="a@b.com", body="halo"):
    return EmailMessage(
        message_id=message_id,
        thread_id="t-" + message_id,
        sender=sender,
        subject=subject,
        received_at=received_at,
        snippet=body[:50],
        body_text=body,
        attachments=[],
    )


def position(title="Security", quantity=5, **kw):
    fields = dict(gender=None, age_range=None, qualifications=None, shift_or_hours=None, rate_or_budget=None)
    fields.update(kw)
    return PositionItem(title=title, quantity=quantity, **fields)


def po_data(positions=None, **kw):
    fields = dict(
        client_company="PT Maju Jaya",
        contact_name=None,
        contact_email=None,
        contact_phone=None,
        po_number="PO-1",
        po_date=None,
        site_name=None,
        work_location="Cikarang",
        start_date=None,
        contract_duration=None,
        benefits=None,
        notes=None,
        summary="5 Security untuk lokasi Cikarang",
    )
    fields.update(kw)
    return POData(positions=positions if positions is not None else [position()], **fields)


def po_analysis(confidence=0.9, po=None):
    return POAnalysis(is_po=True, confidence=confidence, reason="permintaan tenaga kerja", po=po or po_data())


def non_po_analysis():
    return POAnalysis(is_po=False, confidence=0.95, reason="newsletter", po=None)

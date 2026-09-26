from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from faktura_pilot.automation.models import ActionOutcomeUnknown, DocumentRow, ManualReviewRequired
from faktura_pilot.automation.windows import WindowsFakturamaGateway
from tests.factories import sample_order


@pytest.mark.parametrize("kind", ["Order", "Invoice"])
def test_save_retries_readback_without_saving_again(kind):
    source = sample_order()
    gateway = WindowsFakturamaGateway()
    gateway._last_source = source
    gateway._last_order_number = "PO9"
    gateway._read_optional = Mock(return_value="DOC9")
    gateway._dispatch_save = Mock()
    row = DocumentRow("DOC9", kind, source.external_reference, source.totals.gross, "open")
    gateway.find_documents = Mock(side_effect=[
        ManualReviewRequired("Documents has no unique Document header"), [], [row],
    ])
    with patch("faktura_pilot.automation.windows.time.sleep"):
        number = gateway.save_order() if kind == "Order" else gateway.save_invoice()
    assert number == "DOC9"
    gateway._dispatch_save.assert_called_once()
    assert gateway.find_documents.call_count == 3


def test_unreadable_saved_row_stops_with_underlying_reason():
    gateway = WindowsFakturamaGateway()
    gateway._last_source = sample_order()
    gateway._read_optional = Mock(return_value="PO9")
    gateway._dispatch_save = Mock()
    gateway.find_documents = Mock(side_effect=ManualReviewRequired("reference is unreadable"))
    with patch("faktura_pilot.automation.windows.time.sleep"):
        with pytest.raises(ActionOutcomeUnknown, match="reference is unreadable"):
            gateway.save_order()
    gateway._dispatch_save.assert_called_once()
    assert gateway.find_documents.call_count == 3


def test_wrong_total_is_not_confirmed_by_retry():
    source = sample_order()
    gateway = WindowsFakturamaGateway()
    gateway._last_source = source
    gateway.find_documents = Mock(return_value=[
        DocumentRow("PO9", "Order", source.external_reference, Decimal("1"), "open"),
    ])
    with patch("faktura_pilot.automation.windows.time.sleep"):
        with pytest.raises(ActionOutcomeUnknown, match="found 0 exact rows"):
            gateway._confirm_saved_document("PO9", "Order")


def test_final_verification_reads_both_documents_once_and_requires_invoice_link():
    source = sample_order()
    gateway = WindowsFakturamaGateway()
    gateway._invoice_ref = Mock(proposed_invoice_date=source.order_date.isoformat())
    order = DocumentRow("PO9", "Order", source.external_reference,
                        source.totals.gross, "open", document_date=source.order_date)
    invoice = DocumentRow("INV9", "Invoice", source.external_reference,
                          source.totals.gross, source.payment.status.value,
                          linked_order_number="PO9", document_date=source.order_date)
    gateway.find_documents = Mock(return_value=[order, invoice])
    assert gateway.verify_final_documents(source, "PO9", "INV9").verified
    gateway.find_documents.assert_called_once_with(source, "All")
    gateway.capture_evidence = Mock()
    gateway.find_documents.return_value = [order, DocumentRow(
        "INV9", "Invoice", source.external_reference, source.totals.gross,
        source.payment.status.value, linked_order_number="OTHER",
        document_date=source.order_date,
    )]
    assert not gateway.verify_final_documents(source, "PO9", "INV9").verified

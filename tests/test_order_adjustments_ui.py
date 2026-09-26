from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from faktura_pilot.automation import adjustments
from faktura_pilot.automation.windows import WindowsFakturamaGateway


def test_adjustments_select_shipping_without_overwriting_amount_and_are_idempotent():
    source = SimpleNamespace(order_discount_percent=Decimal('5'), shipping=SimpleNamespace(
        name='Standard Shipping', net_amount=Decimal('12.50'), vat_rate_percent=Decimal('19')
    ))
    editor, discount, method, amount = object(), object(), object(), object()
    values = {discount: '0%', method: 'Free of shipping costs', amount: '0,00'}
    g = WindowsFakturamaGateway()
    g._document_editor_identity = Mock(return_value=(editor, 'Order', 'PO1'))
    g._activate_document_tab = Mock()
    g.discover_open_order = Mock(return_value=SimpleNamespace(number="PO1"))
    g._raw_element_value = lambda e: values[e]
    g._element_value = lambda e: values[e]
    def select(*args, **kwargs):
        values[method] = source.shipping.name
        values[amount] = '12,50'
    g._select_combo_by_current_value = Mock(side_effect=select)
    g._type_field_text = Mock(side_effect=lambda e, value, label: values.update({e: value}))
    g._focus_order_header_for_grid = Mock()
    with patch.object(adjustments, 'controls', return_value=(editor, discount, method, amount)), \
         patch('faktura_pilot.automation.shipping.ensure'):
        assert adjustments.apply(g, source).verified
        assert values[amount] == '12,50'
        assert values[discount] == '-5'
        count = g._type_field_text.call_count
        assert adjustments.apply(g, source).verified
        assert g._type_field_text.call_count == count
        values[amount] = '99,00'
        assert not adjustments.verify(g, source).verified
        values[amount] = '12,50'
        assert all(call.args[0] is not amount for call in g._type_field_text.call_args_list)
        values[discount] = '5%'
        assert not adjustments.verify(g, source).verified  # surcharge is not a discount


def test_totals_readback_uses_goods_subtotal_with_adjustments():
    from pathlib import Path

    from faktura_pilot.domain.models import OrderSource
    source = OrderSource.model_validate_json(
        Path('examples/order-source-adjustments.json').read_text(encoding='utf-8')
    )
    g = WindowsFakturamaGateway()
    g._read_optional = Mock(side_effect=['570.00', '105.26', '659.27'])
    assert g.verify_order_totals(source).verified
    g._read_optional = Mock(side_effect=['554.00', '105.26', '659.27'])
    g.capture_evidence = Mock()
    assert not g.verify_order_totals(source).verified


def test_adjusted_source_flows_through_order_and_invoice(tmp_path):
    from pathlib import Path

    from faktura_pilot.domain.models import OrderSource
    from faktura_pilot.workflow.orchestrator import WorkflowOrchestrator
    from faktura_pilot.workflow.store import WorkflowStore
    from tests.workflow_fakes import FixedExtractor, ScriptedGateway
    source = OrderSource.model_validate_json(
        Path('examples/order-source-adjustments.json').read_text(encoding='utf-8')
    )
    gateway = ScriptedGateway()
    open_order = gateway.open_new_order
    def open_after_shipping():
        assert gateway.prepared_adjustment_source.shipping == source.shipping
        return open_order()
    gateway.open_new_order = open_after_shipping
    from dataclasses import replace
    original_verify = gateway.verify_order_document
    gateway.verify_order_document = lambda src, number: replace(
        original_verify(src, number), document_date=src.order_date
    )
    runner = WorkflowOrchestrator(FixedExtractor(source), gateway, WorkflowStore(tmp_path))
    result = runner.run(Path('unused.png'))
    assert result.complete, result.checkpoint.review
    assert gateway.adjustment_source.order_discount_percent == Decimal('5')
    assert gateway.payment_sources[0].totals.gross == Decimal('659.27')
    assert all(row.total == Decimal('659.27') for row in gateway.documents)

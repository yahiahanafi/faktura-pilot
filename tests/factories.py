from datetime import UTC, datetime

from faktura_pilot.domain.models import OrderSource


def sample_order() -> OrderSource:
    return OrderSource.model_validate(
        {
            "order_date": "2026-07-14",
            "external_reference": "WEB-2026-0714-A17",
            "source_customer_id": "CUST-1007",
            "currency": "eur",
            "debtor": {
                "company": "Northstar Office GmbH",
                "first_name": "Mira",
                "last_name": "Weber",
                "alias": "Northstar",
                "email": "mira@example.de",
                "telephone": "+49 30 555 0100",
                "billing_address": {
                    "additional_name": "Northstar Office GmbH",
                    "street": "Hafenstraße 8",
                    "zip_code": "20457",
                    "city": "Hamburg",
                    "country": "Germany",
                    "address_specification": None,
                    "district": None,
                },
                "delivery_address": {
                    "additional_name": "Northstar Office Warehouse",
                    "street": "Werkstraße 12",
                    "zip_code": "20095",
                    "city": "Hamburg",
                    "country": "Germany",
                    "address_specification": "Receiving dock",
                    "district": None,
                },
            },
            "payment": {
                "method": "Bank Transfer",
                "status": "PAID",
                "payment_date": "2026-07-18",
            },
            "items": [
                {
                    "sku": "CHR-ERG-01",
                    "description": "Ergonomic chair",
                    "quantity": "2",
                    "unit": "piece",
                    "unit_net_price": "250.00",
                    "vat_rate_percent": "19",
                    "discount_percent": "10",
                    "source_line_net_total": "450.00",
                },
                {
                    "sku": "MAT-DESK-02",
                    "description": "Desk mat",
                    "quantity": "3",
                    "unit": "piece",
                    "unit_net_price": "40.00",
                    "vat_rate_percent": "19",
                    "discount_percent": "0",
                    "source_line_net_total": "120.00",
                },
            ],
            "totals": {"net": "570.00", "vat": "108.30", "gross": "678.30"},
            "extraction": {
                "image_sha256": "a" * 64,
                "extracted_at": datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
                "model": "fixture",
                "prompt_version": "test",
                "source_values": [
                    {"field_path": "external_reference", "text": "WEB-2026-0714-A17"}
                ],
            },
        }
    )


def minimal_payload() -> dict[str, object]:
    return {
        "order_date": "2026-07-14",
        "external_reference": "WEB-2026-0714-A17",
        "source_customer_id": "CUST-1007",
        "currency": "EUR",
        "debtor": {
            "company": "Northstar Office GmbH",
            "first_name": "Mira",
            "last_name": "Weber",
            "alias": "Northstar",
            "email": "mira@example.de",
            "telephone": "+49 30 555 0100",
            "billing_address": {
                "additional_name": "Northstar Office GmbH",
                "street": "Hafenstraße 8",
                "zip_code": "20457",
                "city": "Hamburg",
                "country": "Germany",
                "address_specification": None,
                "district": None,
            },
            "delivery_address": {
                "additional_name": "Northstar Office Warehouse",
                "street": "Werkstraße 12",
                "zip_code": "20095",
                "city": "Hamburg",
                "country": "Germany",
                "address_specification": "Receiving dock",
                "district": None,
            },
        },
        "payment": {"method": "Bank Transfer", "status": "PAID", "payment_date": "2026-07-18"},
        "items": [
            {
                "sku": "CHR-ERG-01",
                "description": "Ergonomic chair",
                "quantity": "2",
                "unit": "piece",
                "unit_net_price": "250.00",
                "vat_rate_percent": "19",
                "discount_percent": "10",
                "source_line_net_total": "450.00",
            },
            {
                "sku": "MAT-DESK-02",
                "description": "Desk mat",
                "quantity": "3",
                "unit": "piece",
                "unit_net_price": "40.00",
                "vat_rate_percent": "19",
                "discount_percent": "0",
                "source_line_net_total": "120.00",
            },
        ],
        "totals": {"net": "570.00", "vat": "108.30", "gross": "678.30"},
        "source_values": [
            {"field_path": "external_reference", "text": "WEB-2026-0714-A17"},
            {"field_path": "items.0.unit_net_price", "text": "EUR 250.00"},
        ],
    }

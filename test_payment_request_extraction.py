from pathlib import Path

from ai_extractor import normalize_invoice_result
from invoice_dataverse_mapper import (
    build_single_invoice_lookup_plan,
    normalize_label,
)


FIXTURE = Path(__file__).parent / "Invoices" / "Invoices 4" / "1.pdf"


def test_payment_request_uses_vendor_cell_invoice_not_header_ref() -> None:
    llm_result = {
        "document_type": "PAYMENT REQUEST",
        "invoice_date": "04/07/2026",
        "vendor_name": "شركة جوست للمعاينات والاختبارات",
        "vendor_invoice_number": "55219",
        "master_bl_number": "COSU6449596970",
        "consignee_name": "We-Can International Logistics",
        "currency": "EGP",
        "exchange_rate": 1,
        "subtotal_amount": 539,
        "tax_amount": 0,
        "total_amount": 539,
        "amount_in_words": "Currency EGP",
        "container_number": "OERU4057132",
        "line_items": [
            {
                "service_description": "LCL THC FEES - مصاريف فحص ومعاينه",
                "quantity": 1,
                "unit_price": 539,
                "currency": "EGP",
                "exchange_rate": 1,
                "taxable_amount": 539,
                "tax_rate": "0%",
                "tax_amount": 0,
                "total_amount": 539,
            }
        ],
    }

    normalized = normalize_invoice_result(
        llm_result,
        "",
        filename=FIXTURE.name,
        file_bytes=FIXTURE.read_bytes(),
    )

    assert normalized["payment_request_reference"] == "55219"
    assert normalized["vendor_invoice_number"] == "10747"
    assert normalized["payment_request_date"] == "04/07/2026"
    assert normalized["client_name"] == "We-Can International Logistics"
    assert normalized["consignee_name"] is None
    assert normalized["custody_type"] == "SUPPLIER"
    assert normalized["payment_method"] == "Cash"
    assert normalized["activity"] == "LCL"
    assert normalized["account_code"] == "2002030201000"
    assert normalized["sub_account_code"] == "30050331000000000000"
    assert normalized["withholding_tax_amount"] == 0
    assert normalized["amount_in_words"] is None
    assert normalized["exchange_rate"] is None
    assert normalized["creator_name"] == "ESRAA MOHAMED"

    item = normalized["line_items"][0]
    assert item["category"] == "LCL THC FEES"
    assert item["item_description"] == "مصاريف فحص ومعاينه"
    assert item["quantity"] is None
    assert item["unit_price"] is None
    assert item["estimated_amount"] == 539
    assert item["total_amount"] == 539
    assert item["tax_rate"] is None


def test_unicode_vendor_and_thc_service_resolve_without_first_row_fallback() -> None:
    extracted = {
        "currency": "EGP",
        "vendor_name": "شركة جوست للمعاينات والاختبارات",
        "sub_account_code": "30050331000000000000",
        "line_items": [
            {"service_description": "LCL THC FEES - مصاريف فحص ومعاينه"}
        ],
    }
    references = {
        "currencies": [
            {
                "transactioncurrencyid": "egp-id",
                "isocurrencycode": "EGP",
                "currencyname": "Egyptian Pound",
                "exchangerate": 1,
            }
        ],
        "shipping_lines": [
            {"mesco_shippinglineid": "wrong-id", "mesco_name": "Another Vendor"},
            {
                "mesco_shippinglineid": "gost-id",
                "mesco_name": "شركة جوست للمعاينات والاختبارات",
                "mesco_vendorid": "30050331000000000000",
            },
        ],
        "services": [
            {"xollsp_servicedefinitionid": "official-id", "xollsp_name": "Official receipts - THC"},
            {"xollsp_servicedefinitionid": "thc-id", "xollsp_name": "THC"},
        ],
    }

    plan = build_single_invoice_lookup_plan(extracted, references)

    assert normalize_label("شركة جوست") == "شركةجوست"
    assert plan["ready_to_post"] is True
    assert plan["lookups"]["invoice_vendor"]["id"] == "gost-id"
    service = plan["lookups"]["services"]["LCL THC FEES - مصاريف فحص ومعاينه"]
    assert service["target_label"] == "THC"
    assert service["id"] == "thc-id"


def test_missing_vendor_is_unresolved_instead_of_mapping_first_row() -> None:
    extracted = {
        "currency": "EGP",
        "vendor_name": "شركة غير موجودة",
        "line_items": [{"service_description": "THC"}],
    }
    references = {
        "currencies": [
            {"transactioncurrencyid": "egp-id", "isocurrencycode": "EGP"}
        ],
        "shipping_lines": [
            {"mesco_shippinglineid": "wrong-id", "mesco_name": "Another Vendor"}
        ],
        "services": [
            {"xollsp_servicedefinitionid": "thc-id", "xollsp_name": "THC"}
        ],
    }

    plan = build_single_invoice_lookup_plan(extracted, references)

    assert plan["ready_to_post"] is False
    assert plan["lookups"]["invoice_vendor"]["id"] is None
    assert any("invoice vendor: unresolved" in error for error in plan["errors"])

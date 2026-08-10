from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app
from wecan_proxy_bill_extractor import extract_wecan_proxy_bill, to_multi_invoice_payload


def _fixture_path() -> Path:
    matches = list((Path(__file__).parent / "Invoices" / "Invoices 3").glob("*.xls"))
    assert len(matches) == 1
    return matches[0]


def test_wecan_xls_maps_every_invoice_term_and_reconciles() -> None:
    source = _fixture_path()
    parsed = extract_wecan_proxy_bill(source)
    invoice = to_multi_invoice_payload(parsed, source.name)

    assert invoice["vendor_invoice_number"] == "702320601"
    assert invoice["master_bl_number"] == "COSU6501303560"
    assert invoice["container_number"] == "CSGU7177299"
    assert invoice["etd"] == "2026-05-30"
    assert len(invoice["groups"]) == 10
    assert sum(len(group["line_items"]) for group in invoice["groups"]) == 26

    validation = invoice["extraction_validation"]
    assert validation == {
        "record_count": 10,
        "line_items_count": 26,
        "calculated_debit": 7765.32,
        "calculated_credit": 290.0,
        "calculated_final_dn": 7475.32,
        "stated_final_dn": 7475.32,
        "posting_net_total": 7475.32,
        "local_agreement_total": 780.56,
        "is_reconciled": True,
    }
    assert invoice["measurement_validation"] == {
        "group_packages_total": 395.0,
        "group_gross_weight_kg": 23272.1,
        "stated_gross_weight_kg": 23272.1,
        "gross_weight_difference_kg": 0.0,
        "gross_weight_matches": True,
        "group_volume_cbm": 61.679,
        "stated_volume_cbm": 63.066,
        "volume_difference_cbm": 1.387,
        "volume_matches": False,
        "mapping_policy": (
            "Master/container use stated header totals; each House uses its own source row. "
            "Differences are reported and never silently redistributed."
        ),
    }

    port_said = next(group for group in invoice["groups"] if group["house_bl_number"] == "WYSE6050123")
    assert port_said["packages"] == 11.0
    assert port_said["kgs"] == 9219.2
    assert port_said["cbm"] == 14.5
    assert port_said["charged_wm"] == 14.5
    assert port_said["debit_total"] == 2220.0
    assert port_said["credit_total"] == 290.0
    assert port_said["total_amount"] == 1930.0
    assert port_said["local_agreement_total"] == 470.0
    assert any(
        line["service_description"] == "CREDIT - T/S" and line["total_amount"] == -290.0
        for line in port_said["line_items"]
    )
    assert all("LOCAL AGREEMENT" not in line["service_description"] for line in port_said["line_items"])
    assert len(port_said["local_agreement_items"]) == 2


def test_multi_invoice_endpoint_accepts_legacy_xls_without_llm() -> None:
    source = _fixture_path()
    with patch("main.DataverseClientService.get_instance", side_effect=RuntimeError("offline test")):
        with source.open("rb") as stream:
            response = TestClient(app).post(
                "/extract/invoice/multi",
                files={"file": (source.name, stream, "application/vnd.ms-excel")},
                data={"post_to_dataverse": "false"},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["groups_count"] == 10
    assert body["total_line_items"] == 26
    assert body["extraction_validation"]["is_reconciled"] is True
    assert body["extraction_validation"]["posting_net_total"] == 7475.32
    assert body["measurement_validation"]["gross_weight_matches"] is True
    assert body["measurement_validation"]["volume_difference_cbm"] == 1.387
    assert body["groups"][0]["source_row"] == 10
    assert body["groups"][0]["term"] == "CIF"
    assert body["groups"][0]["local_agreement_total"] == 65.0


def test_excel_invoice_endpoint_accepts_legacy_xls() -> None:
    source = _fixture_path()
    with patch("main.DataverseClientService.get_instance", side_effect=RuntimeError("offline test")):
        with source.open("rb") as stream:
            response = TestClient(app).post(
                "/extract/invoice/excel",
                files={"file": (source.name, stream, "application/vnd.ms-excel")},
                data={"post_to_dataverse": "false"},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["groups_count"] == 10
    assert body["total_line_items"] == 26
    assert body["extraction_validation"]["posting_net_total"] == 7475.32


def test_excel_invoice_endpoint_rejects_non_excel_uploads() -> None:
    response = TestClient(app).post(
        "/extract/invoice/excel",
        files={"file": ("invoice.pdf", b"not an excel workbook", "application/pdf")},
        data={"post_to_dataverse": "false"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Excel invoice endpoint accepts only .xls or .xlsx files."


class _FakeResponse:
    def __init__(self, status_code: int = 200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = ""
        self.headers = {}

    def json(self):
        return self._body


class _FakeDataverseClient:
    def __init__(self):
        self.posts = []
        self.patches = []

    def get(self, url: str):
        if url.startswith("xollsp_servicedefinitions"):
            names = {
                "Admin Fees": "00000000-0000-0000-1000-000000000001",
                "Rebate": "00000000-0000-0000-1000-000000000002",
                "Transfer Charges": "00000000-0000-0000-1000-000000000003",
                "Sea Freight + Exwork": "00000000-0000-0000-1000-000000000004",
                "Sea Freight": "00000000-0000-0000-1000-000000000005",
                "Loading Charges": "00000000-0000-0000-1000-000000000006",
                "PCS Port Congestion Surcharges": "00000000-0000-0000-1000-000000000007",
                "THC": "00000000-0000-0000-1000-000000000008",
            }
            return _FakeResponse(body={"value": [
                {"xollsp_servicedefinitionid": guid, "xollsp_name": name}
                for name, guid in names.items()
            ]})
        if url.startswith("transactioncurrencies"):
            return _FakeResponse(body={"value": [{
                "transactioncurrencyid": "00000000-0000-0000-0000-000000000001",
                "currencyname": "US Dollar",
                "isocurrencycode": "USD",
                "exchangerate": 1.0,
            }]})
        if url.startswith("mesco_shippinglines"):
            return _FakeResponse(body={"value": [
                {
                    "mesco_shippinglineid": "00000000-0000-0000-0000-000000000002",
                    "mesco_name": "We-Can International Logistics",
                },
                {
                    "mesco_shippinglineid": "00000000-0000-0000-0000-000000000005",
                    "mesco_name": "COSCO SHIPPING LINE",
                },
            ]})
        if url.startswith("xollsp_addresses"):
            return _FakeResponse(body={"value": [
                {
                    "xollsp_addressid": "00000000-0000-0000-2000-000000000001",
                    "xollsp_name": "Alexandria, Egypt (old)",
                },
                {
                    "xollsp_addressid": "00000000-0000-0000-2000-000000000002",
                    "xollsp_name": "Port Said",
                },
            ]})
        if url.startswith("mesco_vesselsmises"):
            return _FakeResponse(body={"value": [{
                "mesco_vesselsmisid": "00000000-0000-0000-3000-000000000001",
                "mesco_name": "CMA CGM ADONIS",
            }]})
        if url.startswith("xollsp_incoterms"):
            return _FakeResponse(body={"value": [
                {"xollsp_incotermid": "00000000-0000-0000-4000-000000000001", "xollsp_name": "CIF"},
                {"xollsp_incotermid": "00000000-0000-0000-4000-000000000002", "xollsp_name": "FOB"},
                {"xollsp_incotermid": "00000000-0000-0000-4000-000000000003", "xollsp_name": "EXW"},
            ]})
        if url.startswith("xollsp_quotecostlines"):
            return _FakeResponse(body={"value": []})
        if "mesco_masterblno eq 'COSU6501303560'" in url:
            return _FakeResponse(body={"value": [{
                "mesco_operationid": "00000000-0000-0000-0000-000000000003",
                "mesco_code": "O-MASTER",
                "mesco_bltype": 886150001,
                "mesco_masterblno": "COSU6501303560",
            }]})
        if "mesco_masterblno eq '" in url:
            hbl = url.split("mesco_masterblno eq '", 1)[1].split("'", 1)[0]
            suffix = abs(hash(hbl)) % 999999999999
            return _FakeResponse(body={"value": [{
                "mesco_operationid": f"00000000-0000-0000-0001-{suffix:012d}",
                "mesco_code": hbl,
                "mesco_bltype": 886150002,
                "mesco_masterblno": hbl,
            }]})
        raise AssertionError(f"Unexpected Dataverse GET: {url}")

    def post(self, entity: str, json):
        assert entity == "xollsp_quotecostlines"
        self.posts.append(json)
        return _FakeResponse(status_code=204)

    def patch(self, entity: str, json):
        self.patches.append((entity, json))
        return _FakeResponse(status_code=204)


def test_wecan_xls_builds_complete_dynamics_posting_payloads() -> None:
    source = _fixture_path()
    fake_client = _FakeDataverseClient()

    with (
        patch("main.DataverseClientService.get_instance", return_value=fake_client),
        patch("main._ensure_invoice_container", return_value={
            "id": "00000000-0000-0000-0000-000000000004",
            "number": "CSGU7177299",
            "action": "reused",
        }),
    ):
        with source.open("rb") as stream:
            response = TestClient(app).post(
                "/extract/invoice/excel",
                files={"file": (source.name, stream, "application/vnd.ms-excel")},
                data={"post_to_dataverse": "true"},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["mapping_validation"]["ready_to_post"] is True
    assert body["total_posted"] == 26
    assert len(fake_client.posts) == 26
    assert all(post["mesco_vendorinvoicenumber"] == "702320601" for post in fake_client.posts)
    assert all("mesco_Operation@odata.bind" in post for post in fake_client.posts)
    assert all("mesco_Master3@odata.bind" in post for post in fake_client.posts)
    assert all("transactioncurrencyid@odata.bind" in post for post in fake_client.posts)
    assert all("xollsp_LogisticService@odata.bind" in post for post in fake_client.posts)
    assert all("mesco_invoicevendor_shippingline@odata.bind" in post for post in fake_client.posts)
    assert all("LOCAL AGREEMENT" not in post["xollsp_name"] for post in fake_client.posts)

    posting_total = round(sum(
        float(post["xollsp_quantity"]) * float(post["xollsp_unitamount"])
        for post in fake_client.posts
    ), 2)
    assert posting_total == 7475.32
    assert any(
        post["xollsp_name"] == "CREDIT - T/S"
        and post["xollsp_quantity"] == 14.5
        and post["xollsp_unitamount"] == -20.0
        for post in fake_client.posts
    )

    operation_updates = [
        (entity, payload)
        for entity, payload in fake_client.patches
        if entity.startswith("mesco_operations(")
    ]
    assert len(operation_updates) == 11
    master_update = next(
        payload for entity, payload in operation_updates
        if "00000000-0000-0000-0000-000000000003" in entity
    )
    assert "mesco_code" not in master_update
    assert master_update["cr401_totalgrossweight"] == 23272.1
    assert master_update["cr401_totalvolume"] == 63.066
    assert master_update["mesco_direction"] == 300000000
    assert master_update["mesco_transporttype"] == 300000000
    assert master_update["mesco_loadtype"] == 300000001
    assert master_update["mesco_containertype"] == 100000001
    assert "mesco_ShippingLine@odata.bind" in master_update
    assert "mesco_Vessel@odata.bind" in master_update
    assert "transactioncurrencyid@odata.bind" in master_update

    house_updates = [
        payload for entity, payload in operation_updates
        if "00000000-0000-0000-0000-000000000003" not in entity
    ]
    assert len(house_updates) == 10
    assert all("cr401_totalvolume" in payload for payload in house_updates)
    assert all("cr401_totalgrossweight" in payload for payload in house_updates)
    assert all("cr401_totalpackages" in payload for payload in house_updates)
    assert all("mesco_Destination@odata.bind" in payload for payload in house_updates)
    assert all("mesco_Incoterm@odata.bind" in payload for payload in house_updates)
    assert all("mesco_ShippingLine@odata.bind" in payload for payload in house_updates)
    assert all("mesco_Vessel@odata.bind" in payload for payload in house_updates)
    assert all("transactioncurrencyid@odata.bind" in payload for payload in house_updates)
    assert all("mesco_pcfreightterm" not in payload for payload in house_updates)


class _MissingServiceDataverseClient(_FakeDataverseClient):
    def get(self, url: str):
        response = super().get(url)
        if url.startswith("xollsp_servicedefinitions"):
            rows = [
                row for row in response.json()["value"]
                if row["xollsp_name"] != "THC"
            ]
            return _FakeResponse(body={"value": rows})
        return response


def test_excel_posting_is_atomic_when_required_lookup_is_missing() -> None:
    source = _fixture_path()
    fake_client = _MissingServiceDataverseClient()

    with patch("main.DataverseClientService.get_instance", return_value=fake_client):
        with source.open("rb") as stream:
            response = TestClient(app).post(
                "/extract/invoice/excel",
                files={"file": (source.name, stream, "application/vnd.ms-excel")},
                data={"post_to_dataverse": "true"},
            )

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["mapping_validation"]["ready_to_post"] is False
    assert any("service THC: unresolved" in error for error in body["mapping_validation"]["errors"])
    assert body["total_posted"] == 0
    assert body["dataverse_error"].startswith("Posting blocked")
    assert fake_client.posts == []
    assert fake_client.patches == []

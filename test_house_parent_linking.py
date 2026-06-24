import dataverse_uploader as du


class _Response:
    content = b"{}"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_find_existing_house_only_reuses_scoped_or_orphan(monkeypatch):
    calls = []

    def fake_query_first(_client, _entity_set, filter_expr, _select):
        calls.append(filter_expr)
        return None

    monkeypatch.setattr(du, "_query_first", fake_query_first)

    result = du._find_existing_operation(
        object(),
        "NSA26030217",
        is_house=True,
        master_id="614f9a53-d064-f111-a826-000d3abe44c2",
    )

    assert result is None
    assert len(calls) == 2
    assert "_mesco_operation_value eq 614f9a53-d064-f111-a826-000d3abe44c2" in calls[0]
    assert "_mesco_operation_value eq null" in calls[1]


def test_adopt_orphan_houses_patches_parent_lookup():
    class Response:
        def __init__(self, payload):
            self._payload = payload
            self.content = b"{}"

        def json(self):
            return self._payload

    class Client:
        def __init__(self):
            self.patches = []

        def get(self, _query):
            return Response(
                {
                    "value": [
                        {
                            "mesco_operationid": "house-1",
                            "mesco_code": "O-9232",
                            "mesco_masterblno": "NSA26030217",
                        }
                    ]
                }
            )

        def patch(self, path, json):
            self.patches.append((path, json))

    client = Client()
    adopted = du._adopt_orphan_houses_for_master(
        client,
        "master-1",
        "NSA26030217",
    )

    assert adopted == [
        {
            "index": 0,
            "id": "house-1",
            "hbl": "NSA26030217",
            "mbl": "NSA26030217",
            "reused": True,
            "adopted": True,
            "code": "O-9232",
        }
    ]
    assert client.patches == [
        (
            "mesco_operations(house-1)",
            {"mesco_Operation@odata.bind": "/mesco_operations(master-1)"},
        )
    ]


def test_query_many_retries_without_select_when_dataverse_rejects_selected_columns():
    class Client:
        def __init__(self):
            self.queries = []

        def get(self, query):
            self.queries.append(query)
            if "$select=" in query:
                raise RuntimeError("400 Bad Request")
            return _Response({"value": [{"mesco_operationid": "master-1"}]})

    client = Client()
    rows = du._query_many(
        client,
        du._ENTITY,
        "mesco_bltype eq 886150001 and mesco_voytruckno eq '0BEN9W1MA'",
        "bad_select",
        top=50,
    )

    assert rows == [{"mesco_operationid": "master-1"}]
    assert "$select=" in client.queries[0]
    assert "$select=" not in client.queries[1]


def test_find_master_by_shipment_evidence_links_house_without_mbl(monkeypatch):
    def fake_candidate_ids(_client, numbers):
        assert numbers == ["CSNU6873347"]
        return {"master-1"}

    def fake_candidate_rows(_client, _voyage, *, bltype, orphan_house=False):
        assert bltype == du._MASTER_BL_TYPE
        assert orphan_house is False
        return []

    def fake_operation_row(_client, op_id, *, bltype, orphan_house=False):
        assert op_id == "master-1"
        assert bltype == du._MASTER_BL_TYPE
        return {
            "mesco_operationid": "master-1",
            "mesco_masterblno": "COSU6446151350",
            "mesco_voytruckno": "0BEN9W1MA",
            "mesco_etdorigin": "2026-03-16",
            "_mesco_vessel_value@OData.Community.Display.V1.FormattedValue": "CMA CGM SAO PAULO",
            "_mesco_origin_value@OData.Community.Display.V1.FormattedValue": "Shanghai",
            "_mesco_destination_value@OData.Community.Display.V1.FormattedValue": "Alexandria",
        }

    monkeypatch.setattr(du, "_candidate_operation_ids_by_container", fake_candidate_ids)
    monkeypatch.setattr(du, "_candidate_rows_by_voyage", fake_candidate_rows)
    monkeypatch.setattr(du, "_operation_candidate_row", fake_operation_row)

    house_payload = {
        "mesco_masterblno": "TPALX2602005",
        "mesco_vessel": "CMA CGM SAO PAULO",
        "mesco_voytruckno": "0BEN9W1MA",
        "mesco_origin": "Shanghai",
        "mesco_destination": "Alexandria",
        "mesco_etdorigin": "16 Mar 2026",
        "mesco_customerreference": "HRLL20251206",
    }

    assert du._find_master_by_shipment_evidence(
        object(),
        house_payload,
        [{"container_number": "CSNU6873347", "seal_number": "CW794147"}],
    ) == "master-1"


def test_find_master_by_shipment_evidence_can_use_seal_when_master_container_number_missing(monkeypatch):
    def fake_container_ids(_client, numbers):
        assert numbers == ["CSNU6873347"]
        return set()

    def fake_seal_ids(_client, seals):
        assert seals == ["CW794147"]
        return {"master-1"}

    def fake_candidate_rows(_client, _voyage, *, bltype, orphan_house=False):
        assert bltype == du._MASTER_BL_TYPE
        assert orphan_house is False
        return []

    def fake_operation_row(_client, op_id, *, bltype, orphan_house=False):
        assert op_id == "master-1"
        assert bltype == du._MASTER_BL_TYPE
        return {
            "mesco_operationid": "master-1",
            "mesco_masterblno": "COSU6446151350",
            "mesco_etdorigin": "2026-03-16",
            "_mesco_vessel_value@OData.Community.Display.V1.FormattedValue": "CMA CGM SAO PAULO",
            "_mesco_origin_value@OData.Community.Display.V1.FormattedValue": "Shanghai",
            "_mesco_destination_value@OData.Community.Display.V1.FormattedValue": "Alexandria",
        }

    monkeypatch.setattr(du, "_candidate_operation_ids_by_container", fake_container_ids)
    monkeypatch.setattr(du, "_candidate_operation_ids_by_seal", fake_seal_ids)
    monkeypatch.setattr(du, "_candidate_rows_by_voyage", fake_candidate_rows)
    monkeypatch.setattr(du, "_operation_candidate_row", fake_operation_row)

    house_payload = {
        "mesco_masterblno": "TPALX2602005",
        "mesco_vessel": "CMA CGM SAO PAULO",
        "mesco_origin": "SHANGHAI, CHINA",
        "mesco_destination": "ALEXANDRIA, EGYPT",
        "mesco_shippedonboarddate": "16 MAR 2026",
    }

    assert du._find_master_by_shipment_evidence(
        object(),
        house_payload,
        [{"mesco_containernumber": "CSNU687334-7", "mesco_carrierseal": "CW794147"}],
    ) == "master-1"


def test_adopt_orphan_house_by_shipment_evidence(monkeypatch):
    patches = []

    def fake_candidate_ids(_client, numbers):
        assert numbers == ["CSNU6873347"]
        return {"house-1"}

    def fake_candidate_rows(_client, _voyage, *, bltype, orphan_house=False):
        assert bltype == du._HOUSE_BL_TYPE
        assert orphan_house is True
        return []

    def fake_operation_row(_client, op_id, *, bltype, orphan_house=False):
        assert op_id == "house-1"
        assert bltype == du._HOUSE_BL_TYPE
        assert orphan_house is True
        return {
            "mesco_operationid": "house-1",
            "mesco_code": "O-HOUSE",
            "mesco_masterblno": "TPALX2602005",
            "mesco_voytruckno": "0BEN9W1MA",
            "mesco_etdorigin": "2026-03-16",
            "_mesco_vessel_value@OData.Community.Display.V1.FormattedValue": "CMA CGM SAO PAULO",
            "_mesco_origin_value@OData.Community.Display.V1.FormattedValue": "Shanghai",
            "_mesco_destination_value@OData.Community.Display.V1.FormattedValue": "Alexandria",
        }

    def fake_update(_client, entity_set, record_id, fields):
        patches.append((entity_set, record_id, fields))

    monkeypatch.setattr(du, "_candidate_operation_ids_by_container", fake_candidate_ids)
    monkeypatch.setattr(du, "_candidate_rows_by_voyage", fake_candidate_rows)
    monkeypatch.setattr(du, "_operation_candidate_row", fake_operation_row)
    monkeypatch.setattr(du, "_update_entity", fake_update)

    master_payload = {
        "mesco_masterblno": "COSU6446151350",
        "mesco_vessel": "CMA CGM SAO PAULO",
        "mesco_voytruckno": "0BEN9W1MA",
        "mesco_origin": "Shanghai",
        "mesco_destination": "Alexandria",
        "mesco_etdorigin": "16 Mar 2026",
    }

    adopted = du._adopt_orphan_houses_by_shipment_evidence(
        object(),
        "master-1",
        master_payload,
        [{"container_number": "CSNU6873347", "seal_number": "CW794147"}],
    )

    assert adopted[0]["id"] == "house-1"
    assert adopted[0]["hbl"] == "TPALX2602005"
    assert adopted[0]["mbl"] == "COSU6446151350"
    assert "container" in adopted[0]["match_reasons"]
    assert patches == [
        (
            du._ENTITY,
            "house-1",
            {"mesco_Operation@odata.bind": "/mesco_operations(master-1)"},
        )
    ]


def test_upload_reused_standalone_house_patches_parent_after_evidence_match(monkeypatch):
    patches = []

    class FakeClient:
        pass

    monkeypatch.setattr(
        du.DataverseClientService,
        "get_instance",
        classmethod(lambda cls, retry_config=None: FakeClient()),
    )
    monkeypatch.setattr(du, "prepare_standalone_house_upload", lambda payload: None)
    monkeypatch.setattr(du, "_preprocess_payload", lambda payload, entity_set, client: dict(payload))
    monkeypatch.setattr(du, "_upsert_operation", lambda client, fields, **kwargs: ("house-1", True))
    monkeypatch.setattr(du, "_get_operation_parent", lambda client, op_id: None)
    monkeypatch.setattr(du, "_get_operation_lookup_bind", lambda client, op_id, logical_name: None)
    monkeypatch.setattr(du, "_find_master_by_shipment_evidence", lambda client, payload, containers: "master-1")
    monkeypatch.setattr(du, "_update_entity", lambda client, entity_set, op_id, fields: patches.append((entity_set, op_id, fields)))
    monkeypatch.setattr(du, "_find_existing_cargo", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(du, "_dedupe_cargo_for_operation", lambda *args, **kwargs: 0, raising=False)

    result = du.upload_crm_json(
        {
            "mesco_bltype": du._HOUSE_BL_TYPE,
            "mesco_masterblno": "TPALX2602005",
            "mesco_vessel": "CMA CGM SAO PAULO",
        }
    )

    assert result["parent_master_id"] == "master-1"
    assert result["house_linking"] == {
        "status": "linked_after_reuse",
        "parent_master_id": "master-1",
    }
    assert patches == [
        (
            du._ENTITY,
            "house-1",
            {"mesco_Operation@odata.bind": "/mesco_operations(master-1)"},
        )
    ]

import dataverse_uploader as du


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

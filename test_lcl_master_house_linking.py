from crm_output_formatter import LOAD_LCL, records_to_master_json
import dataverse_uploader as uploader


def _consolidated_records():
    return [
        {
            "mesco_masterblno": "NSA26030217",
            "mesco_houseblno": "NAV26MU0714",
            "mesco_loadtype": 300000000,
            "_consolidated_lcl_row": True,
            "mesco_cargodescription": "08 PALLETS STC DRUMS",
            "cr401_totalpackages": "8 PALLETS",
            "cr401_totalgrossweight": "2299.6",
            "cr401_totalvolume": "7.928",
            "container_number": "TRKU4457572",
            "seal_number": "18874",
            "containers": [
                {
                    "container_number": "TRKU4457572",
                    "seal_number": "18874",
                    "container_type": "40HC",
                    "packages": "8 PALLETS",
                    "gross_weight_kg": "2299.6",
                    "measurement_cbm": "7.928",
                }
            ],
        },
        {
            "mesco_masterblno": "NSA26030217",
            "mesco_houseblno": "NAV26MU0821",
            "mesco_loadtype": 300000000,
            "_consolidated_lcl_row": True,
            "mesco_cargodescription": "21 PALLETS STC GOODS",
            "cr401_totalpackages": "21 PALLETS",
            "cr401_totalgrossweight": "8285",
            "cr401_totalvolume": "27.429",
            "container_number": "TRKU4457572",
            "seal_number": "18874",
            "containers": [
                {
                    "container_number": "TRKU4457572",
                    "seal_number": "18874",
                    "container_type": "40HC",
                    "packages": "21 PALLETS",
                    "gross_weight_kg": "8285",
                    "measurement_cbm": "27.429",
                }
            ],
        },
    ]


def test_consolidated_lcl_forces_master_and_houses_to_lcl():
    crm = records_to_master_json(_consolidated_records())

    assert crm["mesco_loadtype"] == LOAD_LCL
    assert crm["mesco_consolidation"] is True

    houses = crm["mesco_Operation_mesco_Operation_mesco_Operation"]
    assert len(houses) == 2
    assert {h["mesco_masterblno"] for h in houses} == {
        "NAV26MU0714",
        "NAV26MU0821",
    }
    assert {h["mesco_masterbllinkno"] for h in houses} == {"NSA26030217"}
    assert all(h["mesco_loadtype"] == LOAD_LCL for h in houses)


def test_upload_payload_links_master_houses_containers_and_cargo(monkeypatch):
    crm = records_to_master_json(_consolidated_records())
    created = []
    container_house_links = []

    class DummyClient:
        base_url = "https://example.crm4.dynamics.com/api/data/v9.2"

    def fake_create(_client, entity_set, fields):
        entity_count = sum(1 for entity, _fields in created if entity == entity_set) + 1
        record_id = f"{entity_set}-{entity_count}"
        created.append((entity_set, dict(fields), record_id))
        return record_id

    def fake_link(_client, house_id, container_id):
        container_house_links.append((house_id, container_id))

    monkeypatch.setattr(
        uploader.DataverseClientService,
        "get_instance",
        staticmethod(lambda _retry: DummyClient()),
    )
    monkeypatch.setattr(uploader, "_preprocess_payload", lambda fields, _entity, _client: fields)
    monkeypatch.setattr(uploader, "_create_entity", fake_create)
    monkeypatch.setattr(uploader, "_link_container_to_house", fake_link)

    result = uploader.upload_crm_json(crm)

    assert result["master_id"] == "mesco_operations-1"
    assert len(result["houses"]) == 2
    assert len(result["containers"]) == 1
    assert len(result["cargo"]) == 2

    house_posts = [fields for entity, fields, _id in created if entity == "mesco_operations"][1:]
    assert all(
        fields["mesco_Operation@odata.bind"] == "/mesco_operations(mesco_operations-1)"
        for fields in house_posts
    )

    container_posts = [fields for entity, fields, _id in created if entity == "mesco_containers"]
    assert container_posts[0]["mesco_MasterOperation@odata.bind"] == "/mesco_operations(mesco_operations-1)"
    assert container_house_links == [
        ("mesco_operations-2", "mesco_containers-1"),
        ("mesco_operations-3", "mesco_containers-1"),
    ]

    cargo_posts = [fields for entity, fields, _id in created if entity == "mesco_cargos"]
    assert cargo_posts[0]["mesco_MasterOperation@odata.bind"] == "/mesco_operations(mesco_operations-1)"
    assert cargo_posts[0]["mesco_HouseOperation@odata.bind"] == "/mesco_operations(mesco_operations-2)"
    assert cargo_posts[1]["mesco_HouseOperation@odata.bind"] == "/mesco_operations(mesco_operations-3)"
    assert all(
        fields["mesco_Conainter@odata.bind"] == "/mesco_containers(mesco_containers-1)"
        for fields in cargo_posts
    )

from dataverse_uploader import (
    _inherit_lookup_bind,
    _lookup_bind_key,
)


def test_house_inherits_master_shippingline_when_blank():
    house = {}
    bind = "/mesco_shippinglines(11111111-1111-1111-1111-111111111111)"

    changed = _inherit_lookup_bind(house, "mesco_shippingline", bind)

    assert changed is True
    assert house[_lookup_bind_key("mesco_ShippingLine")] == bind


def test_house_shippingline_is_not_overwritten():
    house = {"mesco_shippingline": "HOUSE LINE"}
    bind = "/mesco_shippinglines(11111111-1111-1111-1111-111111111111)"

    changed = _inherit_lookup_bind(house, "mesco_shippingline", bind)

    assert changed is False
    assert house == {"mesco_shippingline": "HOUSE LINE"}


def test_uploader_explicitly_overwrites_house_shippingline_on_parent_link(monkeypatch):
    import dataverse_uploader as du

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
    
    monkeypatch.setattr(du, "_find_master_by_shipment_evidence", lambda client, payload, containers: "master-1")
    
    monkeypatch.setattr(
        du, 
        "_get_operation_lookup_bind", 
        lambda client, op_id, logical_name: "/mesco_shippinglines(parent-shippingline-guid)" if logical_name == "mesco_shippingline" else None
    )
    
    monkeypatch.setattr(
        du, 
        "_update_entity", 
        lambda client, entity_set, op_id, fields: patches.append((entity_set, op_id, fields))
    )

    payload = {
        "mesco_bltype": du._HOUSE_BL_TYPE,
        "mesco_masterblno": "TPALX2602005",
        "mesco_shippingline": "TRANS PACIFIC CARGO LIMITED",
    }
    
    result = du.upload_crm_json(payload)
    
    assert result["parent_master_id"] == "master-1"
    assert len(patches) > 0
    target_op_patch = [p for p in patches if p[1] == "house-1"][0]
    patch_fields = target_op_patch[2]
    
    assert patch_fields[du._lookup_bind_key("mesco_ShippingLine")] == "/mesco_shippinglines(parent-shippingline-guid)"


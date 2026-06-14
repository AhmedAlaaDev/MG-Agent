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

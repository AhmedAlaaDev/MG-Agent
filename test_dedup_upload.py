"""Tests for duplicate-safe upsert + house/master linking in dataverse_uploader.

A fake Dataverse client maintains an in-memory store so we can verify that
re-uploading the same master/houses (or uploading a house PDF that already
exists under a master) reuses the existing records instead of creating
duplicates.
"""

import re
import itertools

import dataverse_uploader as du


class _FakeResponse:
    def __init__(self, status_code=200, body=None, location=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = {}
        if location:
            self.headers["Location"] = location
        self.content = b"x"

    def json(self):
        return self._body


def _cond(filter_expr, field):
    m = re.search(rf"{re.escape(field)} eq '([^']*)'", filter_expr)
    if m:
        return m.group(1)
    m = re.search(rf"{re.escape(field)} eq ([0-9a-f\-]+)", filter_expr)
    return m.group(1) if m else None


def _int_cond(filter_expr, field):
    m = re.search(rf"{re.escape(field)} eq (\d+)", filter_expr)
    return int(m.group(1)) if m else None


class FakeClient:
    """Minimal stand-in for DataverseClientService."""

    def __init__(self):
        self.base_url = "https://fake.crm.dynamics.com/api/data/v9.2"
        self.operations = []
        self.containers = []
        self.cargos = []
        self._ids = itertools.count(1)
        self.patches = []

    def _new_id(self):
        n = next(self._ids)
        return f"00000000-0000-0000-0000-{n:012d}"

    # --- GET ---------------------------------------------------------
    def get(self, url, **kwargs):
        set_name = url.split("?", 1)[0]
        filt = ""
        m = re.search(r"\$filter=([^&]+)", url)
        if m:
            from urllib.parse import unquote
            filt = unquote(m.group(1))

        if set_name == "mesco_operations":
            blm = _cond(filt, "mesco_masterblno")
            blh = _cond(filt, "mesco_houseblno")
            bltype = _int_cond(filt, "mesco_bltype")
            parent = _cond(filt, "_mesco_operation_value")
            op_id = _cond(filt, "mesco_operationid")
            rows = []
            for op in self.operations:
                if op_id is not None and op.get("mesco_operationid") != op_id:
                    continue
                if bltype is not None and op.get("mesco_bltype") != bltype:
                    continue
                bl_ok = True
                if blm is not None or blh is not None:
                    bl_ok = (
                        (blm is not None and op.get("mesco_masterblno") == blm)
                        or (blh is not None and op.get("mesco_houseblno") == blh)
                    )
                if not bl_ok:
                    continue
                if parent is not None and op.get("_mesco_operation_value") != parent:
                    continue
                rows.append(dict(op))
            return _FakeResponse(body={"value": rows[:1]})

        if set_name == "mesco_containers":
            num = _cond(filt, "mesco_containernumber")
            master = _cond(filt, "_mesco_masteroperation_value")
            rows = [
                {"mesco_containerid": c["mesco_containerid"]}
                for c in self.containers
                if c.get("mesco_containernumber") == num
                and (master is None or c.get("_mesco_masteroperation_value") == master)
            ]
            return _FakeResponse(body={"value": rows[:1]})

        if set_name == "mesco_cargos":
            master = _cond(filt, "_mesco_masteroperation_value")
            house = _cond(filt, "_mesco_houseoperation_value")
            rows = []
            for c in self.cargos:
                if master is not None and c.get("_mesco_masteroperation_value") != master:
                    continue
                if house is not None and c.get("_mesco_houseoperation_value") != house:
                    continue
                rows.append({
                    "mesco_cargoid": c.get("mesco_cargoid"),
                    "mesco_descriptionofgoods": c.get("mesco_descriptionofgoods"),
                    "mesco_noofpackages": c.get("mesco_noofpackages"),
                    "mesco_grosskg": c.get("mesco_grosskg"),
                    "mesco_volcbm": c.get("mesco_volcbm"),
                })
            return _FakeResponse(body={"value": rows})

        # lookups (accounts, addresses, ...) → no match so the field is dropped
        return _FakeResponse(body={"value": []})

    # --- POST --------------------------------------------------------
    def post(self, url, json=None, **kwargs):
        json = json or {}
        # N:N association ($ref) — ignore
        if "/$ref" in url:
            return _FakeResponse(status_code=204)

        set_name = url.split("?", 1)[0].split("(", 1)[0]
        if set_name == "mesco_operations":
            guid = self._new_id()
            rec = dict(json)
            rec.update({
                "mesco_operationid": guid,
                "mesco_masterblno": json.get("mesco_masterblno"),
                "mesco_houseblno": json.get("mesco_houseblno"),
                "mesco_bltype": json.get("mesco_bltype"),
                "_mesco_operation_value": _bind_id(json.get("mesco_Operation@odata.bind")),
            })
            self.operations.append(rec)
            return _FakeResponse(status_code=204, location=f"/mesco_operations({guid})")

        if set_name == "mesco_containers":
            guid = self._new_id()
            self.containers.append({
                "mesco_containerid": guid,
                "mesco_containernumber": json.get("mesco_containernumber"),
                "_mesco_masteroperation_value": _bind_id(json.get("mesco_MasterOperation@odata.bind")),
            })
            return _FakeResponse(status_code=204, location=f"/mesco_containers({guid})")

        if set_name == "mesco_cargos":
            guid = self._new_id()
            self.cargos.append({
                "mesco_cargoid": guid,
                "mesco_descriptionofgoods": json.get("mesco_descriptionofgoods"),
                "mesco_noofpackages": json.get("mesco_noofpackages"),
                "mesco_grosskg": json.get("mesco_grosskg"),
                "mesco_volcbm": json.get("mesco_volcbm"),
                "_mesco_masteroperation_value": _bind_id(json.get("mesco_MasterOperation@odata.bind")),
                "_mesco_houseoperation_value": _bind_id(json.get("mesco_HouseOperation@odata.bind")),
            })
            return _FakeResponse(status_code=204, location=f"/mesco_cargos({guid})")

        return _FakeResponse(status_code=204, location="/x(00000000-0000-0000-0000-000000000999)")

    # --- PATCH -------------------------------------------------------
    def patch(self, url, json=None, **kwargs):
        self.patches.append((url, json))
        m = re.search(r"mesco_cargos\(([^)]+)\)", url)
        if m and json:
            guid = m.group(1)
            for c in self.cargos:
                if c.get("mesco_cargoid") == guid:
                    c.update(json)
                    break
        m = re.search(r"mesco_operations\(([^)]+)\)", url)
        if m and json:
            guid = m.group(1)
            for o in self.operations:
                if o.get("mesco_operationid") == guid:
                    if "mesco_Operation@odata.bind" in json:
                        o["_mesco_operation_value"] = _bind_id(json["mesco_Operation@odata.bind"])
                    o.update(json)
                    break
        return _FakeResponse(status_code=204)

    # --- DELETE ------------------------------------------------------
    def delete(self, url, **kwargs):
        m = re.search(r"mesco_cargos\(([^)]+)\)", url)
        if m:
            guid = m.group(1)
            self.cargos = [c for c in self.cargos if c.get("mesco_cargoid") != guid]
        return _FakeResponse(status_code=204)


class LookupFailingPatchClient(FakeClient):
    """Fake client that rejects one party lookup PATCH like Dataverse can."""

    def patch(self, url, json=None, **kwargs):
        if json and "mesco_Consignee@odata.bind" in json:
            self.patches.append((url, json))
            raise RuntimeError("lookup bind rejected")
        return super().patch(url, json=json, **kwargs)


def _bind_id(bind):
    if not bind or not isinstance(bind, str):
        return None
    m = re.search(r"\(([^)]+)\)", bind)
    return m.group(1) if m else None


def _master_crm():
    return {
        "mesco_bltype": du._MASTER_BL_TYPE,
        "mesco_masterblno": "2311318",
        "mesco_Operation_mesco_Operation_mesco_Operation": [
            {
                "mesco_bltype": du._HOUSE_BL_TYPE,
                "mesco_masterblno": "TPSALX20260112",
                "mesco_masterbllinkno": "2311318",
            },
            {
                "mesco_bltype": du._HOUSE_BL_TYPE,
                "mesco_masterblno": "TPSALX20260307",
                "mesco_masterbllinkno": "2311318",
            },
        ],
        "mesco_Container_MasterOperation_mesco_Operation": [],
        "mesco_Cargo_MasterOperation_mesco_Operation": [
            {"mesco_descriptionofgoods": "IC902 CONTROLLER OF PCB", "mesco_noofpackages": 74, "mesco_grosskg": 961.8},
            {"mesco_descriptionofgoods": "SOLID WAX HS 3407001000", "mesco_noofpackages": 2, "mesco_grosskg": 779.0},
        ],
    }


def _patch_client(monkeypatch, fake):
    monkeypatch.setattr(du.DataverseClientService, "get_instance", staticmethod(lambda *a, **k: fake))
    monkeypatch.setattr(du, "clear_lookup_cache", lambda: None)


def test_reused_house_refreshes_display_fields_when_lookup_patch_fails():
    """A failed lookup bind must not leave a reused house with stale parties."""
    fake = LookupFailingPatchClient()
    master_id = "11111111-1111-1111-1111-111111111111"
    house_id = "22222222-2222-2222-2222-222222222222"
    fake.operations.append({
        "mesco_operationid": house_id,
        "mesco_masterblno": "TPALX2603001",
        "mesco_bltype": du._HOUSE_BL_TYPE,
        "_mesco_operation_value": None,
        "mesco_consigneenamecontactno": "",
        "mesco_shippernamecontactno": "CARRIER: TRANS",
    })

    op_id, reused = du._upsert_operation(
        fake,
        {
            "mesco_masterblno": "TPALX2603001",
            "mesco_consigneenamecontactno": "NILE TRADING COMPANY",
            "mesco_consigneeaddress": "26 NAGYB EL REHANY ST CAIRO, EGYPT",
            "mesco_shippernamecontactno": "LANBO TONGCHUANG APPLIANCE CO.,LTD",
            "mesco_Consignee@odata.bind": "/accounts(33333333-3333-3333-3333-333333333333)",
            "mesco_Operation@odata.bind": f"/mesco_operations({master_id})",
        },
        bl_no="TPALX2603001",
        is_house=True,
        master_id=master_id,
    )

    assert reused is True
    assert op_id == house_id
    refreshed = fake.operations[0]
    assert refreshed["mesco_consigneenamecontactno"] == "NILE TRADING COMPANY"
    assert refreshed["mesco_shippernamecontactno"] == "LANBO TONGCHUANG APPLIANCE CO.,LTD"
    assert refreshed["_mesco_operation_value"] == master_id
    scalar_patches = [payload for _, payload in fake.patches if payload and not any(k.endswith("@odata.bind") for k in payload)]
    assert scalar_patches
    assert scalar_patches[0]["mesco_consigneenamecontactno"] == "NILE TRADING COMPANY"
    assert any(
        payload == {"mesco_Consignee@odata.bind": "/accounts(33333333-3333-3333-3333-333333333333)"}
        for _, payload in fake.patches
    )


def test_master_ata_pod_propagates_to_existing_linked_house(monkeypatch):
    fake = FakeClient()
    _patch_client(monkeypatch, fake)
    master_id = "11111111-1111-1111-1111-111111111111"
    house_id = "22222222-2222-2222-2222-222222222222"
    fake.operations.extend([
        {
            "mesco_operationid": master_id,
            "mesco_masterblno": "MBL-ATA-1",
            "mesco_bltype": du._MASTER_BL_TYPE,
            "_mesco_operation_value": None,
        },
        {
            "mesco_operationid": house_id,
            "mesco_masterblno": "HBL-ATA-1",
            "mesco_bltype": du._HOUSE_BL_TYPE,
            "_mesco_operation_value": master_id,
        },
    ])

    du.upload_crm_json({
        "mesco_bltype": du._MASTER_BL_TYPE,
        "mesco_masterblno": "MBL-ATA-1",
        "mesco_atadestination": "18/04/2026",
    })

    house = next(op for op in fake.operations if op["mesco_operationid"] == house_id)
    assert house["mesco_atadestination"] == "2026-04-18"


def test_standalone_house_inherits_ata_pod_from_matched_master(monkeypatch):
    fake = FakeClient()
    _patch_client(monkeypatch, fake)
    master_id = "11111111-1111-1111-1111-111111111111"
    fake.operations.append({
        "mesco_operationid": master_id,
        "mesco_masterblno": "MBL-ATA-2",
        "mesco_bltype": du._MASTER_BL_TYPE,
        "_mesco_operation_value": None,
        "mesco_atadestination": "2026-04-18",
    })

    result = du.upload_crm_json({
        "mesco_bltype": du._HOUSE_BL_TYPE,
        "mesco_masterblno": "HBL-ATA-2",
        "mesco_masterbllinkno": "MBL-ATA-2",
    })

    house_id = result["master_id"]
    house = next(op for op in fake.operations if op["mesco_operationid"] == house_id)
    assert house["_mesco_operation_value"] == master_id
    assert house["mesco_atadestination"] == "2026-04-18"


def test_first_upload_creates_then_second_reuses(monkeypatch):
    fake = FakeClient()
    _patch_client(monkeypatch, fake)

    first = du.upload_crm_json(_master_crm())
    assert first["master_reused"] is False
    assert len(first["houses"]) == 2
    assert all(not h["reused"] for h in first["houses"])
    assert len(first["cargo"]) == 2
    assert first["skipped_cargo"] == 0
    assert len(fake.operations) == 3   # 1 master + 2 houses
    assert len(fake.cargos) == 2

    # Re-upload the exact same data — nothing should be duplicated.
    second = du.upload_crm_json(_master_crm())
    assert second["master_reused"] is True
    assert all(h["reused"] for h in second["houses"])
    assert second["skipped_cargo"] == 2
    assert len(second["cargo"]) == 0
    assert len(fake.operations) == 3   # unchanged
    assert len(fake.cargos) == 2       # unchanged


def test_houses_link_to_master(monkeypatch):
    fake = FakeClient()
    _patch_client(monkeypatch, fake)
    du.upload_crm_json(_master_crm())
    master = next(o for o in fake.operations if o["mesco_bltype"] == du._MASTER_BL_TYPE)
    houses = [o for o in fake.operations if o["mesco_bltype"] == du._HOUSE_BL_TYPE]
    assert len(houses) == 2
    for h in houses:
        assert h["_mesco_operation_value"] == master["mesco_operationid"]


def test_house_pdf_relinks_to_existing_master(monkeypatch):
    """A standalone house PDF for an HBL already under a master reuses it."""
    fake = FakeClient()
    _patch_client(monkeypatch, fake)

    # Master + houses already in Dataverse (e.g. from the Excel upload).
    du.upload_crm_json(_master_crm())
    house_before = next(
        o for o in fake.operations
        if o.get("mesco_masterblno") == "TPSALX20260307"
    )

    # Now upload the OHBL-307.pdf as a standalone house B/L.
    pdf_house = {
        "mesco_bltype": du._HOUSE_BL_TYPE,
        "mesco_houseblno": "TPSALX20260307",
        "mesco_masterblno": "TPSALX20260307",
        "mesco_Cargo_HouseOperation_mesco_Operation": [
            {"mesco_descriptionofgoods": "SOLID WAX HS 3407001000", "mesco_noofpackages": 2, "mesco_grosskg": 779.0},
        ],
    }
    result = du.upload_crm_json(pdf_house)

    # It must reuse the existing house record (no new operation, no duplicate cargo).
    assert result["master_reused"] is True
    assert result["master_id"] == house_before["mesco_operationid"]
    assert result["skipped_cargo"] == 1
    assert len([o for o in fake.operations if o["mesco_bltype"] == du._HOUSE_BL_TYPE]) == 2


def test_same_cargo_different_description_is_deduped(monkeypatch):
    """The same physical cargo (same qty) must not duplicate when the
    description text differs across sources, and the best description wins."""
    fake = FakeClient()
    _patch_client(monkeypatch, fake)

    base = {
        "mesco_bltype": du._HOUSE_BL_TYPE,
        "mesco_houseblno": "TPSALX20260305",
        "mesco_masterblno": "TPSALX20260305",
    }
    # First upload: manifest-style short description.
    du.upload_crm_json({
        **base,
        "mesco_Cargo_HouseOperation_mesco_Operation": [
            {"mesco_descriptionofgoods": "AUTOMOBILE PARTS",
             "mesco_noofpackages": 4, "mesco_grosskg": 783.33, "mesco_volcbm": 4.809},
        ],
    })
    assert len(fake.cargos) == 1

    # Second upload: B/L OCR gives a different + fuller description, plus an
    # empty-description row — both are the SAME cargo (same qty).
    result = du.upload_crm_json({
        **base,
        "mesco_Cargo_HouseOperation_mesco_Operation": [
            {"mesco_descriptionofgoods": "SAID TO CONTAINE:4 CASES IN TOTAL AUTOMOBILE PARTS",
             "mesco_noofpackages": 4, "mesco_grosskg": 783.33, "mesco_volcbm": 4.809},
            {"mesco_descriptionofgoods": "",
             "mesco_noofpackages": 4, "mesco_grosskg": 783.33, "mesco_volcbm": 4.809},
        ],
    })

    # No duplicate cargo rows were created.
    assert len(fake.cargos) == 1
    assert result["skipped_cargo"] == 2
    # The fuller description was patched onto the kept row.
    assert any(
        "SAID TO CONTAINE" in (p[1] or {}).get("mesco_descriptionofgoods", "")
        for p in fake.patches
    )


def test_house_cargo_different_gross_updates_not_duplicates(monkeypatch):
    """Re-uploading a house with different gross weight must update, not duplicate."""
    fake = FakeClient()
    _patch_client(monkeypatch, fake)

    du.upload_crm_json(_master_crm())
    assert len(fake.cargos) == 2

    house_pdf = {
        "mesco_bltype": du._HOUSE_BL_TYPE,
        "mesco_houseblno": "TPSALX20260112",
        "mesco_masterblno": "TPSALX20260112",
        "mesco_masterbllinkno": "2311318",
        "mesco_Cargo_HouseOperation_mesco_Operation": [
            {
                "mesco_descriptionofgoods": "IC902 CONTROLLER OF PCB — revised",
                "mesco_noofpackages": 74,
                "mesco_grosskg": 1000.0,
                "mesco_volcbm": 4.5,
            },
        ],
    }
    result = du.upload_crm_json(house_pdf)
    assert len(fake.cargos) == 2
    assert result["skipped_cargo"] == 1
    updated = next(
        c for c in fake.cargos
        if c.get("_mesco_houseoperation_value")
        and c.get("mesco_grosskg") == 1000.0
    )
    assert "revised" in (updated.get("mesco_descriptionofgoods") or "")


def test_manifest_cargo_links_by_house_hbl_not_index(monkeypatch):
    """Cargo rows tagged with _house_hbl must bind to the matching house."""
    fake = FakeClient()
    _patch_client(monkeypatch, fake)

    payload = {
        "mesco_bltype": du._MASTER_BL_TYPE,
        "mesco_masterblno": "NSA26030217",
        "mesco_Operation_mesco_Operation_mesco_Operation": [
            {
                "mesco_bltype": du._HOUSE_BL_TYPE,
                "mesco_masterblno": "NAV26MU0714",
                "mesco_masterbllinkno": "NSA26030217",
            },
            {
                "mesco_bltype": du._HOUSE_BL_TYPE,
                "mesco_masterblno": "NAV26MU0821",
                "mesco_masterbllinkno": "NSA26030217",
            },
        ],
        "mesco_Container_MasterOperation_mesco_Operation": [
            {"mesco_containernumber": "TRKU4457572"},
        ],
        "mesco_Cargo_MasterOperation_mesco_Operation": [
            {
                "_house_hbl": "NAV26MU0821",
                "mesco_descriptionofgoods": "CARVACROL OIL",
                "mesco_noofpackages": 320,
                "mesco_grosskg": 9200,
                "mesco_volcbm": 27.085,
            },
            {
                "_house_hbl": "NAV26MU0714",
                "mesco_descriptionofgoods": "TRIMETHOPRIM EP",
                "mesco_noofpackages": 80,
                "mesco_grosskg": 2299.6,
                "mesco_volcbm": 7.928,
            },
        ],
    }
    du.upload_crm_json(payload)
    houses = {
        o["mesco_masterblno"]: o["mesco_operationid"]
        for o in fake.operations
        if o.get("mesco_bltype") == du._HOUSE_BL_TYPE
    }
    cargos_by_house = {
        c["_mesco_houseoperation_value"]: c for c in fake.cargos
    }
    assert cargos_by_house[houses["NAV26MU0714"]]["mesco_grosskg"] == 2299.6
    assert cargos_by_house[houses["NAV26MU0821"]]["mesco_grosskg"] == 9200


def test_house_cargo_upsert_replaces_wrong_gross_and_syncs_totals(monkeypatch):
    """Wrong gross on an existing house cargo must be corrected on re-upload."""
    fake = FakeClient()
    _patch_client(monkeypatch, fake)

    payload = {
        "mesco_bltype": du._MASTER_BL_TYPE,
        "mesco_masterblno": "NSA26030217",
        "mesco_loadtype": 300000001,
        "mesco_Operation_mesco_Operation_mesco_Operation": [
            {
                "mesco_bltype": du._HOUSE_BL_TYPE,
                "mesco_masterblno": "NAV26MU0794",
                "mesco_masterbllinkno": "NSA26030217",
            },
        ],
        "mesco_Container_MasterOperation_mesco_Operation": [
            {"mesco_containernumber": "TRKU4457572"},
        ],
        "mesco_Cargo_MasterOperation_mesco_Operation": [
            {
                "_house_hbl": "NAV26MU0794",
                "mesco_descriptionofgoods": "WRONG ROW",
                "mesco_noofpackages": 7,
                "mesco_grosskg": 9200,
                "mesco_volcbm": 6.85,
            },
        ],
    }
    du.upload_crm_json(payload)
    assert len(fake.cargos) == 1
    assert fake.cargos[0]["mesco_grosskg"] == 9200

    payload["mesco_Cargo_MasterOperation_mesco_Operation"] = [
        {
            "_house_hbl": "NAV26MU0794",
            "mesco_descriptionofgoods": "MOULDED RUBBER PRODUCTS",
            "mesco_noofpackages": 7,
            "mesco_grosskg": 2019.15,
            "mesco_volcbm": 6.85,
        },
    ]
    du.upload_crm_json(payload)
    assert len(fake.cargos) == 1
    assert fake.cargos[0]["mesco_grosskg"] == 2019.15
    master = next(o for o in fake.operations if o.get("mesco_bltype") == du._MASTER_BL_TYPE)
    assert master.get("cr401_totalgrossweight") == 2019.15
    assert master.get("cr401_totalpackages") == 7


def test_master_upload_normalizes_container_and_keeps_voyage(monkeypatch):
    fake = FakeClient()
    _patch_client(monkeypatch, fake)

    result = du.upload_crm_json(
        {
            "mesco_bltype": du._MASTER_BL_TYPE,
            "mesco_masterblno": "COSU6446151350",
            "mesco_voytruckno": "0BEN9W1MA",
            "mesco_Container_MasterOperation_mesco_Operation": [
                {
                    "mesco_containernumber": "CSNU687334-7",
                    "mesco_containerno": "CSNU687334-7",
                    "mesco_name": "CSNU687334-7",
                    "mesco_containertype": "40HC",
                }
            ],
        }
    )

    master = next(o for o in fake.operations if o.get("mesco_bltype") == du._MASTER_BL_TYPE)
    assert master["mesco_voytruckno"] == "0BEN9W1MA"
    assert fake.containers[0]["mesco_containernumber"] == "CSNU6873347"
    assert result["containers"][0]["container_no"] == "CSNU6873347"


def test_deduplicate_false_always_creates(monkeypatch):
    fake = FakeClient()
    _patch_client(monkeypatch, fake)
    du.upload_crm_json(_master_crm())
    du.upload_crm_json(_master_crm(), deduplicate=False)
    # Second run created everything again.
    assert len(fake.operations) == 6
    assert len(fake.cargos) == 4


if __name__ == "__main__":
    import pytest, sys
    sys.exit(pytest.main([__file__, "-v"]))

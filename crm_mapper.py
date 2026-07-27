import re
from typing import Any, Dict, List, Optional


def _fv(data: dict, field: str) -> Optional[str]:
    """Get the CRM display value — try direct, lookup, then raw field."""
    # OptionSet/numeric: field@OData...
    val = data.get(f"{field}@OData.Community.Display.V1.FormattedValue")
    if val is not None:
        return str(val)
    # Lookup: _field_value@OData...
    val = data.get(f"_{field}_value@OData.Community.Display.V1.FormattedValue")
    if val is not None:
        return str(val)
    # Raw value fallback
    raw = data.get(field)
    if raw is not None:
        return str(raw)
    return None


def _clean(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _date(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", val)
    return m.group(1) if m else val[:10]


def map_crm_operation_to_records(crm_data: dict) -> List[Dict[str, Any]]:
    # Detect format: house-list (value array with nested mesco_Operation per entry)
    # vs single master operation (flat object with mesco_Operation_* arrays)
    if (
        "value" in crm_data
        and isinstance(crm_data["value"], list)
        and crm_data["value"]
        and "mesco_Operation" in crm_data["value"][0]
    ):
        return _map_house_list_format(crm_data)

    """
    Map a Dynamics CRM mesco_operation JSON (with nested houses, containers,
    cargo) to a list of BLEntity-compatible dicts — one per house operation.

    Every output record merges three layers:
      master-level  – vessel, port pair, shipping line, agent, master BL
      house-level   – consignee, shipper, ACID, incoterm, house BL, finances
      cargo-level   – packages, gross weight, volume, cargo description
    plus the single physical container that all share.
    """
    master = crm_data

    # ------------------------------------------------------------------
    # 1.  Container  (shared across all houses)
    # ------------------------------------------------------------------
    containers_raw = master.get("mesco_Container_MasterOperation_mesco_Operation") or []
    cntr: Dict[str, Any] = {}
    if containers_raw:
        c = containers_raw[0]
        cntr = {
            "container_number": _fv(c, "mesco_containerno") or _clean(c.get("mesco_containerno")),
            "seal_number":       _fv(c, "mesco_carrierseal") or _clean(c.get("mesco_carrierseal")),
            "container_type":    _fv(c, "mesco_containertype") or _fv(c, "mesco_um"),
            "_name":             _clean(c.get("mesco_name")),
        }

    # ------------------------------------------------------------------
    # 2.  Cargo lookup  by house-operation GUID
    # ------------------------------------------------------------------
    cargo_items = master.get("mesco_Cargo_MasterOperation_mesco_Operation") or []
    cargo_by_house: Dict[str, List[dict]] = {}
    for cargo in cargo_items:
        guid = cargo.get("_mesco_houseoperation_value")
        if guid:
            cargo_by_house.setdefault(guid, []).append(cargo)

    # ------------------------------------------------------------------
    # 3.  House lookup  by GUID
    # ------------------------------------------------------------------
    houses = master.get("mesco_Operation_mesco_Operation_mesco_Operation") or []
    houses_by_guid: Dict[str, dict] = {}
    for h in houses:
        g = h.get("mesco_operationid")
        if g:
            houses_by_guid[g] = h

    # ------------------------------------------------------------------
    # 4.  Master-level common fields
    # ------------------------------------------------------------------
    master_bl     = _clean(master.get("mesco_masterblno"))
    master_vessel = _fv(master, "mesco_vessel")
    master_origin = _fv(master, "mesco_origin")
    master_dest   = _fv(master, "mesco_destination")
    master_etd    = _date(_clean(master.get("mesco_etdorigin")))
    master_eta    = _date(_clean(master.get("mesco_atadestination")))

    # ------------------------------------------------------------------
    # 5.  Build one record per house (or fallback to single master record)
    # ------------------------------------------------------------------
    records: List[Dict[str, Any]] = []

    if not houses:
        # Edge case – no houses; emit a single master-level record
        cargo_ctns = []
        for cargo in cargo_items:
            cargo_ctns.append({
                "container_number": cntr.get("container_number"),
                "seal_number":      cntr.get("seal_number"),
                "container_type":   cntr.get("container_type"),
                "packages":         _clean(cargo.get("mesco_noofpackages")),
                "gross_weight_kg":  _clean(cargo.get("mesco_grosskg")),
                "measurement_cbm":  _clean(cargo.get("mesco_volcbm")),
            })

        rec = _build_record(
            master=master, house=None, linked_cargo=cargo_items,
            container=cntr, master_bl=master_bl, master_vessel=master_vessel,
            master_origin=master_origin, master_dest=master_dest,
            master_etd=master_etd, master_eta=master_eta,
            cargo_containers=cargo_ctns,
        )
        records.append(rec)
        return records

    for house in houses:
        house_guid = house.get("mesco_operationid")
        linked_cargo = cargo_by_house.get(house_guid, [])

        cargo_ctns = []
        for cargo in linked_cargo:
            cargo_ctns.append({
                "container_number": cntr.get("container_number"),
                "seal_number":      cntr.get("seal_number"),
                "container_type":   cntr.get("container_type"),
                "packages":         _clean(cargo.get("mesco_noofpackages")),
                "gross_weight_kg":  _clean(cargo.get("mesco_grosskg")),
                "measurement_cbm":  _clean(cargo.get("mesco_volcbm")),
            })

        if not cargo_ctns and cntr.get("container_number"):
            cargo_ctns.append({
                "container_number": cntr.get("container_number"),
                "seal_number":      cntr.get("seal_number"),
                "container_type":   cntr.get("container_type"),
                "packages":         _clean(house.get("cr401_totalpackages")),
                "gross_weight_kg":  _clean(house.get("cr401_totalgrossweight")),
                "measurement_cbm":  _clean(house.get("cr401_totalvolume")),
            })

        rec = _build_record(
            master=master, house=house, linked_cargo=linked_cargo,
            container=cntr, master_bl=master_bl, master_vessel=master_vessel,
            master_origin=master_origin, master_dest=master_dest,
            master_etd=master_etd, master_eta=master_eta,
            cargo_containers=cargo_ctns,
        )
        records.append(rec)

    return records


def _map_house_list_format(data: dict) -> List[Dict[str, Any]]:
    """Map a house-level ``value[]`` CRM JSON to BLEntity records.

    Each entry in ``value[]`` contains:
      - ``mesco_Operation``                – nested master operation
      - ``mesco_Container_mesco_houses[]``  – containers for this house
      - ``mesco_Cargo_HouseOperation_mesco_Operation[]`` – cargo items
    """
    records: List[Dict[str, Any]] = []

    for entry in data.get("value") or []:
        master = entry.get("mesco_Operation") or {}

        # -- Master-level fields from nested master operation --
        master_bl     = _clean(master.get("mesco_masterblno"))
        master_vessel = _fv(master, "mesco_vessel")
        master_origin = _fv(master, "mesco_origin")
        master_dest   = _fv(master, "mesco_destination")
        master_etd    = _date(_clean(master.get("mesco_etdorigin")))
        master_eta    = _date(_clean(master.get("mesco_atadestination")))

        # -- Containers for THIS house --
        containers_raw = entry.get("mesco_Container_mesco_houses") or []
        containers_by_guid: Dict[str, dict] = {}
        for c in containers_raw:
            g = c.get("mesco_containerid") or c.get("mesco_operationid")
            if g:
                containers_by_guid[g] = c

        # -- Cargo items (already owned by this house) --
        cargo_items = entry.get("mesco_Cargo_HouseOperation_mesco_Operation") or []

        # Build cargo_containers — pair each cargo item with its container
        cargo_ctns = []
        for cargo in cargo_items:
            cguid = cargo.get("_mesco_conainter_value") or cargo.get("_mesco_container_value")
            container = containers_by_guid.get(cguid, containers_raw[0] if containers_raw else {})
            cargo_ctns.append({
                "container_number": _fv(container, "mesco_containerno"),
                "seal_number":      _fv(container, "mesco_carrierseal") or _clean(container.get("mesco_carrierseal")),
                "container_type":   _fv(container, "mesco_containertype") or _fv(container, "mesco_um"),
                "packages":         _clean(cargo.get("mesco_noofpackages")),
                "gross_weight_kg":  _clean(cargo.get("mesco_grosskg")),
                "measurement_cbm":  _clean(cargo.get("mesco_volcbm")),
            })

        # Fallback: no cargo items but containers exist → one row per container
        if not cargo_ctns and containers_raw:
            for c in containers_raw:
                cargo_ctns.append({
                    "container_number": _fv(c, "mesco_containerno"),
                    "seal_number":      _fv(c, "mesco_carrierseal") or _clean(c.get("mesco_carrierseal")),
                    "container_type":   _fv(c, "mesco_containertype") or _fv(c, "mesco_um"),
                    "packages":         _clean(entry.get("cr401_totalpackages")),
                    "gross_weight_kg":  _clean(entry.get("cr401_totalgrossweight")),
                    "measurement_cbm":  _clean(entry.get("cr401_totalvolume")),
                })

        # Flat container fields for _build_record
        fc = containers_raw[0] if containers_raw else {}
        cntr = {
            "container_number": _fv(fc, "mesco_containerno"),
            "seal_number":      _fv(fc, "mesco_carrierseal") or _clean(fc.get("mesco_carrierseal")),
            "container_type":   _fv(fc, "mesco_containertype") or _fv(fc, "mesco_um"),
            "_name":            _clean(fc.get("mesco_name")),
        }

        rec = _build_record(
            master=master, house=entry, linked_cargo=cargo_items,
            container=cntr, master_bl=master_bl, master_vessel=master_vessel,
            master_origin=master_origin, master_dest=master_dest,
            master_etd=master_etd, master_eta=master_eta,
            cargo_containers=cargo_ctns,
        )
        records.append(rec)

    return records


def _build_record(
    master: dict, house: Optional[dict], linked_cargo: List[dict],
    container: Dict[str, Any],
    master_bl: Optional[str], master_vessel: Optional[str],
    master_origin: Optional[str], master_dest: Optional[str],
    master_etd: Optional[str], master_eta: Optional[str],
    cargo_containers: List[dict],
) -> Dict[str, Any]:
    """Assemble a single BLEntity-shaped record from master/house/cargo data."""

    # -- House-level fields (or master fallback if house is None) --
    src = house or master

    # House BL number: for a house, mesco_masterblno IS the house BL
    house_bl = _clean(src.get("mesco_masterblno"))

    # Determine BL type: default to House (886150002) for house records
    bl_type = src.get("mesco_bltype")
    if bl_type is None and house is not None:
        bl_type = 886150002  # House

    # Telex release from BL status
    bl_status = src.get("mesco_blstatus")
    telex = (bl_status == 886150001)

    # Cargo descriptions
    cargo_descs: List[str] = []
    for cargo in linked_cargo:
        desc = _clean(cargo.get("mesco_descriptionofgoods"))
        if desc and desc not in cargo_descs:
            cargo_descs.append(desc)
    cargo_desc_str = " | ".join(cargo_descs) if cargo_descs else _clean(src.get("mesco_cargodescription"))

    # Default transport-type / load-type / direction from master
    transport = master.get("mesco_transporttype")
    load_type = master.get("mesco_loadtype")
    direction = master.get("mesco_direction")

    rec = {
        "document_type": "Bill of Lading",

        # -- BL identifiers --
        "mesco_masterblno":     master_bl,
        "mesco_houseblno":      house_bl,
        "mesco_bookingnumber":  _clean(src.get("mesco_bookingnumber")) or _clean(master.get("mesco_bookingnumber")),
        "mesco_acidnumber":     _clean(src.get("mesco_acidnumber")),

        # -- Parties --
        "mesco_shippernamecontactno":  _clean(src.get("mesco_shippernamecontactno")),
        "mesco_shipperaddress":        _clean(src.get("mesco_shipperaddress")),
        "mesco_consigneenamecontactno": _fv(src, "mesco_consignee"),
        "mesco_consigneeaddress":       _clean(src.get("mesco_consigneeaddress")),
        "mesco_notify1":                _fv(src, "mesco_notify1"),
        "mesco_notifyaddress":          _clean(src.get("mesco_notifyaddress")),

        # -- Transport --
        "mesco_vessel":      master_vessel,
        "mesco_voytruckno":  _clean(master.get("mesco_voytruckno")) or _clean(src.get("mesco_voytruckno")),
        "mesco_origin":      _fv(src, "mesco_origin") or master_origin,
        "mesco_destination": _fv(src, "mesco_destination") or master_dest,
        "mesco_transhipmentport": _fv(src, "mesco_transhipmentport") or _fv(master, "mesco_transhipmentport"),

        # -- Cargo summary --
        "mesco_cargodescription":               cargo_desc_str,
        "mesco_hscode":                         _clean(src.get("mesco_hscode")) or _clean(master.get("mesco_hscode")),
        "cr401_totalgrossweight":               _clean(src.get("cr401_totalgrossweight")),
        "cr401_totalvolume":                    _clean(src.get("cr401_totalvolume")),
        "cr401_totalpackages":                  _clean(src.get("cr401_totalpackages")),
        "mesco_nooforgbls":                     _clean(master.get("mesco_nooforgbls")),

        # -- Container --
        "mesco_containertype":  container.get("container_type"),
        "mesco_containertype2": _fv(src, "mesco_containertype2") or _fv(master, "mesco_containertype2"),
        "mesco_containertype3": _fv(src, "mesco_containertype3") or _fv(master, "mesco_containertype3"),
        "container_number":    container.get("container_number"),
        "seal_number":         container.get("seal_number"),

        # -- Freight / terms --
        "mesco_handlinginformation": _clean(master.get("mesco_handlinginformation")) or _clean(src.get("mesco_handlinginformation")),
        "mesco_freightpayableat":    _clean(master.get("mesco_freightpayableat")) or _clean(src.get("mesco_freightpayableat")),
        "mesco_ponumber":            _clean(src.get("mesco_ponumber")),
        "mesco_customerreference":   _clean(src.get("mesco_customerreference")),
        "mesco_pcfreightterm":       _fv(src, "mesco_pcfreightterm"),
        "mesco_incoterm":            _fv(src, "mesco_incoterm"),
        "mesco_telexrelease":        telex,
        "mesco_importerstaxno":                  _clean(src.get("mesco_importerstaxno")),
        "mesco_foreignsupplierregistrationnumber": _clean(src.get("mesco_foreignsupplierregistrationnumber")),

        # -- Classifications --
        "mesco_bltype":       bl_type,
        "mesco_transporttype": transport,
        "mesco_loadtype":     load_type,
        "mesco_direction":    direction,
        "cr401_totalteus":    _clean(src.get("cr401_totalteus")) or _clean(master.get("cr401_totalteus")),
        "mesco_imoclass":     _clean(src.get("mesco_imoclass")),
        "mesco_unnumber":     _clean(src.get("mesco_unnumber")),

        # -- Dates / issue --
        "mesco_etdorigin":        _date(_clean(src.get("mesco_etdorigin"))) or master_etd,
        "mesco_etadestination":   _date(_clean(src.get("mesco_etadestination"))) or master_eta,
        "mesco_dateofissue":      _date(_clean(src.get("mesco_dateofissue"))) or _date(_clean(master.get("mesco_dateofissue"))),
        "mesco_shippedonboarddate": _date(_clean(src.get("mesco_shippedonboarddate"))) or _date(_clean(master.get("mesco_shippedonboarddate"))),
        "mesco_placeofissue":     _clean(src.get("mesco_placeofissue")) or _clean(master.get("mesco_placeofissue")),

        # -- Addresses --
        "mesco_pickupaddress":   _clean(src.get("mesco_pickupaddress")),
        "mesco_deliveryaddress": _clean(src.get("mesco_deliveryaddress")),

        # -- Container array --
        "containers": cargo_containers,

        # -- Metadata --
        "extraction_method": "crm_operation_mapper",
        "_source_info":      _source_info(master, house),
        "_master_code":      _clean(master.get("mesco_code")),
        "_house_code":       _clean(src.get("mesco_code")),
    }

    # Strip None / empty values for clean output
    return {k: v for k, v in rec.items() if v is not None and v != [] and v != {}}


def _source_info(master: dict, house: Optional[dict]) -> str:
    master_code = master.get("mesco_code") or "N/A"
    if house:
        house_code = house.get("mesco_code") or "N/A"
        return f"CRM Operation: {master_code} / House: {house_code}"
    return f"CRM Operation: {master_code}"

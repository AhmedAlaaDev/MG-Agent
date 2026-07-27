"""Find and (optionally) clean duplicate mesco_operations in Dataverse.

Duplicates accumulate when the same B/L is uploaded more than once (e.g. an
Excel manifest first, then the individual house PDFs, or earlier manual entry).
A "duplicate group" = operations that share the same ``mesco_masterblno`` AND
``mesco_bltype``.

For each duplicate group the script keeps the *best* record and flags the rest:

    keeper score = (has parent link, cargo count, container count, has createdon)

so a record that is already linked to its master and/or already carries cargo
is preferred over an empty orphan.

Safety:
    * Default mode is a DRY RUN — it only prints what it would do.
    * ``--apply`` deletes the extra duplicates, but ONLY the *empty* ones
      (no cargo, no containers). Non-empty duplicates are never deleted
      automatically; they are listed under "NEEDS MANUAL REVIEW".

Usage (from the project folder):
    python dedup_cleanup.py                 # dry run, all operations
    python dedup_cleanup.py --bl 2311318    # restrict to one master B/L (link)
    python dedup_cleanup.py --apply         # actually delete empty duplicates
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Any, Dict, List, Optional

from dataverse_uploader import (
    _CARGO_ENTITY,
    _CARGO_HOUSE_VALUE_FIELD,
    _CARGO_MASTER_VALUE_FIELD,
    _CONTAINER_ENTITY,
    _CONTAINER_MASTER_VALUE_FIELD,
    _ENTITY,
    _HOUSE_BL_TYPE,
    _MASTER_BL_TYPE,
    _cargo_row_rank,
    _cargo_signature,
    _description_score,
    _sync_operation_totals_from_cargo,
)
from dataverse.client_service import DataverseClientService, RetryConfig

_SELECT = "mesco_operationid,mesco_masterblno,mesco_masterbllinkno,mesco_bltype,_mesco_operation_value,createdon"


def _print_operation_inventory(
    rows: List[Dict[str, Any]],
    bl_link: Optional[str],
) -> Dict[str, Any]:
    """Explain how many master/house operations match a master B/L filter."""
    masters = [r for r in rows if r.get("mesco_bltype") == _MASTER_BL_TYPE]
    houses = [r for r in rows if r.get("mesco_bltype") == _HOUSE_BL_TYPE]
    other = [
        r for r in rows
        if r.get("mesco_bltype") not in (_MASTER_BL_TYPE, _HOUSE_BL_TYPE)
    ]
    wrong_houses: List[Dict[str, Any]] = []
    if bl_link:
        link_up = bl_link.strip().upper()
        wrong_houses = [
            r for r in houses
            if (r.get("mesco_masterblno") or "").strip().upper() == link_up
        ]

    print("== Operation inventory ==")
    print(f"   Total fetched     : {len(rows)}")
    print(f"   Master operations : {len(masters)}")
    print(f"   House operations  : {len(houses)}")
    if other:
        print(f"   Other BL types    : {len(other)}")
    if bl_link:
        print(
            f"   Expected for {bl_link}: 1 master + N houses "
            f"(manifest had 5 houses → expect 6 records, not 7)."
        )
    if len(masters) > 1:
        print(
            f"   ! {len(masters)} MASTER rows share this B/L — "
            "only one master should exist (extra master explains count > 6)."
        )
    if wrong_houses:
        print(
            f"   ! {len(wrong_houses)} HOUSE row(s) use the master B/L as "
            "mesco_masterblno instead of the house HBL — fix or delete these."
        )
    print()
    for r in rows:
        bl = (r.get("mesco_masterblno") or "").strip()
        link = (r.get("mesco_masterbllinkno") or "").strip()
        parent = r.get("_mesco_operation_value")
        print(
            f"   {_bltype_name(r.get('mesco_bltype')):6} "
            f"bl={bl or '?':18} link={link or '-':18} "
            f"parent={'yes' if parent else 'no':3} "
            f"id={r.get('mesco_operationid')}"
        )
    print()
    return {
        "masters": masters,
        "houses": houses,
        "wrong_houses": wrong_houses,
        "other": other,
    }


def _clean_miskeyed_masterbl_houses(
    client: DataverseClientService,
    bl_link: str,
    rows: List[Dict[str, Any]],
    apply: bool,
) -> int:
    """Delete HOUSE rows that incorrectly use the ocean/master B/L as mesco_masterblno.

    A valid house stores its HBL in mesco_masterblno and the master MBL in
    mesco_masterbllinkno. An extra HOUSE with bl=NSA26030217 / no parent is a
    common upload mistake and inflates the operation count (7 instead of 6).
    """
    if not bl_link:
        return 0
    link_up = bl_link.strip().upper()
    deleted = 0
    print("== Mis-keyed house operations (bl = master MBL) ==")
    for r in rows:
        if r.get("mesco_bltype") != _HOUSE_BL_TYPE:
            continue
        if (r.get("mesco_masterblno") or "").strip().upper() != link_up:
            continue
        op_id = r["mesco_operationid"]
        counts = _related_counts(client, op_id, is_house=True)
        has_parent = bool(r.get("_mesco_operation_value"))
        empty = counts["cargo"] == 0
        tag = "DELETE" if empty else "REVIEW"
        print(
            f"   {tag} HOUSE id={op_id} parent={'yes' if has_parent else 'no'} "
            f"cargo={counts['cargo']}"
        )
        if not empty:
            print(
                "      ! has cargo — move/delete cargo manually before removing "
                "this mis-keyed house"
            )
            continue
        if apply:
            try:
                client.delete(f"{_ENTITY}({op_id})")
                deleted += 1
            except Exception as exc:
                print(f"      ! delete failed: {exc}")
    if not any(
        r.get("mesco_bltype") == _HOUSE_BL_TYPE
        and (r.get("mesco_masterblno") or "").strip().upper() == link_up
        for r in rows
    ):
        print("   (none)")
    print()
    return deleted


def _duplicate_master_groups(
    rows: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Masters that share the same mesco_masterblno (true duplicate masters)."""
    by_bl: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("mesco_bltype") != _MASTER_BL_TYPE:
            continue
        bl = (r.get("mesco_masterblno") or "").strip()
        if bl:
            by_bl[bl].append(r)
    return {bl: members for bl, members in by_bl.items() if len(members) > 1}


def _bltype_name(value: Any) -> str:
    if value == _MASTER_BL_TYPE:
        return "MASTER"
    if value == _HOUSE_BL_TYPE:
        return "HOUSE"
    return f"OTHER({value})"


def _fetch_all_operations(
    client: DataverseClientService, bl_link: Optional[str]
) -> List[Dict[str, Any]]:
    """Page through every (matching) operation."""
    flt = ""
    if bl_link:
        safe = bl_link.replace("'", "''")
        flt = (
            f"&$filter=mesco_masterbllinkno eq '{safe}' "
            f"or mesco_masterblno eq '{safe}'"
        )
    url: Optional[str] = f"{_ENTITY}?$select={_SELECT}{flt}&$top=5000"
    rows: List[Dict[str, Any]] = []
    while url:
        data = client.get(url).json()
        rows.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return rows


def _count(client: DataverseClientService, entity: str, value_field: str, op_id: str) -> int:
    try:
        data = client.get(
            f"{entity}?$filter={value_field} eq {op_id}&$select={entity[:-1]}id&$top=500"
        ).json()
        return len(data.get("value", []))
    except Exception:
        return 0


def _related_counts(client: DataverseClientService, op_id: str, is_house: bool) -> Dict[str, int]:
    cargo_field = _CARGO_HOUSE_VALUE_FIELD if is_house else _CARGO_MASTER_VALUE_FIELD
    cargo = _count(client, _CARGO_ENTITY, cargo_field, op_id)
    containers = 0
    if not is_house:
        containers = _count(client, _CONTAINER_ENTITY, _CONTAINER_MASTER_VALUE_FIELD, op_id)
    return {"cargo": cargo, "containers": containers}


def _fetch_cargo_rows_for_op(
    client: DataverseClientService, op_id: str
) -> List[Dict[str, Any]]:
    sel = (
        "mesco_cargoid,mesco_descriptionofgoods,mesco_noofpackages,"
        "mesco_grosskg,mesco_volcbm"
    )
    rows: List[Dict[str, Any]] = []
    seen_ids = set()
    for field in (_CARGO_HOUSE_VALUE_FIELD, _CARGO_MASTER_VALUE_FIELD):
        try:
            data = client.get(
                f"{_CARGO_ENTITY}?$filter={field} eq {op_id}&$select={sel}&$top=500"
            ).json()
        except Exception:
            continue
        for r in data.get("value", []):
            if r["mesco_cargoid"] not in seen_ids:
                seen_ids.add(r["mesco_cargoid"])
                rows.append(r)
    return rows


def _delete_cargo_rows(
    client: DataverseClientService, rows: List[Dict[str, Any]], apply: bool, label: str
) -> int:
    deleted = 0
    if not rows:
        return 0
    print(f"   {label}: remove {len(rows)}")
    if apply:
        for d in rows:
            try:
                client.delete(f"{_CARGO_ENTITY}({d['mesco_cargoid']})")
                deleted += 1
            except Exception as exc:
                print(f"      ! cargo delete failed: {exc}")
    return deleted


def _clean_cargo_for_op(
    client: DataverseClientService,
    op_id: str,
    apply: bool,
    *,
    is_house: bool = False,
) -> tuple[int, int]:
    """Dedup cargo on one operation by quantity signature.

    Keeps the row with the best (longest) description and deletes the rest.
    House operations also collapse to a single cargo row when multiple rows
    remain with different quantities (manifest + house PDF re-upload).
    Returns (duplicate_extras_found, deleted).
    """
    rows = _fetch_cargo_rows_for_op(client, op_id)

    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        sig = _cargo_signature(
            r.get("mesco_descriptionofgoods"),
            r.get("mesco_noofpackages"),
            r.get("mesco_grosskg"),
            r.get("mesco_volcbm"),
        )
        groups[sig].append(r)

    extras = 0
    deleted = 0
    for sig, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(
            key=lambda m: _description_score(m.get("mesco_descriptionofgoods")),
            reverse=True,
        )
        keeper, dups = members[0], members[1:]
        extras += len(dups)
        print(
            f"   cargo dup [{sig}]: keep {keeper['mesco_cargoid']} "
            f"('{(keeper.get('mesco_descriptionofgoods') or '')[:30]}'), "
            f"remove {len(dups)}"
        )
        deleted += _delete_cargo_rows(client, dups, apply, "signature dup")

    if is_house:
        rows = _fetch_cargo_rows_for_op(client, op_id)
        if len(rows) > 1:
            keeper = max(rows, key=_cargo_row_rank)
            dups = [r for r in rows if r["mesco_cargoid"] != keeper["mesco_cargoid"]]
            extras += len(dups)
            print(
                f"   house cargo collapse: keep {keeper['mesco_cargoid']} "
                f"('{(keeper.get('mesco_descriptionofgoods') or '')[:30]}')"
            )
            deleted += _delete_cargo_rows(client, dups, apply, "house extras")

    return extras, deleted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bl", dest="bl_link", default=None, help="restrict to one B/L (master link no.)")
    parser.add_argument("--apply", action="store_true", help="delete empty duplicates (default: dry run)")
    parser.add_argument("--cargo", action="store_true", help="also dedup cargo rows within each operation")
    parser.add_argument(
        "--sync-totals",
        action="store_true",
        help="after cargo dedup, PATCH operation totals from cargo rows",
    )
    args = parser.parse_args()

    client = DataverseClientService.get_instance(RetryConfig())
    rows = _fetch_all_operations(client, args.bl_link)
    print(f"Fetched {len(rows)} operation record(s).\n")

    inventory = _print_operation_inventory(rows, args.bl_link)

    miskeyed_deleted = 0
    if args.bl_link:
        miskeyed_deleted = _clean_miskeyed_masterbl_houses(
            client, args.bl_link, rows, args.apply,
        )

    master_dupes = _duplicate_master_groups(rows)
    if master_dupes:
        print("== Duplicate MASTER operations (same mesco_masterblno) ==")
        for bl, members in sorted(master_dupes.items()):
            print(f"   {bl}: {len(members)} master record(s)")
            for m in members:
                counts = _related_counts(client, m["mesco_operationid"], is_house=False)
                print(
                    f"      id={m['mesco_operationid']} "
                    f"cargo={counts['cargo']} containers={counts['containers']}"
                )
        print()

    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        bl = (r.get("mesco_masterblno") or "").strip()
        if not bl:
            continue
        groups[(bl, r.get("mesco_bltype"))].append(r)

    # ------------------------------------------------------------------
    # Cargo deduplication (within each operation), independent of operation dups.
    # ------------------------------------------------------------------
    if args.cargo:
        print("== Cargo deduplication ==")
        cargo_extras = 0
        cargo_deleted = 0
        for r in rows:
            bl = (r.get("mesco_masterblno") or "").strip()
            link = (r.get("mesco_masterbllinkno") or "").strip()
            btype = _bltype_name(r.get("mesco_bltype"))
            print(f"-- {btype} bl={bl or '?'} link={link or '-'} --")
            is_house = r.get("mesco_bltype") == _HOUSE_BL_TYPE
            e, d = _clean_cargo_for_op(
                client,
                r["mesco_operationid"],
                args.apply,
                is_house=is_house,
            )
            cargo_extras += e
            cargo_deleted += d
            if args.sync_totals and args.apply:
                counts = _related_counts(client, r["mesco_operationid"], is_house)
                _sync_operation_totals_from_cargo(
                    client,
                    r["mesco_operationid"],
                    is_house=is_house,
                    container_count=counts.get("containers", 0),
                )
        print(
            f"\nCargo summary: {cargo_extras} duplicate row(s) "
            f"{'deleted' if args.apply else 'to delete'}"
            + ("" if args.apply else " (re-run with --apply)")
            + "\n"
        )

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    if not dup_groups and not master_dupes and not inventory.get("wrong_houses"):
        print(
            "No duplicate (mesco_masterblno + mesco_bltype) operation groups found."
        )
        if args.bl_link and len(rows) > 6:
            print(
                f"\nNote: {len(rows)} records is more than 1 master + 5 houses (6). "
                "Check the inventory above for an extra master or mis-keyed house."
            )
        return

    deleted = 0
    manual = 0
    for (bl, bltype), members in sorted(dup_groups.items()):
        is_house = bltype == _HOUSE_BL_TYPE
        enriched = []
        for m in members:
            counts = _related_counts(client, m["mesco_operationid"], is_house)
            enriched.append(
                {
                    **m,
                    **counts,
                    "has_parent": bool(m.get("_mesco_operation_value")),
                }
            )
        enriched.sort(
            key=lambda e: (
                e["has_parent"],
                e["cargo"],
                e["containers"],
                e.get("createdon") or "",
            ),
            reverse=True,
        )
        keeper = enriched[0]
        extras = enriched[1:]
        print(f"== {bl} [{_bltype_name(bltype)}] : {len(members)} copies ==")
        print(
            f"   KEEP  {keeper['mesco_operationid']} "
            f"parent={keeper['has_parent']} cargo={keeper['cargo']} cont={keeper['containers']}"
        )
        for e in extras:
            empty = e["cargo"] == 0 and e["containers"] == 0
            tag = "DELETE" if empty else "REVIEW"
            print(
                f"   {tag} {e['mesco_operationid']} "
                f"parent={e['has_parent']} cargo={e['cargo']} cont={e['containers']}"
            )
            if not empty:
                manual += 1
                continue
            if args.apply:
                try:
                    client.delete(f"{_ENTITY}({e['mesco_operationid']})")
                    deleted += 1
                except Exception as exc:
                    print(f"      ! delete failed: {exc}")
        print()

    print("Summary:")
    print(f"  duplicate groups : {len(dup_groups)}")
    print(f"  empty extras {'deleted' if args.apply else 'to delete'} : {deleted if args.apply else sum(1 for (bl, bt), ms in dup_groups.items() for e in ms[1:])}")
    print(f"  non-empty extras needing manual review : {manual}")
    if args.bl_link:
        print(
            f"  mis-keyed master-B/L houses "
            f"{'deleted' if args.apply else 'to delete'} : {miskeyed_deleted}"
        )
    if not args.apply:
        print("\n(DRY RUN — re-run with --apply to delete the empty duplicates.)")


if __name__ == "__main__":
    main()

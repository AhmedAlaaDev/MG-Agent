from pathlib import Path

from dynamics_schema_sync import (
    apply_generated_schema_metadata,
    parse_schema_file,
)


FRONTEND_ROOT = Path(r"E:\OneDrive - MESCO\Desktop\Operations\MG%20Operation")


def test_parse_generated_cargo_schema_for_imo_fields():
    schema_path = FRONTEND_ROOT / "src/modules/Cargo/schema/index.ts"
    if not schema_path.is_file():
        return

    meta = parse_schema_file(schema_path)
    assert meta is not None
    assert meta.entity_set_name == "mesco_cargos"
    assert meta.fields["mesco_imoclass"].attribute_type == "Decimal"
    assert meta.fields["mesco_unno"].attribute_type == "Decimal"
    assert meta.relationships["mesco_houseoperation"].navigation == "mesco_HouseOperation"
    assert meta.relationships["mesco_masteroperation"].navigation == "mesco_MasterOperation"


def test_apply_generated_schema_marks_cargo_imo_class_decimal():
    entity_schemas = {
        "mesco_cargos": {
            "lookups": {},
            "invalid": set(),
            "field_map": {},
            "decimals": set(),
            "picklist_strings": {},
        }
    }
    nav = {}
    entity_sets = {}
    ids = {}

    loaded = apply_generated_schema_metadata(entity_schemas, nav, entity_sets, ids)
    if "mesco_cargos" not in loaded:
        return

    assert "mesco_imoclass" in entity_schemas["mesco_cargos"]["decimals"]
    assert entity_schemas["mesco_cargos"]["lookups"]["mesco_houseoperation"] == "mesco_operation"
    assert nav["mesco_houseoperation"] == "mesco_HouseOperation"
    assert entity_sets["mesco_operation"] == "mesco_operations"
    assert ids["mesco_operations"] == "mesco_operationid"

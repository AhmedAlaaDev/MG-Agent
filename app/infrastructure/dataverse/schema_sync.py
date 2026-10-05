"""Read generated Dynamics TypeScript schemas and apply them to Python upload metadata.

The MG Operation frontend and OperationBackend both generate ``schema/index.ts``
files from Dataverse metadata.  This module parses the small subset the Python
uploader needs: writable fields, numeric/date/lookup types, entity-set names,
ID fields, and lookup navigation properties.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set


DEFAULT_SCHEMA_ROOTS = (
    Path(r"E:\OneDrive - MESCO\Desktop\Operations\MG%20Operation"),
    Path(r"E:\OneDrive - MESCO\Desktop\Operations\OperationBackend"),
)

MODULE_SCHEMA_PATHS = {
    "mesco_operations": (
        Path("src/modules/Operation/schema/index.ts"),
        Path("src/modules/operation/schema/index.ts"),
    ),
    "mesco_cargos": (
        Path("src/modules/Cargo/schema/index.ts"),
        Path("src/modules/cargo/schema/index.ts"),
    ),
    "mesco_containers": (
        Path("src/modules/Container/schema/index.ts"),
        Path("src/modules/container/schema/index.ts"),
    ),
}

DECIMAL_TYPES = {"Decimal", "Double", "Integer", "BigInt", "Money"}
DATE_TYPES = {"DateTime"}


@dataclass
class FieldMeta:
    logical_name: str
    schema_name: Optional[str] = None
    attribute_type: Optional[str] = None
    targets: List[str] = field(default_factory=list)
    is_system: bool = False
    is_primary_id: bool = False
    is_valid_for_create: bool = False
    is_valid_for_update: bool = False

    @property
    def writable(self) -> bool:
        return (
            (self.is_valid_for_create or self.is_valid_for_update)
            and not self.is_system
            and not self.is_primary_id
        )


@dataclass
class RelationshipMeta:
    source_key: str
    target_entity: str
    target_set_name: Optional[str] = None
    target_key: Optional[str] = None
    navigation: Optional[str] = None


@dataclass
class EntitySchemaMeta:
    entity_set_name: str
    logical_name: str
    primary_id_attribute: str
    fields: Dict[str, FieldMeta] = field(default_factory=dict)
    relationships: Dict[str, RelationshipMeta] = field(default_factory=dict)


def _str_prop(body: str, name: str) -> Optional[str]:
    m = re.search(rf"\b{name}\s*:\s*'([^']*)'", body)
    return m.group(1) if m else None


def _bool_prop(body: str, name: str) -> bool:
    m = re.search(rf"\b{name}\s*:\s*(true|false)", body)
    return bool(m and m.group(1) == "true")


def _list_prop(body: str, name: str) -> List[str]:
    m = re.search(rf"\b{name}\s*:\s*\[([^\]]*)\]", body, re.S)
    if not m:
        return []
    return re.findall(r"'([^']+)'", m.group(1))


def _section(text: str, name: str) -> str:
    marker = re.search(rf"\b{name}\s*:\s*\{{", text)
    if not marker:
        return ""
    start = marker.end()
    depth = 1
    idx = start
    while idx < len(text) and depth:
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
        idx += 1
    return text[start : idx - 1]


def _top_level_blocks(section: str) -> Iterable[str]:
    key_re = re.compile(r"^\s{4}[A-Za-z0-9_$]+:\s*\{", re.M)
    matches = list(key_re.finditer(section))
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(section)
        yield section[match.start() : end]


def _parse_fields(text: str) -> Dict[str, FieldMeta]:
    fields: Dict[str, FieldMeta] = {}
    for block in _top_level_blocks(_section(text, "Fields")):
        logical = _str_prop(block, "logicalName")
        if not logical:
            continue
        fields[logical] = FieldMeta(
            logical_name=logical,
            schema_name=_str_prop(block, "schemaName"),
            attribute_type=_str_prop(block, "attributeType"),
            targets=_list_prop(block, "targets"),
            is_system=_bool_prop(block, "isSystem"),
            is_primary_id=_bool_prop(block, "isPrimaryId"),
            is_valid_for_create=_bool_prop(block, "isValidForCreate"),
            is_valid_for_update=_bool_prop(block, "isValidForUpdate"),
        )
    return fields


def _parse_relationships(text: str) -> Dict[str, RelationshipMeta]:
    relationships: Dict[str, RelationshipMeta] = {}
    for block in _top_level_blocks(_section(text, "ManyToOneRelationships")):
        source = _str_prop(block, "sourceKey")
        target = _str_prop(block, "targetEntity")
        if not source or not target:
            continue
        relationships[source] = RelationshipMeta(
            source_key=source,
            target_entity=target,
            target_set_name=_str_prop(block, "targetSetName"),
            target_key=_str_prop(block, "targetKey"),
            navigation=_str_prop(block, "navigation"),
        )
    return relationships


def parse_schema_file(path: Path) -> Optional[EntitySchemaMeta]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    logical_name = _str_prop(text, "logicalName")
    entity_set = _str_prop(text, "entitySetName")
    primary_id = _str_prop(text, "primaryIdAttribute")
    if not logical_name or not entity_set or not primary_id:
        return None

    return EntitySchemaMeta(
        entity_set_name=entity_set,
        logical_name=logical_name,
        primary_id_attribute=primary_id,
        fields=_parse_fields(text),
        relationships=_parse_relationships(text),
    )


def _schema_roots() -> List[Path]:
    env = os.getenv("DYNAMICS_SCHEMA_ROOTS", "")
    roots = [Path(p.strip()) for p in env.split(";") if p.strip()]
    return roots or list(DEFAULT_SCHEMA_ROOTS)


def load_generated_schemas() -> Dict[str, EntitySchemaMeta]:
    loaded: Dict[str, EntitySchemaMeta] = {}
    for entity_set, relative_paths in MODULE_SCHEMA_PATHS.items():
        for root in _schema_roots():
            for relative in relative_paths:
                meta = parse_schema_file(root / relative)
                if meta:
                    loaded[entity_set] = meta
                    break
            if entity_set in loaded:
                break
    return loaded


def apply_generated_schema_metadata(
    entity_schemas: Dict[str, Dict[str, Any]],
    nav_property_map: Dict[str, str],
    entity_set_map: Dict[str, str],
    id_field_map: Dict[str, str],
) -> Dict[str, EntitySchemaMeta]:
    """Merge generated metadata into the uploader's mutable schema tables."""
    generated = load_generated_schemas()
    for entity_set, meta in generated.items():
        schema = entity_schemas.setdefault(entity_set, {})
        schema.setdefault("lookups", {})
        schema.setdefault("invalid", set())
        schema.setdefault("decimals", set())
        schema.setdefault("dates", set())
        schema.setdefault("picklist_strings", {})
        schema.setdefault("field_map", {})

        entity_set_map[meta.logical_name] = meta.entity_set_name
        id_field_map[meta.entity_set_name] = meta.primary_id_attribute

        for field_name, field_meta in meta.fields.items():
            if not field_meta.writable:
                schema["invalid"].add(field_name)
                continue
            if field_meta.attribute_type in DECIMAL_TYPES:
                schema["decimals"].add(field_name)
            elif field_meta.attribute_type in DATE_TYPES:
                schema.setdefault("dates", set()).add(field_name)
            elif field_meta.attribute_type == "Lookup" and field_meta.targets:
                schema["lookups"].setdefault(field_name, field_meta.targets[0])

        for rel in meta.relationships.values():
            schema["lookups"].setdefault(rel.source_key, rel.target_entity)
            if rel.navigation:
                nav_property_map[rel.source_key] = rel.navigation
            if rel.target_set_name:
                entity_set_map[rel.target_entity] = rel.target_set_name
            if rel.target_key and rel.target_set_name:
                id_field_map[rel.target_set_name] = rel.target_key
    return generated

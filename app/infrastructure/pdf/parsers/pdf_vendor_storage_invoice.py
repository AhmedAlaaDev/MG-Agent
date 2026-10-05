"""Deterministic parser for SACO and Globelink container-storage invoices.

These PDFs are often bilingual. Arabic labels are stored in visual order, so
the readable values are the Latin tokens (invoice number, B/L, container,
dates, and amounts). English-only copies of the same form use those tokens
without an Arabic text layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import fitz

_ARABIC = re.compile(r"[\u0600-\u06FF]")
_DATE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
_MONEY = re.compile(r"^\d+\.\d{2}$")
_CONTAINER = re.compile(r"^[A-Z]{4}\d{7}$")
_PUBLIC_INVOICE = re.compile(r"^INV-\d{4}-\d{1,6}$")
_INTERNAL_INVOICE = re.compile(r"^INV-\d{10,}$")
_VOYAGE = re.compile(r"^[A-Z]{1,4}\d{5,}$")
_TAX_REGISTRATION = re.compile(r"^\d{9}$")
_VAT_NUMBER = re.compile(r"^\d{3}-\d{3}-\d{3}$")
_TAX_FILE = re.compile(r"^\d-\d{5}-\d{3}-\d{2}-\d{2}$")
_NAME_TOKEN = re.compile(r"^\.?[A-Za-z][A-Za-z.&'-]*$")
_CURRENCIES = {"EGP", "USD", "EUR", "GBP", "AED", "SAR", "LE"}
_LINE_Y_TOLERANCE = 2.5
_CUSTOMER_Y_WINDOW = 18.0

SACO_VENDOR = "saco"
GLOBELINK_VENDOR = "globelink"
VENDOR_LABELS = {
    SACO_VENDOR: "SACO",
    GLOBELINK_VENDOR: "Globelink",
}


class StorageInvoiceParseError(ValueError):
    """The PDF is not a SACO or Globelink storage invoice we can read."""


@dataclass(frozen=True)
class _Line:
    y: float
    tokens: list[str]


def normalize_vendor_choice(raw: str) -> str:
    """Map a caller choice to ``saco`` or ``globelink``."""
    key = re.sub(r"[^a-z]", "", str(raw or "").casefold())
    if key == "saco":
        return SACO_VENDOR
    if key in {"globelink", "globlink"}:
        return GLOBELINK_VENDOR
    raise StorageInvoiceParseError(
        "Choose a vendor: saco or globelink (globlink is accepted as an alias)."
    )


def vendor_endpoint(vendor_code: str) -> str:
    return f"/extract/invoice/{vendor_code}"


def parse_storage_invoice_pdf(pdf_bytes: bytes) -> dict[str, Any]:
    """Return one storage-invoice payload from the first matching page."""
    if not pdf_bytes:
        raise StorageInvoiceParseError("The uploaded file is empty.")
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except (fitz.EmptyFileError, fitz.FileDataError) as exc:
        raise StorageInvoiceParseError("The file is not a readable PDF.") from exc
    try:
        return _first_storage_page(document)
    finally:
        document.close()


def _first_storage_page(document: fitz.Document) -> dict[str, Any]:
    if document.page_count == 0:
        raise StorageInvoiceParseError("PDF has no pages.")
    failures: list[str] = []
    for number, page in enumerate(document, start=1):
        try:
            return _storage_invoice_from_page(page)
        except StorageInvoiceParseError as exc:
            failures.append(f"page {number}: {exc}")
    raise StorageInvoiceParseError(" ".join(failures))


def _storage_invoice_from_page(page: fitz.Page) -> dict[str, Any]:
    lines = _latin_lines(page)
    _require_text_layer(lines)
    issuer = _issuer_text(lines)
    vendor_code = _require_known_vendor(issuer)
    parties = _party_fields(lines, issuer, vendor_code, _page_has_arabic(page))
    shipment = _shipment_fields(lines)
    charges = _charge_fields(lines, shipment)
    _require_storage_identity(shipment, charges)
    return {**parties, **shipment, **charges}


def _require_text_layer(lines: list[_Line]) -> None:
    if lines:
        return
    raise StorageInvoiceParseError(
        "No text layer on this page. A scanned image without embedded text cannot be read."
    )


def _require_known_vendor(issuer: str) -> str:
    vendor_code = _vendor_code(issuer)
    if vendor_code is None:
        raise StorageInvoiceParseError(
            "The invoice header is neither SACO nor Globelink. "
            f"Header was: {issuer or 'blank'}."
        )
    return vendor_code


def _require_storage_identity(shipment: dict[str, Any], charges: dict[str, Any]) -> None:
    missing = []
    if not shipment.get("container_number"):
        missing.append("container number")
    if not shipment.get("house_bl_number"):
        missing.append("B/L number")
    if not charges.get("line_items"):
        missing.append("charge lines")
    if missing:
        raise StorageInvoiceParseError(
            "This page is missing " + ", ".join(missing) + "."
        )


def _latin_lines(page: fitz.Page) -> list[_Line]:
    words = [word for word in (page.get_text("words") or []) if _keep_token(str(word[4]))]
    words.sort(key=lambda word: (float(word[1]), float(word[0])))
    buckets: list[list[tuple]] = []
    anchor_y: Optional[float] = None
    for word in words:
        word_y = float(word[1])
        if anchor_y is None or abs(word_y - anchor_y) > _LINE_Y_TOLERANCE:
            buckets.append([word])
            anchor_y = word_y
            continue
        buckets[-1].append(word)
        anchor_y = (anchor_y + word_y) / 2
    return [_line_from_bucket(bucket) for bucket in buckets if bucket]


def _line_from_bucket(bucket: list[tuple]) -> _Line:
    ordered = sorted(bucket, key=lambda word: float(word[0]))
    anchor = sum(float(word[1]) for word in ordered) / len(ordered)
    return _Line(anchor, [str(word[4]) for word in ordered])


def _keep_token(token: str) -> bool:
    if _ARABIC.search(token):
        return False
    if token in {'-', '–', '"', "“", "”"}:
        return True
    return bool(re.search(r"[A-Za-z0-9]", token))


def _page_has_arabic(page: fitz.Page) -> bool:
    for word in page.get_text("words") or []:
        if _ARABIC.search(str(word[4])):
            return True
    return False


def _join_tokens(tokens: list[str]) -> str:
    unique: list[str] = []
    for token in tokens:
        if unique and unique[-1] == token:
            continue
        unique.append(token)
    return " ".join(unique).strip()


def _issuer_text(lines: list[_Line]) -> str:
    for line in lines:
        text = _join_tokens(line.tokens)
        if _vendor_code(text):
            return text
    return _join_tokens(lines[0].tokens) if lines else ""


def _vendor_code(issuer: str) -> Optional[str]:
    compact = re.sub(r"[^A-Z]", "", issuer.upper())
    if "GLOBELINK" in compact or "GLOBLINK" in compact:
        return GLOBELINK_VENDOR
    if "SACO" in compact:
        return SACO_VENDOR
    return None


def _party_fields(
    lines: list[_Line],
    issuer: str,
    vendor_code: str,
    arabic: bool,
) -> dict[str, Any]:
    tax_line = _tax_registration_line(lines)
    return {
        "document_type": "STORAGE INVOICE",
        "document_language": "ar" if arabic else "en",
        "vendor_profile": vendor_code,
        "vendor_name": issuer,
        "vendor_address": _address_text(lines),
        "vendor_phone": _labeled_phone(lines, "TEL"),
        "vendor_fax": _labeled_phone(lines, "FAX"),
        **_registration_fields(lines, tax_line),
        "client_name": _customer_name(lines, tax_line.y if tax_line else None),
        "prepared_by": _prepared_by(lines),
    }


def _registration_fields(lines: list[_Line], tax_line: Optional[_Line]) -> dict[str, Optional[str]]:
    return {
        "vendor_invoice_number": _first_token(lines, _PUBLIC_INVOICE),
        "internal_invoice_number": _first_token(lines, _INTERNAL_INVOICE),
        "vendor_vat_number": _first_token(lines, _VAT_NUMBER),
        "vendor_tax_file_number": _first_token(lines, _TAX_FILE),
        "vendor_tax_registration_number": _token_on_line(tax_line, _TAX_REGISTRATION),
        "invoice_date": _token_on_line(tax_line, _DATE),
    }


def _tax_registration_line(lines: list[_Line]) -> Optional[_Line]:
    for line in lines:
        if any(_TAX_REGISTRATION.match(token) for token in line.tokens):
            return line
    return None


def _token_on_line(line: Optional[_Line], pattern: re.Pattern[str]) -> Optional[str]:
    if line is None:
        return None
    for token in line.tokens:
        if pattern.match(token):
            return token
    return None


def _first_token(lines: list[_Line], pattern: re.Pattern[str]) -> Optional[str]:
    for line in lines:
        found = _token_on_line(line, pattern)
        if found:
            return found
    return None


def _address_text(lines: list[_Line]) -> Optional[str]:
    issuer_y = next((line.y for line in lines if _vendor_code(_join_tokens(line.tokens))), 0)
    chunks: list[str] = []
    for line in lines:
        if line.y <= issuer_y + 2:
            continue
        if _phone_kind(line.tokens):
            break
        tokens = [token for token in line.tokens if not _PUBLIC_INVOICE.match(token)]
        tokens = [token for token in tokens if not _INTERNAL_INVOICE.match(token)]
        if tokens:
            chunks.append(_join_tokens(tokens))
    text = ", ".join(chunk for chunk in chunks if chunk)
    return text or None


def _phone_kind(tokens: list[str]) -> Optional[str]:
    for token in tokens:
        label = token.upper().rstrip(":")
        if label in {"TEL", "FAX"}:
            return label
    return None


def _labeled_phone(lines: list[_Line], label: str) -> Optional[str]:
    for line in lines:
        if _phone_kind(line.tokens) != label:
            continue
        body = [token for token in line.tokens if token.upper().rstrip(":") != label]
        return _join_tokens(body) or None
    return None


def _customer_name(lines: list[_Line], tax_y: Optional[float]) -> Optional[str]:
    if tax_y is None:
        return None
    chunks: list[str] = []
    for line in lines:
        if abs(line.y - tax_y) > _CUSTOMER_Y_WINDOW:
            continue
        tokens = [
            token
            for token in line.tokens
            if not _DATE.match(token) and not _TAX_REGISTRATION.match(token)
        ]
        if tokens and all(_NAME_TOKEN.match(token) or token == "-" for token in tokens):
            chunks.append(_join_tokens(tokens))
    name = re.sub(r"(?<=\s)\.", "", " ".join(chunks))
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or None


def _prepared_by(lines: list[_Line]) -> Optional[str]:
    for line in reversed(lines):
        if not line.tokens or line.tokens[0] in {"Only", '"'}:
            continue
        if any(_MONEY.match(token) or _DATE.match(token) or _TAX_FILE.match(token) for token in line.tokens):
            continue
        if "N/A" not in line.tokens and "NA" not in line.tokens:
            continue
        name = [token for token in line.tokens if token not in {"N/A", "NA"}]
        return _join_tokens(name) or None
    return None


def _shipment_fields(lines: list[_Line]) -> dict[str, Any]:
    voyage, port, vessel = _split_vessel_row(_row_tokens(lines, _is_vessel_row))
    container, arrival, house_bl = _split_container_row(_row_tokens(lines, _is_container_row))
    color, release_date, storage_start = _split_storage_dates(_row_tokens(lines, _is_storage_date_row))
    booking, storage_days = _split_booking_row(_row_tokens(lines, _is_booking_row))
    return {
        "vessel_name": vessel,
        "voyage_number": voyage,
        "port_of_loading": port,
        "container_number": container,
        "container_color": color,
        "arrival_date": arrival,
        "release_date": release_date,
        "storage_start_date": storage_start,
        "house_bl_number": house_bl,
        "shipment_ref": booking,
        "storage_days": storage_days,
    }


def _row_tokens(lines: list[_Line], predicate) -> list[str]:
    line = _find_line(lines, predicate)
    return line.tokens if line else []


def _find_line(lines: list[_Line], predicate) -> Optional[_Line]:
    for line in lines:
        if predicate(line.tokens):
            return line
    return None


def _is_vessel_row(tokens: list[str]) -> bool:
    return bool(tokens) and bool(_VOYAGE.match(tokens[0])) and not any(_CONTAINER.match(token) for token in tokens)


def _is_container_row(tokens: list[str]) -> bool:
    has_container = any(_CONTAINER.match(token) for token in tokens)
    has_date = any(_DATE.match(token) for token in tokens)
    return has_container and has_date


def _is_storage_date_row(tokens: list[str]) -> bool:
    dates = [token for token in tokens if _DATE.match(token)]
    letters = [token for token in tokens if re.fullmatch(r"[A-Za-z]", token)]
    return len(dates) >= 2 or (len(dates) == 1 and bool(letters))


def _is_booking_row(tokens: list[str]) -> bool:
    if any(_DATE.match(token) or token in _CURRENCIES for token in tokens):
        return False
    day_tokens = [token for token in tokens if token.isdigit() and len(token) <= 4]
    others = [token for token in tokens if token not in day_tokens and token != "-"]
    return len(day_tokens) == 1 and len(others) == 1


def _split_vessel_row(tokens: list[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not tokens:
        return None, None, None
    voyage = tokens[0] if _VOYAGE.match(tokens[0]) else None
    rest = tokens[1:] if voyage else list(tokens)
    if len(rest) >= 2:
        return voyage, rest[0], " ".join(rest[1:])
    if len(rest) == 1:
        return voyage, None, rest[0]
    return voyage, None, None


def _split_container_row(tokens: list[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    container = next((token for token in tokens if _CONTAINER.match(token)), None)
    arrival = next((token for token in tokens if _DATE.match(token)), None)
    house_bl = next(
        (
            token
            for token in tokens
            if token not in {container, arrival} and token != "-"
        ),
        None,
    )
    return container, arrival, house_bl


def _split_storage_dates(tokens: list[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    color = next((token for token in tokens if re.fullmatch(r"[A-Za-z]", token)), None)
    dates = [token for token in tokens if _DATE.match(token)]
    release_date = dates[0] if dates else None
    storage_start = dates[1] if len(dates) > 1 else None
    return color, release_date, storage_start


def _split_booking_row(tokens: list[str]) -> tuple[Optional[str], Optional[int]]:
    if not tokens:
        return None, None
    days_token = next((token for token in tokens if token.isdigit()), None)
    booking = next((token for token in tokens if token != days_token and token != "-"), None)
    return booking, int(days_token) if days_token else None


def _charge_fields(lines: list[_Line], shipment: dict[str, Any]) -> dict[str, Any]:
    charge_lines = [line for line in lines if _is_charge_row(line.tokens)]
    items = [_charge_item(line, shipment) for line in charge_lines]
    amounts = _totals_after_charges(lines, charge_lines)
    subtotal = amounts[0] if amounts else _sum_line_amounts(items)
    tax_amount = amounts[1] if len(amounts) > 1 else None
    tax_rate = _vat_rate_label(subtotal, tax_amount)
    _copy_tax_onto_only_line(items, tax_amount, tax_rate)
    return {
        "currency": items[0]["currency"] if items else None,
        "subtotal_amount": subtotal,
        "tax_amount": tax_amount,
        "tax_rate": tax_rate,
        "total_amount": amounts[2] if len(amounts) > 2 else None,
        "amount_in_words": _amount_in_words(lines),
        "line_items": items,
    }


def _copy_tax_onto_only_line(
    items: list[dict[str, Any]],
    tax_amount: Optional[float],
    tax_rate: Optional[str],
) -> None:
    if len(items) == 1 and tax_amount is not None:
        items[0]["tax_amount"] = tax_amount
        items[0]["tax_rate"] = tax_rate


def _is_charge_row(tokens: list[str]) -> bool:
    return any(token in _CURRENCIES for token in tokens) and any(_MONEY.match(token) for token in tokens)


def _charge_description(tokens: list[str]) -> str:
    words = [
        token
        for token in tokens
        if token not in _CURRENCIES
        and not _MONEY.match(token)
        and not token.isdigit()
        and token != "-"
    ]
    return " ".join(words) or "Storage"


def _charge_item(line: _Line, shipment: dict[str, Any]) -> dict[str, Any]:
    amount = float(next(token for token in line.tokens if _MONEY.match(token)))
    currency = next(token for token in line.tokens if token in _CURRENCIES)
    return {
        "service_description": _charge_description(line.tokens),
        "quantity": 1,
        "unit_price": amount,
        "currency": currency,
        "taxable_amount": amount,
        "tax_rate": None,
        "tax_amount": None,
        "total_amount": amount,
        "comments": _storage_comment(shipment),
    }


def _storage_comment(shipment: dict[str, Any]) -> Optional[str]:
    parts: list[str] = []
    if shipment.get("storage_days") is not None:
        parts.append(f"Storage days: {shipment['storage_days']}.")
    if shipment.get("container_number"):
        parts.append(f"Container {shipment['container_number']}.")
    if shipment.get("arrival_date") and shipment.get("release_date"):
        parts.append(
            f"Stored from {shipment.get('storage_start_date') or shipment['arrival_date']} "
            f"until {shipment['release_date']}."
        )
    return " ".join(parts) or None


def _totals_after_charges(lines: list[_Line], charge_lines: list[_Line]) -> list[float]:
    if not charge_lines:
        return []
    last_charge_y = max(line.y for line in charge_lines)
    amounts: list[float] = []
    for line in lines:
        if line.y <= last_charge_y + 1:
            continue
        money_tokens = [token for token in line.tokens if _MONEY.match(token)]
        if len(line.tokens) == 1 and money_tokens:
            amounts.append(float(money_tokens[0]))
    return amounts


def _sum_line_amounts(items: list[dict[str, Any]]) -> Optional[float]:
    if not items:
        return None
    return round(sum(float(item["total_amount"]) for item in items), 2)


def _vat_rate_label(subtotal: Optional[float], tax_amount: Optional[float]) -> Optional[str]:
    if not subtotal or tax_amount is None:
        return None
    if abs((tax_amount / subtotal) - 0.14) < 0.005:
        return "14%"
    return None


def _amount_in_words(lines: list[_Line]) -> Optional[str]:
    for line in lines:
        if not line.tokens or line.tokens[0] != "Only":
            continue
        words = [token for token in line.tokens[1:] if token not in {'"', "“", "”"}]
        return _join_tokens(words) or None
    return None

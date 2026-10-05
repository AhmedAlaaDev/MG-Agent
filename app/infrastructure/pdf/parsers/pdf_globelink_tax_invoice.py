"""Deterministic parser for Globelink Egypt tax invoices.

The form is a two-column TAX INVOICE. Each label sits left of a colon and
the value sits to its right. Charge rows are a serial, a description, a
currency, and a comma-grouped amount. Empty HBL and container boxes stay empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import fitz

from app.infrastructure.pdf.parsers.pdf_vendor_storage_invoice import (
    GLOBELINK_VENDOR,
    StorageInvoiceParseError,
)

_ARABIC = re.compile(r"[\u0600-\u06FF]")
_MONEY = re.compile(r"^[\d,]+\.\d{2}$")
_MARKERS = ("GLOBELINK", "TAX INVOICE", "Detail of Charges")
_CURRENCIES = {"EGP", "USD", "EUR", "GBP", "AED", "SAR"}
_COLUMN_SPLIT = 280.0
_ROW_TOLERANCE = 2.5


@dataclass(frozen=True)
class _Word:
    x: float
    y: float
    text: str


@dataclass(frozen=True)
class _PageRead:
    text: str
    rows: list[list[_Word]]
    fields: dict[str, str]
    charges: list[dict[str, Any]]
    totals: tuple[float, float, float, str]


@dataclass
class _PairScan:
    pairs: list[tuple[str, str]]
    label: list[str]
    entered: list[str]
    after_colon: bool = False


def is_globelink_tax_invoice_pdf(pdf_bytes: bytes) -> bool:
    """True when the first page is the Globelink tax-invoice form."""
    try:
        document = _open_pdf(pdf_bytes)
    except StorageInvoiceParseError:
        return False
    try:
        return _page_is_tax_invoice(document)
    finally:
        document.close()


def parse_globelink_tax_invoice_pdf(pdf_bytes: bytes) -> dict[str, Any]:
    """Return one Globelink tax-invoice payload from the first page."""
    document = _open_pdf(pdf_bytes)
    try:
        if document.page_count < 1:
            raise StorageInvoiceParseError("The PDF has no pages.")
        return _parse_page(document[0])
    finally:
        document.close()


def _open_pdf(pdf_bytes: bytes) -> fitz.Document:
    if not pdf_bytes:
        raise StorageInvoiceParseError("The uploaded file is empty.")
    try:
        return fitz.open(stream=pdf_bytes, filetype="pdf")
    except (fitz.EmptyFileError, fitz.FileDataError) as exc:
        raise StorageInvoiceParseError("The file is not a readable PDF.") from exc


def _page_is_tax_invoice(document: fitz.Document) -> bool:
    if document.page_count < 1:
        return False
    return _has_tax_markers(_page_text(document[0]))


def _page_text(page: fitz.Page) -> str:
    return page.get_text("text") or ""


def _has_tax_markers(text: str) -> bool:
    return all(marker in text for marker in _MARKERS)


def _parse_page(page: fitz.Page) -> dict[str, Any]:
    text = _page_text(page)
    _require_tax_form(text)
    rows = _rows(_words(page))
    reading = _PageRead(text, rows, _labeled_fields(rows), _charge_lines(rows), _invoice_totals(rows))
    return _payload(reading)


def _require_tax_form(text: str) -> None:
    if _has_tax_markers(text):
        return
    raise StorageInvoiceParseError(
        "This PDF is not a Globelink tax invoice. "
        "The form needs GLOBELINK, TAX INVOICE, and Detail of Charges."
    )


def _words(page: fitz.Page) -> list[_Word]:
    words: list[_Word] = []
    for item in page.get_text("words") or []:
        text = str(item[4]).strip()
        if text and not _ARABIC.search(text):
            words.append(_Word(float(item[0]), float(item[1]), text))
    return words


def _rows(words: list[_Word]) -> list[list[_Word]]:
    ordered = sorted(words, key=lambda word: (word.y, word.x))
    grouped: list[list[_Word]] = []
    for word in ordered:
        _append_word(grouped, word)
    return [sorted(row, key=lambda word: word.x) for row in grouped]


def _append_word(rows: list[list[_Word]], word: _Word) -> None:
    if rows and abs(word.y - rows[-1][0].y) <= _ROW_TOLERANCE:
        rows[-1].append(word)
        return
    rows.append([word])


def _left(row: list[_Word]) -> list[_Word]:
    return [word for word in row if word.x < _COLUMN_SPLIT]


def _right(row: list[_Word]) -> list[_Word]:
    return [word for word in row if word.x >= _COLUMN_SPLIT]


def _labeled_fields(rows: list[list[_Word]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for row in rows:
        _store_pairs(found, _pairs(_left(row)))
        _store_pairs(found, _pairs(_right(row)))
    return found


def _store_pairs(found: dict[str, str], pairs: list[tuple[str, str]]) -> None:
    for label, entered in pairs:
        key = _label_key(label)
        if key and (key not in found or (not found[key] and entered)):
            found[key] = entered


def _label_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()


def _pairs(words: list[_Word]) -> list[tuple[str, str]]:
    expanded = _expand_colons(words)
    scan = _PairScan([], [], [])
    for index, word in enumerate(expanded):
        _consume_pair_token(scan, expanded[index:], word)
    if scan.label or scan.entered:
        scan.pairs.append((_join(scan.label), _join(scan.entered)))
    return scan.pairs


def _consume_pair_token(scan: _PairScan, pending: list[_Word], word: _Word) -> None:
    if word.text == ":":
        _close_open_value(scan)
        return
    if scan.after_colon and _starts_next_label(pending):
        _close_open_value(scan)
        scan.label = [word.text]
        return
    _append_pair_token(scan, word.text)


def _close_open_value(scan: _PairScan) -> None:
    if scan.after_colon:
        scan.pairs.append((_join(scan.label), _join(scan.entered)))
        scan.label, scan.entered, scan.after_colon = [], [], False
        return
    scan.after_colon = True


def _append_pair_token(scan: _PairScan, token: str) -> None:
    if scan.after_colon:
        scan.entered.append(token)
        return
    scan.label.append(token)


def _starts_next_label(pending: list[_Word]) -> bool:
    if not pending or not pending[0].text[:1].isalpha():
        return False
    for word in pending[1:]:
        if word.text == ":":
            return True
        if any(char.isdigit() for char in word.text):
            return False
    return False


def _expand_colons(words: list[_Word]) -> list[_Word]:
    expanded: list[_Word] = []
    for word in words:
        expanded.extend(_colon_pieces(word))
    return expanded


def _colon_pieces(word: _Word) -> list[_Word]:
    if ":" not in word.text or word.text == ":":
        return [word]
    left, right = word.text.split(":", 1)
    pieces = [_Word(word.x, word.y, left)] if left else []
    pieces.append(_Word(word.x, word.y, ":"))
    if right:
        pieces.append(_Word(word.x + 0.2, word.y, right))
    return pieces


def _join(parts) -> str:
    return " ".join(part for part in parts if part).strip()


def _optional(raw: str) -> Optional[str]:
    text = str(raw or "").strip()
    if text.startswith("/") and text.count("/") == 1:
        text = text[1:].strip()
    if text.strip("/") == "":
        return None
    return text


def _payload(reading: _PageRead) -> dict[str, Any]:
    identity = _identity(reading.text, reading.rows, reading.fields)
    _require_identity(identity, reading.charges)
    amounts = _money_block(reading.charges, reading.totals)
    return {**identity, **amounts, **_references(reading.fields, reading.rows)}


def _require_identity(identity: dict[str, Any], charges: list[dict[str, Any]]) -> None:
    if not identity["vendor_invoice_number"]:
        raise StorageInvoiceParseError("Globelink tax invoice is missing Invoice No.")
    if not identity["client_name"]:
        raise StorageInvoiceParseError("Globelink tax invoice is missing the customer name.")
    if not charges:
        raise StorageInvoiceParseError("Globelink tax invoice has no charge lines.")


def _identity(text: str, rows: list[list[_Word]], fields: dict[str, str]) -> dict[str, Any]:
    return {
        "document_type": "TAX INVOICE",
        "document_status": _document_status(rows),
        "document_language": "en",
        "invoice_layout": "globelink_tax_invoice",
        "vendor_profile": GLOBELINK_VENDOR,
        "vendor_name": "GLOBELINK EGYPT",
        **_vendor_contact(text, rows),
        "vendor_invoice_number": _optional(fields.get("invoice no", "")),
        "invoice_date": _optional(fields.get("date", "")),
        "client_name": _client_name(rows, fields),
        "client_vat_number": _optional(fields.get("consignee vat no", "")),
        "prepared_by": _prepared_by(rows),
    }


def _vendor_contact(text: str, rows: list[list[_Word]]) -> dict[str, Optional[str]]:
    return {
        "vendor_address": _address(rows),
        "vendor_phone": _labeled_contact(text, "Tel"),
        "vendor_fax": _labeled_contact(text, "Fax"),
        "vendor_vat_number": _vendor_vat(text),
    }


def _labeled_contact(text: str, label: str) -> Optional[str]:
    match = re.search(rf"{label}:\s*(\+\d+\s*\d+)", text)
    return match.group(1) if match else None


def _vendor_vat(text: str) -> Optional[str]:
    match = re.search(r"Vat\s*No\.?\s*:?\s*(\d{6,})", text, re.IGNORECASE)
    return match.group(1) if match else None


def _address(rows: list[list[_Word]]) -> Optional[str]:
    lines = [_join(word.text for word in row).strip(" ,") for row in _address_rows(rows)]
    text = ", ".join(line for line in lines if line)
    return text or None


def _address_rows(rows: list[list[_Word]]) -> list[list[_Word]]:
    start = next((index for index, row in enumerate(rows) if _is_issuer_row(row)), None)
    if start is None:
        return []
    kept: list[list[_Word]] = []
    for row in rows[start + 1 :]:
        if any(word.text.startswith("Tel") for word in row):
            break
        kept.append([word for word in row if word.text != "DRAFT"])
    return kept


def _is_issuer_row(row: list[_Word]) -> bool:
    text = _join(word.text for word in row)
    return "GLOBELINK" in text and "EGYPT" in text


def _document_status(rows: list[list[_Word]]) -> Optional[str]:
    for row in rows:
        if any(word.text == "DRAFT" for word in row):
            return "DRAFT"
    return None


def _client_name(rows: list[list[_Word]], fields: dict[str, str]) -> Optional[str]:
    to_value = fields.get("to", "").strip()
    if to_value and not to_value.isdigit():
        return to_value
    return _name_under_to(rows) or None


def _name_under_to(rows: list[list[_Word]]) -> str:
    for index, row in enumerate(rows):
        if _row_has_label(_left(row), "to"):
            return _plain_left(rows, index + 1)
    return ""


def _row_has_label(words: list[_Word], key: str) -> bool:
    return any(_label_key(label) == key for label, _value in _pairs(words))


def _plain_left(rows: list[list[_Word]], index: int) -> str:
    if index >= len(rows):
        return ""
    words = _left(rows[index])
    if not words or any(":" in word.text for word in words):
        return ""
    return _join(word.text for word in words)


def _prepared_by(rows: list[list[_Word]]) -> Optional[str]:
    for index, row in enumerate(rows):
        texts = [word.text for word in row]
        if texts[:2] == ["Prepared", "by"] and index:
            name = [word.text for word in _left(rows[index - 1])]
            return _join(name) or None
    return None


def _references(fields: dict[str, str], rows: list[list[_Word]]) -> dict[str, Any]:
    loading, discharge = _ports(fields.get("pol fdest", ""))
    job_ref = _optional(fields.get("job ref", ""))
    return {
        **_shipment_refs(fields, job_ref, loading, discharge),
        "payment_term": _optional(fields.get("payment term", "")),
        "sn_dn_number": _sn_dn(fields),
        "remarks": _remarks(rows, fields),
        "house_bl_number": _optional(fields.get("hbl no", "")),
        "container_number": _optional(fields.get("cntr no", "")),
        "obl_number": _optional(fields.get("obl no", "")),
        "storage_place": _optional(fields.get("storage place", "")),
        "release_date": _optional(fields.get("release date", "")),
        "receipt_number": _optional(fields.get("receipt no", "")),
    }


def _shipment_refs(fields, job_ref, loading, discharge) -> dict[str, Any]:
    vessel, voyage = _vessel_voyage(fields.get("ves voy", ""))
    return {
        "job_ref": job_ref,
        "shipment_ref": job_ref,
        "imp_number": _optional(fields.get("imp no", "")),
        "eta_date": _optional(fields.get("eta pod", "")),
        "vessel_name": vessel,
        "voyage_number": voyage,
        "port_of_loading": loading,
        "port_of_discharge": discharge,
    }


def _vessel_voyage(value: str) -> tuple[Optional[str], Optional[str]]:
    text = _optional(value)
    if not text or "/" not in text:
        return text, None
    vessel, _, voyage = text.partition("/")
    return _optional(vessel), _optional(voyage)


def _ports(value: str) -> tuple[Optional[str], Optional[str]]:
    if "/" not in value:
        return None, _optional(value)
    loading, _, discharge = value.partition("/")
    return _optional(loading), _optional(discharge)


def _sn_dn(fields: dict[str, str]) -> Optional[str]:
    explicit = _optional(fields.get("sn dn no", ""))
    if explicit:
        return explicit
    to_value = fields.get("to", "").strip()
    if to_value.isdigit():
        return to_value
    return None


def _remarks(rows: list[list[_Word]], fields: dict[str, str]) -> Optional[str]:
    text = _join((fields.get("remarks", ""), _remark_continuation(rows)))
    return text or None


def _remark_continuation(rows: list[list[_Word]]) -> str:
    for index, row in enumerate(rows):
        if _row_has_label(_left(row), "remarks"):
            return _plain_left(rows, index + 1)
    return ""


def _charge_lines(rows: list[list[_Word]]) -> list[dict[str, Any]]:
    return [_charge(row) for row in rows if _is_charge(row)]


def _is_charge(row: list[_Word]) -> bool:
    texts = [word.text for word in row]
    if not texts or not texts[0].isdigit():
        return False
    has_currency = any(word.text in _CURRENCIES for word in row)
    return has_currency and any(_MONEY.match(word.text) for word in row)


def _charge(row: list[_Word]) -> dict[str, Any]:
    currency = next(word.text for word in row if word.text in _CURRENCIES)
    amount = _money(next(word.text for word in row if _MONEY.match(word.text)))
    return {"description": _charge_description(row), "currency": currency, "amount": amount}


def _charge_description(row: list[_Word]) -> str:
    words = [
        word.text
        for word in row
        if word.text not in _CURRENCIES and not _MONEY.match(word.text) and not word.text.isdigit()
    ]
    return _join(words)


def _invoice_totals(rows: list[list[_Word]]) -> tuple[float, float, float, str]:
    for index, row in enumerate(rows):
        texts = [word.text for word in row]
        if "Sub" in texts and "Total" in texts:
            return _read_totals(rows[index + 1 :], row)
    raise StorageInvoiceParseError("Globelink tax invoice is missing Sub Total.")


def _read_totals(following: list[list[_Word]], subtotal_row: list[_Word]) -> tuple[float, float, float, str]:
    return (
        _last_money(subtotal_row),
        _find_money(following, "VAT"),
        _find_money(following, "Total"),
        _amount_words(subtotal_row),
    )


def _last_money(row: list[_Word]) -> float:
    amounts = [word.text for word in row if _MONEY.match(word.text)]
    if not amounts:
        raise StorageInvoiceParseError("Globelink tax invoice is missing Sub Total.")
    return _money(amounts[-1])


def _find_money(rows: list[list[_Word]], label: str) -> float:
    for row in rows:
        amounts = [word.text for word in row if _MONEY.match(word.text)]
        if any(word.text == label for word in row) and len(amounts) == 1:
            return _money(amounts[0])
    raise StorageInvoiceParseError(f"Globelink tax invoice is missing {label}.")


def _amount_words(row: list[_Word]) -> str:
    words: list[str] = []
    for word in row:
        if word.text == "Sub":
            break
        words.append(word.text)
    if words[:2] == ["TOTAL", "EGP"]:
        words = words[2:]
    return _join(words)


def _money(token: str) -> float:
    return float(token.replace(",", ""))


def _money_block(charges: list[dict[str, Any]], totals: tuple[float, float, float, str]) -> dict[str, Any]:
    subtotal, tax_amount, total_amount, words = totals
    tax_rate = _tax_rate(subtotal, tax_amount)
    return {
        "currency": charges[0]["currency"],
        "subtotal_amount": subtotal,
        "tax_amount": tax_amount,
        "tax_rate": tax_rate,
        "total_amount": total_amount,
        "amount_in_words": words or None,
        "line_items": _line_items(charges, tax_rate, tax_amount),
    }


def _tax_rate(subtotal: float, tax_amount: float) -> Optional[str]:
    if subtotal <= 0:
        return None
    if abs((tax_amount / subtotal) - 0.14) < 0.005:
        return "14%"
    return None


def _line_items(
    charges: list[dict[str, Any]],
    tax_rate: Optional[str],
    tax_amount: float,
) -> list[dict[str, Any]]:
    items = [_line_item(charge) for charge in charges]
    if len(items) == 1:
        items[0]["tax_rate"] = tax_rate
        items[0]["tax_amount"] = tax_amount
    return items


def _line_item(charge: dict[str, Any]) -> dict[str, Any]:
    return {
        "service_description": charge["description"],
        "quantity": 1,
        "unit_price": charge["amount"],
        "currency": charge["currency"],
        "taxable_amount": charge["amount"],
        "tax_rate": None,
        "tax_amount": None,
        "total_amount": charge["amount"],
    }

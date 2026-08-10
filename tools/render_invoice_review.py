"""Render the first page of each invoice PDF for visual QA."""

from pathlib import Path
import re

import fitz


SERVICE_ROOT = Path(__file__).resolve().parents[1]
INVOICES_ROOT = SERVICE_ROOT.parent / "Invoices"
OUTPUT_ROOT = SERVICE_ROOT / "tmp" / "pdfs" / "invoice_review"


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for candidate in (*INVOICES_ROOT.rglob("*.pdf"), *INVOICES_ROOT.rglob("*.PDF")):
        paths[str(candidate.resolve()).casefold()] = candidate

    for path in sorted(paths.values(), key=lambda value: str(value).lower()):
        document = fitz.open(str(path))
        try:
            if len(document) == 0:
                continue
            image_name = re.sub(r"[^A-Za-z0-9]+", "_", path.stem)[:80] + "_p1.png"
            page = document[0]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.15, 1.15), alpha=False)
            pixmap.save(str(OUTPUT_ROOT / image_name))
            print(f"{path.name}\tpages={len(document)}\t{image_name}")
        finally:
            document.close()


if __name__ == "__main__":
    main()

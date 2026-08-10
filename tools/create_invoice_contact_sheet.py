"""Create a compact contact sheet of rendered invoice first pages."""

from pathlib import Path

from PIL import Image, ImageOps, ImageDraw


ROOT = Path(__file__).resolve().parents[1] / "tmp" / "pdfs" / "invoice_review"
OUTPUT = ROOT / "invoice_first_pages_contact_sheet.png"


def main() -> None:
    images = sorted(ROOT.glob("*_p1.png"), key=lambda path: path.name.lower())
    if not images:
        raise SystemExit("No rendered invoice pages found")
    thumb_width = 300
    margin = 24
    label_height = 28
    thumbs = []
    for path in images:
        image = Image.open(path).convert("RGB")
        ratio = thumb_width / image.width
        image = image.resize((thumb_width, int(image.height * ratio)))
        canvas = Image.new("RGB", (thumb_width, image.height + label_height), "white")
        canvas.paste(ImageOps.contain(image, (thumb_width, image.height)), (0, label_height))
        ImageDraw.Draw(canvas).text((6, 6), path.stem[:42], fill="black")
        thumbs.append(canvas)

    columns = 4
    rows = (len(thumbs) + columns - 1) // columns
    cell_width = thumb_width + margin
    cell_height = max(image.height for image in thumbs) + margin
    sheet = Image.new("RGB", (columns * cell_width + margin, rows * cell_height + margin), "#e8edf2")
    for index, image in enumerate(thumbs):
        x = margin + (index % columns) * cell_width
        y = margin + (index // columns) * cell_height
        sheet.paste(image, (x, y))
    sheet.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()

from spreadsheet_extractor import extract_document_text_professionally

filepath = r"C:\Users\AhmedAlaa\Downloads\LCL-HBLs\2311135-MESCO LCL-SZ TO ALEX.xlsx"
with open(filepath, "rb") as f:
    data = f.read()

extracted = extract_document_text_professionally(data, "2311135-MESCO LCL-SZ TO ALEX.xlsx")
records = extracted.get("records", [])
print("Found %d records" % len(records))
for rec in records:
    values = rec.get("values_by_header", {})
    print("\n--- Record row %d ---" % rec.get("source_row"))
    print("values_by_header keys: %s" % list(values.keys()))
    for k, v in sorted(values.items()):
        print("  %s = %s" % (k, v[:60] if v else "NONE"))
    hbl_keys = [k for k in values if 'hbl' in k.lower() or 'b/l' in k.lower() or 'house' in k.lower()]
    print("HBL-related keys: %s" % hbl_keys)

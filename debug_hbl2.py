from spreadsheet_extractor import extract_document_text_professionally
from main import _record_hbl, _is_manifest_record, _direct_manifest_record, _drop_empty_values

filepath = r"C:\Users\AhmedAlaa\Downloads\LCL-HBLs\2311135-MESCO LCL-SZ TO ALEX.xlsx"
with open(filepath, "rb") as f:
    data = f.read()

extracted = extract_document_text_professionally(data, "2311135-MESCO LCL-SZ TO ALEX.xlsx")
records = extracted.get("records", [])
print("Found %d records" % len(records))
for rec in records:
    values = rec.get("values_by_header", {})
    print("\n--- Record row %d ---" % rec.get("source_row"))
    hbl = _record_hbl(rec)
    print("_record_hbl return: %s" % repr(hbl))
    print("_is_manifest_record: %s" % _is_manifest_record(rec))
    
    # Check the exact match
    target_key = "H/BL Nos."
    if target_key in values:
        print("Direct key match '%s': %s" % (target_key, values[target_key]))
    else:
        print("NO direct match for '%s'" % target_key)
        # Show what's actually there
        for k in values:
            print("  key[%d]=%s" % (len(k), repr(k)))
    
    direct = _direct_manifest_record(rec)
    if direct:
        print("mesco_houseblno: %s" % repr(direct.get("mesco_houseblno")))
        conf = direct.get("confidence", {})
        print("confidence: %s" % conf)

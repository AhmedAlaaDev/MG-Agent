from spreadsheet_extractor import extract_xlsx_text

filepath = r"C:\Users\AhmedAlaa\Downloads\LCL-HBLs\2311135-MESCO LCL-SZ TO ALEX.xlsx"

with open(filepath, "rb") as f:
    data = f.read()

result = extract_xlsx_text(data, "2311135-MESCO LCL-SZ TO ALEX.xlsx")

print(f"Method: {result['method']}")
print(f"Records found: {len(result['records'])}")
print()

for r in result["records"]:
    print(f"Record {r['record_index']} (Row {r['source_row']}):")
    bl = r["values_by_header"].get("H/BL Nos.", "N/A")
    shipper = r["values_by_header"].get("Shipper", "N/A")[:60]
    consignee = r["values_by_header"].get("Consignee", "N/A")[:60]
    print(f"  H/BL: {bl}")
    print(f"  Shipper: {shipper}...")
    print(f"  Consignee: {consignee}...")
    print()
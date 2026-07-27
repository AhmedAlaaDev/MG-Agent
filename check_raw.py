import sys
sys.stdout.reconfigure(encoding='utf-8')

from spreadsheet_extractor import extract_xlsx_text

filepath = r"C:\Users\AhmedAlaa\Downloads\LCL-HBLs\2311135-MESCO LCL-SZ TO ALEX.xlsx"

with open(filepath, "rb") as f:
    data = f.read()

result = extract_xlsx_text(data, "2311135-MESCO LCL-SZ TO ALEX.xlsx")

print("Record 1 values_by_header:")
print("=" * 60)
for k, v in result["records"][0]["values_by_header"].items():
    print(f"  {k}: {v}")
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
# Secrets are now loaded from environment variables (e.g. .env file)
# os.environ["AZURE_OPENAI_ENDPOINT"] = "..."
# os.environ["AZURE_OPENAI_API_KEY"] = "..."

from spreadsheet_extractor import extract_xlsx_text
from ai_extractor import extract_with_azure_openai
from validator import validate_and_correct

filepath = r"C:\Users\AhmedAlaa\Downloads\LCL-HBLs\2311135-MESCO LCL-SZ TO ALEX.xlsx"

with open(filepath, "rb") as f:
    data = f.read()

extracted = extract_xlsx_text(data, "2311135-MESCO LCL-SZ TO ALEX.xlsx")
rec = extracted["records"][0]

print("Input text to AI:")
print("=" * 60)
print(rec["text"])
print()

print("AI extracted fields:")
print("=" * 60)
ai_result = extract_with_azure_openai(rec["text"])
validated = validate_and_correct(ai_result, rec["text"])

for k, v in validated.items():
    if v is not None and v != [] and v != {}:
        print(f"  {k}: {v}")
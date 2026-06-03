import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
# Secrets are now loaded from environment variables

from spreadsheet_extractor import extract_document_text_professionally
from ai_extractor import extract_with_azure_openai
from validator import validate_and_correct

filepath = r"C:\Users\AhmedAlaa\Downloads\LCL-HBLs\2311135-MESCO LCL-SZ TO ALEX.xlsx"

with open(filepath, "rb") as f:
    data = f.read()

extracted = extract_document_text_professionally(data, "2311135-MESCO LCL-SZ TO ALEX.xlsx")

records = extracted.get("records", [])
print(f"Found {len(records)} individual records in spreadsheet")
print()

for rec in records:
    record_text = rec.get("text", "")
    source_info = f"Sheet: {rec.get('sheet_name')}, Row: {rec.get('source_row')}, HBL: {rec.get('values_by_header', {}).get('H/BL Nos.', 'N/A')}"
    
    print(f"Processing: {source_info}")
    print("-" * 50)
    
    ai_result = extract_with_azure_openai(record_text)
    validated = validate_and_correct(ai_result, record_text)
    
    print(f"  mesco_masterblno: {validated.get('mesco_masterblno')}")
    print(f"  mesco_shippernamecontactno: {validated.get('mesco_shippernamecontactno', '')[:50]}...")
    print(f"  mesco_consigneenamecontactno: {validated.get('mesco_consigneenamecontactno', '')[:50]}...")
    print(f"  cr401_totalgrossweight: {validated.get('cr401_totalgrossweight')}")
    print(f"  mesco_hscode: {validated.get('mesco_hscode')}")
    print()
import os
# Secrets are now loaded from environment variables

from spreadsheet_extractor import extract_xlsx_text
from ai_extractor import extract_with_azure_openai
from validator import validate_and_correct

filepath = r"C:\Users\AhmedAlaa\Downloads\LCL-HBLs\2311135-MESCO LCL-SZ TO ALEX.xlsx"

print("1. Extracting spreadsheet...")
with open(filepath, "rb") as f:
    data = f.read()

extracted = extract_xlsx_text(data, "2311135-MESCO LCL-SZ TO ALEX.xlsx")
raw_text = extracted["text"]
records = extracted["records"]

print(f"   Records found: {len(records)}")
print(f"   Text length: {len(raw_text)}")
print()

# Process each record through AI
for rec in records:
    print(f"2. Processing Record {rec['record_index']} (H/BL: {rec['values_by_header'].get('H/BL Nos.', 'N/A')})")
    print("-" * 50)
    
    try:
        # Extract with AI
        ai_result = extract_with_azure_openai(rec["text"])
        
        # Validate
        validated = validate_and_correct(ai_result, rec["text"])
        
        print(f"   mesco_masterblno: {validated.get('mesco_masterblno')}")
        print(f"   mesco_shippernamecontactno: {validated.get('mesco_shippernamecontactno', '')[:60]}...")
        print(f"   mesco_consigneenamecontactno: {validated.get('mesco_consigneenamecontactno', '')[:60]}...")
        print(f"   mesco_origin: {validated.get('mesco_origin')}")
        print(f"   mesco_destination: {validated.get('mesco_destination')}")
        print(f"   cr401_totalgrossweight: {validated.get('cr401_totalgrossweight')}")
        print(f"   cr401_totalpackages: {validated.get('cr401_totalpackages')}")
        print(f"   mesco_containertype: {validated.get('mesco_containertype')}")
        print(f"   warnings: {validated.get('warnings', [])}")
        print()
    except Exception as e:
        print(f"   ERROR: {e}")
        print()
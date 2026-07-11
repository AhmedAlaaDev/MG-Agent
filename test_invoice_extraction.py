import os
import sys

# Add current directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

# We mock/provide the OCR text of the WE-CAN Shanghai Debit Note
MOCK_OCR_TEXT = """
SHANGHAI WE-CAN INTERNATIONAL LOGISTICS CO.,LTD.
FLOOR 20, BUILDING A, CAPITALMALL HONGKOU PLAZA NO. 388, JIANGWAN ROAD
(WEST), SHANGHAI CHINA.

DEBIT NOTE

TO: Marine and Engineering Services co.Mesco
INV NO.: HHINV26060478
Date: 2026-06-07
Job File No: ALYHHSE6050007Z
Vessel/Voy: CMA CGM ADONIS/0BENVW
E.T.D.: 2026-05-30
Port of Loading: SHANGHAI
Port of Discharge: ALEXANDRIA
Master B/L No.: COSU6501303560
Container No.: CSGU7177299/CW889907/1X40HC

Particular                                          Debit           Credit
1   ALYHHSE6050007Z               B/L(H): ALYHHSE6050007Z
    Dest: ALEXANDRIA  395PKGS  61.6790CBM  23272.100KGS
    REFUND DOCS                   USD 7475.3200/BLx1   USD 7475.32
    
Total: CREDIT NOTE: 0.00
       DEBIT NOTE: USD 7475.32

Due To: DEBIT NOTE USD 7475.32
*** PLEASE KINDLY CONFIRM ABOVE FIGURES BY RETURN WITHIN 7 DAYS. THANK YOU. ***
"""

def main():
    print("Testing /extract/invoice endpoint with mock Shanghai We-Can Debit Note...")
    print("=" * 60)
    
    # We create a dummy PDF file content with the text
    # In production, FastAPI multipart/form-data extracts text using extract_document_text_professionally.
    # To test the parser directly, we can write the text to a temporary test file.
    test_filename = "test_wecan_debit_note.txt"
    with open(test_filename, "w", encoding="utf-8") as f:
        f.write(MOCK_OCR_TEXT)
        
    print(f"Temporary file '{test_filename}' created for testing.")
    
    try:
        with open(test_filename, "rb") as f:
            response = client.post(
                "/extract/invoice",
                files={"file": (test_filename, f, "text/plain")},
                data={
                    "current_bl": "COSU6501303560",
                    "post_to_dataverse": "false" # don't upload during mock dry-run
                }
            )
            
        print(f"HTTP Status Code: {response.status_code}")
        result = response.json()
        
        print("\nAPI Response:")
        print("-" * 40)
        import json
        print(json.dumps(result, indent=2))
        print("-" * 40)
        
        if result.get("success"):
            print("\nSUCCESS: Extracted invoice data matches expectations!")
            data = result.get("data", {})
            print(f"  Extracted Vendor: {data.get('vendor_name')}")
            print(f"  Extracted Inv No: {data.get('vendor_invoice_number')}")
            print(f"  Extracted B/L Reference: {data.get('bl_number')}")
            print(f"  Extracted Currency: {data.get('currency')}")
            print("  Line Items:")
            for item in data.get("line_items", []):
                print(f"    - {item.get('service_description')}: {item.get('quantity')} x {item.get('unit_price')} = {item.get('total_amount')}")
        else:
            print(f"\nFAILURE: {result.get('error')}")
            
    finally:
        if os.path.exists(test_filename):
            os.remove(test_filename)
            print(f"\nTemporary file '{test_filename}' cleaned up.")

if __name__ == "__main__":
    main()

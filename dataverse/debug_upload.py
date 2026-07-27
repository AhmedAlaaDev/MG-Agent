"""Debug: try uploading the actual CRM JSON from a PDF extraction."""
import json, logging, sys
logging.basicConfig(level=logging.INFO)

sys.path.insert(0, r"C:\Users\AhmedAlaa\Downloads\intelligent_bl_extractor_fastapi")

# 1. First run a PDF extraction to get the CRM output
from spreadsheet_extractor import extract_document_text_professionally
from ai_extractor import extract_with_azure_openai
from validator import validate_and_correct
from crm_output_formatter import records_to_master_json
from dataverse_client_service import DataverseClientService, RetryConfig

# Use a test PDF
import glob
pdfs = glob.glob(r"C:\Users\AhmedAlaa\Downloads\*.pdf") + glob.glob(r"C:\Users\AhmedAlaa\Downloads\*.PDF")
if pdfs:
    pdf_path = pdfs[0]
    print(f"Using PDF: {pdf_path}")
    with open(pdf_path, "rb") as f:
        data = f.read()
    
    extracted = extract_document_text_professionally(data, pdf_path)
    raw_text = extracted.get("text", "")
    records = extracted.get("records", [])
    
    if records:
        for rec in records:
            record_text = rec.get("text", "")
            if record_text:
                ai_result = extract_with_azure_openai(record_text)
                validated = validate_and_correct(ai_result, record_text)
                crm = records_to_master_json([validated])
                break
    else:
        # No records, use whole document
        ai_result = extract_with_azure_openai(raw_text)
        validated = validate_and_correct(ai_result, raw_text)
        crm = records_to_master_json([validated])
    
    print("\n=== CRM Output ===")
    print(json.dumps(crm, indent=2, default=str)[:3000])
    print("...")
    
    # 2. Now try uploading to Dataverse
    print("\n=== Attempting Dataverse upload ===")
    client = DataverseClientService.get_instance(RetryConfig(max_retries=0, delay=1000))
    
    from dataverse_uploader import _clean_odata_meta, _strip_null, _lookup_key
    
    # Clean the payload
    payload = _clean_odata_meta(crm)
    payload = _strip_null(payload)
    
    # Get master fields
    master_fields = dict(payload)
    master_fields.pop("mesco_Operation_mesco_Operation_mesco_Operation", None)
    master_fields.pop("mesco_Container_MasterOperation_mesco_Operation", None)
    master_fields.pop("mesco_Cargo_MasterOperation_mesco_Operation", None)
    
    # Remove any internal/unknown fields that might cause issues
    problematic = [k for k in master_fields if k.startswith('_') or k.startswith('@') or 'odata' in k.lower()]
    for k in problematic:
        master_fields.pop(k, None)
        print(f"  Removed problematic key: {k}")
    
    print(f"\nMaster fields count: {len(master_fields)}")
    print(f"Master field names: {list(master_fields.keys())}")
    
    # POST with full payload to see error
    try:
        resp = client._session.request(
            "POST",
            f"{client.base_url}/mesco_operations",
            json=master_fields,
            headers={"Authorization": f"Bearer {client._get_access_token()}"}
        )
        print(f"\nStatus: {resp.status_code}")
        if resp.status_code >= 400:
            print(f"Error: {resp.text[:2000]}")
        else:
            print(f"Location: {resp.headers.get('Location', 'N/A')}")
            print("SUCCESS!")
    except Exception as e:
        print(f"Exception: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text[:2000]}")
else:
    print("No PDF files found in Downloads")

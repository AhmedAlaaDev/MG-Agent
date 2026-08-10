import os
from dotenv import load_dotenv
load_dotenv()
from dataverse.client_service import DataverseClientService

client = DataverseClientService.get_instance()
try:
    resp = client.get("mesco_operations?$filter=mesco_masterblno eq 'TEST_MBL_999'")
    rows = resp.json().get("value", [])
    for row in rows:
        client.delete(f"mesco_operations({row['mesco_operationid']})")
        print("Deleted", row['mesco_operationid'])
except Exception as e:
    import traceback
    traceback.print_exc()

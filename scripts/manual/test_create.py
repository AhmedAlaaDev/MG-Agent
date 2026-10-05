import os
from dotenv import load_dotenv
load_dotenv()
from app.infrastructure.dataverse.client_service import DataverseClientService
from app.infrastructure.dataverse.uploader import _create_entity

client = DataverseClientService.get_instance()
op_fields = {
    "mesco_code": "TEST_MBL_999",
    "mesco_masterblno": "TEST_MBL_999",
    "mesco_bltype": 886150001,  # Master B/L
}
try:
    master_id = _create_entity(client, "mesco_operations", op_fields)
    print("SUCCESS, created master:", master_id)
except Exception as e:
    import traceback
    traceback.print_exc()

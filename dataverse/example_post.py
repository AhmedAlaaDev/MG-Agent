"""
Example: Post extracted B/L data to Dynamics 365 Dataverse.

Usage:
    python -m dataverse.example_post

Requires these in .env:
    TENANT_ID, CLIENT_ID, CLIENT_SECRET, AZURE_APP_API_URL
"""

import json
import logging

logging.basicConfig(level=logging.INFO)

from dataverse import DataverseClientService, RetryConfig

# 1. Get the shared client (singleton)
client = DataverseClientService.get_instance(
    RetryConfig(max_retries=3, delay=2000, use_exponential_backoff=True)
)

# 2. Build the master operation payload (matches master.json structure)
payload = {
    "mesco_masterblno": "10225983",
    "mesco_vessel": "MUSTAFA DAYI",
    "mesco_voytruckno": None,
    "mesco_origin": "KUMPORT",
    "mesco_destination": "Port Said West",
    "mesco_transporttype": 300000000,  # Sea
    "mesco_loadtype": 300000001,       # LCL
    "mesco_direction": 300000000,      # Import
    "mesco_shippernamecontactno": "STRAIGHT LINE LOGISTIC & TRADE LIMITED",
    "mesco_consigneenamecontactno": "STRAIGHT LINE 52 MAKRAM EBID, NASR CITY, CAIRO, EGYPT",
    "cr401_totalgrossweight": 3455.84,
    "cr401_totalvolume": 15.54,
    "cr401_totalpackages": 51.0,
    "mesco_bltype": 886150001,         # Master
    "mesco_pcfreightterm": 100000001,  # Collect
    "mesco_nooforgbls": "3",
}

# 3. POST to create the master operation
#    Dataverse entity collection path: mesco_operations
print("Creating master operation...")
try:
    resp = client.post("mesco_operations", json=payload)
    print(f"Created! Status: {resp.status_code}")
    created = resp.json()
    master_id = created.get("mesco_operationid")
    print(f"Master operation ID: {master_id}")
except Exception as e:
    print(f"Error: {e}")
    # For demo purposes, show what would be sent
    print("\n--- Payload that would be POSTED ---")
    print(json.dumps(payload, indent=2, default=str))

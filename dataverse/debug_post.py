"""Debug Dataverse connection and test a simple POST."""
import json, logging
logging.basicConfig(level=logging.INFO)

from dataverse import DataverseClientService, RetryConfig

client = DataverseClientService.get_instance(
    RetryConfig(max_retries=1, delay=1000)
)

# 1. Test getting a token
print("=== Step 1: Get Access Token ===")
try:
    token = client._get_access_token()
    print(f"Token obtained: {token[:50]}...{token[-20:]}")
    print(f"Token length: {len(token)}")
except Exception as e:
    print(f"Token error: {e}")

# 2. Test a simple POST with minimal payload
print("\n=== Step 2: Minimal POST to mesco_operations ===")
minimal = {
    "mesco_masterblno": "TEST-DEBUG-001",
    "mesco_bltype": 886150001,
}
try:
    resp = client.post("mesco_operations", json=minimal)
    print(f"Status: {resp.status_code}")
    print(f"Headers: {dict(resp.headers)}")
    if resp.content:
        print(f"Body: {resp.json()}")
except Exception as e:
    print(f"POST error: {e}")
    if hasattr(e, 'response') and e.response is not None:
        print(f"Response status: {e.response.status_code}")
        print(f"Response body: {e.response.text[:2000]}")

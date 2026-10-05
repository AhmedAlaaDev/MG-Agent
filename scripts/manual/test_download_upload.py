import requests, json

# Create a simple text file that looks like a BL
bl_text = """SHIPPER: TEST SHIPPER
CONSIGNEE: TEST CONSIGNEE
MASTER B/L NO: TEST-MBL-001
VESSEL: TEST VESSEL
VOYAGE NO: 1234
PORT OF LOADING: HAMBURG
PORT OF DISCHARGE: ALEXANDRIA
CARGO DESCRIPTION: MACHINE PARTS
TOTAL GROSS WEIGHT: 1000 KGS
TOTAL VOLUME: 5 CBM
TOTAL PACKAGES: 10
H.S. CODE: 82089000
DATE OF ISSUE: 2026-03-25
PLACE OF ISSUE: GOTHENBURG"""

import io
files = {'file': ('test_bl.txt', bl_text.encode('utf-8'), 'text/plain')}

# Without download (normal response)
resp = requests.post('http://localhost:8001/extract/file?post_to_dataverse=false', files=files)
print(f'Normal response: {resp.status_code}, Content-Type: {resp.headers.get("Content-Type")}')
d = resp.json()
print(f'  success={d.get("success")}, has data={d.get("data") is not None}')

# With download=true
files2 = {'file': ('test_bl.txt', bl_text.encode('utf-8'), 'text/plain')}
resp2 = requests.post('http://localhost:8001/extract/file?download=true&post_to_dataverse=false', files=files2)
print(f'\nDownload response: {resp2.status_code}, Content-Type: {resp2.headers.get("Content-Type")}')
print(f'  Content-Disposition: {resp2.headers.get("Content-Disposition")}')
d2 = resp2.json()
print(f'  Has mesco_masterblno: {"mesco_masterblno" in d2}')
print(f'  File size: {len(resp2.content)} bytes')

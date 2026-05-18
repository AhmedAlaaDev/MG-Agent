import urllib.request, json
from urllib.parse import urlencode

# Test 1: Excel file extraction
print("=" * 60)
print("TEST 1: Excel /extract/file")
print("=" * 60)

url = 'http://localhost:8000/extract/file'
filepath = r'C:\Users\AhmedAlaa\Downloads\LCL-HBLs\2311135-MESCO LCL-SZ TO ALEX.xlsx'

boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
body = []
with open(filepath, 'rb') as f:
    file_data = f.read()

body.append(b'--' + boundary.encode())
body.append(b'Content-Disposition: form-data; name="file"; filename="2311135-MESCO LCL-SZ TO ALEX.xlsx"')
body.append(b'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
body.append(b'')
body.append(file_data)
body.append(b'--' + boundary.encode() + b'--')
body_data = b'\r\n'.join(body)

req = urllib.request.Request(url, data=body_data)
req.add_header('Content-Type', 'multipart/form-data; boundary=' + boundary)
resp = urllib.request.urlopen(req)
data = json.loads(resp.read().decode('utf-8'))

if data.get('success'):
    crm = data.get('data', {})
    print('SUCCESS')
    print('Master keys:', list(crm.keys()))
    print('mesco_houseblno in master:', 'mesco_houseblno' in crm)
    print('containers in master:', 'containers' in crm)
    ops = crm.get('mesco_Operation_mesco_Operation_mesco_Operation', [])
    print('Houses: %d' % len(ops))
    for i, op in enumerate(ops):
        print('  [%d] mesco_masterblno: %s' % (i+1, op.get('mesco_masterblno')))
else:
    print('ERROR: %s' % data.get('error', 'unknown'))

# Test 2: /extract/crm with master.json payload
print()
print("=" * 60)
print("TEST 2: CRM /extract/crm with master.json")
print("=" * 60)

with open('master.json', 'r', encoding='utf-8') as f:
    master_payload = json.load(f)

crm_url = 'http://localhost:8000/extract/crm'
crm_body = json.dumps({"crm_json": master_payload}).encode('utf-8')
crm_req = urllib.request.Request(crm_url, data=crm_body)
crm_req.add_header('Content-Type', 'application/json')
crm_resp = urllib.request.urlopen(crm_req)
crm_data = json.loads(crm_resp.read().decode('utf-8'))

if crm_data.get('success'):
    records = crm_data.get('records', [])
    print('SUCCESS: %d records extracted' % len(records))
    for i, rec in enumerate(records):
        print('  [%d] MBL: %s, Vessel: %s, Consignee: %s' % (
            i+1,
            rec.get('mesco_masterblno', 'N/A'),
            rec.get('mesco_vessel', 'N/A'),
            (rec.get('mesco_consigneenamecontactno') or 'N/A')[:40]
        ))
else:
    print('ERROR: %s' % crm_data.get('error', 'unknown'))

# Test 3: /extract/crm with house.json payload
print()
print("=" * 60)
print("TEST 3: CRM /extract/crm with house.json")
print("=" * 60)

with open('house.json', 'r', encoding='utf-8') as f:
    house_payload = json.load(f)

h_body = json.dumps({"crm_json": house_payload}).encode('utf-8')
h_req = urllib.request.Request(crm_url, data=h_body)
h_req.add_header('Content-Type', 'application/json')
h_resp = urllib.request.urlopen(h_req)
h_data = json.loads(h_resp.read().decode('utf-8'))

if h_data.get('success'):
    records = h_data.get('records', [])
    print('SUCCESS: %d records extracted' % len(records))
    for i, rec in enumerate(records):
        print('  [%d] MBL: %s, HBL: %s, Vessel: %s' % (
            i+1,
            rec.get('mesco_masterblno', 'N/A'),
            rec.get('mesco_houseblno', 'N/A'),
            rec.get('mesco_vessel', 'N/A')
        ))
else:
    print('ERROR: %s' % h_data.get('error', 'unknown'))

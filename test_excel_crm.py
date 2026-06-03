import urllib.request, json, io, os
from urllib.parse import urlencode

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
    eq = data.get('extraction_quality', {})
    print('SUCCESS')
    rr = eq.get('record_routing', {})
    print('Records: direct=%s, azure=%s, skipped=%s' % (rr.get('direct'), rr.get('azure_fallback'), rr.get('skipped')))
    crm = data.get('data', {})
    if crm:
        ops = crm.get('mesco_Operation_mesco_Operation_mesco_Operation', [])
        print('Houses in CRM output: %d' % len(ops))
        for i, op in enumerate(ops):
            mbl = op.get('mesco_masterblno', 'N/A')
            hbl = op.get('mesco_houseblno') or op.get('mesco_hblno') or op.get('mesco_houseno', 'N/A')
            booking = op.get('mesco_bookingnumber', 'N/A')
            print('  [%d] MBL: %s, HBL: %s, Booking: %s' % (i+1, mbl, hbl, booking))
            # Print all keys for first record
            if i == 0:
                print('  All keys: %s' % list(op.keys()))
        containers = crm.get('mesco_Container_MasterOperation_mesco_Operation', [])
        cargo = crm.get('mesco_Cargo_MasterOperation_mesco_Operation', [])
        print('Containers: %d, Cargo items: %d' % (len(containers), len(cargo)))
        if containers:
            print('  Container keys: %s' % list(containers[0].keys()))
        if cargo:
            print('  Cargo keys: %s' % list(cargo[0].keys()))
else:
    print('ERROR: %s' % data.get('error', 'unknown'))

# Also save raw output for inspection
with open('_last_excel_crm_output.json', 'w') as f:
    json.dump(data, f, indent=2, default=str)
print('Full output saved to _last_excel_crm_output.json')

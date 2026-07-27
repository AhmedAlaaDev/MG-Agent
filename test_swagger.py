import requests, json

r = requests.get('http://localhost:8002/openapi.json')
spec = r.json()
schemas = spec.get('components', {}).get('schemas', {})

print("=== Response Schemas ===")
for name, schema in sorted(schemas.items()):
    props = schema.get('properties', {})
    prop_names = list(props.keys())[:5]
    print(f"  {name}: {list(prop_names)}...")

# Check extract/file response
ep = '/extract/file'
post = spec['paths'][ep]['post']
resp = post.get('responses', {}).get('200', {})
print(f"\n=== /extract/file 200 response ===")
print(f"  description: {resp.get('description', '')}")
content = resp.get('content', {})
for media_type, media_schema in content.items():
    ref = media_schema.get('schema', {}).get('$ref', 'N/A')
    print(f"  {media_type}: {ref}")

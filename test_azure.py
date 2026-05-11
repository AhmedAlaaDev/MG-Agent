import os
from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv()

endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
api_key = os.getenv("AZURE_OPENAI_API_KEY")
api_version = os.getenv("AZURE_OPENAI_API_VERSION")

print(f"Testing Endpoint: {endpoint}")
print(f"Testing API Version: {api_version}")

client = AzureOpenAI(
    azure_endpoint=endpoint,
    api_key=api_key,
    api_version=api_version,
)

# In Azure OpenAI, you can't easily list "Deployments" via the chat client,
# but we can try a dummy call to see if we can get a better error or list models.
print("\n--- Available Models (may not show deployment names) ---")
try:
    models = client.models.list()
    for model in models:
        print(f"Model ID: {model.id}")
except Exception as e:
    print(f"Error listing models: {e}")

print("\n--- Testing Common Deployment Names ---")
test_names = ["gpt-4o-mini", "gpt-4o", "houseblreader", "houseblreader-deployment", "default"]
for name in test_names:
    print(f"Testing '{name}'...", end=" ", flush=True)
    try:
        client.chat.completions.create(
            model=name,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1
        )
        print("SUCCESS!")
        print(f"\n>>> FOUND IT! Use this in .env: AZURE_OPENAI_DEPLOYMENT={name}")
        break
    except Exception as e:
        if "DeploymentNotFound" in str(e):
            print("Not found.")
        else:
            print(f"Error: {e}")

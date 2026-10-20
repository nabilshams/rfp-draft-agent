import os

from azure.ai.agents import AgentsClient
from azure.identity import DefaultAzureCredential

endpoint = os.environ["FOUNDRY_AGENTS_ENDPOINT"]
client = AgentsClient(endpoint=endpoint, credential=DefaultAzureCredential())

existing = {a.name: a.id for a in client.list_agents()}
print("Existing agents:", existing)

if "rfp-draft-agent" not in existing:
    a = client.create_agent(model="gpt-4o", name="rfp-draft-agent", instructions="RFP Response Assistant")
    print(f"Created draft: {a.id}")

if "rfp-qa-agent" not in existing:
    a = client.create_agent(model="gpt-4o", name="rfp-qa-agent", instructions="RFP QA Assistant")
    print(f"Created qa: {a.id}")

print("\nAll agents:")
for a in client.list_agents():
    print(f"  {a.name}: {a.id}")

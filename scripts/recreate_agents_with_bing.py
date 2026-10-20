from azure.ai.agents import AgentsClient
from azure.ai.agents.models import BingGroundingTool
from azure.identity import DefaultAzureCredential
import os

# Project endpoint, e.g. https://<account>.services.ai.azure.com/api/projects/<project>
endpoint = os.environ["FOUNDRY_AGENTS_ENDPOINT"]
client = AgentsClient(endpoint=endpoint, credential=DefaultAzureCredential())

BING_CONN = os.environ["BING_CONNECTION_ID"]

# Delete old agents
print("Cleaning up old agents...")
for a in list(client.list_agents()):
    if a.name in ("rfp-draft-agent", "rfp-qa-agent", "test-bing-agent"):
        print(f"  Deleting: {a.name} ({a.id})")
        client.delete_agent(a.id)

# Create new agents with Bing Grounding
bing_tool = BingGroundingTool(connection_id=BING_CONN)
print(f"\nCreating agents with Bing grounding (connection: ...{BING_CONN[-30:]})...")

a1 = client.create_agent(
    model="gpt-4o",
    name="rfp-draft-agent",
    instructions="RFP Response Assistant with web search capability.",
    tools=bing_tool.definitions,
)
print(f"  rfp-draft-agent: {a1.id}")

a2 = client.create_agent(
    model="gpt-4o",
    name="rfp-qa-agent",
    instructions="RFP QA Assistant with web search capability.",
    tools=bing_tool.definitions,
)
print(f"  rfp-qa-agent: {a2.id}")

print("\nAll agents:")
for a in client.list_agents():
    tools = [t.type if hasattr(t, "type") else str(t) for t in (a.tools or [])]
    print(f"  {a.name}: {a.id}  tools={tools}")

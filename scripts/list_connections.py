import os

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

endpoint = os.environ["FOUNDRY_AGENTS_ENDPOINT"]
client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())

for conn in client.connections.list():
    print(f"  Name: {conn.name}")
    print(f"  ID: {conn.id}")
    # Print all attributes
    for attr in dir(conn):
        if not attr.startswith("_"):
            try:
                val = getattr(conn, attr)
                if not callable(val):
                    print(f"    {attr}: {val}")
            except:
                pass
    print("---")

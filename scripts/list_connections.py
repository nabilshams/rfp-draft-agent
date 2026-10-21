"""List the connections of a Foundry project (to find the resource ID to use as BING_CONNECTION_ID).

Usage:
    FOUNDRY_AGENTS_ENDPOINT=https://<account>.services.ai.azure.com/api/projects/<project> \\
        python scripts/list_connections.py
"""

import os

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential


def main() -> None:
    endpoint = os.environ["FOUNDRY_AGENTS_ENDPOINT"]
    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())

    for conn in client.connections.list():
        print(f"Name: {conn.name}")
        print(f"ID:   {conn.id}")
        print("---")


if __name__ == "__main__":
    main()

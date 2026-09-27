"""Prompt agent `financiero`: análisis de estados financieros de banca corporativa.

Config propia del agente en agents/financiero/.env (MODEL, etc.). El estado
financiero llega adjunto en cada conversación; el intérprete de código lo lee
del propio turno, no de un archivo fijo subido de antemano.

Publicar una nueva versión en Microsoft Foundry:
    python agents/financiero/main.py --publish

Probarlo contra la versión publicada:
    python agents/financiero/main.py -m "¿Qué analizas?"
"""

import argparse
import asyncio
import os
from pathlib import Path

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient, to_prompt_agent
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import AzureCliCredential
from dotenv import load_dotenv

AGENT_DIR = Path(__file__).parent
REPO_ROOT = AGENT_DIR.parents[1]

AGENT_NAME = "financiero"
DESCRIPTION = "Analista de crédito de banca corporativa peruana sobre estados financieros."
INSTRUCTIONS = (AGENT_DIR / "instructions.md").read_text(encoding="utf-8").rstrip()


def build_agent(client: FoundryChatClient) -> Agent:
    return Agent(
        name=AGENT_NAME,
        description=DESCRIPTION,
        client=client,
        instructions=INSTRUCTIONS,
        tools=[
            client.get_code_interpreter_tool(),
        ],
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Publica una nueva versión en Foundry.")
    parser.add_argument("-m", "--message", default="Preséntate en una frase.")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")  # config compartida del proyecto Foundry
    load_dotenv(AGENT_DIR / ".env")  # config propia de este agente

    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        raise SystemExit("Falta FOUNDRY_PROJECT_ENDPOINT (defínelo en .env en la raíz del repo).")
    model = os.environ.get("MODEL")
    if not model:
        raise SystemExit(f"Falta MODEL (defínelo en {AGENT_DIR / '.env'}).")

    async with AzureCliCredential() as credential:
        client = FoundryChatClient(project_endpoint=endpoint, model=model, credential=credential)
        agent = build_agent(client)

        if args.publish:
            async with AIProjectClient(endpoint=endpoint, credential=credential) as project:
                version = await project.agents.create_version(
                    agent_name=AGENT_NAME,
                    definition=to_prompt_agent(agent),
                    description=DESCRIPTION,
                )
            print(f"Publicado {version.name} v{version.version}")
        else:
            print((await agent.run(args.message)).text)


if __name__ == "__main__":
    asyncio.run(main())

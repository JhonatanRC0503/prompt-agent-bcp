"""Prompt agent `financiero`: análisis de estados financieros de banca corporativa.

Config propia del agente en agents/financiero/.env (MODEL, BLOB_CONTAINER).
La cuenta de Blob Storage se resuelve en tiempo de ejecución a través de las
conexiones del proyecto de Foundry, así que este mismo código funciona en
cualquier tenant/proyecto sin tocar una línea.

El analista solo da el RUC: el agente llama a `obtener_estados_financieros`,
que busca el Excel de esa empresa en el repositorio y lo devuelve como JSON.

Publicar una nueva versión en Microsoft Foundry:
    python agents/financiero/main.py --publish

Probarlo contra la versión publicada:
    python agents/financiero/main.py -m "Analiza el RUC 20512437891"
"""

import argparse
import asyncio
import os
from pathlib import Path

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient, to_prompt_agent
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import Connection, ConnectionType, PromptAgentDefinition
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv

from tools import build_obtener_estados_financieros

AGENT_DIR = Path(__file__).parent
REPO_ROOT = AGENT_DIR.parents[1]

AGENT_NAME = "financiero"
DESCRIPTION = "Analista de crédito de banca corporativa peruana sobre estados financieros."
INSTRUCTIONS = (AGENT_DIR / "instructions.md").read_text(encoding="utf-8").rstrip()


async def find_storage_connection(project: AIProjectClient) -> Connection:
    """Devuelve la conexión de Blob Storage del proyecto, sin hardcodear su URL."""
    async for connection in project.connections.list(
        connection_type=ConnectionType.AZURE_STORAGE_ACCOUNT
    ):
        return connection
    raise SystemExit(
        "El proyecto de Foundry no tiene ninguna conexión de Azure Storage. "
        "Agrégala en Administrar > Detalles del proyecto > Recursos conectados."
    )


def build_agent(client: FoundryChatClient, obtener_estados_financieros) -> Agent:
    return Agent(
        name=AGENT_NAME,
        description=DESCRIPTION,
        client=client,
        instructions=INSTRUCTIONS,
        tools=[
            obtener_estados_financieros,
            client.get_code_interpreter_tool(),
        ],
    )


def definicion_publicable(agent: Agent) -> PromptAgentDefinition:
    """Definición del agente lista para Foundry.

    `to_prompt_agent` marca las function tools como `strict`, y en ese modo Foundry exige
    `additionalProperties: false` en el schema, que el Agent Framework no incluye; sin esto
    el agente falla con `invalid_function_parameters`.
    """
    definicion = to_prompt_agent(agent)
    for tool in definicion.tools:
        if getattr(tool, "type", None) == "function":
            tool.parameters.setdefault("additionalProperties", False)
    return definicion


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
    container = os.environ.get("BLOB_CONTAINER")
    if not container:
        raise SystemExit(f"Falta BLOB_CONTAINER (defínelo en {AGENT_DIR / '.env'}).")

    # Resuelve `az login` en local y la identidad administrada al desplegar en Azure.
    async with DefaultAzureCredential() as credential:
        client = FoundryChatClient(project_endpoint=endpoint, model=model, credential=credential)

        async with AIProjectClient(endpoint=endpoint, credential=credential) as project:
            storage = await find_storage_connection(project)
            agent = build_agent(
                client,
                build_obtener_estados_financieros(storage.target, container, credential),
            )

            if args.publish:
                version = await project.agents.create_version(
                    agent_name=AGENT_NAME,
                    definition=definicion_publicable(agent),
                    description=DESCRIPTION,
                )
                print(f"Publicado {version.name} v{version.version}")
            else:
                print((await agent.run(args.message)).text)


if __name__ == "__main__":
    asyncio.run(main())

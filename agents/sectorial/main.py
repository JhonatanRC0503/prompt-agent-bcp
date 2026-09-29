"""Prompt agent `sectorial`: análisis sectorial de riesgos de banca corporativa.

Config propia del agente en agents/sectorial/.env (MODEL, KNOWLEDGE_BASE_NAME).
La base de conocimiento se resuelve en tiempo de ejecución a través de las
conexiones del proyecto de Foundry: no se hardcodea ninguna URL ni ID de
conexión, así que este mismo código funciona en cualquier tenant/proyecto que
tenga una base de conocimiento con el nombre indicado en KNOWLEDGE_BASE_NAME.

Publicar una nueva versión en Microsoft Foundry:
    python agents/sectorial/main.py --publish

Probarlo contra la versión publicada:
    python agents/sectorial/main.py -m "¿Qué analizas?"
"""

import argparse
import asyncio
import os
from pathlib import Path

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient, to_prompt_agent
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import Connection
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv

AGENT_DIR = Path(__file__).parent # agents/sectorial
REPO_ROOT = AGENT_DIR.parents[1] # raíz del repo

AGENT_NAME = "sectorial"
DESCRIPTION = "Analista sectorial senior de riesgos de banca corporativa y de negocios."
INSTRUCTIONS = (AGENT_DIR / "instructions.md").read_text(encoding="utf-8").rstrip()


async def find_knowledge_base_connection(project: AIProjectClient, name: str) -> Connection:
    """Busca, entre las conexiones del proyecto, la base de conocimiento con este nombre.

    El portal de Foundry crea una conexión tipo MCP por cada base de conocimiento que
    se conecta a un agente (metadata.type == "knowledgeBase_MCP"). Buscarla por nombre
    en vez de guardar su connection_id/URL hace que el código sea portable entre
    proyectos y tenants.
    """
    async for connection in project.connections.list():
        metadata = connection.metadata or {}
        if metadata.get("type") == "knowledgeBase_MCP" and metadata.get("knowledgeBaseName") == name:
            return connection
    raise SystemExit(
        f"No se encontró ninguna base de conocimiento llamada '{name}' en el proyecto. "
        "Créala en Foundry (Compilación > Conocimiento) o revisa KNOWLEDGE_BASE_NAME."
    )


def build_agent(client: FoundryChatClient, knowledge_base: Connection) -> Agent:
    return Agent(
        name=AGENT_NAME,
        description=DESCRIPTION,
        client=client,
        instructions=INSTRUCTIONS,
        tools=[
            client.get_web_search_tool(),
            # El analista adjunta el documento (informe comercial, estados financieros,
            # etc.) en cada conversación; el modelo lo lee del propio turno.
            client.get_code_interpreter_tool(),
            client.get_mcp_tool(
                name=knowledge_base.name,
                url=knowledge_base.target,
                project_connection_id=knowledge_base.name,
                approval_mode="never_require",
            ),
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
    knowledge_base_name = os.environ.get("KNOWLEDGE_BASE_NAME")
    if not knowledge_base_name:
        raise SystemExit(f"Falta KNOWLEDGE_BASE_NAME (defínelo en {AGENT_DIR / '.env'}).")

    # Resuelve `az login` en local y la identidad administrada al desplegar en Azure.
    async with DefaultAzureCredential() as credential:
        client = FoundryChatClient(project_endpoint=endpoint, model=model, credential=credential)

        async with AIProjectClient(endpoint=endpoint, credential=credential) as project:
            knowledge_base = await find_knowledge_base_connection(project, knowledge_base_name)
            agent = build_agent(client, knowledge_base)

            if args.publish:
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

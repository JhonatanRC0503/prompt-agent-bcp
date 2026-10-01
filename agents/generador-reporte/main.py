"""Prompt agent `generador-reporte`: integra los análisis de los tres especialistas.

Es el agente final del flujo. No analiza: recibe los textos ya producidos por `financiero`,
`sectorial` y `reportes-previos` y los funde en un informe de crédito único, detectando
contradicciones entre ellos.

No lleva herramientas a propósito. Es una tarea de síntesis sobre el texto que recibe en el
propio turno: no busca en web, no consulta bases de conocimiento y no recalcula ratios (las
cifras vienen ya calculadas por el agente financiero). Darle herramientas solo añadiría latencia
y la tentación de inventar datos que no están en la entrada.

Publicar una nueva versión en Microsoft Foundry:
    python agents/generador-reporte/main.py --publish

Probarlo contra la versión publicada:
    python agents/generador-reporte/main.py -m "EMPRESA: ACME ..."
"""

import argparse
import asyncio
import os
from pathlib import Path

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient, to_prompt_agent
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv

AGENT_DIR = Path(__file__).parent  # agents/generador-reporte
REPO_ROOT = AGENT_DIR.parents[1]  # raíz del repo

# El nombre coincide con el de la carpeta a propósito: `scripts/pull_agents.py` escribe las
# instrucciones en `agents/<nombre-del-agente-en-foundry>/`, así que si divergen se crearía una
# carpeta paralela. Foundry además no acepta guion bajo en el nombre.
AGENT_NAME = "generador-reporte"
DESCRIPTION = "Redactor final que integra los análisis financiero, sectorial e histórico en un informe de crédito."
INSTRUCTIONS = (AGENT_DIR / "instructions.md").read_text(encoding="utf-8").rstrip()


def build_agent(client: FoundryChatClient) -> Agent:
    return Agent(
        name=AGENT_NAME,
        description=DESCRIPTION,
        client=client,
        instructions=INSTRUCTIONS,
        tools=[],  # ver el docstring del módulo: la síntesis no necesita herramientas
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

    # Resuelve `az login` en local y la identidad administrada al desplegar en Azure.
    async with DefaultAzureCredential() as credential:
        client = FoundryChatClient(project_endpoint=endpoint, model=model, credential=credential)
        agent = build_agent(client)

        async with AIProjectClient(endpoint=endpoint, credential=credential) as project:
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

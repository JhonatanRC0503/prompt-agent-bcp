"""Prompt agent `financiero`: análisis de estados financieros de banca corporativa.

Config propia del agente en agents/financiero/.env.
Las dependencias (Blob Storage, conexión de la Function) se resuelven en tiempo de ejecución
por las conexiones del proyecto de Foundry, así que este código funciona en cualquier
tenant/proyecto sin tocar una línea.

El analista solo da el RUC: el agente llama a `obtener_estados_financieros`, que busca el Excel
de esa empresa en el repositorio y lo devuelve como JSON.

**Cómo se ejecuta la herramienta, según el modo:**

| Modo | Herramienta | Quién la ejecuta |
|---|---|---|
| `--publish` (producto) | tool `openapi` | Foundry, server-side, llamando a la Function App |
| `-m` (prueba local) | function tool | este proceso, client-side |

Son el mismo código (`function_app/eeff.py`), así que no hay divergencia de comportamiento: solo
cambia quién la ejecuta. La versión publicada lleva **únicamente** la tool `openapi`, que es la que
hace que el agente también funcione desde el playground del portal.

Publicar una nueva versión en Microsoft Foundry:
    python agents/financiero/main.py --publish

Probarlo en local (usa la function tool client-side):
    python agents/financiero/main.py -m "Analiza el RUC 20512437891"
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient, to_prompt_agent
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import (
    Connection,
    ConnectionType,
    OpenApiFunctionDefinition,
    OpenApiProjectConnectionAuthDetails,
    OpenApiProjectConnectionSecurityScheme,
    OpenApiTool,
    PromptAgentDefinition,
)
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv

from tools import build_obtener_estados_financieros

AGENT_DIR = Path(__file__).parent
REPO_ROOT = AGENT_DIR.parents[1]

# El spec vive junto a la Function que lo sirve, para que no se desincronicen.
sys.path.insert(0, str(REPO_ROOT / "function_app"))
from openapi_spec import NOMBRE_HERRAMIENTA, construir_spec  # noqa: E402

AGENT_NAME = "financiero"
DESCRIPTION = "Analista de crédito de banca corporativa peruana sobre estados financieros."
INSTRUCTIONS = (AGENT_DIR / "instructions.md").read_text(encoding="utf-8").rstrip()

DESCRIPCION_HERRAMIENTA = (
    "Obtiene del repositorio los estados financieros de una empresa a partir de su RUC. "
    "Devuelve las hojas del libro (Estado de Resultados, Balance General, Ratios Financieros) "
    "con sus periodos como columnas y sus cuentas como filas."
)


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


async def find_function_connection(project: AIProjectClient, name: str) -> Connection:
    """Comprueba que exista la conexión con la function key antes de publicar.

    Sin esto, el agente se publicaría apuntando a una conexión inexistente y el fallo solo
    aparecería al invocar la herramienta, ya en el playground.
    """
    async for connection in project.connections.list():
        if connection.name == name:
            return connection
    raise SystemExit(
        f"No existe la conexión '{name}' en el proyecto. Créala en Administrar > Recursos "
        "conectados > Agregar conexión > Claves personalizadas, con la clave 'x-functions-key' "
        "y el valor de:\n"
        "  az functionapp keys list -g <RG> -n <FUNCTION-APP> --query functionKeys.default -o tsv"
    )


def build_agent(client: FoundryChatClient, obtener_estados_financieros=None) -> Agent:
    """El agente. Con `obtener_estados_financieros` se le añade la tool client-side.

    Al publicar se omite: esa versión lleva la tool `openapi`, que Foundry ejecuta server-side.
    """
    tools = [client.get_code_interpreter_tool()]
    if obtener_estados_financieros is not None:
        tools.insert(0, obtener_estados_financieros)
    return Agent(
        name=AGENT_NAME,
        description=DESCRIPTION,
        client=client,
        instructions=INSTRUCTIONS,
        tools=tools,
    )


def definicion_publicable(agent: Agent, base_url: str, conexion: str) -> PromptAgentDefinition:
    """Definición del agente lista para Foundry, con la herramienta como tool `openapi`.

    El *function calling* de Foundry es client-side: una tool de tipo `function` solo se ejecuta
    si la corre tu propia app, por lo que en el playground se quedaría sin resultado. Declarada
    como `openapi`, Foundry la llama él mismo contra la Function App.

    El Agent Framework no expone un `client.get_openapi_tool()`, así que la tool se construye
    aquí con el SDK de proyectos y se añade a la definición.
    """
    definicion = to_prompt_agent(agent)
    definicion.tools.append(
        OpenApiTool(
            openapi=OpenApiFunctionDefinition(
                name=NOMBRE_HERRAMIENTA,
                description=DESCRIPCION_HERRAMIENTA,
                spec=construir_spec(base_url),
                # La function key está guardada en una conexión del proyecto, no en el código.
                auth=OpenApiProjectConnectionAuthDetails(
                    security_scheme=OpenApiProjectConnectionSecurityScheme(
                        project_connection_id=conexion,
                    )
                ),
            )
        )
    )
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

    # Resuelve `az login` en local y la identidad administrada al desplegar en Azure.
    async with DefaultAzureCredential() as credential:
        client = FoundryChatClient(project_endpoint=endpoint, model=model, credential=credential)

        async with AIProjectClient(endpoint=endpoint, credential=credential) as project:
            if args.publish:
                base_url = os.environ.get("FUNCTION_BASE_URL")
                if not base_url:
                    raise SystemExit(f"Falta FUNCTION_BASE_URL (defínelo en {AGENT_DIR / '.env'}).")
                conexion = os.environ.get("FUNCTION_CONNECTION_NAME")
                if not conexion:
                    raise SystemExit(
                        f"Falta FUNCTION_CONNECTION_NAME (defínelo en {AGENT_DIR / '.env'})."
                    )

                await find_function_connection(project, conexion)
                version = await project.agents.create_version(
                    agent_name=AGENT_NAME,
                    definition=definicion_publicable(
                        build_agent(client), base_url.rstrip("/"), conexion
                    ),
                    description=DESCRIPTION,
                )
                print(f"Publicado {version.name} v{version.version}")
                print(f"  herramienta: {NOMBRE_HERRAMIENTA} (openapi, server-side)")
                print(f"  contra     : {base_url.rstrip('/')}")
            else:
                container = os.environ.get("BLOB_CONTAINER")
                if not container:
                    raise SystemExit(f"Falta BLOB_CONTAINER (defínelo en {AGENT_DIR / '.env'}).")
                storage = await find_storage_connection(project)
                agent = build_agent(
                    client,
                    build_obtener_estados_financieros(storage.target, container, credential),
                )
                print((await agent.run(args.message)).text)


if __name__ == "__main__":
    asyncio.run(main())

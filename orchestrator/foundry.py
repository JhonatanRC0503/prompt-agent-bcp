"""Punto único de acceso a Microsoft Foundry.

Dos clientes distintos, por motivos distintos:

* `FoundryAgent` se conecta a un prompt agent **ya publicado** por su nombre. Es lo que hace que
  Foundry siga siendo la única fuente de verdad de los agentes: el orquestador no redefine sus
  instrucciones ni sus herramientas, solo los invoca. Y, al ejecutarlos como agentes publicados,
  sus tools `openapi` y `mcp` corren server-side en Foundry (ver README §4), así que el
  orquestador no necesita permisos sobre Storage ni sobre la knowledge base.

* `FoundryChatClient` es una llamada suelta al modelo, sin agente detrás. La usan el intake y el
  enrutador de seguimiento, que no son agentes: son dos decisiones cortas y estructuradas.

Todo se construye una vez por proceso. Crear el credential en cada petición abriría una conexión
nueva y pediría un token nuevo cada vez.
"""

from __future__ import annotations

import logging
from typing import TypeVar

from agent_framework import ChatOptions, Message
from agent_framework.foundry import FoundryAgent, FoundryChatClient
from azure.identity.aio import DefaultAzureCredential
from pydantic import BaseModel

from config import AJUSTES

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class Foundry:
    def __init__(self) -> None:
        # Resuelve `az login` en local y la identidad administrada en App Service.
        self._credential = DefaultAzureCredential()
        self._chat = FoundryChatClient(
            project_endpoint=AJUSTES.endpoint,
            model=AJUSTES.modelo_orquestador,
            credential=self._credential,
        )
        self._agentes: dict[str, FoundryAgent] = {}

    def agente(self, nombre: str) -> FoundryAgent:
        """El prompt agent publicado con ese nombre, en su última versión.

        Sin `agent_version`: así un `--publish` de cualquier especialista entra en producción sin
        redesplegar el orquestador. Si alguna vez hiciera falta fijar una versión concreta, es el
        único sitio donde habría que tocarlo.
        """
        if nombre not in self._agentes:
            self._agentes[nombre] = FoundryAgent(
                project_endpoint=AJUSTES.endpoint,
                agent_name=nombre,
                credential=self._credential,
            )
        return self._agentes[nombre]

    async def estructurado(self, instrucciones: str, mensaje: str, esquema: type[T]) -> T:
        """Una llamada al modelo que devuelve el objeto pydantic pedido.

        `response_format` con un modelo pydantic obliga al servicio a responder con ese esquema,
        así que no hay que parsear JSON a mano ni defenderse de texto suelto alrededor.
        """
        respuesta = await self._chat.get_response(
            [Message("system", [instrucciones]), Message("user", [mensaje])],
            options=ChatOptions(response_format=esquema, temperature=0),
        )
        valor = respuesta.value
        if not isinstance(valor, esquema):
            # No debería ocurrir con response_format, pero si el servicio devolviera texto suelto
            # es mejor fallar aquí con el motivo que propagar un None que explote más adelante.
            raise RuntimeError(f"El modelo no devolvió un {esquema.__name__}: {respuesta.text[:200]!r}")
        return valor

    async def texto(self, instrucciones: str, mensaje: str) -> str:
        """Una respuesta en texto libre del modelo del orquestador."""
        respuesta = await self._chat.get_response(
            [Message("system", [instrucciones]), Message("user", [mensaje])],
            options=ChatOptions(temperature=0.2),
        )
        return (respuesta.text or "").strip()

    async def cerrar(self) -> None:
        await self._credential.close()

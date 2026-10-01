"""Snapshot de las sesiones en Blob Storage. Siempre best-effort.

La cuenta de Storage **no se hardcodea**: se resuelve por las conexiones del proyecto de Foundry,
igual que hace `agents/financiero/main.py`. Es la convención del repo (README, "Nunca hardcodear
IDs de tenant") y es lo que permite mover esto a otro tenant sin tocar código.

Nada de lo que pasa aquí puede romper un análisis. Si falta el rol `Storage Blob Data Contributor`
sobre la identidad del App Service, el orquestador funciona igual: pierde la durabilidad entre
reinicios y el rastro de auditoría, no la funcionalidad. Por eso cada fallo se registra una vez y
se desactiva la persistencia en lugar de reintentar en cada escritura.
"""

from __future__ import annotations

import json
import logging

from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import ConnectionType
from azure.storage.blob.aio import BlobServiceClient

from config import AJUSTES
from sesiones import Sesion

log = logging.getLogger(__name__)


class Persistencia:
    def __init__(self, credential) -> None:
        self._credential = credential
        self._cliente: BlobServiceClient | None = None
        self._contenedor_listo = False
        self._desactivada = False

    async def _conectar(self) -> BlobServiceClient | None:
        if self._desactivada:
            return None
        if self._cliente is not None:
            return self._cliente
        try:
            async with AIProjectClient(
                endpoint=AJUSTES.endpoint, credential=self._credential
            ) as proyecto:
                async for conexion in proyecto.connections.list(
                    connection_type=ConnectionType.AZURE_STORAGE_ACCOUNT
                ):
                    self._cliente = BlobServiceClient(
                        account_url=conexion.target, credential=self._credential
                    )
                    log.info("Persistencia de sesiones sobre %s", conexion.target)
                    return self._cliente
            log.warning(
                "El proyecto de Foundry no tiene conexión de Azure Storage; "
                "las sesiones vivirán solo en memoria."
            )
        except Exception:
            log.warning("No se pudo resolver la cuenta de Storage", exc_info=True)
        self._desactivada = True
        return None

    async def guardar(self, sesion: Sesion) -> None:
        cliente = await self._conectar()
        if cliente is None:
            return
        try:
            contenedor = cliente.get_container_client(AJUSTES.contenedor_sesiones)
            if not self._contenedor_listo:
                try:
                    await contenedor.create_container()
                except Exception:
                    pass  # ya existe, que es el caso normal
                self._contenedor_listo = True
            await contenedor.upload_blob(
                name=f"{sesion.id}.json",
                data=json.dumps(sesion.como_dict(incluir_eventos=True), ensure_ascii=False, indent=2),
                overwrite=True,
            )
        except Exception:
            log.warning("No se pudo guardar la sesión %s; se desactiva la persistencia", sesion.id, exc_info=True)
            self._desactivada = True

    async def cerrar(self) -> None:
        if self._cliente is not None:
            await self._cliente.close()

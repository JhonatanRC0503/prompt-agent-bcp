"""API HTTP que expone las herramientas de los agentes para que Foundry las ejecute server-side.

Existe por una limitación de diseño de Foundry: el *function calling* es client-side, así que
una tool de tipo `function` solo se ejecuta si la corre tu propia app. En el playground del
portal nadie devuelve el resultado y el Output queda vacío.

Publicando la misma herramienta como tool de tipo `openapi`, Foundry la llama server-side y el
agente financiero también funciona desde el playground.

La herramienta no se reimplementa aquí: se reutiliza `eeff.py`, que es la única fuente de
verdad del parser y la comparte con `agents/financiero/tools.py`.

Endpoints:
    POST /api/estados-financieros   la herramienta (requiere function key)
    GET  /api/openapi.json          el spec para registrarla en Foundry (anónimo)
    GET  /api/health                sonda de estado (anónimo)

App settings:
    STORAGE_ACCOUNT_URL   https://<cuenta>.blob.core.windows.net
    BLOB_CONTAINER        contenedor del repositorio de estados financieros
"""

from __future__ import annotations

import json
import logging
import os

import azure.functions as func
from azure.identity.aio import DefaultAzureCredential

from eeff import build_obtener_estados_financieros
from openapi_spec import RUTA_HERRAMIENTA, construir_spec

app = func.FunctionApp()

# Un único credential y una única tool por proceso: construirlos en cada request abriría una
# conexión nueva y pediría un token nuevo cada vez.
_herramienta = None


def _obtener_herramienta():
    global _herramienta
    if _herramienta is None:
        cuenta = os.environ.get("STORAGE_ACCOUNT_URL")
        contenedor = os.environ.get("BLOB_CONTAINER")
        if not cuenta or not contenedor:
            raise RuntimeError("Faltan los app settings STORAGE_ACCOUNT_URL y/o BLOB_CONTAINER.")
        # La identidad administrada de la Function App necesita `Storage Blob Data Reader`
        # sobre la cuenta de Storage (ver roles.md).
        _herramienta = build_obtener_estados_financieros(
            cuenta, contenedor, DefaultAzureCredential()
        )
    return _herramienta


def _json(payload: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False), status_code=status, mimetype="application/json"
    )


@app.function_name(name="estados_financieros")
@app.route(route=RUTA_HERRAMIENTA, methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
async def estados_financieros(req: func.HttpRequest) -> func.HttpResponse:
    try:
        cuerpo = req.get_json()
    except ValueError:
        return _json({"error": "El cuerpo de la petición debe ser JSON."}, 400)

    if not isinstance(cuerpo, dict):
        return _json({"error": "El cuerpo de la petición debe ser un objeto JSON."}, 400)

    try:
        herramienta = _obtener_herramienta()
    except Exception as error:
        # Captura amplia a propósito: si esto falla es por configuración o por una
        # dependencia ausente, y conviene responder con el motivo antes que un 500 vacío.
        logging.exception("No se pudo construir la herramienta")
        return _json({"error": f"Function App mal configurada: {error}"}, 500)

    try:
        # La tool devuelve siempre JSON, incluidos los casos "RUC inválido" y "RUC no
        # encontrado". Se reenvía tal cual con 200: para el agente son resultados legítimos
        # que sabe explicarle al analista, no fallos de la herramienta.
        resultado = await herramienta(str(cuerpo.get("ruc") or ""))
    except Exception:
        logging.exception("Fallo al leer los estados financieros")
        return _json({"error": "Error interno al leer los estados financieros."}, 500)

    if '"error"' in resultado[:40]:
        logging.warning("La herramienta devolvió un error: %s", resultado[:200])

    return func.HttpResponse(resultado, status_code=200, mimetype="application/json")


@app.function_name(name="health")
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json({"status": "ok"})


@app.function_name(name="openapi")
@app.route(route="openapi.json", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def openapi(req: func.HttpRequest) -> func.HttpResponse:
    """Spec de la herramienta, con la URL tomada del propio request.

    Tomarla del request evita hardcodear el hostname: el mismo código sirve en local y en
    cualquier Function App a la que se despliegue.
    """
    return _json(construir_spec(req.url.split("/api/")[0] + "/api"))

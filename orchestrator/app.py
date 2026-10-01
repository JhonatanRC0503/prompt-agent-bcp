"""API del orquestador de análisis de riesgo crediticio.

El flujo completo dura minutos, así que **ninguna petición HTTP espera al resultado**. El patrón
es: el POST acepta el trabajo y responde al instante; el progreso y el resultado viajan por un
stream SSE que el navegador mantiene abierto.

No es una preferencia de estilo. Azure Load Balancer corta toda conexión que pase 230 s sin
tráfico, en App Service igual que en Functions, y no es configurable. Un POST que bloqueara
esperando los tres especialistas moriría ahí. El stream, en cambio, nunca está inactivo: emite
eventos de progreso y latidos cada 20 s.

Rutas:
    POST   /api/sesiones                    abre una sesión
    POST   /api/sesiones/{id}/mensaje       mensaje + adjuntos (multipart); devuelve de inmediato
    GET    /api/sesiones/{id}/eventos       stream SSE de progreso y resultados
    GET    /api/sesiones/{id}               estado completo (para reconectar)
    GET    /api/salud                       sonda de estado
    GET    /                                la interfaz del analista
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

import adjuntos as mod_adjuntos
import intake
import seguimiento
from config import AJUSTES
from foundry import Foundry
from orquestacion import analizar
from persistencia import Persistencia
from reporte import redactar
from sesiones import Almacen, Sesion

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("orquestador")

ESTATICOS = Path(__file__).parent / "static"

recursos: dict = {}


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    recursos["foundry"] = Foundry()
    recursos["persistencia"] = Persistencia(recursos["foundry"]._credential)
    recursos["almacen"] = Almacen(recursos["persistencia"])
    log.info("Orquestador listo | endpoint=%s | agentes=%s", AJUSTES.endpoint, AJUSTES.especialistas)
    try:
        yield
    finally:
        await recursos["persistencia"].cerrar()
        await recursos["foundry"].cerrar()


app = FastAPI(title="Orquestador de riesgo crediticio", version="1.0", lifespan=ciclo_de_vida)


def _sesion(id_sesion: str) -> Sesion:
    sesion = recursos["almacen"].obtener(id_sesion)
    if sesion is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada o expirada.")
    return sesion


# --------------------------------------------------------------------------- trabajo de fondo


async def _procesar(sesion: Sesion, mensaje: str) -> None:
    """El trabajo largo. Corre fuera del ciclo de la petición y habla por el bus de eventos."""
    foundry = recursos["foundry"]
    # Un solo flujo a la vez por sesión: dos envíos seguidos no deben pisarse las secciones.
    async with sesion._cerrojo:
        try:
            contexto = mod_adjuntos.como_contexto(sesion.adjuntos)

            # Con informe ya hecho, todo mensaje nuevo es una pregunta de seguimiento, salvo que
            # el analista aporte datos que cambien la base (eso lo decide el enrutador).
            if sesion.reporte and sesion.datos.completos():
                sesion.estado = "analizando"
                sesion.emitir("estado", estado="analizando")
                await seguimiento.atender(sesion, foundry, mensaje)
                sesion.estado = "listo"
                sesion.emitir("estado", estado="listo")
                recursos["almacen"].guardar(sesion)
                return

            # --- intake: ¿tenemos los tres datos? ---
            sesion.emitir("intake_inicio")
            extraccion = await intake.extraer_datos(foundry, mensaje, contexto, sesion.datos)
            intake.fusionar(sesion, extraccion)
            sesion.emitir(
                "intake_fin",
                datos=sesion.datos.__dict__,
                faltantes=sesion.datos.faltantes(),
                de_donde=extraccion.de_donde,
                sector_deducido=extraccion.sector_deducido,
            )

            if not sesion.datos.completos():
                pregunta = intake.pedir_lo_que_falta(sesion.datos)
                sesion.estado = "esperando_datos"
                sesion.anotar("asistente", pregunta)
                sesion.emitir("necesita_datos", texto=pregunta, faltantes=sesion.datos.faltantes())
                sesion.emitir("estado", estado="esperando_datos")
                recursos["almacen"].guardar(sesion)
                return

            # --- fan-out concurrente + fan-in ---
            sesion.estado = "analizando"
            sesion.emitir("estado", estado="analizando")
            await analizar(sesion, foundry)
            await redactar(sesion, foundry)
            sesion.anotar("asistente", sesion.reporte)
            sesion.estado = "listo"
            sesion.emitir("estado", estado="listo")

        except Exception as error:
            log.exception("La sesión %s terminó en error", sesion.id)
            sesion.estado = "error"
            sesion.emitir("error", mensaje=str(error))
            sesion.emitir("estado", estado="error")
        finally:
            recursos["almacen"].guardar(sesion)


# --------------------------------------------------------------------------- rutas


@app.get("/api/salud")
async def salud():
    return {
        "estado": "ok",
        "endpoint": AJUSTES.endpoint,
        "agentes": {**AJUSTES.especialistas, "generador": AJUSTES.agente_generador},
        "sesiones_vivas": len(recursos["almacen"].listar()) if "almacen" in recursos else 0,
    }


@app.post("/api/sesiones")
async def crear_sesion():
    sesion = recursos["almacen"].crear()
    return {"id": sesion.id, "estado": sesion.estado}


@app.post("/api/sesiones/{id_sesion}/mensaje")
async def enviar_mensaje(
    id_sesion: str,
    texto: str = Form(default=""),
    archivos: list[UploadFile] = File(default=[]),
):
    """Acepta el trabajo y responde al instante. El resultado sale por el stream."""
    sesion = _sesion(id_sesion)

    nuevos = []
    for archivo in archivos:
        datos = await archivo.read()
        if not datos:
            continue
        if len(datos) > AJUSTES.max_bytes_adjunto:
            raise HTTPException(
                status_code=413,
                detail=f"{archivo.filename} supera el límite de {AJUSTES.max_bytes_adjunto // (1024*1024)} MB.",
            )
        adjunto = await mod_adjuntos.extraer(archivo.filename or "adjunto", datos)
        sesion.adjuntos.append(adjunto)
        nuevos.append({"nombre": adjunto.nombre, "extraido_con": adjunto.extraido_con, "caracteres": len(adjunto.texto)})

    if not texto.strip() and not nuevos:
        raise HTTPException(status_code=400, detail="Envía un mensaje o al menos un documento.")

    sesion.anotar("analista", texto)
    if nuevos:
        sesion.emitir("adjuntos", archivos=nuevos)
    sesion.emitir("mensaje_recibido", texto=texto)

    # Fire-and-forget: la petición no espera los minutos que dura el flujo.
    tarea = asyncio.create_task(_procesar(sesion, texto))
    # Guardar la referencia evita que el recolector de basura cancele la tarea a mitad.
    recursos.setdefault("tareas", set()).add(tarea)
    tarea.add_done_callback(recursos["tareas"].discard)

    return {"aceptado": True, "id": sesion.id, "adjuntos": nuevos}


@app.get("/api/sesiones/{id_sesion}")
async def estado_sesion(id_sesion: str):
    return _sesion(id_sesion).como_dict(incluir_eventos=True)


@app.get("/api/sesiones/{id_sesion}/eventos")
async def stream_eventos(id_sesion: str, request: Request, desde: int = 0):
    """Stream SSE. `desde` permite reconectar sin perder eventos."""
    sesion = _sesion(id_sesion)

    async def generar():
        try:
            async for evento in sesion.escuchar(desde):
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        generar(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Sin esto, el proxy de App Service bufferiza la respuesta y el progreso llega de
            # golpe al final, que es justo lo que el stream viene a evitar.
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/")
async def interfaz():
    return FileResponse(ESTATICOS / "index.html")

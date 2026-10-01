"""Fan-in: las tres secciones se funden en el informe final.

El informe siempre se **regenera a partir de las secciones**, nunca se parchea sobre el texto
anterior. Así, cuando una pregunta de seguimiento actualiza el análisis sectorial, el informe
resultante es coherente con esa sección nueva y con las otras dos, sin párrafos huérfanos del
análisis viejo. Las secciones son la fuente de verdad; el informe es una proyección de ellas.
"""

from __future__ import annotations

import asyncio
import logging

from adjuntos import como_contexto
from config import AJUSTES
from orquestacion import TITULOS, invocar_con_reintentos
from sesiones import Sesion

log = logging.getLogger(__name__)


def componer_encargo(sesion: Sesion) -> str:
    """El mensaje para el redactor, en el formato que declara su `instructions.md`."""
    d = sesion.datos
    partes = [f"EMPRESA: {d.empresa}", f"RUC: {d.ruc}", f"SECTOR: {d.sector}", ""]

    for clave, titulo in TITULOS.items():
        seccion = sesion.secciones.get(clave)
        partes.append(f"=== {titulo} ===")
        if seccion and seccion.estado == "ok":
            partes.append(seccion.texto)
        else:
            motivo = (seccion.error if seccion else None) or "no se ejecutó"
            # Se le dice explícitamente al redactor, porque su prompt le obliga a declarar en el
            # informe qué faltó en lugar de disimularlo.
            partes.append(f"[NO DISPONIBLE: {motivo}]")
        partes.append("")

    contexto = como_contexto(sesion.adjuntos)
    if contexto:
        partes.append("=== CONTEXTO ADICIONAL ===")
        partes.append(contexto)

    return "\n".join(partes)


async def redactar(sesion: Sesion, foundry) -> str:
    """Genera el informe integrado y lo deja en `sesion.reporte`."""
    nombre = AJUSTES.agente_generador
    sesion.emitir("reporte_inicio", agente=nombre)
    try:
        agente = foundry.agente(nombre)
        respuesta = await invocar_con_reintentos(
            agente, componer_encargo(sesion), sesion, "generador"
        )
        sesion.reporte = (respuesta.text or "").strip()
    except Exception as error:
        log.exception("Fallo del redactor en la sesión %s", sesion.id)
        sesion.emitir("reporte_error", error=str(error))
        # Degradación honesta: antes que dejar al analista sin nada, se le entregan las secciones
        # crudas y se le dice que la integración falló.
        sesion.reporte = _informe_de_emergencia(sesion, str(error))

    sesion.emitir("reporte_fin", caracteres=len(sesion.reporte))
    return sesion.reporte


def _informe_de_emergencia(sesion: Sesion, error: str) -> str:
    partes = [
        f"# Análisis de {sesion.datos.empresa}",
        "",
        f"> ⚠️ El agente redactor no pudo integrar las secciones ({error}). "
        "A continuación van los análisis de los especialistas tal cual los entregaron.",
        "",
    ]
    for clave, titulo in TITULOS.items():
        seccion = sesion.secciones.get(clave)
        partes.append(f"## {titulo.title()}")
        if seccion and seccion.estado == "ok":
            partes.append(seccion.texto)
        else:
            partes.append(f"_No disponible: {(seccion.error if seccion else 'no se ejecutó')}_")
        partes.append("")
    return "\n".join(partes)

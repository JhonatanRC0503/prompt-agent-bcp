"""Fase interactiva: qué hacer con lo que el analista pregunta después del informe.

Aquí el patrón ya no es el simultáneo. Volver a lanzar los tres especialistas porque alguien pide
"profundiza en los reportes previos" cuesta 85 s y tres invocaciones de modelo para tirar dos de
ellas. Esta fase es **enrutado dinámico**: un clasificador barato decide el destino y solo se
reactiva lo que haga falta.

Tres destinos posibles:

* `responder`   — la respuesta ya está en el informe ("¿cuál era el ratio de liquidez?").
                  No se invoca a ningún especialista: se contesta en ~2 s.
* `profundizar` — hay que reactivar uno o varios especialistas con una instrucción nueva
                  ("mejor análisis del sector", "mira estos enlaces"). Se reejecuta solo a esos y
                  se regenera el informe.
* `reanalizar`  — cambió un dato de base (el analista corrige el RUC). Se repite el fan-out
                  completo.

El clasificador corre sobre `gpt-4.1-mini`: es una decisión de una sola etiqueta, no necesita el
modelo grande, y así la ruta barata (`responder`) se mantiene barata de verdad.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, Field

from adjuntos import como_contexto
from config import AJUSTES
from orquestacion import TITULOS, invocar_con_reintentos
from sesiones import Sesion

log = logging.getLogger(__name__)

INSTRUCCIONES_ENRUTADOR = """\
Eres el enrutador de un sistema de análisis de riesgo crediticio. Ya existe un informe sobre una
empresa, integrado a partir de tres análisis especializados:

- financiero: estados financieros, ratios, liquidez, endeudamiento, rentabilidad.
- sectorial: situación y perspectivas del sector, fuentes web, indicadores del sector.
- reportes_previos: historial del cliente con la institución, comportamiento de pago, calificaciones.

Decide qué hacer con el mensaje del analista:

- "responder": la pregunta se contesta con lo que YA está en el informe. Es el caso por defecto
  para preguntas de lectura o aclaración.
- "profundizar": el analista pide más profundidad, datos nuevos, que se revisen unos enlaces, o
  corrige/añade información que afecta a uno o varios análisis. Indica en `secciones` cuáles hay
  que reactivar y en `instruccion` qué debe hacer ese especialista, redactado como una orden
  directa y autosuficiente (el especialista no ve esta conversación).
- "reanalizar": cambió un dato base de la empresa (RUC, razón social o sector), así que los tres
  análisis quedan obsoletos.

Reglas:
1. Prefiere "responder" si la información ya está. Reactivar un especialista cuesta más de un
   minuto; no lo hagas por una pregunta de lectura.
2. En `secciones` pon solo las imprescindibles. Si piden más detalle del sector, es ["sectorial"],
   no las tres.
3. Si el analista aporta enlaces o documentos, inclúyelos literalmente dentro de `instruccion`
   para que el especialista los use.
"""


class Decision(BaseModel):
    accion: str = Field(description="responder | profundizar | reanalizar")
    secciones: list[str] = Field(
        default_factory=list,
        description="Claves a reactivar: financiero, sectorial, reportes_previos",
    )
    instruccion: str = Field(default="", description="Orden autosuficiente para el especialista")
    motivo: str = Field(default="", description="Por qué se eligió esta acción, en una frase")


INSTRUCCIONES_RESPUESTA = """\
Eres el asistente que acompaña a un analista de riesgo crediticio mientras lee un informe ya
generado. Responde su pregunta usando EXCLUSIVAMENTE el contenido del informe que se te entrega.

- Si el informe no contiene la respuesta, dilo claramente y sugiere que puedes pedirle al
  especialista correspondiente que profundice. No inventes datos.
- Sé directo y breve. El analista tiene el informe delante: no se lo resumas entero.
- Cita las cifras tal como aparecen en el informe.
"""


def _claves_validas(claves: list[str]) -> list[str]:
    return [c for c in claves if c in AJUSTES.especialistas]


async def _responder_desde_informe(sesion: Sesion, foundry, pregunta: str) -> str:
    return await foundry.texto(
        INSTRUCCIONES_RESPUESTA,
        f"INFORME:\n{sesion.reporte}\n\nPREGUNTA DEL ANALISTA:\n{pregunta}",
    )


async def _reactivar(sesion: Sesion, foundry, claves: list[str], instruccion: str) -> None:
    """Reejecuta los especialistas indicados, en paralelo entre sí, con la instrucción nueva.

    El encargo original de ese especialista se conserva y la instrucción del analista se añade
    encima: sin el encargo, un "profundiza en los riesgos" llegaría sin empresa ni RUC.
    """
    from orquestacion import _encargo

    contexto = como_contexto(sesion.adjuntos)

    async def una(clave: str):
        nombre = AJUSTES.especialistas[clave]
        sesion.emitir("agente_inicio", seccion=clave, agente=nombre, motivo="seguimiento")
        agente = foundry.agente(nombre)
        encargo = (
            f"{_encargo(clave, sesion)}\n\n"
            f"INSTRUCCIÓN ADICIONAL DEL ANALISTA (tiene prioridad sobre el encargo base):\n"
            f"{instruccion}"
        )
        if contexto and contexto not in encargo:
            encargo += f"\n\nDocumentos aportados por el analista:\n{contexto}"

        seccion = sesion.secciones.get(clave)
        try:
            respuesta = await invocar_con_reintentos(agente, encargo, sesion, clave)
            texto = (respuesta.text or "").strip()
            if texto and seccion is not None:
                seccion.texto, seccion.estado, seccion.error = texto, "ok", None
            elif texto:
                from sesiones import Seccion

                sesion.secciones[clave] = Seccion(agente=nombre, texto=texto, estado="ok")
            sesion.emitir("agente_fin", seccion=clave, agente=nombre, estado="ok", caracteres=len(texto))
        except Exception as error:
            log.exception("Fallo reactivando %s en la sesión %s", nombre, sesion.id)
            # La sección anterior se conserva: una profundización fallida no debe borrar el
            # análisis que ya estaba bien.
            sesion.emitir(
                "agente_fin", seccion=clave, agente=nombre, estado="error", error=str(error)
            )

    await asyncio.gather(*(una(c) for c in claves))


async def atender(sesion: Sesion, foundry, mensaje: str) -> dict:
    """Clasifica el mensaje y ejecuta la acción. Devuelve qué se hizo."""
    contexto = [
        f"EMPRESA ANALIZADA: {sesion.datos.empresa} (RUC {sesion.datos.ruc}, sector {sesion.datos.sector})",
        "",
        "SECCIONES DISPONIBLES EN EL INFORME:",
    ]
    for clave, titulo in TITULOS.items():
        seccion = sesion.secciones.get(clave)
        estado = seccion.estado if seccion else "no ejecutada"
        contexto.append(f"- {clave} ({titulo}): {estado}")
    contexto.append(f"\nMENSAJE DEL ANALISTA:\n{mensaje}")

    decision = await foundry.estructurado(
        INSTRUCCIONES_ENRUTADOR, "\n".join(contexto), Decision
    )
    sesion.emitir(
        "enrutado",
        accion=decision.accion,
        secciones=decision.secciones,
        motivo=decision.motivo,
    )

    if decision.accion == "responder":
        respuesta = await _responder_desde_informe(sesion, foundry, mensaje)
        sesion.anotar("asistente", respuesta)
        sesion.emitir("respuesta", texto=respuesta)
        return {"accion": "responder", "respuesta": respuesta}

    claves = _claves_validas(decision.secciones)
    if decision.accion == "reanalizar" or not claves:
        # Sin secciones válidas, "profundizar" no tiene destino: se trata como reanálisis
        # completo antes que descartar la petición en silencio.
        claves = list(AJUSTES.especialistas)

    await _reactivar(sesion, foundry, claves, decision.instruccion or mensaje)

    from reporte import redactar

    await redactar(sesion, foundry)
    sesion.anotar("asistente", sesion.reporte)
    return {"accion": decision.accion, "secciones": claves, "reporte": sesion.reporte}

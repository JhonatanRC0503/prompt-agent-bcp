"""Fan-out concurrente a los tres especialistas y fan-in al redactor del informe.

Es el patrón de **orquestación simultánea** de la guía de patrones de agentes de Azure: los tres
análisis son independientes —ninguno necesita la salida de otro— así que el coste es el del más
lento y no la suma. Con las latencias medidas del README (financiero 77 s, sectorial 85 s), en
secuencia serían ~4 min; en paralelo, ~85 s.

Dos decisiones que conviene entender:

**El fan-out es código, no un LLM.** Siempre corren los tres. Si un modelo decidiera a quién
llamar, algún día decidiría saltarse uno y el informe saldría incompleto sin que nadie se entere.
El criterio del LLM se usa donde aporta —el análisis en sí— y el reparto se mantiene determinista.
Es la misma conclusión que ya estaba en el README (§3, "El fan-out debe ser determinista").

**Un especialista que falla no tumba el informe.** `return_exceptions=True` más un timeout por
rama: si el sectorial se cae, el informe se redacta con los otros dos y declara explícitamente
qué faltó. Lo contrario —perder 85 s de trabajo bueno porque una rama falló— es inaceptable
cuando cada corrida cuesta minutos.
"""

from __future__ import annotations

import asyncio
import logging

from adjuntos import como_contexto
from config import AJUSTES
from sesiones import Seccion, Sesion

log = logging.getLogger(__name__)

# Reintentos ante 429 del modelo. 3 intentos con espera 8 s -> 16 s cubren las ventanas de
# limitación habituales sin alargar de más la espera del analista.
MAX_INTENTOS = 3
ESPERA_INICIAL_REINTENTO = 8

TITULOS = {
    "financiero": "ANÁLISIS FINANCIERO",
    "sectorial": "ANÁLISIS SECTORIAL",
    "reportes_previos": "REPORTES PREVIOS",
}


def _es_limite_de_cuota(error: BaseException) -> bool:
    texto = str(error).lower()
    return "429" in texto or "rate_limit" in texto or "rate limit" in texto


async def invocar_con_reintentos(agente, encargo: str, sesion: Sesion, etiqueta: str, **kwargs):
    """Ejecuta un agente reintentando solo los 429 de cuota del modelo.

    El fan-out concurrente multiplica el consumo instantáneo de tokens: tres especialistas sobre
    el mismo despliegue de gpt-4.1 pueden superar el TPM de la región aunque cada uno por
    separado quepa de sobra. Medido en la primera corrida real del flujo: el sectorial cayó con
    `rate_limit_exceeded` mientras los otros dos pasaban.

    El reintento con espera creciente es la mitigación correcta porque el 429 es transitorio por
    definición: la ventana de tokens se libera sola. Lo que NO arregla es una cuota estructuralmente
    baja; para eso hay que subir el TPM del despliegue (ver app_service.md).

    Solo se reintentan los 429. Un fallo de autenticación o un prompt inválido no mejoran por
    esperar, y reintentarlos solo gastaría minutos del analista.
    """
    espera = ESPERA_INICIAL_REINTENTO
    for intento in range(1, MAX_INTENTOS + 1):
        try:
            return await asyncio.wait_for(
                agente.run(encargo, **kwargs), timeout=AJUSTES.timeout_agente
            )
        except Exception as error:
            ultimo = intento == MAX_INTENTOS
            if ultimo or not _es_limite_de_cuota(error):
                raise
            sesion.emitir(
                "reintento", seccion=etiqueta, intento=intento, espera=espera, motivo="cuota del modelo"
            )
            log.warning(
                "Cuota excedida en %s (intento %s/%s); reintento en %ss",
                etiqueta, intento, MAX_INTENTOS, espera,
            )
            await asyncio.sleep(espera)
            espera *= 2


def _encargo(clave: str, sesion: Sesion) -> str:
    """El mensaje para cada especialista, con solo lo que ese especialista necesita.

    No se les manda el mismo texto a los tres: al financiero le sobra el sector y al sectorial le
    sobra el RUC. Cada prompt lleva lo que su agente sabe usar.
    """
    d = sesion.datos
    if clave == "financiero":
        base = (
            f"Analiza los estados financieros de la empresa {d.empresa} con RUC {d.ruc}.\n"
            f"Sector de la empresa: {d.sector}."
        )
    elif clave == "sectorial":
        base = (
            f"Realiza el análisis sectorial de la empresa {d.empresa}, "
            f"cuyo sector es {d.sector}. RUC: {d.ruc}."
        )
    else:
        base = (
            f"Busca y sintetiza todos los reportes previos disponibles de la empresa {d.empresa}"
            f" (RUC {d.ruc}, sector {d.sector})."
        )

    contexto = como_contexto(sesion.adjuntos)
    if contexto:
        base += (
            "\n\nEl analista adjuntó los siguientes documentos. Ya vienen con el texto extraído, "
            "no necesitas abrirlos:\n\n" + contexto
        )
    return base


async def _ejecutar(clave: str, sesion: Sesion, foundry) -> Seccion:
    """Invoca a un especialista y devuelve su sección. Nunca lanza: los fallos van en la sección."""
    nombre = AJUSTES.especialistas[clave]
    seccion = Seccion(agente=nombre)
    sesion.emitir("agente_inicio", seccion=clave, agente=nombre)

    try:
        agente = foundry.agente(nombre)
        # Sesión propia por especialista: deja la puerta abierta a que una pregunta de
        # seguimiento continúe esa conversación en vez de empezar desde cero.
        sesion_agente = agente.create_session()
        respuesta = await invocar_con_reintentos(
            agente, _encargo(clave, sesion), sesion, clave, session=sesion_agente
        )
        seccion.texto = (respuesta.text or "").strip()
        seccion.sesion_agente = getattr(sesion_agente, "service_session_id", None) or getattr(
            sesion_agente, "session_id", None
        )
        if not seccion.texto:
            seccion.estado = "error"
            seccion.error = "El agente respondió vacío."
        else:
            seccion.estado = "ok"
    except asyncio.TimeoutError:
        seccion.estado = "error"
        seccion.error = f"El agente superó el límite de {AJUSTES.timeout_agente} s."
        log.warning("Timeout del agente %s en la sesión %s", nombre, sesion.id)
    except Exception as error:
        seccion.estado = "error"
        seccion.error = str(error)
        log.exception("Fallo del agente %s en la sesión %s", nombre, sesion.id)

    sesion.emitir(
        "agente_fin",
        seccion=clave,
        agente=nombre,
        estado=seccion.estado,
        error=seccion.error,
        caracteres=len(seccion.texto),
    )
    return seccion


async def analizar(sesion: Sesion, foundry) -> dict[str, Seccion]:
    """Lanza los tres especialistas a la vez y devuelve sus secciones."""
    claves = list(AJUSTES.especialistas)
    sesion.emitir("fanout_inicio", agentes=[AJUSTES.especialistas[c] for c in claves])

    resultados = await asyncio.gather(
        *(_ejecutar(c, sesion, foundry) for c in claves),
        return_exceptions=True,
    )

    secciones: dict[str, Seccion] = {}
    for clave, resultado in zip(claves, resultados):
        if isinstance(resultado, BaseException):
            # `_ejecutar` ya captura lo suyo; esto solo cubre un fallo del propio gather
            # (por ejemplo, cancelación) para que el dict quede siempre completo.
            log.exception("Rama %s terminó en excepción", clave, exc_info=resultado)
            secciones[clave] = Seccion(
                agente=AJUSTES.especialistas[clave], estado="error", error=str(resultado)
            )
        else:
            secciones[clave] = resultado

    sesion.secciones.update(secciones)
    ok = sum(1 for s in secciones.values() if s.estado == "ok")
    sesion.emitir("fanout_fin", completados=ok, total=len(claves))
    return secciones

"""Intake: de lo que escribe el analista (y de lo que adjunta) a los tres datos del flujo.

**Por qué esto existe y por qué va aquí y no en los agentes.** Los tres especialistas están
escritos para no preguntar nunca:

* `sectorial`  -> "Nunca te detengas a hacer preguntas: no preguntes el sector..."
* `reportes-previos` -> "Nunca te detengas a hacer preguntas: no pidas confirmación..."
* `financiero` -> "Si el mensaje no trae un RUC, pídelo y no continúes."

Es la decisión correcta para ellos —un especialista que pregunta a mitad de un fan-out deja la
rama colgada— pero significa que, si falta el RUC, el financiero se detiene y el informe sale
cojo sin que nadie haya avisado al analista. Por eso la aclaración tiene que ocurrir **antes**
del fan-out, en un único sitio: aquí.

El intake pide de una vez todo lo que falte. Pedir un dato, esperar, y luego pedir otro es la
forma más rápida de que el analista abandone.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from sesiones import Datos, Sesion

log = logging.getLogger(__name__)

INSTRUCCIONES = """\
Eres el componente de admisión de un sistema de análisis de riesgo crediticio de banca corporativa
peruana. Tu única tarea es identificar tres datos sobre la empresa a analizar:

- empresa: la razón social o nombre comercial.
- ruc: el Registro Único de Contribuyentes (identificador tributario peruano, 11 dígitos).
- sector: el sector económico (minería, agroexportación, pesca, construcción, retail, textil...).

Reglas:
1. Usa el mensaje del analista Y el texto de los documentos adjuntos. El RUC suele aparecer en el
   encabezado de un reporte de créditos o de un informe comercial aunque el analista no lo escriba.
2. Si un dato no aparece de forma explícita y verificable, devuélvelo como null. NO lo inventes y
   NO lo deduzcas por parecido de nombre.
3. El sector SÍ puedes deducirlo del giro de negocio descrito en el documento o del propio nombre
   de la empresa cuando sea inequívoco (p. ej. "Compañía Minera X" -> minería). Si lo deduces,
   marca sector_deducido = true.
4. Si el analista ya había dado un dato antes y ahora lo corrige, vale el nuevo.
5. En `de_donde` explica en una frase de dónde sacaste cada dato encontrado.
"""


class Extraccion(BaseModel):
    empresa: str | None = Field(default=None, description="Razón social o nombre comercial")
    ruc: str | None = Field(default=None, description="RUC peruano, solo dígitos")
    sector: str | None = Field(default=None, description="Sector económico")
    sector_deducido: bool = Field(default=False, description="True si el sector se dedujo, no venía explícito")
    de_donde: str = Field(default="", description="De dónde salió cada dato, en una frase")


def normalizar_ruc(bruto: str | None) -> str | None:
    """Quita separadores. No valida la longitud a propósito.

    El agente financiero es explícito: "No rechaces un RUC por su longitud: pásalo tal cual a la
    herramienta, que es quien lo valida". Si aquí filtráramos por 11 dígitos, un RUC atípico se
    perdería antes de llegar a quien sabe juzgarlo.
    """
    if not bruto:
        return None
    limpio = re.sub(r"[^0-9]", "", bruto)
    return limpio or None


async def extraer_datos(foundry, mensaje: str, contexto_adjuntos: str, previos: Datos) -> Extraccion:
    conocido = {k: v for k, v in previos.__dict__.items() if v}
    partes = [f"MENSAJE DEL ANALISTA:\n{mensaje or '(sin texto)'}"]
    if conocido:
        partes.append(f"DATOS YA CONOCIDOS DE ESTA CONVERSACIÓN:\n{conocido}")
    if contexto_adjuntos:
        partes.append(f"TEXTO DE LOS DOCUMENTOS ADJUNTOS:\n{contexto_adjuntos}")
    return await foundry.estructurado(INSTRUCCIONES, "\n\n".join(partes), Extraccion)


def fusionar(sesion: Sesion, extraccion: Extraccion) -> None:
    """Vuelca lo extraído sobre la sesión. Lo nuevo pisa lo viejo; lo vacío no borra."""
    if extraccion.empresa:
        sesion.datos.empresa = extraccion.empresa.strip()
    ruc = normalizar_ruc(extraccion.ruc)
    if ruc:
        sesion.datos.ruc = ruc
    if extraccion.sector:
        sesion.datos.sector = extraccion.sector.strip()


ETIQUETAS = {
    "empresa": "la **razón social** de la empresa",
    "ruc": "el **RUC** (lo necesita el análisis financiero para localizar los estados financieros)",
    "sector": "el **sector económico** (lo necesita el análisis sectorial)",
}


def pedir_lo_que_falta(datos: Datos) -> str:
    """El mensaje de aclaración. Todo lo que falta de una vez, nunca de a poco."""
    faltan = datos.faltantes()
    tengo = [f"{c}: **{getattr(datos, c)}**" for c in ("empresa", "ruc", "sector") if getattr(datos, c)]

    lineas = []
    if tengo:
        lineas.append("Tengo " + ", ".join(tengo) + ".")
    if len(faltan) == 1:
        lineas.append(f"Para arrancar el análisis me falta {ETIQUETAS[faltan[0]]}.")
    else:
        lineas.append("Para arrancar el análisis me falta:")
        lineas.extend(f"- {ETIQUETAS[c]}" for c in faltan)
    lineas.append(
        "\nPuedes escribírmelo directamente, o adjuntar el reporte de créditos o el informe "
        "comercial de la empresa y lo extraigo de ahí."
    )
    return "\n".join(lineas)

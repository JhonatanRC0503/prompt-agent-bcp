"""Configuración del orquestador, leída del entorno.

Convención del repo (ver README, "Nunca hardcodear IDs de tenant"): aquí solo viven nombres
lógicos. Las URLs y los identificadores concretos de recursos se resuelven en tiempo de
ejecución a través de las conexiones del proyecto de Foundry, para que el mismo código corra en
otro tenant sin tocar una línea.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

RAIZ_REPO = Path(__file__).resolve().parents[1]

# En la VM / App Service la configuración llega por variables de entorno (app settings) y estos
# .env no existen; en local sí. `load_dotenv` no pisa lo que ya esté en el entorno.
load_dotenv(RAIZ_REPO / ".env")
load_dotenv(Path(__file__).parent / ".env")


@dataclass(frozen=True)
class Ajustes:
    # --- Foundry ---
    endpoint: str
    # Modelo del propio orquestador: solo hace extracción de entidades y enrutado de preguntas.
    # Son tareas cortas y acotadas, así que va el mini: más barato y varios segundos más rápido.
    modelo_orquestador: str = "gpt-4.1-mini"

    # --- Nombres de los prompt agents publicados en Foundry ---
    agente_financiero: str = "financiero"
    agente_sectorial: str = "sectorial"
    agente_reportes_previos: str = "reportes-previos"
    agente_generador: str = "generador-reporte"

    # --- Persistencia (opcional: si falla, el orquestador sigue funcionando en memoria) ---
    contenedor_sesiones: str = "sesiones"

    # --- Document Intelligence (opcional: OCR de PDFs escaneados) ---
    document_intelligence_endpoint: str | None = None

    # --- Límites ---
    # Un especialista tarda 77-85 s en condiciones normales. 420 s deja margen para un día malo
    # sin que una sesión se quede colgada para siempre.
    timeout_agente: int = 420
    max_bytes_adjunto: int = 20 * 1024 * 1024
    # Cuánto texto de los adjuntos se inyecta en el prompt de cada especialista.
    max_chars_adjunto: int = 60_000

    @property
    def especialistas(self) -> dict[str, str]:
        """Las tres secciones del informe y el agente que produce cada una."""
        return {
            "financiero": self.agente_financiero,
            "sectorial": self.agente_sectorial,
            "reportes_previos": self.agente_reportes_previos,
        }


def _entero(nombre: str, por_defecto: int) -> int:
    bruto = os.environ.get(nombre)
    if not bruto:
        return por_defecto
    try:
        return int(bruto)
    except ValueError:
        raise SystemExit(f"{nombre} debe ser un número entero, no {bruto!r}.")


def cargar_ajustes() -> Ajustes:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        raise SystemExit(
            "Falta FOUNDRY_PROJECT_ENDPOINT. En local va en el .env de la raíz del repo; "
            "en App Service, en los app settings."
        )
    return Ajustes(
        endpoint=endpoint,
        modelo_orquestador=os.environ.get("MODELO_ORQUESTADOR", "gpt-4.1-mini"),
        agente_financiero=os.environ.get("AGENTE_FINANCIERO", "financiero"),
        agente_sectorial=os.environ.get("AGENTE_SECTORIAL", "sectorial"),
        agente_reportes_previos=os.environ.get("AGENTE_REPORTES_PREVIOS", "reportes-previos"),
        agente_generador=os.environ.get("AGENTE_GENERADOR", "generador-reporte"),
        contenedor_sesiones=os.environ.get("CONTENEDOR_SESIONES", "sesiones"),
        document_intelligence_endpoint=os.environ.get("DOCUMENT_INTELLIGENCE_ENDPOINT") or None,
        timeout_agente=_entero("TIMEOUT_AGENTE", 420),
        max_bytes_adjunto=_entero("MAX_BYTES_ADJUNTO", 20 * 1024 * 1024),
        max_chars_adjunto=_entero("MAX_CHARS_ADJUNTO", 60_000),
    )


AJUSTES = cargar_ajustes()

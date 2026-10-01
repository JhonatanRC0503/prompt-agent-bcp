"""Herramientas del agente financiero.

La implementación vive en `function_app/eeff.py` porque la comparten dos consumidores:
este agente, que la registra como function tool (client-side), y la Function App, que la
expone por HTTP para que Foundry la ejecute server-side. Mantenerla en un solo sitio evita
que un arreglo del parser de Excel haya que aplicarlo dos veces.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "function_app"))

from eeff import build_obtener_estados_financieros  # noqa: E402

__all__ = ["build_obtener_estados_financieros"]

"""Spec OpenAPI de las herramientas, en un solo sitio.

Lo usan dos consumidores, y por eso no vive dentro del trigger HTTP:
  - `function_app.py`, que lo sirve en `GET /api/openapi.json` con la URL del propio request.
  - `agents/financiero/main.py`, que lo registra como tool `openapi` al publicar el agente.

Mantenerlo aquí evita que el spec publicado en Foundry y el que sirve la API se desincronicen.
"""

from __future__ import annotations

from typing import Any

RUTA_HERRAMIENTA = "estados-financieros"
NOMBRE_HERRAMIENTA = "obtener_estados_financieros"


def construir_spec(base_url: str) -> dict[str, Any]:
    """Spec OpenAPI 3.0 de la herramienta, apuntando a `base_url`.

    `base_url` es la raíz de la API (`https://<host>/api`), sin barra final.
    """
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Herramientas de riesgo crediticio",
            "description": "Acceso al repositorio de estados financieros de banca corporativa.",
            "version": "1.0.0",
        },
        "servers": [{"url": base_url}],
        "paths": {
            f"/{RUTA_HERRAMIENTA}": {
                "post": {
                    "operationId": NOMBRE_HERRAMIENTA,
                    "summary": (
                        "Obtiene del repositorio los estados financieros de una empresa "
                        "a partir de su RUC."
                    ),
                    "description": (
                        "Devuelve las hojas del libro (Estado de Resultados, Balance General, "
                        "Ratios Financieros) con sus periodos como columnas y sus cuentas "
                        "como filas."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["ruc"],
                                    # Foundry publica las tools en modo `strict`, que exige
                                    # este campo; sin él falla la validación del schema.
                                    "additionalProperties": False,
                                    "properties": {
                                        "ruc": {
                                            "type": "string",
                                            "description": (
                                                "RUC de 11 dígitos de la empresa cuyos "
                                                "estados financieros se necesitan."
                                            ),
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Estados financieros de la empresa.",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            }
        },
        "components": {
            "securitySchemes": {
                "functionKey": {"type": "apiKey", "name": "x-functions-key", "in": "header"}
            }
        },
        "security": [{"functionKey": []}],
    }

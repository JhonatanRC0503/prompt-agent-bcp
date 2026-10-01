"""Lectura de estados financieros desde Blob Storage: implementación única del parser.

La usan dos consumidores, y por eso vive aquí y no dentro del agente:
  - `agents/financiero/tools.py`, que la registra como function tool (client-side).
  - `function_app.py`, que la expone por HTTP para que Foundry la ejecute server-side.

El repositorio se organiza por banca y cliente (`BN|BC|BE/<RUC>/<RUC>.xlsx`). El archivo siempre
se llama como el RUC y se busca por nombre en todas las carpetas del contenedor, sin importar a
qué profundidad esté: por eso reorganizar las carpetas no rompe la herramienta.
"""

from __future__ import annotations

import datetime
import json
from io import BytesIO
from typing import Annotated, Any

import openpyxl
from azure.storage.blob.aio import BlobServiceClient

EXTENSIONES_EXCEL = (".xlsx", ".xlsm")

# Los ratios vienen con precisión de punto flotante completa (0.08141814159292035);
# recortarlos reduce ruido en el contexto del modelo sin perder exactitud útil.
DECIMALES = 6


def _es_archivo_del_ruc(blob_name: str, ruc: str) -> bool:
    """El archivo de estados financieros siempre se llama `<RUC>.xlsx`, en cualquier carpeta."""
    archivo = blob_name.rsplit("/", 1)[-1].lower()
    return any(archivo == f"{ruc.lower()}{ext}" for ext in EXTENSIONES_EXCEL)


def _limpiar(valor: Any) -> Any:
    if isinstance(valor, float):
        return round(valor, DECIMALES)
    if hasattr(valor, "isoformat"):
        return valor.isoformat()
    return valor


def _texto_de_encabezado(celda: Any) -> str:
    """Los periodos suelen venir como fecha real de Excel; `str()` daría '2022-12-31 00:00:00'."""
    if isinstance(celda, datetime.datetime):
        return celda.strftime("%d/%m/%Y")
    if isinstance(celda, datetime.date):
        return celda.strftime("%d/%m/%Y")
    return str(celda).strip() if celda is not None else ""


def _fila_de_encabezado(filas: list[tuple[Any, ...]]) -> int:
    """Índice de la fila que define las columnas de periodos.

    Las hojas traen 3-4 líneas de título antes de la tabla, así que no se puede
    asumir que la primera fila sea el encabezado.
    """
    for indice, fila in enumerate(filas):
        primera = str(fila[0]).strip().lower() if fila[0] is not None else ""
        if primera == "cuenta":
            return indice
    for indice, fila in enumerate(filas):
        if sum(1 for celda in fila if celda is not None) >= 2:
            return indice
    return 0


def _nombres_de_columnas(encabezado: tuple[Any, ...]) -> list[tuple[int, str]]:
    """Empareja cada columna de datos con su nombre, como pares (índice, nombre).

    Se indexa por posición y no por los encabezados no vacíos: una columna sin título
    en medio de la tabla desplazaría todos los valores siguientes. Los nombres repetidos
    se desambiguan porque se usan como claves del JSON.
    """
    ultimo = len(encabezado)
    while ultimo > 1 and encabezado[ultimo - 1] is None:
        ultimo -= 1

    nombres: list[tuple[int, str]] = []
    vistos: dict[str, int] = {}
    for columna in range(1, ultimo):
        base = _texto_de_encabezado(encabezado[columna])
        if not base:
            base = f"Columna {columna + 1}"
        vistos[base] = vistos.get(base, 0) + 1
        nombres.append((columna, base if vistos[base] == 1 else f"{base} ({vistos[base]})"))
    return nombres


def _hoja_a_dict(hoja: Any) -> dict[str, Any]:
    filas = list(hoja.iter_rows(values_only=True))
    if not filas:
        return {"nombre": hoja.title, "titulos": [], "columna_clave": "", "periodos": [], "filas": []}

    indice_encabezado = _fila_de_encabezado(filas)
    columnas = _nombres_de_columnas(filas[indice_encabezado])

    titulos = [
        str(fila[0]).strip()
        for fila in filas[:indice_encabezado]
        if fila and fila[0] is not None and str(fila[0]).strip()
    ]

    filas_datos: list[dict[str, Any]] = []
    for fila in filas[indice_encabezado + 1 :]:
        if not fila or all(celda is None for celda in fila):
            continue
        etiqueta = str(fila[0]) if fila[0] is not None else ""
        valores = {
            nombre: _limpiar(fila[columna]) if columna < len(fila) else None
            for columna, nombre in columnas
        }
        filas_datos.append(
            {
                "cuenta": etiqueta.strip(),
                # La jerarquía del estado financiero se codifica con sangría de 3 espacios.
                "nivel": (len(etiqueta) - len(etiqueta.lstrip())) // 3,
                "sin_valores": all(valor is None for valor in valores.values()),
                "valores": valores,
            }
        )

    return {
        "nombre": hoja.title,
        "titulos": titulos,
        # Normalmente es el nombre de la cuenta, pero en hojas de registros puede ser un ID;
        # el encabezado de esa columna dice qué contiene realmente `cuenta` en cada fila.
        "columna_clave": _texto_de_encabezado(filas[indice_encabezado][0]),
        "periodos": [nombre for _, nombre in columnas],
        "filas": filas_datos,
    }


def excel_a_dict(contenido: bytes) -> list[dict[str, Any]]:
    """Convierte el libro de Excel en una estructura que preserva filas, columnas y jerarquía."""
    libro = openpyxl.load_workbook(BytesIO(contenido), data_only=True, read_only=True)
    try:
        hojas = [_hoja_a_dict(hoja) for hoja in libro.worksheets]
    finally:
        libro.close()
    return [hoja for hoja in hojas if hoja["filas"]]


def build_obtener_estados_financieros(account_url: str, container: str, credential: Any):
    """Crea la herramienta, ligada a la cuenta de Storage y al contenedor resueltos en main."""

    async def obtener_estados_financieros(
        ruc: Annotated[str, "RUC de la empresa (solo dígitos) cuyos estados financieros se necesitan."],
    ) -> str:
        """Obtiene del repositorio los estados financieros de una empresa a partir de su RUC.

        Devuelve, en JSON, las hojas del libro (Estado de Resultados, Balance General,
        Ratios Financieros) con sus periodos como columnas y sus cuentas como filas.
        """
        ruc = ruc.strip()
        # La longitud no se valida: el identificador es el nombre del archivo, y los datos de
        # prueba usan RUC de 9 dígitos. Basta con que sea numérico y no vacío; la búsqueda es
        # por nombre exacto, así que un valor inexistente solo devuelve "no se encontró".
        if not ruc.isdigit():
            return json.dumps(
                {"error": f"El RUC debe ser numérico. Se recibió: {ruc!r}"},
                ensure_ascii=False,
            )

        async with BlobServiceClient(account_url, credential=credential) as servicio:
            cliente_container = servicio.get_container_client(container)

            ruta: str | None = None
            async for blob in cliente_container.list_blobs():
                if _es_archivo_del_ruc(blob.name, ruc):
                    ruta = blob.name
                    break

            if ruta is None:
                return json.dumps(
                    {
                        "error": f"No se encontró el archivo {ruc}.xlsx en el repositorio de estados "
                        f"financieros (contenedor '{container}'). Verifica que el RUC sea correcto y "
                        f"que el archivo esté cargado."
                    },
                    ensure_ascii=False,
                )

            descarga = await cliente_container.download_blob(ruta)
            contenido = await descarga.readall()

        return json.dumps(
            {"ruc": ruc, "archivo": f"{container}/{ruta}", "hojas": excel_a_dict(contenido)},
            ensure_ascii=False,
        )

    return obtener_estados_financieros

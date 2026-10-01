"""Despliega el orquestador (`orchestrator/`) en el App Service.

El despliegue tiene dos naturalezas distintas y el script las separa a propósito:

* `--bootstrap` se hace **una vez**: crear la Web App, darle identidad, asignarle roles y dejar
  la configuración puesta. Necesita permisos de Owner o User Access Administrator sobre la
  suscripción, porque asigna roles.
* El despliegue del código se hace **cada vez que cambias algo**. Solo necesita Contributor.

Mezclarlos en un único comando obligaría a repetir la asignación de roles en cada subida, que es
ruido y además exige permisos que el despliegue normal no necesita.

Todo es idempotente: volver a ejecutar cualquier fase no rompe nada ni duplica roles.

Nada de esto hardcodea identificadores: la suscripción y el grupo salen del `.env` de la raíz y
la cuenta de Foundry se deduce de `FOUNDRY_PROJECT_ENDPOINT`. Es la convención del repo (README,
"Nunca hardcodear IDs de tenant"), así que el script sirve igual en otro tenant.

Uso:
    python scripts/deploy_webapp.py --bootstrap      # una vez: crea, da roles y configura
    python scripts/deploy_webapp.py                  # habitual: empaqueta y despliega el código
    python scripts/deploy_webapp.py --verify         # solo comprueba que responde
    python scripts/deploy_webapp.py --all            # bootstrap + despliegue + verificación
    python scripts/deploy_webapp.py --all --dry-run  # enseña los comandos sin ejecutarlos

Configuración opcional por entorno (hay valores por defecto para este proyecto):
    APP_SERVICE_NAME       nombre de la Web App            (awaws1rmiad02)
    APP_SERVICE_PLAN       plan que la hospeda             (aspleu2rmiad02)
    STORAGE_ACCOUNT_NAME   cuenta para el snapshot         (se descubre sola en el grupo)
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
ORQUESTADOR = REPO_ROOT / "orchestrator"
PAQUETE = REPO_ROOT / "orquestador.zip"

PLAN_POR_DEFECTO = "aspleu2rmiad02"
APP_POR_DEFECTO = "awaws1rmiad02"
RUNTIME = "PYTHON:3.11"

# Un solo worker, a propósito: el estado de las sesiones vive en memoria del proceso. Con dos,
# el POST del analista y su stream SSE pueden caer en procesos distintos y la pantalla se queda
# congelada. Para escalar hay que sacar el estado del proceso primero (ver app_service.md §8).
ARRANQUE = (
    "gunicorn -w 1 -k uvicorn.workers.UvicornWorker app:app "
    "--bind 0.0.0.0:8000 --timeout 1800 --graceful-timeout 60 "
    "--access-logfile - --error-logfile -"
)

DRY_RUN = False


# --------------------------------------------------------------------------- utilidades


def paso(texto: str) -> None:
    print(f"\n\033[1m==> {texto}\033[0m", flush=True)


def aviso(texto: str) -> None:
    print(f"    ! {texto}", flush=True)


def ok(texto: str) -> None:
    print(f"    ✓ {texto}", flush=True)


def az(*args: str, salida_json: bool = True, tolerar_fallo: bool = False):
    """Ejecuta `az` y devuelve su salida ya parseada.

    En `--dry-run` imprime el comando y devuelve None, salvo las consultas de solo lectura:
    esas sí se ejecutan, porque sin ellas el script no podría decidir qué haría de verdad.
    """
    comando = ["az", *args]
    solo_lectura = args[-1:] == ("--output",) or any(
        a in ("show", "list") for a in args[:3]
    )

    if DRY_RUN and not solo_lectura:
        print("    $ " + shlex.join(comando))
        return None

    if salida_json and "--output" not in args and "-o" not in args:
        comando += ["--output", "json"]

    proceso = subprocess.run(comando, capture_output=True, text=True)
    if proceso.returncode != 0:
        if tolerar_fallo:
            return None
        sys.exit(
            f"\nFalló: {' '.join(comando)}\n\n{proceso.stderr.strip()}\n"
        )
    if not salida_json or not proceso.stdout.strip():
        return proceso.stdout.strip()
    try:
        return json.loads(proceso.stdout)
    except json.JSONDecodeError:
        return proceso.stdout.strip()


def entorno() -> dict:
    """Lee el .env de la raíz y deduce de ahí todo lo demás."""
    load_dotenv(REPO_ROOT / ".env")

    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    suscripcion = os.environ.get("AZURE_SUBSCRIPTION_ID")
    grupo = os.environ.get("AZURE_RESOURCE_GROUP")
    faltan = [
        n
        for n, v in (
            ("FOUNDRY_PROJECT_ENDPOINT", endpoint),
            ("AZURE_SUBSCRIPTION_ID", suscripcion),
            ("AZURE_RESOURCE_GROUP", grupo),
        )
        if not v
    ]
    if faltan:
        sys.exit(f"Falta {', '.join(faltan)} en {REPO_ROOT / '.env'}.")

    # https://<cuenta>.services.ai.azure.com/api/projects/<proyecto> -> <cuenta>
    cuenta_foundry = endpoint.split("://", 1)[-1].split(".", 1)[0]

    return {
        "endpoint": endpoint,
        "suscripcion": suscripcion,
        "grupo": grupo,
        "cuenta_foundry": cuenta_foundry,
        "app": os.environ.get("APP_SERVICE_NAME", APP_POR_DEFECTO),
        "plan": os.environ.get("APP_SERVICE_PLAN", PLAN_POR_DEFECTO),
    }


def comprobar_sesion(env: dict) -> None:
    cuenta = az("account", "show", tolerar_fallo=True)
    if not cuenta:
        sys.exit("No hay sesión de Azure. Ejecuta `az login` y vuelve a intentarlo.")
    ok(f"sesión: {cuenta['user']['name']}")
    if cuenta["id"] != env["suscripcion"]:
        aviso(
            f"la suscripción activa ({cuenta['id']}) no es la del .env ({env['suscripcion']}); "
            f"se cambiará para este despliegue"
        )
        az("account", "set", "--subscription", env["suscripcion"], salida_json=False)


def cuenta_de_storage(env: dict) -> str | None:
    """La cuenta para el snapshot de sesiones. Si hay varias, hay que decir cuál."""
    nombre = os.environ.get("STORAGE_ACCOUNT_NAME")
    if nombre:
        return nombre
    cuentas = az("storage", "account", "list", "-g", env["grupo"], "--query", "[].name") or []
    if len(cuentas) == 1:
        return cuentas[0]
    if not cuentas:
        aviso("no hay cuentas de Storage en el grupo; se omite el rol de persistencia")
    else:
        aviso(
            f"hay {len(cuentas)} cuentas de Storage en el grupo {cuentas}; "
            f"define STORAGE_ACCOUNT_NAME para elegir una"
        )
    return None


def document_intelligence(env: dict) -> tuple[str, str] | None:
    """El recurso de Document Intelligence del grupo, como (nombre, endpoint).

    Lee los PDFs adjuntos con `prebuilt-layout`: devuelve las tablas estructuradas y hace OCR.
    Sin él, el orquestador cae a pypdf, que pierde las tablas y no lee escaneados.
    """
    nombre = os.environ.get("DOCUMENT_INTELLIGENCE_NAME")
    cuentas = az(
        "cognitiveservices", "account", "list", "-g", env["grupo"],
        "--query", "[?kind=='FormRecognizer'].{nombre:name,endpoint:properties.endpoint}",
    ) or []
    if nombre:
        cuentas = [c for c in cuentas if c["nombre"] == nombre]
    if len(cuentas) == 1:
        return cuentas[0]["nombre"], cuentas[0]["endpoint"]
    if not cuentas:
        aviso("no hay recurso de Document Intelligence en el grupo; los PDFs se leerán con pypdf")
    else:
        aviso(f"hay varios recursos de Document Intelligence; define DOCUMENT_INTELLIGENCE_NAME")
    return None


# --------------------------------------------------------------------------- fases


def asegurar_webapp(env: dict) -> None:
    existe = az(
        "webapp", "show", "-g", env["grupo"], "-n", env["app"], "--query", "name",
        tolerar_fallo=True,
    )
    if existe:
        ok(f"la Web App {env['app']} ya existe")
        return
    az(
        "webapp", "create", "-g", env["grupo"], "-p", env["plan"], "-n", env["app"],
        "--runtime", RUNTIME,
    )
    ok(f"Web App {env['app']} creada sobre el plan {env['plan']}")


def asegurar_rol(principal: str, rol: str, ambito: str, etiqueta: str) -> None:
    """Asigna el rol solo si no lo tiene ya. Azure no falla al duplicar, pero el log mentiría."""
    existentes = az(
        "role", "assignment", "list",
        "--assignee", principal, "--role", rol, "--scope", ambito, "--query", "[].id",
        tolerar_fallo=True,
    )
    if existentes:
        ok(f"{rol} sobre {etiqueta}: ya asignado")
        return
    resultado = az(
        "role", "assignment", "create",
        "--assignee-object-id", principal, "--assignee-principal-type", "ServicePrincipal",
        "--role", rol, "--scope", ambito,
        tolerar_fallo=True,
    )
    if DRY_RUN:
        return
    if resultado is None:
        aviso(
            f"no se pudo asignar {rol} sobre {etiqueta}. Hace falta Owner o "
            f"User Access Administrator sobre la suscripción."
        )
    else:
        ok(f"{rol} sobre {etiqueta}: asignado")


def bootstrap(env: dict) -> None:
    paso("1/3 · Web App e identidad administrada")
    asegurar_webapp(env)

    identidad = az("webapp", "identity", "assign", "-g", env["grupo"], "-n", env["app"])
    principal = identidad["principalId"] if identidad else "<principalId>"
    ok(f"identidad administrada: {principal}")

    paso("2/3 · Roles RBAC")
    base = f"/subscriptions/{env['suscripcion']}/resourceGroups/{env['grupo']}/providers"
    asegurar_rol(
        principal, "Foundry User",
        f"{base}/Microsoft.CognitiveServices/accounts/{env['cuenta_foundry']}",
        f"Foundry {env['cuenta_foundry']}",
    )
    storage = cuenta_de_storage(env)
    if storage:
        asegurar_rol(
            principal, "Storage Blob Data Contributor",
            f"{base}/Microsoft.Storage/storageAccounts/{storage}",
            f"Storage {storage}",
        )
    doc_int = document_intelligence(env)
    if doc_int:
        asegurar_rol(
            principal, "Cognitive Services User",
            f"{base}/Microsoft.CognitiveServices/accounts/{doc_int[0]}",
            f"Document Intelligence {doc_int[0]}",
        )

    paso("3/3 · Configuración del App Service")
    ajustes_opcionales = []
    if doc_int:
        ajustes_opcionales.append(f"DOCUMENT_INTELLIGENCE_ENDPOINT={doc_int[1]}")
    az(
        "webapp", "config", "appsettings", "set", "-g", env["grupo"], "-n", env["app"],
        "--settings",
        *ajustes_opcionales,
        f"FOUNDRY_PROJECT_ENDPOINT={env['endpoint']}",
        "MODELO_ORQUESTADOR=gpt-4.1-mini",
        "AGENTE_FINANCIERO=financiero",
        "AGENTE_SECTORIAL=sectorial",
        "AGENTE_REPORTES_PREVIOS=reportes-previos",
        "AGENTE_GENERADOR=generador-reporte",
        "CONTENEDOR_SESIONES=sesiones",
        "TIMEOUT_AGENTE=420",
        "LOG_LEVEL=INFO",
        # Las dos hacen falta para que Oryx instale requirements.txt en el servidor.
        # Sin build, el contenedor arranca sin dependencias y gunicorn muere con
        # "ModuleNotFoundError: No module named 'uvicorn'".
        "SCM_DO_BUILD_DURING_DEPLOYMENT=true",
        "ENABLE_ORYX_BUILD=true",
        # El arranque carga el Agent Framework; el límite por defecto (230 s) se queda corto.
        "WEBSITES_CONTAINER_START_TIME_LIMIT=600",
        "--output", "none",
    )
    ok("app settings")

    # Always On importa más aquí que en una web normal: el análisis sigue corriendo en segundo
    # plano después de que el POST haya respondido. Sin esto, App Service descarga la app por
    # inactividad y se lleva por delante los análisis en curso.
    az(
        "webapp", "config", "set", "-g", env["grupo"], "-n", env["app"],
        "--always-on", "true", "--startup-file", ARRANQUE, "--output", "none",
    )
    ok("Always On y comando de arranque")

    az("webapp", "update", "-g", env["grupo"], "-n", env["app"], "--https-only", "true", "--output", "none")
    ok("solo HTTPS")


def empaquetar() -> Path:
    """Empaqueta solo `orchestrator/`. El resto del repo no lo necesita el App Service."""
    if not ORQUESTADOR.is_dir():
        sys.exit(f"No existe {ORQUESTADOR}.")

    if DRY_RUN:
        print(f"    $ (empaquetaría {ORQUESTADOR} -> {PAQUETE})")
        return PAQUETE

    PAQUETE.unlink(missing_ok=True)
    incluidos = 0
    with zipfile.ZipFile(PAQUETE, "w", zipfile.ZIP_DEFLATED) as z:
        for archivo in sorted(ORQUESTADOR.rglob("*")):
            if not archivo.is_file():
                continue
            relativo = archivo.relative_to(ORQUESTADOR)
            # `.env` se excluye a propósito: en App Service la configuración son los app
            # settings, y un .env de desarrollo colado los pisaría.
            if (
                "__pycache__" in relativo.parts
                or archivo.suffix == ".pyc"
                or relativo.name == ".env"
            ):
                continue
            z.write(archivo, relativo.as_posix())
            incluidos += 1
    ok(f"{incluidos} archivos -> {PAQUETE.name} ({PAQUETE.stat().st_size // 1024} KB)")
    return PAQUETE


def desplegar(env: dict) -> None:
    paso("Empaquetando el orquestador")
    empaquetar()
    paso("Desplegando (la primera vez tarda varios minutos: Oryx instala las dependencias)")
    # `az webapp deployment source config-zip` publica por el endpoint /api/zipdeploy de Kudu,
    # que SÍ invoca a Oryx. `az webapp deploy --type zip` usa OneDeploy, que en este stack se
    # limita a copiar los archivos con rsync: el build "termina" en 3 s sin instalar nada y el
    # contenedor luego no arranca porque no existe ni el virtualenv `antenv` ni uvicorn.
    # `tolerar_fallo`: el CLI deja de esperar a los 10 minutos y devuelve error aunque el
    # despliegue haya ido bien. Pasó en el primer despliegue correcto: el CLI dijo "site failed
    # to start" mientras el contenedor ya estaba arrancado y /api/salud respondía. Quien decide
    # si funcionó es la verificación contra el endpoint, no el código de salida del CLI.
    resultado = az(
        "webapp", "deployment", "source", "config-zip",
        "-g", env["grupo"], "-n", env["app"], "--src", str(PAQUETE), "--output", "none",
        tolerar_fallo=True,
    )
    if resultado is None and not DRY_RUN:
        aviso("el CLI reportó error; puede ser solo que dejó de esperar. Lo comprueba la verificación.")
    else:
        ok("código desplegado")

    # Un despliegue fallido deja el sitio parado, así que hay que levantarlo explícitamente.
    az("webapp", "start", "-g", env["grupo"], "-n", env["app"], "--output", "none",
       tolerar_fallo=True)


def verificar(env: dict, intentos: int = 20, espera: int = 15) -> bool:
    """Sondea /api/salud hasta que responda. Tras un despliegue, el arranque tarda."""
    url = f"https://{env['app']}.azurewebsites.net/api/salud"
    paso(f"Verificando {url}")
    if DRY_RUN:
        print(f"    $ (sondearía {url})")
        return True

    for intento in range(1, intentos + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as respuesta:
                datos = json.loads(respuesta.read())
            ok(f"responde: {json.dumps(datos, ensure_ascii=False)}")
            print(f"\n    Interfaz: https://{env['app']}.azurewebsites.net/")
            return True
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            print(f"    intento {intento}/{intentos}: aún no responde ({error})", flush=True)
            if intento < intentos:
                time.sleep(espera)

    aviso("no respondió. Revisa los logs:")
    print(f"      az webapp log tail -g {env['grupo']} -n {env['app']}")
    print(
        "\n    Si el log dice \"Could not find virtual environment directory\" o\n"
        "    \"No module named 'uvicorn'\", Oryx no instaló las dependencias:\n"
        "    comprueba que ENABLE_ORYX_BUILD y SCM_DO_BUILD_DURING_DEPLOYMENT estén en true\n"
        "    y vuelve a ejecutar este script."
    )
    return False


# --------------------------------------------------------------------------- entrada


def main() -> None:
    global DRY_RUN
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bootstrap", action="store_true",
                        help="Una vez: crea la Web App, asigna roles y deja la configuración puesta.")
    parser.add_argument("--verify", action="store_true", help="Solo comprueba que la app responde.")
    parser.add_argument("--all", action="store_true", help="Bootstrap + despliegue + verificación.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Enseña los comandos que ejecutaría, sin ejecutarlos.")
    args = parser.parse_args()

    DRY_RUN = args.dry_run
    if DRY_RUN:
        print("\033[33mMODO DRY-RUN: no se ejecuta ningún cambio.\033[0m")

    env = entorno()
    paso("Entorno")
    print(f"    suscripción : {env['suscripcion']}")
    print(f"    grupo       : {env['grupo']}")
    print(f"    app         : {env['app']}  (plan {env['plan']})")
    print(f"    foundry     : {env['cuenta_foundry']}")
    comprobar_sesion(env)

    # Sin opciones, la acción por defecto es la frecuente: desplegar el código.
    hacer_bootstrap = args.bootstrap or args.all
    hacer_deploy = args.all or not (args.bootstrap or args.verify)
    hacer_verify = args.verify or args.all or hacer_deploy

    if hacer_bootstrap:
        bootstrap(env)
    if hacer_deploy:
        desplegar(env)
    if hacer_verify and not verificar(env):
        sys.exit(1)

    print("\n\033[1mListo.\033[0m")


if __name__ == "__main__":
    main()

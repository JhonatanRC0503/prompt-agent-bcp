# Despliegue del orquestador en App Service

Runbook del orquestador (`orchestrator/`): la app que recibe al analista, coordina a los tres
especialistas y devuelve el informe integrado.

Este documento es el equivalente a [`function.md`](function.md), pero para el App Service. Solo
lo necesario: el código ya está escrito y probado.

---

## 0. Estado del despliegue

| Paso | Estado |
|---|---|
| Web App `awaws1rmiad02` creada sobre el plan B1 `aspleu2rmiad02` | ✅ hecho |
| Identidad administrada (system-assigned) habilitada | ✅ hecha — `71687d8f-2550-441e-8eb6-4ee27a9d98a8` |
| Roles RBAC sobre Foundry y Storage | ⬜ **pendiente — `--bootstrap`** |
| App settings, Always On y comando de arranque | ⬜ **pendiente — `--bootstrap`** |
| Código desplegado | ⬜ **pendiente — `deploy_webapp.py`** |

Los tres pendientes los resuelve `python scripts/deploy_webapp.py --all` (sección 1). Están probados en su forma, pero requieren
permisos de escritura sobre la suscripción que la sesión que generó este documento no tenía.

```bash
# Variables que usan todos los comandos de este runbook
RG=RSGRSC1RMIAD02
APP=awaws1rmiad02
SUB=256902ad-0a22-4f6a-a834-090e2ea0ccf5
PRINCIPAL=71687d8f-2550-441e-8eb6-4ee27a9d98a8   # identidad del App Service
```

---

## 1. La vía rápida: el script

Todo lo de las secciones 2 a 5 está automatizado en
[`scripts/deploy_webapp.py`](scripts/deploy_webapp.py):

```bash
# Una sola vez: crea la Web App si falta, le da identidad, asigna los roles y la configura.
# Requiere Owner o User Access Administrator (asigna roles).
python scripts/deploy_webapp.py --bootstrap

# Cada vez que cambies el código. Requiere solo Contributor.
python scripts/deploy_webapp.py

# Todo seguido, o solo comprobar que responde
python scripts/deploy_webapp.py --all
python scripts/deploy_webapp.py --verify

# Enseña lo que haría sin tocar nada (las consultas de lectura sí se ejecutan,
# así que refleja el estado real)
python scripts/deploy_webapp.py --all --dry-run
```

Por qué dos fases y no un solo comando: el bootstrap se hace una vez y necesita permisos para
asignar roles; el despliegue del código se repite constantemente y solo necesita Contributor.
Unirlos obligaría a repetir la asignación de roles en cada subida y exigiría permisos de más para
una operación rutinaria.

Todas las fases son idempotentes: el script comprueba si la Web App existe antes de crearla y si
un rol ya está asignado antes de volver a pedirlo.

No hardcodea identificadores: la suscripción y el grupo salen del `.env` de la raíz, y la cuenta
de Foundry se deduce de `FOUNDRY_PROJECT_ENDPOINT`. La cuenta de Storage se descubre sola en el
grupo de recursos (o se fija con `STORAGE_ACCOUNT_NAME` si hubiera varias).

> **El resto de este documento son los mismos pasos a mano.** Sirven para entender qué hace el
> script y para depurar cuando algo falla; en el día a día basta con el script.

---

## 2. Roles (hazlo primero: sin esto la app arranca pero no funciona)

> El síntoma de saltarse este paso es engañoso: la app responde en `/api/salud` y la interfaz
> carga, pero **todo análisis falla** con `AuthorizationFailed` o "failed to fetch connections".
> No es un error del código: es que la identidad del App Service no hereda nada de tu usuario.

| Rol | Alcance | Para qué | ¿Obligatorio? |
|---|---|---|---|
| `Foundry User` | cuenta Foundry `aaifs1rmiad02` | Invocar los prompt agents publicados y leer las conexiones del proyecto | **Sí** |
| `Storage Blob Data Contributor` | cuenta `stacs1miabackd02` | Guardar el snapshot de las sesiones | No — sin él la app funciona, pero pierde las sesiones al reiniciar |
| `Cognitive Services User` | Document Intelligence `aidieu2rmiad02` | Leer los PDFs adjuntos con `prebuilt-layout` | Sí — sin él se cae a `pypdf` y se pierden las tablas |

```bash
# Obligatorio: invocar agentes y resolver conexiones del proyecto
az role assignment create \
  --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Foundry User" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/aaifs1rmiad02"

# Opcional: persistencia de sesiones en Blob
az role assignment create \
  --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/stacs1miabackd02"

# Leer los PDFs adjuntos con prebuilt-layout
az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services User" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/aidieu2rmiad02"
```

**Lo que el orquestador NO necesita**, y conviene no darle: permisos sobre el contenedor
`financiero` ni sobre AI Search. Los especialistas corren como agentes publicados **dentro de
Foundry**, así que sus tools `openapi` (estados financieros) y `mcp` (bases de conocimiento) se
ejecutan server-side con la identidad del proyecto y la de la Function App, no con la del
orquestador. Mínimo privilegio sin esfuerzo.

---

## 3. Configuración del App Service

### 3.1 App settings

```bash
az webapp config appsettings set -g $RG -n $APP --settings \
  FOUNDRY_PROJECT_ENDPOINT="https://aaifs1rmiad02.services.ai.azure.com/api/projects/prj_aaifs1rmiad02" \
  MODELO_ORQUESTADOR="gpt-4.1-mini" \
  AGENTE_FINANCIERO="financiero" \
  AGENTE_SECTORIAL="sectorial" \
  AGENTE_REPORTES_PREVIOS="reportes-previos" \
  AGENTE_GENERADOR="generador-reporte" \
  CONTENEDOR_SESIONES="sesiones" \
  TIMEOUT_AGENTE="420" \
  LOG_LEVEL="INFO" \
  SCM_DO_BUILD_DURING_DEPLOYMENT="true" \
  ENABLE_ORYX_BUILD="true" \
  WEBSITES_CONTAINER_START_TIME_LIMIT="600"
```

| Ajuste | Por qué este valor |
|---|---|
| `MODELO_ORQUESTADOR=gpt-4.1-mini` | El orquestador solo extrae entidades y enruta preguntas. El modelo grande se reserva para el análisis, que es donde aporta. |
| `TIMEOUT_AGENTE=420` | Un especialista tarda 77-85 s normalmente. 420 s deja margen para un día malo sin dejar sesiones colgadas. |
| `SCM_DO_BUILD_DURING_DEPLOYMENT=true` y `ENABLE_ORYX_BUILD=true` | Hacen que Oryx instale `requirements.txt` en el servidor. Sin ellas se despliega el código sin dependencias y el contenedor no arranca. |
| `WEBSITES_CONTAINER_START_TIME_LIMIT=600` | El arranque instala/carga el Agent Framework; el límite por defecto (230 s) se queda corto en el primer despliegue. |

### 3.2 Always On y comando de arranque

```bash
az webapp config set -g $RG -n $APP \
  --always-on true \
  --startup-file "gunicorn -w 1 -k uvicorn.workers.UvicornWorker app:app --bind 0.0.0.0:8000 --timeout 1800 --graceful-timeout 60 --access-logfile - --error-logfile -"

az webapp update -g $RG -n $APP --https-only true
```

Tres detalles del comando que **no son opcionales**:

- **`-w 1` (un solo worker).** El estado de las sesiones vive en memoria del proceso. Con dos
  workers, el POST del analista y su stream SSE pueden caer en procesos distintos y el analista
  se queda mirando una pantalla que nunca avanza. Si algún día hace falta escalar, el estado
  tiene que salir del proceso primero (Cosmos o Redis); no basta con subir el número de workers.
- **`-k uvicorn.workers.UvicornWorker`.** FastAPI es ASGI; el worker síncrono de gunicorn no
  sirve.
- **`--timeout 1800`.** Es el guardián de gunicorn sobre su worker, no el de la petición. Con el
  valor por defecto (30 s) gunicorn mataría al proceso en mitad de un análisis.

**Always On es imprescindible aquí**, más que en una web normal: el análisis sigue corriendo en
segundo plano después de que el POST haya respondido. Sin Always On, App Service descarga la app
por inactividad y se lleva por delante los análisis en curso.

---

## 4. Desplegar el código

Desde la raíz del repo (en tu máquina o en la VM):

```bash
cd orchestrator
zip -r ../orquestador.zip . -x '*.pyc' -x '__pycache__/*' -x '.env'
cd ..
az webapp deployment source config-zip -g $RG -n $APP --src orquestador.zip
```

> Se despliega **solo** el contenido de `orchestrator/`, no el repo entero: la app no necesita
> `agents/`, `infra/` ni `scripts/`. Es el mismo criterio de un manifiesto por artefacto
> desplegable que ya sigue `function_app/` (README §"Dos requirements.txt, a propósito").

> `.env` se excluye a propósito: en App Service la configuración son los app settings. Si se
> colara, pisaría la configuración del servicio con valores de desarrollo.

> ⚠️ **Tiene que ser `config-zip`, no `az webapp deploy --type zip`.** Los dos suben el mismo
> zip, pero por endpoints distintos: `config-zip` publica por `/api/zipdeploy` de Kudu, que
> invoca a Oryx; `az webapp deploy --type zip` usa OneDeploy, que en este stack se limita a
> copiar los archivos con rsync.
>
> El fallo es desagradable porque **el despliegue dice que funcionó**: el build "termina" en
> 3 segundos sin instalar nada, y 10 minutos después el contenedor muere con
> `ModuleNotFoundError: No module named 'uvicorn'` porque no existe el virtualenv `antenv`.
> Verificado en el primer intento real de despliegue.

El primer despliegue tarda varios minutos porque Oryx instala las dependencias. Si termina en
pocos segundos, Oryx no corrió y el arranque va a fallar.

---

## 5. Verificar

En este orden. Cada paso falla por un motivo distinto, así que el primero que falle te dice dónde
está el problema.

```bash
# 1. ¿Arrancó la app? (no toca Foundry: si falla, es despliegue o comando de arranque)
curl -s https://$APP.azurewebsites.net/api/salud

# 2. ¿Tiene permisos sobre Foundry? (si falla, es el rol de la sección 2)
SID=$(curl -s -X POST https://$APP.azurewebsites.net/api/sesiones | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST https://$APP.azurewebsites.net/api/sesiones/$SID/mensaje \
     -F "texto=Hazme el analisis de Compania Minera Andes Dorado SA, RUC 20498765432, sector mineria"

# 3. ¿Avanza el flujo? (deja el stream abierto ~3 min y mira pasar los eventos)
curl -N https://$APP.azurewebsites.net/api/sesiones/$SID/eventos
```

Si no arranca, el log del contenedor dice exactamente qué pasó:

```bash
az webapp log download -g $RG -n $APP --log-file logs.zip
unzip -o logs.zip -d logs && cat logs/LogFiles/StartupLogs/*_failure.log
```

| Síntoma en el log | Causa | Solución |
|---|---|---|
| `Could not find virtual environment directory /home/site/wwwroot/antenv` + `No module named 'uvicorn'` | Oryx no instaló las dependencias | Desplegar con `config-zip` y `ENABLE_ORYX_BUILD=true` |
| `Build failed` tras varios minutos, y el log de Oryx se corta sin mensaje de error | Alguna restricción de `requirements.txt` no se puede satisfacer: pip falla y el log no siempre lo refleja | Reproducirlo en local (comando de abajo) antes de volver a desplegar |
| `AuthorizationFailed` al analizar | Falta el rol `Foundry User` | Sección 2 |
| La app responde pero los PDFs pierden las tablas | Falta `Cognitive Services User` sobre Document Intelligence | Sección 2 |

### Comprobar `requirements.txt` sin desplegar

Un build fallido cuesta 10 minutos y el log de Oryx no siempre dice por qué. Esto resuelve las
mismas dependencias en local, contra wheels de Linux, en un directorio temporal:

```bash
pip install --dry-run --target /tmp/comprobacion \
  --platform manylinux2014_x86_64 --python-version 3.11 \
  --implementation cp --only-binary=:all: \
  -r orchestrator/requirements.txt
```

Si aquí falla, en el servidor también. Así se detectó que `agent-framework-foundry>=1.19.0` no
existía —la última publicada es la 1.13.1— y que la versión 1.19.0 que se veía instalada era en
realidad la de `agent-framework-core`, otro paquete.

La respuesta sana de `/api/salud`:

```json
{"estado":"ok","endpoint":"https://aaifs1rmiad02.services.ai.azure.com/api/projects/prj_aaifs1rmiad02",
 "agentes":{"financiero":"financiero","sectorial":"sectorial",
            "reportes_previos":"reportes-previos","generador":"generador-reporte"},
 "sesiones_vivas":0}
```

Y la interfaz del analista queda en `https://awaws1rmiad02.azurewebsites.net/`.

---

## 6. Cuota del modelo: el problema que te vas a encontrar

**Medido en la primera corrida real del flujo**, no es teoría: el agente sectorial falló con

```
429 rate_limit_exceeded — Your requests to gpt-4.1 for gpt-4.1 in westus
have exceeded token rate limit.
```

La causa es el propio fan-out. Los tres especialistas corren a la vez sobre el **mismo despliegue
de gpt-4.1**, y el sectorial maneja contextos grandes (su salida ronda los 18.000 caracteres). En
secuencia cada uno cabría de sobra; simultáneos, el pico de tokens por minuto se sale de la cuota.

Es el coste escondido de la orquestación simultánea: cambias latencia por consumo instantáneo.

Mitigaciones, en orden de preferencia:

1. **Subir el TPM del despliegue `gpt-4.1`** (Foundry > Modelos > `gpt-4.1` > Editar cuota). Es lo
   correcto: el flujo necesita ese pico por diseño.
2. **Reintentos con espera creciente** — ya implementado en
   [`orchestrator/orquestacion.py`](orchestrator/orquestacion.py) (`invocar_con_reintentos`):
   3 intentos, 8 s y 16 s de espera, y **solo** ante 429. Tras añadirlo, la misma corrida pasó de
   2/3 a 3/3 secciones. Resuelve los picos puntuales, no una cuota estructuralmente baja.
3. **Degradación**: si aun así un especialista cae, el informe se genera con los demás y declara
   explícitamente cuál faltó. Nunca se pierden los 85 s de los que sí funcionaron.

---

## 7. Operación

```bash
# Logs en vivo (es donde se ve el avance de cada análisis)
az webapp log tail -g $RG -n $APP

# Habilitar el guardado de logs la primera vez
az webapp log config -g $RG -n $APP --application-logging filesystem --level information

# Reiniciar (ojo: se pierden las sesiones en memoria que estén a medias)
az webapp restart -g $RG -n $APP

# Consola para depurar dentro del contenedor
az webapp ssh -g $RG -n $APP
```

### Application Insights

Sigue siendo el recurso que más urge (README §5). La Function App ya tiene uno
(`afaws1rmiad02`); conectar el App Service al mismo recurso es un solo comando y te da trazas
correlacionadas de todo el flujo:

```bash
CLAVE=$(az monitor app-insights component show -g $RG -a afaws1rmiad02 --query connectionString -o tsv)
az webapp config appsettings set -g $RG -n $APP \
  --settings APPLICATIONINSIGHTS_CONNECTION_STRING="$CLAVE" \
             ApplicationInsightsAgent_EXTENSION_VERSION="~3"
```

---

## 8. Configuración: dónde vive cada valor

El orquestador se configura **solo por variables de entorno**. No hay ningún valor de
infraestructura escrito en el código: `config.py` tiene valores por defecto y
[`orchestrator/.env.example`](orchestrator/.env.example) los documenta todos con su valor actual,
de modo que nunca hace falta leer código para saber qué se puede cambiar.

| Entorno | Mecanismo | ¿Hace falta `.env`? |
|---|---|---|
| App Service | App settings (los pone `deploy_webapp.py --bootstrap`) | **No.** El zip excluye `.env` a propósito |
| VM o local | `orchestrator/.env` | **Sí.** Es el único sitio donde se configura |

**Precedencia, verificada:** las variables que ya estén en el entorno **ganan** sobre el archivo
`.env` (`load_dotenv` no pisa lo que ya existe). Por eso el mismo código sirve en los dos sitios
sin condicionales: en App Service mandan los app settings, y en la VM, al no haberlos, manda el
archivo.

### En la VM

```bash
cp orchestrator/.env.example orchestrator/.env
# edita orchestrator/.env y ajusta lo que cambie en ese entorno
```

`FOUNDRY_PROJECT_ENDPOINT` es la **única variable obligatoria**; el resto tiene valor por defecto.
Si ejecutas desde la raíz del repo, el endpoint ya se hereda del `.env` de la raíz y puedes
dejarlo comentado. Si te llevas **solo la carpeta `orchestrator/`**, tiene que estar en su `.env`
(probado: la carpeta aislada arranca sin el `.env` de la raíz).

`.env` está en `.gitignore`, así que a la VM se copia a mano. Es la misma convención que los
`.env` de cada agente, ya documentada en [`roles.md`](roles.md).

Para **ejecutarlo** en la VM en vez de en App Service:

```bash
pip install -r orchestrator/requirements.txt
cd orchestrator
gunicorn -w 1 -k uvicorn.workers.UvicornWorker app:app --bind 0.0.0.0:8000 --timeout 1800
```

Mismo `-w 1` y mismo motivo que en App Service: el estado de las sesiones vive en memoria del
proceso. Y la identidad de la VM necesita el rol `Foundry User`, igual que la del App Service
(ver [`roles.md`](roles.md)).

---

## 9. Si despliegas desde la VM

Lo que la VM necesita para **desplegar** (no para ejecutar: el código corre en App Service):

| Requisito | Detalle |
|---|---|
| Azure CLI | `az login` con una cuenta que tenga `Contributor` sobre el grupo de recursos |
| `zip` | `sudo apt install zip` |
| El repo | Solo hace falta la carpeta `orchestrator/` |
| Python | **No** hace falta en la VM: Oryx instala las dependencias en el servidor |
| Red | Salida HTTPS (443) hacia `management.azure.com` y `*.scm.azurewebsites.net` |

Si además quieres **ejecutar o publicar agentes** desde la VM, eso es otra cosa y ya está
documentado en [`roles.md`](roles.md) §"Migrar el código a una máquina virtual": ahí la identidad
que necesita los roles es la de la VM, no la del App Service.

---

## 10. Limitaciones conocidas

| Limitación | Consecuencia | Cuándo abordarla |
|---|---|---|
| Estado de sesión en memoria del proceso | Un reinicio corta los análisis en curso. El plan B1 es de una sola instancia, así que no hay incoherencia entre workers mientras se mantenga `-w 1`. | Al escalar a más de una instancia: mover el estado a Cosmos o Redis |
| Sin autenticación | Cualquiera con la URL entra | Antes de exponerlo a usuarios reales: `az webapp auth update` con Entra ID |
| Las sesiones no caducan | Un proceso muy longevo acumula sesiones en memoria | Cuando el uso lo justifique: caducidad por antigüedad en `Almacen` |
| El informe sale en Markdown | La exportación a Word/PPT del diagrama no está implementada | Siguiente iteración (ver README §roadmap) |
| Los gráficos del sectorial viven en el sandbox de Foundry | Expiran con la sesión del agente y no quedan embebidos en el informe | Al implementar la exportación: descargarlos con `AgentFileStore` (README §6) |

# Migración a la VM: Function App y agente financiero

Guía para dejar funcionando, en un entorno nuevo, la herramienta `obtener_estados_financieros` y el
agente `financiero` que la usa.

Está escrita para el caso concreto de la migración: **la Function App ya existe** (te la
aprovisionaron), así que no hay que crearla — hay que apuntar a ella, configurarla, desplegar el
código y reconectar el agente.

Al final hay un [anexo](#anexo-qué-se-hizo-en-el-entorno-actual) con lo que se ejecutó en el entorno
actual, por si necesitas comparar.

---

## 0. Antes de empezar: qué es cada pieza

Conviene tener claro el flujo, porque determina qué permiso va dónde:

```
Analista ──> Agente `financiero` (Foundry) ──> Function App ──> Blob Storage
                                   │                 │               │
                      tool `openapi`,        identidad          Excel por RUC
                      key en conexión        administrada
```

**Por qué existe la Function.** El *function calling* de Foundry es client-side: una tool de tipo
`function` solo se ejecuta si la corre tu propia app, así que en el playground del portal se queda
sin resultado. Expuesta como tool `openapi`, Foundry la llama él mismo y el agente funciona en
cualquier contexto.

Dos consecuencias que importan para la migración:

1. Quien lee los Excel es la **identidad administrada de la Function App**, no la tuya ni la del
   proyecto Foundry.
2. La function key no está en el código: vive en una **conexión del proyecto Foundry** y el agente
   la referencia por nombre.

---

## 1. Datos que necesitas reunir

Rellena esto antes de ejecutar nada. Todos los comandos de esta guía usan estas variables.

```bash
# --- Function App que te aprovisionaron ---
export FUNC_RG="<resource group de la Function>"
export FUNC_NAME="<nombre de la Function App>"

# --- Storage con los estados financieros ---
export ST_RG="<resource group del storage>"
export ST_NAME="<nombre de la cuenta de storage>"
export ST_CONTAINER="financiero"          # contenedor con los <RUC>.xlsx

# --- Foundry ---
export FOUNDRY_RG="<resource group de Foundry>"
export FOUNDRY_ACCOUNT="<nombre de la cuenta Foundry>"
export FOUNDRY_PROJECT="<nombre del proyecto>"

# --- VM desde la que se ejecuta/publica ---
export VM_RG="<resource group de la VM>"
export VM_NAME="<nombre de la VM>"

export SUB="<id de suscripción>"
az account set --subscription "$SUB"
```

---

## 2. Verificar la Function App que te dieron

**Hazlo primero.** Si no cumple, el resto falla y es más caro descubrirlo tarde.

```bash
az rest --method get \
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$FUNC_RG/providers/Microsoft.Web/sites/$FUNC_NAME?api-version=2023-12-01" \
  -o json | python3 -c "
import sys,json; d=json.load(sys.stdin); p=d['properties']
print('kind      :', d.get('kind'))
print('runtime   :', (p.get('functionAppConfig') or {}).get('runtime') or p.get('linuxFxVersion'))
print('estado    :', p.get('state'))
print('host      :', p.get('defaultHostName'))
print('httpsOnly :', p.get('httpsOnly'))
print('identidad :', (d.get('identity') or {}).get('principalId'))
"
```

Lo que debe salir:

| Campo | Valor requerido | Si no cumple |
| --- | --- | --- |
| `kind` | debe contener **`linux`** | **Bloqueante.** En Windows el runtime de Python no está soportado: hay que recrearla como Linux |
| `runtime` | **python 3.11** | Ajustable (ver abajo). Otra 3.x probablemente funcione, pero 3.11 es la probada |
| `estado` | `Running` | Arráncala |
| `identidad` | un GUID | Si es `None`, actívala en el paso 3 |
| `httpsOnly` | `true` | Actívalo: `az functionapp update -g $FUNC_RG -n $FUNC_NAME --set httpsOnly=true` |

Si el runtime no es 3.11:

```bash
az functionapp config set -g $FUNC_RG -n $FUNC_NAME --linux-fx-version "Python|3.11"
```

> El **tipo de plan** (Flex Consumption, Consumption, Premium o Dedicated) **no afecta** a esta
> guía: el despliegue y la configuración son iguales. Solo cambia el arranque en frío y el límite
> de ejecución, irrelevantes aquí porque la herramienta responde en segundos.

---

## 3. Identidad y permisos

Tres identidades distintas, cada una con su rol. Es la parte que más fallos causa.

### 3.1 Identidad de la Function App → leer los Excel

```bash
# Actívala si el paso 2 devolvió identidad None
az functionapp identity assign -g $FUNC_RG -n $FUNC_NAME

FUNC_PRINCIPAL=$(az functionapp identity show -g $FUNC_RG -n $FUNC_NAME --query principalId -o tsv)
echo "principal de la Function: $FUNC_PRINCIPAL"

az role assignment create \
  --assignee-object-id "$FUNC_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" \
  --scope "/subscriptions/$SUB/resourceGroups/$ST_RG/providers/Microsoft.Storage/storageAccounts/$ST_NAME"
```

`Reader` basta: la herramienta solo lista y descarga.

### 3.2 Identidad de la VM → publicar y ejecutar agentes

Sin esto, el código fallará en la VM aunque funcione en tu máquina. **No es un bug del código: es
que tu usuario probablemente es Owner de la suscripción y la VM no hereda nada de eso.**

```bash
az vm identity assign -g $VM_RG -n $VM_NAME
VM_PRINCIPAL=$(az vm identity show -g $VM_RG -n $VM_NAME --query principalId -o tsv)

# Publicar y ejecutar agentes + leer las conexiones del proyecto
az role assignment create \
  --assignee-object-id "$VM_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "Foundry User" \
  --scope "/subscriptions/$SUB/resourceGroups/$FOUNDRY_RG/providers/Microsoft.CognitiveServices/accounts/$FOUNDRY_ACCOUNT"

# Solo si la VM va a ejecutar `main.py -m` (prueba local, tool client-side)
az role assignment create \
  --assignee-object-id "$VM_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" \
  --scope "/subscriptions/$SUB/resourceGroups/$ST_RG/providers/Microsoft.Storage/storageAccounts/$ST_NAME"
```

`Foundry User` incluye `dataActions: Microsoft.CognitiveServices/*` y
`connections/listsecrets/action`, que es justo lo que usan `project.connections.list(...)` y
`project.agents.create_version(...)`. No hace falta `Foundry Project Manager` salvo que la VM deba
crear proyectos o asignar roles.

> **Si la VM usa identidad asignada por el usuario** (en vez de system-assigned), exporta también
> `AZURE_CLIENT_ID=<clientId de esa identidad>`. Con varias identidades, `DefaultAzureCredential` no
> sabe cuál usar y falla.

### 3.3 Tu usuario → solo para desplegar

Para ejecutar los comandos de esta guía necesitas `Contributor` sobre los resource groups
implicados, o `Owner`. No hace falta nada en runtime.

---

## 4. Preparar la VM

```bash
python3 --version          # debe ser 3.11
az --version               # Azure CLI
func --version             # Core Tools v4 (4.10.0 o superior)
```

Si falta algo:

```bash
# Azure CLI
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash

# Azure Functions Core Tools v4 (Ubuntu)
curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > microsoft.gpg
sudo mv microsoft.gpg /etc/apt/trusted.gpg.d/microsoft.gpg
sudo sh -c 'echo "deb [arch=amd64] https://packages.microsoft.com/repos/microsoft-ubuntu-$(lsb_release -cs)-prod $(lsb_release -cs) main" > /etc/apt/sources.list.d/dotnetdev.list'
sudo apt-get update && sudo apt-get install azure-functions-core-tools-4
```

Luego el repo:

```bash
git clone <repo> && cd prompt-agent-bcp
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt       # el de la RAÍZ: publica agentes
```

> **Dos `requirements.txt`, a propósito.** El de la raíz instala el Agent Framework y el SDK de
> Foundry, necesarios para definir y publicar agentes. El de `function_app/` declara solo lo que la
> Function importa en ejecución, y lo usa el despliegue — **no lo instales en la VM**.

**La VM no necesita `az login`** si tiene identidad administrada: `DefaultAzureCredential` la detecta
sola. Si no la tiene, `az login` y asigna los roles del paso 3.2 a tu usuario.

### Salida de red requerida (HTTPS 443)

| Destino | Para qué |
| --- | --- |
| `login.microsoftonline.com` | obtener tokens |
| `*.services.ai.azure.com` | proyecto Foundry |
| `*.blob.core.windows.net` | leer los Excel |
| `*.azurewebsites.net` | llamar a la Function |
| `management.azure.com` | Azure CLI |

---

## 5. Configurar la Function App

```bash
ST_URL="https://$ST_NAME.blob.core.windows.net"

az functionapp config appsettings set -g $FUNC_RG -n $FUNC_NAME --settings \
  STORAGE_ACCOUNT_URL="$ST_URL" \
  BLOB_CONTAINER="$ST_CONTAINER"
```

| App setting | Valor | Para qué |
| --- | --- | --- |
| `STORAGE_ACCOUNT_URL` | `https://<cuenta>.blob.core.windows.net` | Dónde buscar los Excel |
| `BLOB_CONTAINER` | `financiero` | Contenedor dentro de esa cuenta |

Sin alguno de los dos, la herramienta devuelve `500` con
`"Function App mal configurada: Faltan los app settings..."`.

> Si la Function **no** tiene Application Insights conectado, añádelo. Sin él no hay forma de
> diagnosticar un fallo en producción:
> ```bash
> az monitor app-insights component create -g $FUNC_RG -a $FUNC_NAME -l <region> --kind web --application-type web
> CONN=$(az monitor app-insights component show -g $FUNC_RG -a $FUNC_NAME --query connectionString -o tsv)
> az functionapp config appsettings set -g $FUNC_RG -n $FUNC_NAME --settings APPLICATIONINSIGHTS_CONNECTION_STRING="$CONN"
> ```

---

## 6. Desplegar el código de la Function

```bash
cd function_app
func azure functionapp publish $FUNC_NAME --python
```

Debe terminar listando tres funciones:

```
estados_financieros - [httpTrigger]   /api/estados-financieros
health              - [httpTrigger]   /api/health
openapi             - [httpTrigger]   /api/openapi.json
```

> ⚠️ **`aiohttp` es obligatorio y no es evidente.** Los clientes async de Azure
> (`azure.identity.aio`, `azure.storage.blob.aio`) lo necesitan como transporte HTTP, pero **ninguno
> lo declara como dependencia**. Ya está en `function_app/requirements.txt`; si alguien lo quita
> "porque no se importa en ningún sitio", la Function devolverá `500` con cuerpo vacío y
> `ImportError: aiohttp package is not installed` en los logs. En local no se nota, porque el
> Agent Framework lo arrastra como transitiva.

### Verificar el despliegue

```bash
# Ojo: `az functionapp show --query defaultHostName` devuelve vacío en Flex Consumption
# (bug del CLI con los flags en preview). Por la API REST sí sale.
HOST=$(az rest --method get \
  --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$FUNC_RG/providers/Microsoft.Web/sites/$FUNC_NAME?api-version=2023-12-01" \
  --query properties.defaultHostName -o tsv)
KEY=$(az functionapp keys list -g $FUNC_RG -n $FUNC_NAME --query functionKeys.default -o tsv)
echo "host: $HOST"

curl -s https://$HOST/api/health                      # {"status": "ok"}
curl -s https://$HOST/api/openapi.json | head -c 200  # el spec
curl -s -X POST https://$HOST/api/estados-financieros \
  -H "x-functions-key: $KEY" -H "Content-Type: application/json" \
  -d '{"ruc":"<UN RUC QUE EXISTA>"}' | head -c 300
```

La última debe devolver `{"ruc": "...", "archivo": "...", "hojas": [...]}`. Si devuelve
`{"error": ...}`, ve a [Diagnóstico](#10-diagnóstico).

> **La estructura de carpetas del contenedor da igual.** La búsqueda compara solo el nombre del
> archivo (`<RUC>.xlsx`), descartando la ruta. Funciona con `BC/20512437891/20512437891.xlsx`, con
> `<cliente>/<RUC>.xlsx` o con el archivo en la raíz.

---

## 7. Crear la conexión en Foundry

Guarda la function key en el proyecto para que el agente no la lleve en el código. **Una sola vez
por proyecto.**

```bash
az functionapp keys list -g $FUNC_RG -n $FUNC_NAME --query functionKeys.default -o tsv
```

En el portal de Foundry:

1. **Administrar → Detalles del proyecto → Recursos conectados → Agregar conexión**
2. Elegir **Claves personalizadas** (*Custom keys*)
3. Rellenar:

| Campo | Valor |
| --- | --- |
| Nombre de la clave | `x-functions-key` |
| Valor | la key del comando de arriba |
| ¿Es secreto? | **Sí** |
| Nombre de la conexión | `func-eeff` |

> ⚠️ **Tiene que ser "Claves personalizadas", no "Clave de API".** Azure Functions exige que el
> header se llame literalmente `x-functions-key`, y "Clave de API" no permite nombrarlo. Con la
> opción equivocada la Function responde `401`.

> **No pide endpoint, y es correcto.** La URL sale del spec (`servers[0].url`); la conexión solo
> aporta las credenciales. Que el destino aparezca como `_` en el portal es normal.

### Verificar la conexión

```bash
BASE="https://management.azure.com/subscriptions/$SUB/resourceGroups/$FOUNDRY_RG/providers/Microsoft.CognitiveServices/accounts/$FOUNDRY_ACCOUNT/projects/$FOUNDRY_PROJECT/connections/func-eeff"

az rest --method get --url "$BASE?api-version=2025-06-01" \
  --query "{category:properties.category, auth:properties.authType}" -o json
# -> {"category": "CustomKeys", "auth": "CustomKeys"}

# ¿la clave guardada es la correcta?
REAL=$(az functionapp keys list -g $FUNC_RG -n $FUNC_NAME --query functionKeys.default -o tsv)
GUARDADA=$(az rest --method post --url "$BASE/listsecrets?api-version=2025-06-01" \
  --query 'properties.credentials.keys."x-functions-key"' -o tsv)
[ "$REAL" = "$GUARDADA" ] && echo "COINCIDE" || echo "NO COINCIDE: recrea la conexión"
```

> Si **rotas** la function key, hay que actualizar esta conexión. El agente no se republica: sigue
> apuntando al mismo nombre.

---

## 8. Configurar el agente

`agents/financiero/.env` (está en `.gitignore`: **hay que crearlo a mano en la VM**):

```bash
MODEL=gpt-4.1
BLOB_CONTAINER=financiero

# Function App que expone la herramienta
FUNCTION_BASE_URL=https://<host de tu function>/api
FUNCTION_CONNECTION_NAME=func-eeff
```

Y en la raíz, `.env`:

```bash
FOUNDRY_PROJECT_ENDPOINT=https://<cuenta>.services.ai.azure.com/api/projects/<proyecto>
```

| Variable | Dónde | Para qué |
| --- | --- | --- |
| `FOUNDRY_PROJECT_ENDPOINT` | `.env` raíz | Proyecto contra el que se publica |
| `MODEL` | `.env` del agente | Modelo del prompt agent |
| `BLOB_CONTAINER` | `.env` del agente | Solo para `-m` (prueba local client-side) |
| `FUNCTION_BASE_URL` | `.env` del agente | URL base de la API, **con `/api` y sin barra final** |
| `FUNCTION_CONNECTION_NAME` | `.env` del agente | Nombre de la conexión del paso 7 |

---

## 9. Publicar el agente

```bash
cd agents/financiero
python main.py --publish
```

Salida esperada:

```
Publicado financiero v<N>
  herramienta: obtener_estados_financieros (openapi, server-side)
  contra     : https://<host>/api
```

`main.py` comprueba que la conexión exista **antes** de publicar; si no, aborta con instrucciones en
lugar de dejar un agente roto.

### Verificar lo que quedó publicado

```bash
cd ../.. && python - <<'EOF'
import os
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
load_dotenv(".env")
with DefaultAzureCredential() as cred:
    c = AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=cred)
    for a in c.agents.list():
        d = a.as_dict()
        if d["name"] != "financiero": continue
        latest = d["versions"]["latest"]
        print("version:", latest["version"])
        for t in latest["definition"].get("tools", []):
            if t.get("type") == "openapi":
                o = t["openapi"]
                print("  openapi:", o["name"], "->", o["spec"]["servers"])
                print("  auth   :", o["auth"]["type"], o["auth"].get("security_scheme"))
            else:
                print("  tool   :", t.get("type"))
EOF
```

Debe mostrar `code_interpreter` y `openapi`, **sin** ninguna tool de tipo `function`.

### Prueba final en el playground

Foundry → Agentes → `financiero` → Área de juegos:

> `puedes hacer un analisis financiero de la empresa con ruc <RUC>`

Debe ejecutar la herramienta y devolver el análisis. **Si aparece un cuadro pidiendo "Escriba la
salida de la función como JSON", la tool quedó registrada como `function` y no como `openapi`**:
revisa que publicaste con el `main.py` nuevo.

---

## 10. Diagnóstico

### Cómo leer los logs de la Function

```bash
APPID=$(az monitor app-insights component show -g $FUNC_RG -a $FUNC_NAME --query appId -o tsv)
az monitor app-insights query --app "$APPID" --analytics-query \
  "exceptions | where timestamp > ago(30m) | project timestamp, outerMessage, innermostMessage | order by timestamp desc | take 5" \
  -o json
```

### Tabla de síntomas

| Síntoma | Causa | Solución |
| --- | --- | --- |
| `500` con **cuerpo vacío** | Excepción no capturada; casi siempre falta `aiohttp` | Verifica `function_app/requirements.txt` y redespliega |
| `ImportError: aiohttp package is not installed` | La dependencia se perdió | Igual que arriba |
| `401` al llamar la herramienta | Key equivocada, o header mal nombrado | El header debe ser exactamente `x-functions-key`. Conexión tipo *Claves personalizadas*, no *Clave de API* |
| `{"error": "Function App mal configurada..."}` | Faltan app settings | Paso 5 |
| `{"error": "No se encontró el archivo <RUC>.xlsx"}` | El RUC no existe, o la Function no ve el blob | Comprueba que el archivo exista y que el rol del paso 3.1 esté asignado |
| `{"error": "El RUC debe ser numérico"}` | El valor trae letras o está vacío | No es un fallo: es validación. La longitud no se valida (los datos de prueba usan 9 dígitos) |
| `AuthorizationFailed` al publicar desde la VM | Falta `Foundry User` | Paso 3.2 |
| `No existe la conexión 'func-eeff'` | La conexión no está, o el nombre no coincide | Paso 7, y revisa `FUNCTION_CONNECTION_NAME` |
| `Failed to fetch knowledge bases` | Roles del proyecto Foundry | Ver `roles.md` |
| La tool queda esperando JSON en el playground | El agente tiene la tool como `function` | Republica con el `main.py` nuevo |
| `ManagedIdentityCredential authentication unavailable` | La VM no tiene identidad, o hay varias | Paso 3.2; con user-assigned, exporta `AZURE_CLIENT_ID` |
| `az functionapp show` devuelve campos vacíos | Bug del CLI con Flex Consumption (flags en preview) | Consulta por `az rest` sobre `management.azure.com`, como en el paso 6 |

### Prueba de aislamiento

Si algo falla, ejecuta en este orden. El primero que falle señala la capa:

```bash
curl -s https://$HOST/api/health                                  # 1. red y Function viva
curl -s -X POST https://$HOST/api/estados-financieros \
  -H "x-functions-key: $KEY" -d '{"ruc":"<RUC>"}'                 # 2. key + rol de Storage
python agents/sectorial/main.py -m "Preséntate en una frase."     # 3. Foundry User desde la VM
python agents/financiero/main.py --publish                        # 4. conexión + publicación
```

---

## 11. Resumen de la migración

| # | Paso | Dónde |
| --- | --- | --- |
| 1 | Verificar que la Function sea Linux + Python 3.11 | Azure CLI |
| 2 | Identidad de la Function + `Storage Blob Data Reader` | Azure CLI |
| 3 | Identidad de la VM + `Foundry User` | Azure CLI |
| 4 | Python 3.11, az, Core Tools v4, `pip install -r requirements.txt` | VM |
| 5 | App settings `STORAGE_ACCOUNT_URL` y `BLOB_CONTAINER` | Azure CLI |
| 6 | `func azure functionapp publish` | VM, carpeta `function_app/` |
| 7 | Conexión `func-eeff` (Claves personalizadas) | Portal Foundry |
| 8 | Crear los dos `.env` | VM |
| 9 | `python main.py --publish` | VM |
| 10 | Probar en el playground | Portal Foundry |

### Las tres cosas que más fallan

1. **`aiohttp`** — la Function devuelve `500` vacío y el error solo se ve en Application Insights.
2. **La conexión creada como "Clave de API"** en vez de "Claves personalizadas" — da `401`.
3. **Los roles de la VM** — el código funciona en tu máquina porque eres Owner; la VM no hereda eso.

---

## Anexo: qué se hizo en el entorno actual

Referencia de lo ejecutado en `RSGRSC1RMIAD02`. **En la migración no repites el paso 1**: la Function
ya existe.

```bash
# 1. Crear la Function App (Flex Consumption; creó su plan FC1 y Application Insights)
az functionapp create -g RSGRSC1RMIAD02 -n afaws1rmiad02 \
  --flexconsumption-location westus --runtime python --runtime-version 3.11 \
  --storage-account stacs1miabackd02

# 2. Identidad administrada
az functionapp identity assign -g RSGRSC1RMIAD02 -n afaws1rmiad02

# 3. Permiso de lectura sobre los Excel
az role assignment create \
  --assignee-object-id 52bd096a-7e93-44d5-b3b7-27863973e346 \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" \
  --scope ".../storageAccounts/stacs1miabackd02"

# 4. Configuración
az functionapp config appsettings set -g RSGRSC1RMIAD02 -n afaws1rmiad02 --settings \
  STORAGE_ACCOUNT_URL=https://stacs1miabackd02.blob.core.windows.net BLOB_CONTAINER=financiero

# 5. HTTPS obligatorio (venía desactivado)
az functionapp update -g RSGRSC1RMIAD02 -n afaws1rmiad02 --set httpsOnly=true

# 6. Código
cd function_app && func azure functionapp publish afaws1rmiad02 --python

# 7. Conexión `func-eeff` en el portal (Claves personalizadas)

# 8. Agente
cd agents/financiero && python main.py --publish     # -> financiero v7
```

### Entorno de referencia

| Pieza | Valor |
| --- | --- |
| Function App | `afaws1rmiad02` (`functionapp,linux`, Python 3.11, Flex Consumption) |
| Plan | `ASP-RSGRSC1RMIAD02-5e47` (FC1) — convive con el B1 sin conflicto |
| Application Insights | `afaws1rmiad02` |
| Storage | `stacs1miabackd02`, contenedor `financiero` |
| Conexión Foundry | `func-eeff` (CustomKeys, header `x-functions-key`) |
| Agente | `financiero` v7: `code_interpreter` + `openapi` |
| Core Tools | 4.10.0 · Python 3.11.11 |

### Dato sobre planes

El plan **Consumption clásico (Y1)** no puede convivir con un plan Linux Dedicated o Premium en el
mismo resource group. **Flex Consumption (FC1) sí**: se desplegó junto al plan B1 existente sin
conflicto. Si en el entorno nuevo hay que crear la Function y el RG ya tiene un plan Dedicated,
usa Flex Consumption.

### Latencias medidas

| Operación | Tiempo |
| --- | --- |
| Herramienta (arranque en frío) | ~4,6 s |
| Herramienta (caliente) | ~1,2–1,8 s |
| Agente financiero completo | ~77 s |

El coste de la Function es ~2% del tiempo del agente: despreciable.

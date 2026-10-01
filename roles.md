# Roles asignados (RBAC) — Conexiones de Foundry

Roles que faltaban y que causaban el error "Failed to fetch knowledge bases for connection" al usar las conexiones del proyecto (`azcssc1rmiad02t923k1` y `stacs1miabackd02t923k1`), ambas con autenticación **Identidad administrada del proyecto**.

| Rol | Para quién (asignado a) | Desde dónde (IAM del recurso) |
| --- | --- | --- |
| Search Service Contributor | Identidad administrada del proyecto `prj_aaifs1rmiad02` | IAM de Azure AI Search `azcssc1rmiad02` |
| Search Index Data Contributor | Identidad administrada del proyecto `prj_aaifs1rmiad02` | IAM de Azure AI Search `azcssc1rmiad02` |
| Storage Blob Data Contributor | Identidad administrada del proyecto `prj_aaifs1rmiad02` | IAM de la cuenta de Storage `stacs1miabackd02` |
| Storage Blob Data Contributor | Identidad administrada del propio servicio Azure AI Search `azcssc1rmiad02` | IAM de la cuenta de Storage `stacs1miabackd02` |

Sin estos roles, la identidad del proyecto podía conectarse pero no tenía permiso para listar/gestionar índices (Search) ni leer blobs (Storage), por eso Foundry IQ no podía obtener las bases de conocimiento. La última fila es un rol distinto: es la identidad del **servicio de Search** (no la del proyecto) la que necesita leer blobs para poder indexarlos al crear una fuente de conocimiento de tipo Azure Blob Storage. Este último también quedó confirmado desde el propio asistente "Conceder acceso" del portal de Foundry al crear la fuente de conocimiento (mismo rol, mismo resultado que por CLI).

## Rol para la herramienta `obtener_estados_financieros` (agente financiero)

| Rol | Para quién (asignado a) | Desde dónde (IAM del recurso) |
| --- | --- | --- |
| Storage Blob Data Contributor | Usuario `jhonatan.rodriguez@gestionysistemas.com` (`3dd36cbc-510c-4bc7-88a2-a2f3fc1b8480`) | IAM de la cuenta de Storage `stacs1miabackd02` |

La herramienta `obtener_estados_financieros` del agente financiero lista y descarga blobs del
contenedor `financiero` usando `DefaultAzureCredential`, es decir, **la identidad de quien ejecuta
el agente** — no la identidad administrada del proyecto. Por eso el rol va sobre el usuario. Al mover
esto a un orquestador desplegado en Azure, hay que asignar el mismo rol (o `Storage Blob Data
Reader`, que basta porque solo se lee) a la identidad administrada de ese servicio.

Comando usado:

```bash
az role assignment create \
  --assignee-object-id 3dd36cbc-510c-4bc7-88a2-a2f3fc1b8480 \
  --assignee-principal-type User \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/256902ad-0a22-4f6a-a834-090e2ea0ccf5/resourceGroups/RSGRSC1RMIAD02/providers/Microsoft.Storage/storageAccounts/stacs1miabackd02"
```

## Rol para la Function App `afaws1rmiad02`

| Rol | Para quién (asignado a) | Desde dónde (IAM del recurso) |
| --- | --- | --- |
| Storage Blob Data Reader | Identidad administrada (system-assigned) de la Function App `afaws1rmiad02` (`52bd096a-7e93-44d5-b3b7-27863973e346`) | IAM de la cuenta de Storage `stacs1miabackd02` |

La Function App expone `obtener_estados_financieros` por HTTP para que Foundry la ejecute
server-side. Al correr la herramienta usa **su propia identidad administrada**, no la del
proyecto Foundry ni la del usuario. Basta `Reader` porque solo lista y descarga blobs.

Comando usado:

```bash
az role assignment create \
  --assignee-object-id 52bd096a-7e93-44d5-b3b7-27863973e346 \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" \
  --scope "/subscriptions/256902ad-0a22-4f6a-a834-090e2ea0ccf5/resourceGroups/RSGRSC1RMIAD02/providers/Microsoft.Storage/storageAccounts/stacs1miabackd02"
```

## Migrar el código a una máquina virtual

> **El punto crítico:** hoy el código funciona en local porque tu usuario es **Owner de toda la
> suscripción**, así que `DefaultAzureCredential` puede con todo. Una VM **no hereda nada de eso**.
> Si solo copias el código y lo ejecutas, fallará con `AuthorizationFailed` o
> `Failed to fetch connections` — no por un error del código, sino por falta de roles.

### 1. Darle identidad a la VM

```bash
az vm identity assign -g <RG-DE-LA-VM> -n <NOMBRE-VM>
```

Anota el `principalId` que devuelve: es a quien se le asignan los roles de abajo.

> Si usas **identidad asignada por el usuario** en vez de system-assigned, además hay que exportar
> `AZURE_CLIENT_ID=<clientId de esa identidad>` en la VM. Sin eso `DefaultAzureCredential` no sabe
> cuál usar y falla cuando hay más de una.

### 2. Roles que necesita esa identidad

| Rol | Alcance | Para qué | ¿Obligatorio? |
| --- | --- | --- | --- |
| `Foundry User` | cuenta Foundry `aaifs1rmiad02` | Publicar y ejecutar agentes, y resolver las conexiones del proyecto (knowledge base y storage) | **Sí** |
| `Storage Blob Data Reader` | cuenta de Storage `stacs1miabackd02` | Solo si la VM ejecuta la tool **client-side** (`agents/financiero/tools.py`). Si llama a la Function App, **no hace falta** | Depende |

```bash
PRINCIPAL=<principalId de la VM>
SUB=256902ad-0a22-4f6a-a834-090e2ea0ccf5

# Publicar y ejecutar agentes + leer las conexiones del proyecto
az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Foundry User" \
  --scope "/subscriptions/$SUB/resourceGroups/RSGRSC1RMIAD02/providers/Microsoft.CognitiveServices/accounts/aaifs1rmiad02"

# Solo si la VM lee los Excel directamente
az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" \
  --scope "/subscriptions/$SUB/resourceGroups/RSGRSC1RMIAD02/providers/Microsoft.Storage/storageAccounts/stacs1miabackd02"
```

`Foundry User` incluye `dataActions: Microsoft.CognitiveServices/*` y
`accounts/projects/connections/listsecrets/action`, que es justo lo que usan
`project.connections.list(...)` y `project.agents.create_version(...)`. Si además quieres que la VM
pueda asignar roles o crear proyectos, haría falta `Foundry Project Manager`, pero para ejecutar el
código **no** es necesario: aplica el mínimo privilegio.

### 3. Entorno de la VM

| Requisito | Detalle |
| --- | --- |
| Python | **3.11** (es el runtime con el que se probó y el de la Function App) |
| Dependencias | `pip install -r requirements.txt` (el de la raíz, no el de `function_app/`) |
| `.env` de la raíz | `FOUNDRY_PROJECT_ENDPOINT` |
| `.env` por agente | `MODEL` y `BLOB_CONTAINER` / `KNOWLEDGE_BASE_NAME` — están gitignored, hay que copiarlos a mano |
| Red | Salida HTTPS (443) hacia `*.cognitiveservices.azure.com`, `*.blob.core.windows.net` y `login.microsoftonline.com` |
| `az login` | **No hace falta** si la VM tiene identidad administrada: `DefaultAzureCredential` la detecta sola |

### 4. Comprobar que la migración quedó bien

Desde la VM, en este orden: si el primero falla es un problema de red o de identidad; si falla el
segundo, es el rol de Foundry; si falla el tercero, es el rol de Storage.

```bash
curl -s https://afaws1rmiad02.azurewebsites.net/api/health          # 1. red
python agents/sectorial/main.py -m "Preséntate en una frase."       # 2. Foundry User
python agents/financiero/main.py -m "Analiza el RUC 20498765432"    # 3. + Storage (client-side)
```

## Nota pendiente

El servicio de Azure AI Search (`azcssc1rmiad02`) tiene `authOptions: apiKeyOnly`, es decir, solo acepta autenticación por clave API. Aunque los roles estén bien asignados, mientras esto no cambie a `aadOrApiKey` ("Both"), las conexiones basadas en identidad administrada seguirán fallando. Pendiente de confirmación del usuario para aplicar el cambio.

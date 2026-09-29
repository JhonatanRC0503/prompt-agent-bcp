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
contenedor `financiero` usando `AzureCliCredential`, es decir, **la identidad de quien ejecuta el
agente** — no la identidad administrada del proyecto. Por eso el rol va sobre el usuario. Al mover
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

## Nota pendiente

El servicio de Azure AI Search (`azcssc1rmiad02`) tiene `authOptions: apiKeyOnly`, es decir, solo acepta autenticación por clave API. Aunque los roles estén bien asignados, mientras esto no cambie a `aadOrApiKey` ("Both"), las conexiones basadas en identidad administrada seguirán fallando. Pendiente de confirmación del usuario para aplicar el cambio.

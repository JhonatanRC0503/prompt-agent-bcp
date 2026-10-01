# prompt-agent-bcp

Sistema multi-agente para análisis de riesgo crediticio de banca corporativa, construido sobre
**Microsoft Foundry** (prompt agents) y **Microsoft Agent Framework** (orquestación en código).

> Para **desplegar esto en otro entorno** (por ejemplo, la VM), sigue [`function.md`](function.md):
> es el runbook paso a paso con permisos, comandos y diagnóstico.

---

## 1. Estado actual

### Agentes operativos

| Agente | Modelo | Herramientas | Latencia medida |
|---|---|---|---|
| `financiero` | gpt-4.1 | `obtener_estados_financieros` (custom) + code interpreter | **77 s** |
| `sectorial` | gpt-4.1 | web search + code interpreter + MCP (knowledge base) | **85 s** |

Ambos son **prompt agents** publicados en Foundry, definidos desde código con `to_prompt_agent`.

### Estructura del repo

```
agents/
  financiero/
    main.py           # define y publica el agente
    tools.py          # adaptador: reexporta la tool desde function_app/eeff.py
    instructions.md   # el prompt (~12 KB)
    .env              # MODEL, BLOB_CONTAINER          (gitignored)
  sectorial/
    main.py
    instructions.md
    .env              # MODEL, KNOWLEDGE_BASE_NAME     (gitignored)
  reportes_previos/
    main.py
    instructions.md
    .env              # MODEL, KNOWLEDGE_BASE_NAME     (gitignored)
function_app/         # Function App desplegable (Flex Consumption)
  function_app.py     # HTTP triggers
  openapi_spec.py     # spec OpenAPI (lo usan la API y main.py al publicar)
  eeff.py             # tool: Blob Storage -> JSON (implementación única)
  host.json
  requirements.txt    # solo dependencias de runtime, ver nota abajo
scripts/
  pull_agents.py      # re-sincroniza instrucciones desde el portal
infra/
  main.bicep          # recursos base
roles.md              # RBAC asignado
function.md           # runbook: migrar la Function y el agente a otro entorno
requirements.txt      # dependencias para definir/publicar agentes
.env                  # FOUNDRY_PROJECT_ENDPOINT       (gitignored)
```

**Dos `requirements.txt`, a propósito.** El de la raíz es para desarrollo: define y publica los
prompt agents, así que necesita el Agent Framework y el SDK de Foundry. El de `function_app/`
declara solo lo que la Function importa en ejecución. En Flex Consumption el paquete se monta
desde Blob en cada arranque en frío, así que incluir ~9 MB de dependencias que nunca se importan
se pagaría en latencia. La regla: un manifiesto por artefacto desplegable.

**El parser de Excel vive en un solo sitio** (`function_app/eeff.py`) porque lo comparten dos
consumidores: el agente, que lo registra como function tool (client-side), y la Function, que lo
expone por HTTP (server-side). `agents/financiero/tools.py` es un adaptador de ~15 líneas.

**Comandos:**

```bash
python agents/<nombre>/main.py --publish        # publica nueva versión en Foundry
python agents/<nombre>/main.py -m "mensaje"     # prueba local
python scripts/pull_agents.py                   # trae cambios hechos en el portal
```

### Infraestructura desplegada (`RSGRSC1RMIAD02`)

| Recurso | Nombre | Estado |
|---|---|---|
| Foundry + proyecto | `aaifs1rmiad02` / `prj_aaifs1rmiad02` | en uso |
| Blob Storage | `stacs1miabackd02` | en uso (EEFF + PDFs) |
| AI Search | `azcssc1rmiad02` | en uso (knowledge base sectorial) |
| Document Intelligence | `aidieu2rmiad02` | **sin usar** |
| App Service Plan (B1 Linux) | `aspleu2rmiad02` | **vacío, sin apps** |
| Function App (Flex Consumption) | `afaws1rmiad02` | en uso (tool `obtener_estados_financieros`) |
| Application Insights | `afaws1rmiad02` | en uso (trazas de la Function) |
| Azure Bot | `azbseu2rmiad02` | **endpoint placeholder** |

---

## 2. Decisión clave: ¿App Service, Function, o ambos?

**Recomendación: una Azure Function App sobre el App Service Plan B1 que ya tienes. Ni App Service
aparte, ni ambos.**

### Por qué

El problema no es dónde corre el código, es **cuánto tarda**. Estos son los tiempos reales:

| Concepto | Tiempo |
|---|---|
| Agente financiero | 77 s |
| Agente sectorial | 85 s |
| Fan-out (ambos en paralelo) | ~85 s |
| + validador + generador de reporte | ~90 s |
| **Total estimado del flujo completo** | **~3 min** |

Contra estos límites:

| Límite | Valor | Origen |
|---|---|---|
| Respuesta al canal (Teams) | **~15 s** | Bot Framework |
| Respuesta HTTP de una Function | **230 s** | idle timeout del Azure Load Balancer — *no configurable* |
| Ejecución en plan Dedicated | **ilimitado** | requiere Always On |

Un flujo de 3 minutos **no cabe** en una respuesta HTTP síncrona. La arquitectura tiene que ser
asíncrona sí o sí, sin importar el servicio elegido.

### Comparación

| Opción | Veredicto |
|---|---|
| **App Service solo** | Funciona, pero tendrías que implementar a mano la cola y el worker en background. |
| **Function App** ✅ | El `Queue trigger` te da el worker gratis. Y sobre el plan B1 (Dedicated): sin cold start y timeout ilimitado. |
| **Ambos** | Innecesario. Duplicas despliegue y costo sin ganar nada. |

> ⚠️ **Matiz verificado en el despliegue.** La advertencia *"Linux Consumption apps aren't supported
> in the same resource group as Linux Dedicated or Linux Premium plans"* aplica al plan **Consumption
> clásico (Y1)**. **Flex Consumption (FC1) no tiene esa restricción**: se desplegó `afaws1rmiad02` en
> `RSGRSC1RMIAD02` conviviendo con el plan B1 sin conflicto, y creó su propio plan serverless
> (`ASP-RSGRSC1RMIAD02-5e47`, FC1). Para el orquestador largo sigue valiendo lo de arriba: si
> necesitas timeout ilimitado y Always On, va sobre el B1.

---

## 3. Arquitectura recomendada

```
Analista (Teams)
      │  mensaje + reporte de créditos + informe comercial
      ▼
Azure Bot Service  (solo enruta, no ejecuta código)
      │
      ▼
┌─────────────────── Function App (plan B1, Always On) ───────────────────┐
│                                                                         │
│  [HTTP trigger] /api/messages                                           │
│     ├─ responde "Analizando..." en < 15 s                               │
│     ├─ guarda los adjuntos en Blob Storage                              │
│     └─ encola el trabajo (Storage Queue)                                │
│                             │                                           │
│  [Queue trigger]  ◄─────────┘                                           │
│     └─ ORQUESTADOR (WorkflowBuilder, código Python)                     │
│           │                                                             │
│           ├─ Document Intelligence: extrae el RUC de los documentos     │
│           │                                                             │
│           ├──── fan-out (paralelo) ────┐                                │
│           │                            │                                │
│      agente financiero           agente sectorial                       │
│      (RUC -> Blob -> EEFF)       (docs + web + knowledge base)          │
│           │                            │                                │
│           └──── fan-in ────────────────┘                                │
│                       │                                                 │
│               agente validador de consistencia                          │
│                       │                                                 │
│               agente generador de reporte                               │
│                       │                                                 │
│           └─ mensaje proactivo a Teams con el informe                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### El orquestador NO debe ser un prompt agent

En tu diagrama el orquestador aparece como un agente más. **Recomiendo que sea código Python**, no
un prompt agent. Razones concretas:

1. **Recibe archivos binarios** (PDF/Excel). Un prompt agent recibe texto, no maneja la ingesta.
2. **El fan-out debe ser determinista.** Siempre corren financiero y sectorial. Si lo decide un LLM,
   a veces se salta uno y el informe sale incompleto.
3. **Necesita checkpoints.** Si el paso 3 de 5 falla, quieres reanudar, no re-ejecutar 3 minutos.
4. **Costo y latencia.** Un LLM orquestando agrega llamadas al modelo que no aportan criterio.

Los **4 agentes especialistas sí son prompt agents** (ahí el juicio del LLM es el valor).
El orquestador es plumbing: código.

### Orquestación con `WorkflowBuilder`

`agent-framework` ya trae lo necesario (verificado en la versión instalada):

```python
from agent_framework import WorkflowBuilder

workflow = (
    WorkflowBuilder(start_executor=extractor_ruc, checkpoint_storage=storage)
    .add_fan_out_edges(extractor_ruc, [agente_financiero, agente_sectorial])
    .add_fan_in_edges([agente_financiero, agente_sectorial], agente_validador)
    .add_edge(agente_validador, agente_generador)
    .build()
)

resultado = await workflow.run(mensaje)
```

API disponible y confirmada:

| Necesidad | API |
|---|---|
| Ejecutar en paralelo | `add_fan_out_edges(source, targets)` |
| Consolidar resultados | `add_fan_in_edges(sources, target)` |
| Secuencia | `add_edge(a, b)` / `add_chain([...])` |
| Ramas condicionales | `add_switch_case_edge_group(...)` |
| Reanudar tras fallo | `workflow.run(checkpoint_id=..., checkpoint_storage=...)` |
| Diagrama del flujo | `WorkflowViz` |

---

## 4. Limitación crítica: function calling es client-side

**Esto condiciona todo el diseño.** Documentación oficial de Foundry:

> **Function Calling (client-side)** — *"Your app executes the function and returns results."*
> *"Not available via toolbox — function calling executes in the client process."*

Significa que `obtener_estados_financieros`:

| Dónde se ejecuta el agente | ¿La tool corre? |
|---|---|
| Tu código / el orquestador en la Function App | ✅ sí |
| Playground del portal o Foundry Toolkit | ❌ no — la tool se invoca y el `Output` queda vacío |

No es un bug ni una limitación del prompt agent: **es el diseño de function calling**. El playground
no es "tu app", así que nadie devuelve el resultado.

**Para el producto final no es problema** — el orquestador es tu app. Solo afecta las pruebas desde
el portal.

Si quisieras que también funcione en el playground, la tool tendría que dejar de ser `function` y
pasar a ser **`openapi`** o **`mcp`** (Foundry las ejecuta server-side).

### ✅ Resuelto: la tool ya está expuesta como `openapi`

`function_app/` despliega esa herramienta como API HTTP en la Function App `afaws1rmiad02`:

| Endpoint | Auth | Para qué |
|---|---|---|
| `POST /api/estados-financieros` | function key | la herramienta |
| `GET /api/openapi.json` | anónimo | el spec a registrar en Foundry |
| `GET /api/health` | anónimo | sonda de estado |

Probado end-to-end: el RUC `20498765432` devuelve sus 3 hojas (Estado de Resultados, Balance
General, Ratios Financieros) con 4 periodos, en ~4,6 s.

**Desplegar:**

```bash
cd function_app && func azure functionapp publish afaws1rmiad02
```

**La tool ya está declarada en el código** (`agents/financiero/main.py`), así que sobrevive a los
`--publish`. Publicada en `financiero` **v7**: `code_interpreter` + `openapi`, sin function tool.

```bash
python agents/financiero/main.py --publish
```

| Modo | Herramienta | Quién la ejecuta |
|---|---|---|
| `--publish` (producto) | tool `openapi` | Foundry, server-side, contra la Function App |
| `-m` (prueba local) | function tool | el proceso local, client-side |

Son el mismo código (`function_app/eeff.py`): solo cambia quién la ejecuta. La versión publicada
lleva **únicamente** la `openapi`, que es la que hace funcionar el playground.

### Por qué hubo que construir la tool a mano

El Agent Framework **no** expone un `client.get_openapi_tool()` (sí `get_mcp_tool`,
`get_web_search_tool`...). Se construye con el SDK de proyectos y se añade a la definición en
`definicion_publicable()`:

```python
OpenApiTool(openapi=OpenApiFunctionDefinition(
    name=NOMBRE_HERRAMIENTA,
    spec=construir_spec(base_url),
    auth=OpenApiProjectConnectionAuthDetails(
        security_scheme=OpenApiProjectConnectionSecurityScheme(project_connection_id=conexion)),
))
```

### La conexión con la function key

La key **no está en el código**: vive en una conexión del proyecto de tipo *Claves personalizadas*,
y el agente la referencia por nombre (`FUNCTION_CONNECTION_NAME`). Crearla una sola vez:

1. Foundry > Administrar > Recursos conectados > **Agregar conexión > Claves personalizadas**
2. Clave `x-functions-key`, valor de
   `az functionapp keys list -g RSGRSC1RMIAD02 -n afaws1rmiad02 --query functionKeys.default -o tsv`,
   marcada como secreto
3. Nombre: `func-eeff`

> No pide endpoint, y es correcto: la URL sale del spec (`servers[0].url`); la conexión solo aporta
> las credenciales.

**Config nueva en `agents/financiero/.env`:**

```bash
FUNCTION_BASE_URL=https://afaws1rmiad02.azurewebsites.net/api
FUNCTION_CONNECTION_NAME=func-eeff
```

`main.py` comprueba que la conexión exista **antes** de publicar: si no, el fallo aparecería solo al
invocar la herramienta desde el playground.

---

## 5. Lo que te falta

### Recursos a crear

| Recurso | Para qué | Prioridad |
|---|---|---|
| **Function App** (Flex Consumption) | Tool server-side | ✅ **hecha** (`afaws1rmiad02`) |
| **Application Insights** | Trazas, depurar por qué un informe salió mal | ✅ **hecha** (`afaws1rmiad02`) |
| Hosting del orquestador (plan B1) | Flujo largo de ~3 min con Always On | **Alta** |
| **Storage Queue** (en `stacs1miabackd02`) | Desacoplar el trabajo largo | **Alta** |
| Contenedor `reportes` en Blob | Guardar informes generados y adjuntos entrantes | Media |
| Cosmos DB | Estado de conversaciones y `ConversationReference` | Media |

> **Application Insights es el que más urge.** Hoy no tienes ninguna visibilidad: cuando un informe
> salga mal en producción, no vas a poder saber si falló el RUC, la tool, el validador o el modelo.

### Recurso que ya tienes y no usas

**Document Intelligence (`aidieu2rmiad02`).** Es la pieza que resuelve *"de esos dos documentos puede
extraer el RUC"*. Hoy el RUC se lo pasas a mano. Con Document Intelligence el orquestador lo extrae
del reporte de créditos o del informe comercial automáticamente.

Nota: si los PDFs son escaneados, el code interpreter **no** puede leerlos (no hace OCR). Document
Intelligence sí.

### Cambios de código pendientes

| Cambio | Estado |
|---|---|
| `AzureCliCredential` → `DefaultAzureCredential` | ✅ **hecho** |
| Rol `Storage Blob Data Reader` para la identidad de la Function | ✅ **hecho** (ver `roles.md`) |
| Agentes validador y generador de reporte | pendiente |
| Extracción de RUC con Document Intelligence | pendiente |
| Mensaje proactivo a Teams | pendiente |

---

## 6. Detalles de implementación a tener en cuenta

### El reporte final con tablas e imágenes

El agente sectorial genera 6 gráficos con el code interpreter. Esos archivos viven en el sandbox de
Foundry y hay que **descargarlos** antes de que expire la sesión para incrustarlos en el Word/PDF
final. `agent-framework` expone `AgentFileStore` para esto. Planifícalo: es el detalle que suele
romper este tipo de flujos al final.

### Identidad y permisos

La tool usa la identidad de **quien ejecuta el agente**, no la del proyecto Foundry. En local es tu
`az login`; en la Function será su managed identity. Hay que asignarle `Storage Blob Data Reader`
sobre `stacs1miabackd02` **y documentarlo en `roles.md`** (convención del repo).

### Nunca hardcodear IDs de tenant

Ambos agentes resuelven sus dependencias en tiempo de ejecución vía las conexiones del proyecto:

```python
# knowledge base
metadata.get("type") == "knowledgeBase_MCP" and metadata.get("knowledgeBaseName") == name
# storage
project.connections.list(connection_type=ConnectionType.AZURE_STORAGE_ACCOUNT)
```

Por eso el código funciona en otro tenant sin tocar una línea: basta crear los recursos con los
mismos nombres lógicos. **Mantén este patrón** en los agentes nuevos.

### Parser de Excel: limitación conocida

`function_app/eeff.py` asume **una tabla por hoja**. Si un Excel trae dos tablas en la misma hoja,
se mezclan. Cobertura verificada a nivel de celda (355/355 celdas del archivo de prueba), y probado
contra los 4 layouts reales del cliente.

### La estructura de carpetas del contenedor no afecta a la tool

El contenedor `financiero` es hoy `<banca>/<RUC>/<RUC>.xlsx` (p. ej. `BC/20512437891/20512437891.xlsx`).
La búsqueda compara **solo el nombre del archivo**, no la ruta:

```python
archivo = blob_name.rsplit("/", 1)[-1].lower()   # descarta toda la ruta
return archivo == f"{ruc}.xlsx"
```

Así que da igual cuántos niveles de carpeta haya o cómo se llamen. Verificado contra la Function
desplegada con los dos RUC reales del contenedor.

> ⚠️ **Dos supuestos que sí importan.** (1) Si el mismo RUC existiera en dos bancas, se devuelve el
> primero que aparezca en el listado, sin avisar. (2) La búsqueda recorre el contenedor entero en
> cada llamada; con 2 archivos es instantáneo, pero con miles habrá que acotarla por prefijo.

### Pendiente en `roles.md`

AI Search (`azcssc1rmiad02`) está en `authOptions: apiKeyOnly`. Mientras no cambie a `aadOrApiKey`,
las conexiones por identidad administrada seguirán fallando.

---

## 7. Roadmap sugerido

| # | Paso | Resultado |
|---|---|---|
| 1 | Application Insights + tracing | Visibilidad antes de crecer |
| 2 | Function App sobre el plan B1 + Storage Queue | Base de ejecución asíncrona |
| 3 | Orquestador con `WorkflowBuilder` (fan-out/fan-in) | Financiero + sectorial en paralelo |
| 4 | Document Intelligence para extraer el RUC | Se acabó pasarlo a mano |
| 5 | Agentes validador y generador de reporte | Flujo completo |
| 6 | Bot backend + mensaje proactivo | Producto usable en Teams |
| 7 | Agente revisor de reportes previos | Cierra tu diagrama |

Los pasos 1 y 2 son la base: hacerlos primero evita rehacer trabajo después.

---

## 8. Resumen de decisiones

| Pregunta | Respuesta |
|---|---|
| ¿App Service, Function o ambos? | **Function App** sobre el plan B1 existente |
| ¿El orquestador es un prompt agent? | **No**, código Python con `WorkflowBuilder` |
| ¿Los especialistas son prompt agents? | **Sí**, los 4 |
| ¿Hace falta Azure Function para la tool? | **No**, salvo que quieras usar el playground |
| ¿Por qué asíncrono? | El flujo dura ~3 min; Teams corta a los 15 s |
| ¿Qué recurso urge más? | **Application Insights** |
| ¿Qué recurso tienes sin usar? | **Document Intelligence** (extracción del RUC) |

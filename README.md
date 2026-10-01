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
| `financiero` | gpt-4.1 | `obtener_estados_financieros` (openapi) + code interpreter | **77 s** |
| `sectorial` | gpt-4.1 | web search + code interpreter + MCP (knowledge base) | **85 s** |
| `reportes-previos` | gpt-4.1 | code interpreter + MCP (`knowledgereportesprevios`) | **~60 s** |
| `generador-reporte` | gpt-4.1 | ninguna (síntesis pura) | **~45 s** |

Los cuatro son **prompt agents** publicados en Foundry, definidos desde código con
`to_prompt_agent`. Los tres primeros analizan; el cuarto integra sus salidas en el informe final.

El **orquestador** (`orchestrator/`) no es un agente: es código Python desplegado en App Service.
Ver §3.

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
  generador-reporte/
    main.py
    instructions.md
    .env              # MODEL                         (gitignored)
orchestrator/         # App Service desplegable: el orquestador
  app.py              # API FastAPI + stream SSE + interfaz
  config.py           # ajustes desde el entorno
  foundry.py          # acceso único a Foundry (agentes publicados + modelo suelto)
  intake.py           # extrae empresa/RUC/sector y pide lo que falte
  orquestacion.py     # fan-out concurrente a los 3 especialistas
  reporte.py          # fan-in: encarga el informe al generador
  seguimiento.py      # enrutado de las preguntas posteriores al informe
  sesiones.py         # estado de sesión + bus de eventos
  persistencia.py     # snapshot de sesiones en Blob (best-effort)
  adjuntos.py         # extracción de texto de PDF/XLSX/DOCX (+ OCR opcional)
  static/index.html   # interfaz del analista
  requirements.txt    # dependencias de runtime del App Service
function_app/         # Function App desplegable (Flex Consumption)
  function_app.py     # HTTP triggers
  openapi_spec.py     # spec OpenAPI (lo usan la API y main.py al publicar)
  eeff.py             # tool: Blob Storage -> JSON (implementación única)
  host.json
  requirements.txt    # solo dependencias de runtime, ver nota abajo
scripts/
  pull_agents.py      # re-sincroniza instrucciones desde el portal
  deploy_webapp.py    # despliega el orquestador en App Service (bootstrap + deploy)
infra/
  main.bicep          # recursos base
roles.md              # RBAC asignado
function.md           # runbook: migrar la Function y el agente a otro entorno
app_service.md        # runbook: desplegar el orquestador en App Service
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
| Document Intelligence | `aidieu2rmiad02` | en uso (lectura de PDFs adjuntos, `prebuilt-layout`) |
| App Service Plan (B1 Linux) | `aspleu2rmiad02` | en uso (hospeda el orquestador) |
| App Service (orquestador) | `awaws1rmiad02` | creado; falta config y despliegue (`app_service.md`) |
| Function App (Flex Consumption) | `afaws1rmiad02` | en uso (tool `obtener_estados_financieros`) |
| Application Insights | `afaws1rmiad02` | en uso (trazas de la Function) |
| Azure Bot | `azbseu2rmiad02` | **endpoint placeholder** |

---

## 2. Decisión: App Service, no Function App

**El orquestador va en un App Service sobre el plan B1 `aspleu2rmiad02`.** Esta sección
reemplaza la recomendación anterior (Function App con `Queue trigger`), porque cambió el
requisito, no porque aquel análisis estuviera mal.

### Qué cambió

El diseño original era de *disparar y olvidar*: el analista pedía un informe por Teams, el flujo
corría ~3 minutos y devolvía un mensaje proactivo. Para eso, `Queue trigger` es ideal.

El requisito actual es **conversacional**: tras recibir el informe, el analista pregunta, pide
profundizar en una sección, aporta enlaces o adjunta un documento, y el informe se actualiza. Eso
cambia dos cosas:

| | Flujo por cola | Flujo conversacional |
|---|---|---|
| Estado | No hay: cada ejecución es independiente | Hay sesión viva: las tres secciones, el informe y el hilo |
| Interacción | Una respuesta y se acabó | N turnos sobre el mismo estado |
| Progreso | Irrelevante, llega al final | El analista espera minutos: necesita ver el avance |

Un `Queue trigger` no sostiene una sesión interactiva: cada mensaje sería un trabajo aislado que
tendría que rehidratar el estado desde un almacén externo. Un proceso web con la sesión en
memoria lo hace directo. Y el App Service sirve además la interfaz del analista, que ahora hace
falta.

### El límite de los 230 s no desaparece, se esquiva

Azure Load Balancer corta toda conexión que pase **230 s sin tráfico**. Aplica igual a App
Service y a Functions, y no es configurable. El flujo dura más que eso.

La solución no es un timeout más largo —no existe— sino **que la conexión nunca esté inactiva**:

```
POST /api/sesiones/{id}/mensaje   -> acepta el trabajo y responde al instante
GET  /api/sesiones/{id}/eventos   -> stream SSE: progreso, latidos cada 20 s, resultado
```

El trabajo largo corre en una tarea de fondo y habla por el stream. De paso, el analista ve qué
agente va por dónde en lugar de un spinner de tres minutos.

> **Always On es obligatorio**, más que en una web normal: el análisis continúa después de que el
> POST haya respondido. Sin Always On, App Service descarga la app por inactividad y se lleva por
> delante los análisis en curso.

La Function App `afaws1rmiad02` **se queda como está**: sigue sirviendo la tool
`obtener_estados_financieros` por HTTP para que Foundry la ejecute server-side. Son dos piezas con
responsabilidades distintas, no duplicadas.

---

## 3. Arquitectura

```
Analista (navegador)
      │  mensaje + adjuntos (reporte de créditos, informe comercial, EEFF)
      ▼
┌──────────── App Service B1 · awaws1rmiad02 · Always On ────────────┐
│                                                                    │
│  POST /mensaje  ──> acepta y responde al instante                  │
│  GET  /eventos  <── stream SSE (progreso + latidos + resultado)    │
│                                                                    │
│  ① INTAKE (gpt-4.1-mini)                                           │
│     extrae empresa / RUC / sector del mensaje y de los adjuntos    │
│     ¿falta algo? -> pregunta y se detiene aquí                     │
│                      │                                             │
│  ② FAN-OUT CONCURRENTE (código, siempre los tres)                  │
│         ┌────────────┼────────────┐                                │
│    financiero     sectorial    reportes-previos                    │
│         └────────────┼────────────┘                                │
│  ③ FAN-IN                                                          │
│                generador-reporte  ──> informe integrado            │
│                      │                                             │
│  ④ SEGUIMIENTO (enrutado dinámico, gpt-4.1-mini)                   │
│     "¿cuál era el ratio?"      -> responde del informe    (~2 s)   │
│     "profundiza en sectorial"  -> reactiva SOLO ese agente         │
│     "cambia el RUC"            -> repite el fan-out completo       │
│                                   y regenera el informe            │
└────────────────────────────────────────────────────────────────────┘
         │ invoca por nombre (FoundryAgent)
         ▼
   Prompt agents en Foundry — sus tools openapi/mcp corren server-side
```

### Un patrón por fase, no uno para todo

La guía de patrones de agentes de Azure describe la **orquestación simultánea** (concurrente), y
es la correcta para la fase ②: los tres análisis son independientes, así que el coste es el del
más lento (~85 s) y no la suma (~4 min).

Pero aplicarla a las cuatro fases sería un error:

| Fase | Patrón | Por qué no el concurrente |
|---|---|---|
| ① Intake | Puerta determinista + extracción | No hay nada que paralelizar: o están los datos o se pregunta |
| ② Análisis | **Concurrente** | Aquí sí: independientes y lentos |
| ③ Integración | Secuencial (fan-in) | Necesita las tres salidas; por definición va después |
| ④ Seguimiento | **Enrutado dinámico** | Relanzar los tres por un "profundiza en sectorial" cuesta 85 s para tirar dos |

La fase ④ es la que más se aleja del patrón simultáneo, y es la que más usará el analista.

### El intake no es un adorno: los especialistas no pueden preguntar

Los tres especialistas están escritos para **no preguntar nunca**, y es la decisión correcta: un
agente que se detiene a preguntar en mitad de un fan-out deja la rama colgada.

```
sectorial        -> "Nunca te detengas a hacer preguntas: no preguntes el sector..."
reportes-previos -> "Nunca te detengas a hacer preguntas: no pidas confirmación..."
financiero       -> "Si el mensaje no trae un RUC, pídelo y no continúes."
```

Consecuencia: si falta el RUC, el financiero se planta y el informe sale cojo **sin que nadie
avise al analista**. Por eso la aclaración ocurre antes del fan-out, en un solo sitio
(`orchestrator/intake.py`), y pide de una vez todo lo que falte.

El intake lee también los adjuntos, así que "analiza esta empresa" + el informe comercial en PDF
basta: el RUC y el sector salen del documento. Verificado end-to-end.

### Qué es código y qué es LLM

El README anterior sostenía que el orquestador debía ser código y no un prompt agent. Sigue
siendo cierto **en lo que importa**: el reparto de trabajo es determinista. Siempre corren los
tres; ningún modelo decide saltarse uno.

Pero el sistema conversacional necesita criterio en dos puntos muy acotados, y ahí el LLM sí
aporta: entender "analiza Andes Dorado, aquí va su informe comercial" (intake) y entender si
"profundiza en el sector" significa releer el informe o relanzar un agente (enrutado). Ambos
corren sobre `gpt-4.1-mini`: son clasificaciones cortas, no análisis.

```
Determinista (código)        Criterio (LLM)
─────────────────────        ──────────────
qué agentes corren           qué empresa/RUC/sector hay en el texto
en qué orden                 si una pregunta necesita un agente o no
qué hacer si uno falla       el análisis en sí (los especialistas)
cómo se arma el informe
```

### Resiliencia: un especialista caído no tumba el informe

`asyncio.gather(return_exceptions=True)` más un timeout por rama. Si el sectorial falla, el
informe se redacta con los otros dos y **declara explícitamente** qué faltó: el prompt del
generador se lo exige.

No es teórico. En la primera corrida real el sectorial cayó con un 429 de cuota y el informe
salió igual, con esta frase: *"No se cuenta con análisis sectorial actualizado en este informe, lo
que deja sin cubrir la validación de tendencias macro"*. Perder 85 s de trabajo bueno porque una
rama falló no es aceptable cuando cada corrida cuesta minutos.

> ⚠️ **El fan-out multiplica el consumo instantáneo de tokens.** Tres agentes a la vez sobre el
> mismo despliegue de gpt-4.1 pueden superar el TPM de la región aunque cada uno quepa de sobra.
> Hay reintentos con espera creciente ante 429 (`invocar_con_reintentos`), que llevaron esa misma
> corrida de 2/3 a 3/3. Lo que no arreglan es una cuota baja de forma estructural: para eso hay
> que subir el TPM del despliegue. Ver `app_service.md` §6.

### El informe se regenera, no se parchea

Las **secciones son la fuente de verdad**; el informe es una proyección de ellas. Cuando una
pregunta de seguimiento actualiza el análisis sectorial, el informe se vuelve a generar completo
desde las tres secciones. Parchear el texto anterior dejaría párrafos huérfanos del análisis
viejo contradiciendo al nuevo.

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
| Hosting del orquestador (plan B1) | Flujo largo con Always On | ✅ **creado** (`awaws1rmiad02`, falta config) |
| ~~Storage Queue~~ | Ya no aplica: el flujo es interactivo, no por cola (§2) | — |
| Contenedor `sesiones` en Blob | Snapshot de sesiones y auditoría | Lo crea la app sola |
| Cosmos DB | Solo si se escala a más de una instancia | Baja |

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
| Agente `reportes-previos` | ✅ **publicado** (v1) |
| Agente `generador-reporte` | ✅ **publicado** (v1) |
| Orquestador con fan-out concurrente | ✅ **hecho** (`orchestrator/`, probado end-to-end) |
| Interacción de seguimiento sobre el informe | ✅ **hecha** (`orchestrator/seguimiento.py`) |
| Extracción del RUC desde los adjuntos | ✅ **hecha** (`orchestrator/intake.py` + `adjuntos.py`) |
| Lectura de PDFs adjuntos con Document Intelligence (`prebuilt-layout`) | ✅ **hecha**, activada por defecto |
| Agente validador de consistencia | **aplazado a propósito** (ver abajo) |
| Exportación del informe a Word / PPT | pendiente |
| Mensaje proactivo a Teams | pendiente |

### Sobre el validador de consistencia

Está fuera del alcance actual por decisión tuya, y encaja bien que así sea: **parte de su trabajo
ya lo hace el generador de reporte**. Su prompt le obliga a cruzar las tres fuentes y declarar las
contradicciones en la sección "Discrepancias entre fuentes" (p. ej. liquidez deteriorada frente a
historial de pago impecable).

Cuando lo retomes, la pregunta útil es qué añade sobre eso. Dos opciones con sentido distinto:

- **Validador de hechos**: comprueba que cada cifra del informe exista en la sección de origen, para
  cazar invenciones del redactor. Es verificación, no análisis, y encaja mejor como código que como
  agente.
- **Segunda opinión**: un agente que juzgue la solidez del análisis. Es criterio, y ahí sí un prompt
  agent aporta.

Enchufarlo es un `add_edge` conceptual entre `orquestacion.py` y `reporte.py`: el sitio ya está.

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

## 7. Roadmap

| # | Paso | Estado |
|---|---|---|
| 1 | Agentes `reportes-previos` y `generador-reporte` | ✅ publicados |
| 2 | Orquestador: intake, fan-out concurrente, fan-in, seguimiento | ✅ probado end-to-end |
| 3 | App Service `awaws1rmiad02` sobre el plan B1 | ✅ creado |
| 4 | **Roles RBAC + app settings + despliegue del código** | ⬜ `python scripts/deploy_webapp.py --all` |
| 5 | Subir el TPM de `gpt-4.1` (el fan-out satura la cuota) | ⬜ `app_service.md` §6 |
| 6 | Application Insights sobre el App Service | ⬜ sigue siendo lo que más urge |
| 7 | Autenticación con Entra ID antes de usuarios reales | ⬜ |
| 8 | Exportación del informe a Word / PPT | ⬜ |
| 9 | Validador de consistencia (decidir antes qué añade, §5) | ⬜ aplazado |
| 10 | Bot de Teams contra la misma API | ⬜ |

El paso 4 es el único que bloquea el uso real: el código está escrito y probado, falta permiso y
configuración.

---

## 8. Resumen de decisiones

| Pregunta | Respuesta |
|---|---|
| ¿App Service, Function o ambos? | **App Service** para el orquestador (§2); la Function sigue sirviendo la tool |
| ¿El orquestador es un prompt agent? | **No**: código Python. El reparto de trabajo es determinista |
| ¿Y la parte conversacional? | LLM en dos puntos acotados: intake y enrutado, con `gpt-4.1-mini` |
| ¿Los especialistas son prompt agents? | **Sí**, los 4 (3 analistas + el redactor) |
| ¿Cómo los invoca el orquestador? | `FoundryAgent` por nombre: Foundry sigue siendo la fuente de verdad |
| ¿Es buena la orquestación simultánea? | **Sí, para la fase de análisis.** Para el seguimiento, enrutado dinámico (§3) |
| ¿Por qué streaming y no un POST que espere? | El flujo supera los 230 s del balanceador; el stream nunca está inactivo |
| ¿Qué pasa si un especialista falla? | El informe sale con los demás y declara qué faltó. Probado con un 429 real |
| ¿Qué recurso urge más? | **Application Insights**, y subir el TPM de `gpt-4.1` |
| ¿Qué falta para usarlo? | Un comando: `python scripts/deploy_webapp.py --all` |

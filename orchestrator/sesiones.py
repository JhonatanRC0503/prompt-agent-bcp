"""Estado de una sesión de análisis y el bus de eventos que alimenta el streaming SSE.

Por qué hay un bus de eventos y no un simple `await` sobre el resultado: el flujo completo dura
unos 90 s (los tres especialistas en paralelo) más la redacción del informe. Azure Load Balancer
corta cualquier conexión HTTP que pase **230 s sin tráfico**, y ese límite no es configurable ni
en App Service ni en Functions. La solución no es subir el timeout —no se puede— sino que la
conexión nunca esté inactiva: el orquestador emite eventos de progreso según avanza y, si no hay
nada que contar, un latido. Además el analista ve en qué paso va en vez de mirar un spinner.

El estado vive en memoria y se replica a Blob en segundo plano. Es deliberado: el plan B1 corre
una sola instancia, así que la memoria es coherente; el blob sirve para sobrevivir a un reinicio
y para auditoría. Si algún día se escala a varias instancias, esto hay que mover a Cosmos o Redis
(ver app_service.md).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# Cada cuántos segundos se emite un latido si no hubo ningún evento real. Muy por debajo de los
# 230 s del balanceador, con margen de sobra para reintentos del proxy.
SEGUNDOS_LATIDO = 20


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Datos:
    """Los tres datos que el flujo necesita antes de poder arrancar."""

    empresa: str | None = None
    ruc: str | None = None
    sector: str | None = None

    def faltantes(self) -> list[str]:
        return [c for c in ("empresa", "ruc", "sector") if not getattr(self, c)]

    def completos(self) -> bool:
        return not self.faltantes()


@dataclass
class Seccion:
    """El resultado de un especialista. Es la unidad que las preguntas de seguimiento actualizan."""

    agente: str
    texto: str = ""
    estado: str = "pendiente"  # pendiente | ok | error
    error: str | None = None
    actualizada: str = field(default_factory=_ahora)
    # Id de sesión del agente en Foundry: permite que una pregunta de seguimiento continúe la
    # conversación con ese especialista en vez de empezar de cero.
    sesion_agente: str | None = None


@dataclass
class Adjunto:
    nombre: str
    tipo: str
    bytes: int
    texto: str = ""
    extraido_con: str = ""
    blob: str | None = None


@dataclass
class Sesion:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    creada: str = field(default_factory=_ahora)
    estado: str = "nueva"  # nueva | esperando_datos | analizando | listo | error
    datos: Datos = field(default_factory=Datos)
    secciones: dict[str, Seccion] = field(default_factory=dict)
    adjuntos: list[Adjunto] = field(default_factory=list)
    reporte: str = ""
    historial: list[dict] = field(default_factory=list)
    eventos: list[dict] = field(default_factory=list)

    # No se serializa: coordina a los lectores del stream con el productor.
    _senal: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # Evita que dos peticiones del mismo analista disparen el flujo a la vez.
    _cerrojo: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # ---------------------------------------------------------------- eventos

    def emitir(self, tipo: str, **datos) -> dict:
        evento = {"i": len(self.eventos), "tipo": tipo, "ts": _ahora(), **datos}
        self.eventos.append(evento)
        self._senal.set()
        return evento

    async def escuchar(self, desde: int = 0):
        """Emite los eventos desde `desde` y se queda esperando los siguientes.

        Nunca termina por su cuenta: la sesión sigue viva para preguntas de seguimiento, así que
        el stream se cierra cuando el cliente se desconecta. El latido mantiene la conexión con
        tráfico para que el balanceador no la corte.
        """
        i = desde
        while True:
            # Limpiar ANTES de drenar: si llega un evento mientras emitimos, la señal queda
            # puesta y el `wait` de abajo retorna de inmediato en vez de perderlo.
            self._senal.clear()
            while i < len(self.eventos):
                yield self.eventos[i]
                i += 1
            try:
                await asyncio.wait_for(self._senal.wait(), timeout=SEGUNDOS_LATIDO)
            except asyncio.TimeoutError:
                yield {"tipo": "latido", "ts": _ahora()}

    # ---------------------------------------------------------------- historial

    def anotar(self, rol: str, texto: str) -> None:
        self.historial.append({"rol": rol, "texto": texto, "ts": _ahora()})

    # ---------------------------------------------------------------- serialización

    def como_dict(self, incluir_eventos: bool = False) -> dict:
        d = {
            "id": self.id,
            "creada": self.creada,
            "estado": self.estado,
            "datos": asdict(self.datos),
            "secciones": {k: asdict(v) for k, v in self.secciones.items()},
            "adjuntos": [
                # El texto extraído puede ser enorme; en el payload va solo el metadato.
                {k: v for k, v in asdict(a).items() if k != "texto"}
                for a in self.adjuntos
            ],
            "reporte": self.reporte,
            "historial": self.historial,
        }
        if incluir_eventos:
            d["eventos"] = self.eventos
        return d


class Almacen:
    """Registro de sesiones vivas, con réplica best-effort a Blob.

    La escritura a blob nunca bloquea ni rompe el flujo: si Storage no está disponible o faltan
    permisos, el analista no se entera y la sesión sigue en memoria.
    """

    def __init__(self, persistencia=None) -> None:
        self._sesiones: dict[str, Sesion] = {}
        self._persistencia = persistencia

    def crear(self) -> Sesion:
        sesion = Sesion()
        self._sesiones[sesion.id] = sesion
        return sesion

    def obtener(self, id_sesion: str) -> Sesion | None:
        return self._sesiones.get(id_sesion)

    def listar(self) -> list[Sesion]:
        return list(self._sesiones.values())

    def guardar(self, sesion: Sesion) -> None:
        if self._persistencia is None:
            return
        # Se dispara y se olvida: el snapshot es para durabilidad, no para la ruta caliente.
        asyncio.create_task(self._persistencia.guardar(sesion))

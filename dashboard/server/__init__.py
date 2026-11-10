"""Servidor de ingesta del sistema de monitoreo estructural.

Este paquete implementa el lado servidor del contrato de datos
(``CONTRATO_DE_DATOS.md``): parseo del protocolo serie, métricas derivadas,
persistencia en SQLite y exposición REST + WebSocket al dashboard.

Decisiones de diseño que afectan a todo el paquete:

* Los identificadores van en inglés y los comentarios en español: el código se
  lee en tesis pero se integra con un firmware y un ecosistema Python en inglés.
* Toda la semántica de nombres (prefijos seriales, columnas SQL, campos JSON)
  vive en :mod:`server.config` para que exista un único punto de verdad y sea
  verificable contra el contrato carácter a carácter.
"""

__all__ = ["config", "db", "derived", "protocol", "ingest", "app"]

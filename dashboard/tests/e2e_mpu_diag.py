"""E2E: ráfaga de eventos repetidos del firmware -> colapso en WS y en el DOM.

Reproduce el escenario real de avería del MPU6050: un PTY emite la misma línea
``[MPU6050] MUESTRAS INVALIDAS`` decenas de veces por segundo (con magnitudes
variables, como el firmware real) y luego cambia a ``I2C ERROR`` para forzar el
cierre de ráfaga. Se comprueba que:

* el WebSocket publica la primera aparición tal cual, agrega las repeticiones
  como ``(×N repeticiones)`` y no inunda (frente a ~150 líneas emitidas);
* el nivel de la línea es ``warn`` (ámbar en la consola);
* el dashboard en Chrome headless renderiza la consola sin inundar, con la
  fila en ámbar, los ángulos del HUD en ``—`` (sin MPU6050) y la vibración del
  ADXL345 con valores.

Uso (desde dashboard/):

    .venv/bin/python tests/e2e_mpu_diag.py

Requiere Chrome en /Applications y poder hacer bind en 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import json
import os
import pty
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HTTP_PORT = 8765
CHROME_PORT = 9333
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DASH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond or not detail else " — " + detail), flush=True)


def cdp(cmd_id: int, method: str, params: dict, ws_url: str):
    async def run():
        from websockets.asyncio.client import connect

        async with connect(ws_url, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"id": cmd_id, "method": method, "params": params}))
            while True:
                resp = json.loads(await ws.recv())
                if resp.get("id") == cmd_id:
                    return resp
    return asyncio.run(run())


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="e2e_mpu_")
    db_path = os.path.join(tmp, "e2e.db")
    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)

    server = subprocess.Popen(
        [os.path.join(DASH, ".venv/bin/python"), "-m", "server.run",
         "--serial-port", slave_name, "--http-port", str(HTTP_PORT),
         "--db", db_path],
        cwd=DASH, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu",
         f"--remote-debugging-port={CHROME_PORT}",
         f"--user-data-dir={os.path.join(tmp, 'chrome')}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    stop = threading.Event()
    sent = {"A": 0, "B": 0, "adxl": 0}

    def feeder() -> None:
        # Fase A (3 s): la misma línea de muestras inválidas a 50 Hz, con
        # magnitudes variables como el firmware real. Fase B: cambio de mensaje
        # (fuerza el cierre de ráfaga con el agregado) hasta que se pare.
        t0 = time.monotonic()
        i = 0
        while not stop.is_set() and time.monotonic() - t0 < 30.0:
            t = time.monotonic() - t0
            if t < 3.0:
                line = ("[MPU6050] MUESTRAS INVALIDAS - revise cableado I2C "
                        f"(|a|={(i % 7) * 0.111:.3f} m/s2 |w|={(i % 5) * 0.7:.5f} rad/s)\n")
                sent["A"] += 1
            else:
                line = "[MPU6050] I2C ERROR - sin respuesta en el bus\n"
                sent["B"] += 1
            os.write(master, line.encode())
            os.write(master, f"ADXL,{(i % 9) * 0.05:.3f},0.010,{9.81 + (i % 4) * 0.2:.3f}\n".encode())
            sent["adxl"] += 1
            i += 1
            time.sleep(0.02)

    logs: list[dict] = []
    samples: list[dict] = []

    def ws_collect() -> None:
        from websockets.sync.client import connect
        try:
            with connect(f"ws://127.0.0.1:{HTTP_PORT}/ws") as ws:
                while not stop.is_set():
                    try:
                        msg = json.loads(ws.recv(timeout=0.5))
                    except Exception:
                        continue
                    if msg.get("type") == "log":
                        logs.append(msg)
                    elif msg.get("type") == "sample":
                        samples.append(msg)
        except Exception as exc:  # pragma: no cover
            print("WS collector error:", exc, flush=True)

    try:
        # Esperar al servidor.
        deadline = time.monotonic() + 15
        up = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/api/health", timeout=1) as r:
                    if r.status == 200:
                        up = True
                        break
            except Exception:
                time.sleep(0.2)
        check("servidor arrancado en 8765", up)
        if not up:
            raise RuntimeError("el servidor de prueba no arrancó")

        threading.Thread(target=ws_collect, daemon=True).start()

        # Esperar al endpoint de depuración de Chrome (arranque en frío ~2 s).
        deadline = time.monotonic() + 15
        targets = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{CHROME_PORT}/json/list", timeout=1) as r:
                    targets = json.load(r)
                break
            except Exception:
                time.sleep(0.2)
        check("Chrome headless con depuración remota", targets is not None)
        if targets is None:
            raise RuntimeError("Chrome no abrió el puerto de depuración")
        page = next(t for t in targets if t.get("type") == "page")
        ws_url = page["webSocketDebuggerUrl"]
        cdp(1, "Page.navigate", {"url": f"http://127.0.0.1:{HTTP_PORT}/"}, ws_url)
        time.sleep(1.5)  # el dashboard abre su WebSocket

        # Ráfaga A (3 s) -> cambio a B (cierre de ráfaga con agregado) -> corte.
        threading.Thread(target=feeder, daemon=True).start()
        time.sleep(6.0)
        stop.set()
        time.sleep(0.5)

        # ── Aserciones por WebSocket ──────────────────────────────────
        inv = [m for m in logs if "MUESTRAS INVALIDAS" in m.get("line", "")]
        i2c = [m for m in logs if "I2C ERROR" in m.get("line", "")]
        check(f"ráfaga suprimida: {sent['A']} líneas A emitidas -> {len(inv)} avisos WS",
              len(inv) <= 4 and sent["A"] > 100, f"{len(inv)} avisos de {sent['A']} líneas")
        check("primera aparición sin sufijo",
              bool(inv) and "(×" not in inv[0]["line"], inv[0]["line"] if inv else "sin avisos")
        agg = [m for m in inv if "(×" in m["line"]]
        n_agg = 0
        if agg:
            found = re.search(r"×(\d+)", agg[0]["line"])
            n_agg = int(found.group(1)) if found else 0
        check("cierre de ráfaga con agregado (×N)",
              n_agg >= 100, f"agregado visto: {agg[0]['line'] if agg else 'ninguno'}")
        check("nivel warn (ámbar) en la línea",
              bool(inv) and inv[0].get("level") == "warn", inv[0].get("level", "—") if inv else "—")
        check(f"I2C ERROR también colapsado: {sent['B']} emitidas -> {len(i2c)} avisos",
              len(i2c) <= 2 and sent["B"] > 50, f"{len(i2c)} avisos de {sent['B']} líneas")
        adxl_samples = [s for s in samples if s.get("sensor") == "adxl345"]
        vib_ok = any(
            isinstance(s.get("derived", {}).get("vib_rms"), (int, float))
            for s in adxl_samples
        )
        check(f"muestras ADXL con vib_rms por WS ({len(adxl_samples)} recibidas)",
              vib_ok and len(adxl_samples) > 50)

        # ── Aserciones sobre el DOM del dashboard ─────────────────────
        expr = """(() => {
          const rows = [...document.querySelectorAll('#events-console .log-row')];
          const inv = rows.filter(r => r.textContent.includes('MUESTRAS INVALIDAS'));
          return {
            invRows: inv.length,
            invWarn: inv.some(r => r.classList.contains('log-warn')),
            roll: document.getElementById('model3d-roll').textContent.trim(),
            vib: document.getElementById('model3d-vib-rms').textContent.trim(),
            connected: document.querySelector('.conn') ? document.querySelector('.conn').textContent.trim() : null,
          };
        })()"""
        resp = cdp(2, "Runtime.evaluate", {"expression": expr, "returnByValue": True}, ws_url)
        dom = resp.get("result", {}).get("result", {}).get("value") or {}
        check(f"DOM: consola no inundada ({dom.get('invRows')} filas 'MUESTRAS INVALIDAS')",
              dom.get("invRows") is not None and dom["invRows"] <= 4, str(dom))
        check("DOM: fila en ámbar (log-warn)", dom.get("invWarn") is True, str(dom))
        check("DOM: ángulos del HUD en '—' sin MPU6050", dom.get("roll") == "—",
              f"roll={dom.get('roll')!r}")
        # El HUD imprime el valor con unidad ("0.376 m/s²"): se extrae el número.
        m_vib = re.match(r"-?\d+(\.\d+)?", str(dom.get("vib") or ""))
        vib_val = float(m_vib.group(0)) if m_vib else float("nan")
        check("DOM: vibración RMS con valor numérico (ADXL345)",
              vib_val == vib_val, f"vib={dom.get('vib')!r}")
    except Exception as exc:
        check("harness sin excepciones", False, str(exc))
    finally:
        stop.set()
        for proc in (server, chrome):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    fails = sum(1 for _, ok, _ in results if not ok)
    print("---")
    print(f"RESULTADO: {'OK' if fails == 0 else str(fails) + ' fallo(s)'}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

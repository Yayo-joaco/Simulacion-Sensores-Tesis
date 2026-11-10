"""E2E: muestras DATA válidas del MPU6050 → HUD de orientación y modelos 3D.

Cierra la brecha de e2e_mpu_diag.py (que solo emite eventos y ADXL): este
harness emite un flujo REAL de líneas ``DATA,...`` por un PTY y comprueba que
el dashboard las procesa de punta a punta:

* el HUD del modelo 3D (``#model3d-roll`` etc.) deja de mostrar "—" y
  refleja los ángulos derivados por el servidor;
* la tarjeta de Orientación del panel MPU6050 (``#val-roll`` etc.) también;
* el modelo 3D de la antena (``#antenna3d``) existe y no rompe la página
  (su construcción corre al cargar app.js: si el id fallara, todo el
  dashboard se caería);
* el badge del modelo pasa a "en vivo".

Uso (desde dashboard/):

    .venv/bin/python tests/e2e_orientation.py

Requiere Chrome en /Applications y poder hacer bind en 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import pty
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HTTP_PORT = 8766
CHROME_PORT = 9334
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
    tmp = tempfile.mkdtemp(prefix="e2e_orient_")
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
    sent = {"data": 0}

    def feeder() -> None:
        # Flujo nominal: DATA (MPU6050) a 50 Hz durante ~4 s. Los ángulos
        # derived los calcula el servidor a partir de la aceleración; una
        # inclinación estacionaria en Y da roll/pitch estables y no nulos.
        t0 = time.monotonic()
        i = 0
        while not stop.is_set() and time.monotonic() - t0 < 30.0:
            # Inclinación suave y realista: gravedad repartida entre Y y Z.
            ang = math.radians(20.0)
            ax = 0.05 * math.sin(i * 0.15)
            ay = 9.81 * math.sin(ang)
            az = 9.81 * math.cos(ang)
            os.write(master, f"DATA,{ax:.3f},{ay:.3f},{az:.3f},0.00100,-0.00200,0.00050,28.50\n".encode())
            os.write(master, f"ADXL,{ax:.3f},{ay:.3f},{az:.3f}\n".encode())
            sent["data"] += 1
            i += 1
            time.sleep(0.02)

    def ws_collect() -> None:
        from websockets.sync.client import connect
        try:
            with connect(f"ws://127.0.0.1:{HTTP_PORT}/ws") as ws:
                while not stop.is_set():
                    try:
                        ws.recv(timeout=0.5)
                    except Exception:
                        continue
        except Exception:
            pass

    try:
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
        check("servidor arrancado en 8766", up)
        if not up:
            raise RuntimeError("el servidor de prueba no arrancó")

        threading.Thread(target=ws_collect, daemon=True).start()

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
        time.sleep(1.5)

        threading.Thread(target=feeder, daemon=True).start()
        time.sleep(5.0)
        stop.set()
        time.sleep(0.5)

        # ── Aserciones sobre el DOM del dashboard ─────────────────────
        expr = """(() => {
          const t = (id) => { const el = document.getElementById(id);
                               return el ? el.textContent.trim() : null; };
          return {
            model3dRoll: t('model3d-roll'), model3dPitch: t('model3d-pitch'),
            model3dYaw: t('model3d-yaw'),
            valRoll: t('val-roll'), valPitch: t('val-pitch'),
            antenna3d: !!document.getElementById('antenna3d'),
            antenna3dW: document.getElementById('antenna3d')
              ? document.getElementById('antenna3d').width : 0,
            badge: t('badge-model3d'),
            conn: t('conn-text'),
          };
        })()"""
        resp = cdp(2, "Runtime.evaluate", {"expression": expr, "returnByValue": True}, ws_url)
        dom = resp.get("result", {}).get("result", {}).get("value") or {}

        def num(s):
            m = re.match(r"-?\d+(\.\d+)?", str(s or ""))
            return float(m.group(0)) if m else float("nan")

        check(f"{sent['data']} líneas DATA emitidas → HUD del modelo 3D con ángulos",
              num(dom.get("model3dRoll")) == num(dom.get("model3dRoll")) and
              dom.get("model3dRoll") != "—",
              f"roll={dom.get('model3dRoll')!r}")
        check("tarjeta de Orientación (val-roll) actualizada",
              num(dom.get("valRoll")) == num(dom.get("valRoll")) and dom.get("valRoll") != "—",
              f"val-roll={dom.get('valRoll')!r}")
        # Con la gravedad repartida entre Y y Z (20° de inclinación) el roll
        # derivado es claramente no nulo y el pitch también.
        check("roll derivado no nulo (módulo inclinado 20°)",
              abs(num(dom.get("model3dRoll"))) > 1.0,
              f"roll={dom.get('model3dRoll')!r}")
        check("modelo 3D de la antena presente (#antenna3d)",
              dom.get("antenna3d") is True and (dom.get("antenna3dW") or 0) > 0,
              str(dom))
        check("badge del modelo 'en vivo' con flujo activo",
              dom.get("badge") == "en vivo", str(dom.get("badge")))
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

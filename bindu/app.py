"""Eigenes Fenster statt Browser: startet den bindu-Server im Hintergrund und zeigt die Oberfläche mit pywebview.

Aufruf:
    bindu [--port N] [weitere Optionen des Servers, z. B. --rns-config ~/.reticulum --data ~/.bindu]
Alle Optionen außer --port gehen unverändert an den Server (siehe `bindu-server --help`).
Beim Schließen des Fensters wird der Server mit SIGTERM beendet, er schreibt dabei seine Daten weg.
pywebview ist optional:  pip install "bindu[app]"
"""
import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

TITLE = "Bindupāla – Jālakam"


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Der Hintergrund-Server läuft auf diesem Rechner: nie über einen Proxy aus den Umgebungsvariablen fragen
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_for(url, proc, timeout=60):
    """Wartet, bis der Server antwortet. False, wenn er vorher endet oder die Zeit abläuft."""
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            _LOCAL.open(url, timeout=2)
            return True
        except urllib.error.HTTPError:
            return True  # der Server antwortet, nur mit einem Fehlercode
        except Exception:
            time.sleep(0.5)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bindu", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=0, help="Port des Hintergrund-Servers (Standard: einen freien wählen)")
    args, extra = ap.parse_known_args(argv)
    extra = [a for a in extra if a != "--"]

    try:
        import webview
    except ImportError:
        sys.exit("pywebview fehlt. Installieren mit:\n  %s -m pip install pywebview" % sys.executable)

    port = args.port or free_port()
    cmd = [sys.executable, "-m", "bindu.cli", "--host", "127.0.0.1", "--port", str(port)] + extra
    print("Starte:", " ".join(cmd))
    # PYTHONSAFEPATH: Python 3.11+ nimmt das aktuelle Verzeichnis nicht mehr in den Suchpfad. Ohne das verdeckt ein
    # Ordner namens "bindu" im aktuellen Verzeichnis (z. B. ~/build/bindu) das Paket, und der Server startet nicht.
    proc = subprocess.Popen(cmd, env=dict(os.environ, PYTHONSAFEPATH="1"))
    url = "http://127.0.0.1:%d/" % port
    try:
        if not wait_for(url, proc):
            sys.exit("Der Server ist nicht angesprungen (Meldungen siehe oben). Läuft schon eine Instanz mit demselben Reticulum?")
        # text_select: ohne diesen Schalter lässt sich im Fenster nichts markieren und kopieren
        webview.create_window(TITLE, url, width=1280, height=860, min_size=(900, 600), text_select=True)
        webview.start()  # kehrt zurück, wenn das Fenster geschlossen wird
    finally:
        proc.terminate()  # SIGTERM: der Server schreibt dabei seine Daten weg
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())

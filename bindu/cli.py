import argparse
import logging
import logging.handlers
import os
import platform
import signal
import ssl
import sys
import threading
import time

from . import __version__
from .access import AccessPolicy
from .nomad import NomadBackend, NomadError
from .web import make_server, make_tls_context

LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3


class _LogFormat(logging.Formatter):
    """Eigene Zeilen mit Zeit und Ebene; Reticulum-Zeilen haben schon ihr eigenes Format."""

    def format(self, record):
        if record.name == "bindu.rns":
            return record.getMessage()
        return super().format(record)


def setup_logging(path):
    """Schreibt Protokollzeilen von bindu (und Reticulum) in eine rotierende Datei. Gibt den Pfad zurück."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
    except OSError as exc:
        print("Logdatei nicht beschreibbar (%s): %s" % (path, exc), file=sys.stderr)
        return None
    handler.setFormatter(_LogFormat("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("bindu")
    logger.setLevel(logging.INFO)
    for old in list(logger.handlers):
        if isinstance(old, logging.handlers.RotatingFileHandler):
            logger.removeHandler(old)
            old.close()
    logger.addHandler(handler)
    return os.path.abspath(path)


def _flush_on_signal(*parts):
    """Reticulum beendet den Prozess bei SIGINT/SIGTERM mit os._exit; vorher gebündelte Schreibvorgänge sichern."""

    def flush_all():
        for part in parts:
            try:
                part.flush()
            except Exception:
                pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)

        def handler(signum, frame, previous=previous):
            flush_all()
            if callable(previous):
                previous(signum, frame)
            else:
                raise KeyboardInterrupt

        try:
            signal.signal(sig, handler)
        except ValueError:  # nicht im Hauptthread (Tests)
            return
    return flush_all


def _rns_to_file(RNS):
    """Reticulum-Zeilen weiter auf der Konsole ausgeben und zusätzlich in die Logdatei schreiben."""
    rns_log = logging.getLogger("bindu.rns")

    def emit(line):
        # Darf nie eine Ausnahme werfen: Reticulum würde sonst innerhalb seiner Log-Sperre erneut loggen (Deadlock)
        try:
            print(line)
        except Exception:
            pass
        try:
            rns_log.info(line)
        except Exception:
            pass

    RNS.logdest = RNS.LOG_CALLBACK
    RNS.logcall = emit


def main(argv=None):
    p = argparse.ArgumentParser(prog="bindu", description="Bindupāla – Jālakam: NomadNet-Browser, LXMF-Chat und Node-Verwaltung für microReticulum")
    p.add_argument("--host", default="127.0.0.1", help="Bind-Adresse (Standard 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="HTTP-Port (Standard 8765)")
    p.add_argument("--data", default=os.path.expanduser("~/.bindu"), help="Datenverzeichnis (Identities, Nodes)")
    p.add_argument("--rns-config", help="vorhandenes Reticulum-Config-Verzeichnis nutzen")
    p.add_argument(
        "--tcp",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help="TCP-Client-Interface beim ersten Start in die Config schreiben (wiederholbar)",
    )
    p.add_argument(
        "--allow-cidr",
        action="append",
        default=[],
        metavar="NETZ",
        help="zusätzlich erlaubtes Netz, z. B. 192.168.1.0/24 (wiederholbar); Standard: Tailnet + Loopback",
    )
    p.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="NAME",
        help="zusätzlicher Name im Host-Header (z. B. MagicDNS-Name oder Reverse-Proxy, wiederholbar; * = jeder). "
        "Erlaubt sind sonst nur IP-Adressen und localhost",
    )
    p.add_argument("--tls-cert", metavar="DATEI", help="HTTPS: Zertifikat (PEM), z. B. von `tailscale cert`; ohne = HTTP")
    p.add_argument("--tls-key", metavar="DATEI", help="HTTPS: privater Schlüssel (PEM); entfällt, wenn er in der Zertifikatsdatei steht")
    p.add_argument("--no-access-check", action="store_true", help="Quell-IP-Prüfung abschalten (nicht empfohlen)")
    p.add_argument("--name", default="bindu", help="LXMF-Anzeigename, der beim Announce mitgesendet wird")
    p.add_argument("--no-chat", action="store_true", help="LXMF-Chat nicht starten")
    p.add_argument("--no-announce", action="store_true", help="LXMF-Adresse nicht automatisch beim Start bekanntgeben")
    p.add_argument("-v", "--verbose", action="store_true", help="Reticulum-Log auf Debug (Fehlersuche bei Links)")
    p.add_argument("--log-file", metavar="PFAD", help="Logdatei (Standard: <Datenverzeichnis>/bindu.log, rotiert)")
    p.add_argument("--no-log-file", action="store_true", help="keine Logdatei schreiben")
    p.add_argument("--version", action="version", version="bindu " + __version__)
    args = p.parse_args(argv)
    try:
        access = AccessPolicy.disabled() if args.no_access_check else AccessPolicy.default(args.allow_cidr)
    except ValueError as exc:
        p.error("--allow-cidr: %s" % exc)

    if args.tls_key and not args.tls_cert:
        p.error("--tls-key braucht --tls-cert")
    tls = None
    if args.tls_cert:
        try:
            tls = make_tls_context(args.tls_cert, args.tls_key)
        except (OSError, ssl.SSLError) as exc:
            print("Fehler: TLS-Zertifikat/Schlüssel nicht ladbar (%s)" % exc, file=sys.stderr)
            return 2
    scheme = "https" if tls else "http"

    log_path = None if args.no_log_file else setup_logging(args.log_file or os.path.join(args.data, "bindu.log"))
    log = logging.getLogger("bindu")

    try:
        backend = NomadBackend(args.data, args.rns_config, args.tcp, 6 if args.verbose else None)
    except NomadError as exc:
        print("Fehler:", exc, file=sys.stderr)
        log.error("Start fehlgeschlagen: %s", exc)
        return 2
    if log_path:
        _rns_to_file(backend.RNS)

    chat = None
    if not args.no_chat:
        try:
            from .chat import ChatBackend

            chat = ChatBackend(backend.RNS, args.data, args.name)
        except ImportError:
            print("Chat deaktiviert: Paket 'lxmf' fehlt (pip install lxmf)", file=sys.stderr)
        except NomadError as exc:
            print("Chat deaktiviert:", exc, file=sys.stderr)

    from .provision import ProvisionBackend

    prov = ProvisionBackend(backend)
    meta = {
        "version": __version__,
        "file": os.path.abspath(os.path.dirname(__file__)),
        "python": platform.python_version(),
        "rns": getattr(backend.RNS, "__version__", "?"),
        "lxmf": getattr(getattr(chat, "LXMF", None), "__version__", None) if chat is not None else None,
        "log": log_path,
        "started": int(time.time()),
    }
    _flush_on_signal(*[x for x in (backend, chat, prov) if x is not None])
    server = make_server(backend, args.host, args.port, chat, prov, meta, access, tls, args.allow_host)
    host, port = server.server_address[:2]
    banner = "bindu %s  Code: %s  Python %s  RNS %s  LXMF %s" % (
        meta["version"], meta["file"], meta["python"], meta["rns"], meta["lxmf"] or "-")
    print(banner)
    log.info(banner)
    if log_path:
        print("Logdatei: %s" % log_path)
    print("bindu läuft auf %s://%s:%d/  (Reticulum-Config: %s)" % (scheme, host, port, backend.config_dir))
    log.info("läuft auf %s://%s:%d/ (Reticulum-Config %s, Interfaces: %s)", scheme, host, port, backend.config_dir,
             ", ".join(backend.interface_names()) or "keine")
    print("Interfaces: " + (", ".join(backend.interface_names()) or "keine"))
    if chat is not None:
        print("LXMF-Adresse: %s  (Name: %s)" % (chat.address, chat.display_name))
        if not args.no_announce:
            # Interfaces brauchen einen Moment, bis sie senden können
            threading.Timer(8.0, chat.announce).start()
    print("Zugriff: " + access.describe())
    log.info("Zugriff: %s", access.describe())
    if not access.enabled and args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNUNG: Die Oberfläche hat keine Anmeldung und die Zugriffsprüfung ist aus.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if chat is not None:
            chat.shutdown()
        prov.shutdown()
        backend.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Bindupāla – Jālakam

*बिन्दुपाल – जालकम्* – „Knoten-Hüter – Das feine Netz“

Eine kleine Desktop-App für [Reticulum](https://reticulum.network):

- **Browser** – NomadNet-Seiten ansehen, mit Identity-Anmeldung
- **Chat** – LXMF-Nachrichten senden und empfangen
- **Nodes** – microReticulum-Nodes aus der Ferne verwalten (Konfiguration lesen, ändern, committen, neu starten)

Die Oberfläche ist eine einzelne HTML-Seite, der Server nutzt nur die Standardbibliothek. Das Fenster kommt von
[pywebview](https://pywebview.flowrl.com), es ist also kein Browser nötig.

## Installation

```
python3 -m venv ~/venvs/bindu
~/venvs/bindu/bin/pip install ".[app]"
```

`rns` und `lxmf` sind auf die getesteten Versionen festgelegt (1.5.4 und 1.1.1).

## Start

Mit eigenem Fenster:

```
bindu --rns-config ~/.reticulum
```

Nur der Server, zum Öffnen im Browser (Standard `http://127.0.0.1:8765/`):

```
bindu-server --rns-config ~/.reticulum
```

Wichtige Optionen (für beide gleich, Fenster und Server):

| Option | Bedeutung |
| --- | --- |
| `--rns-config ORDNER` | vorhandene Reticulum-Config nutzen, sonst legt bindu eine eigene im Datenordner an |
| `--tcp HOST:PORT` | TCP-Client-Interface in die eigene Config schreiben (wiederholbar) |
| `--data ORDNER` | Datenordner für Identities, Nachrichten und Node-Listen (Standard `~/.bindu`) |
| `--no-announce` | LXMF-Adresse nicht automatisch bekanntgeben |
| `--no-chat` | Chat nicht starten |
| `--allow-host NAME` | zusätzlicher Name im Host-Header, z. B. MagicDNS-Name oder Name hinter einem Reverse-Proxy; mit oder ohne Port (wiederholbar) |
| `--trusted-proxy IP/NETZ` | Reverse-Proxy, dessen `X-Forwarded-For` als Besucheradresse für die Zugriffsprüfung gilt (wiederholbar) |

Alle Optionen: `bindu-server --help`.

Hinweis: Bei Python vor 3.11 nicht aus einem Ordner starten, der einen Unterordner `bindu` enthält (zum Beispiel
`~/build`, wenn das Repo dort als `bindu` liegt). Python findet sonst den Ordner statt des Pakets.

## Sicherheit

Die Oberfläche hat **keine Anmeldung**. Der Server bindet standardmäßig nur an `127.0.0.1`, prüft die Quell-Adresse
(Loopback und Tailnet, weitere Netze mit `--allow-cidr`), nimmt POST nur als JSON an und schickt eine
Content-Security-Policy mit (Skripte nur der Seite selbst, keine Einbettung in fremde Seiten). Der Host-Header wird gegen
DNS-Rebinding geprüft, auch bei anderer Bindung: erlaubt sind IP-Adressen und `localhost`, andere Namen (MagicDNS,
Reverse-Proxy) nur mit `--allow-host`. Wer den Server auf eine andere Adresse legt, sollte ihn nur in einem
vertrauenswürdigen Netz betreiben.

Remote-Verwaltung heißt: Ein Node nimmt Anfragen nur von Identities in seiner Allow-Liste an. Wer sich per
Konfigurationsänderung aus dieser Liste streicht, sperrt sich aus; die Oberfläche warnt davor.

## Hinter einem Reverse-Proxy

bindu hat keine Anmeldung. Hinter einem Reverse-Proxy (zum Beispiel Caddy) sieht der Server nur die Adresse des Proxys,
nicht die des Besuchers. Damit die Zugriffsprüfung weiter schützt, braucht es zwei Angaben:

```
bindu-server --host 100.64.0.10 --port 8766 \
  --allow-host bindu.example.org \
  --trusted-proxy 10.0.0.5
```

- `--allow-host` erlaubt den Namen, unter dem der Proxy bindu erreicht (der Proxy schickt ihn meist ohne Port).
- `--trusted-proxy` nennt den Proxy (eine Adresse oder ein Netz). Nur von ihm beachtet bindu `X-Forwarded-For` und prüft
  die Adresse des Besuchers gegen Loopback, Tailnet und `--allow-cidr`. Ohne diese Option lässt ein Proxy auf demselben
  Rechner (Loopback) jeden Besucher durch.
- Der Proxy muss `X-Forwarded-For` selbst setzen und darf einen vom Besucher mitgeschickten Wert nicht übernehmen
  (Caddy tut das standardmäßig).

Caddy-Beispiel:

```
bindu.example.org {
    reverse_proxy 100.64.0.10:8766
}
```

Wer bindu so über das Internet erreichbar macht, sollte zusätzlich im Proxy eine Anmeldung oder eine Adressfilterung
vorschalten.

## Tests

```
pip install pytest
python3 -m pytest tests
```

Die Node-Tests laufen gegen ein simuliertes Gerät (`tests/fakedevice.py`), nicht gegen Hardware.

## Herkunft

bindu ist aus der Web-App *mikara* herausgelöst: Browser, Chat und Node-Verwaltung. Die Abläufe für Verbindung
und Schema-Abruf sind unverändert übernommen.

## Lizenz

Dieses Projekt steht unter der [Reticulum License](LICENSE).

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

Alle Optionen: `bindu-server --help`.

Hinweis: Bei Python vor 3.11 nicht aus einem Ordner starten, der einen Unterordner `bindu` enthält (zum Beispiel
`~/build`, wenn das Repo dort als `bindu` liegt). Python findet sonst den Ordner statt des Pakets.

## Sicherheit

Die Oberfläche hat **keine Anmeldung**. Der Server bindet standardmäßig nur an `127.0.0.1`, prüft die Quell-Adresse
(Loopback und Tailnet, weitere Netze mit `--allow-cidr`) und den Host-Header, und nimmt POST nur als JSON an. Wer ihn
auf eine andere Adresse legt, sollte ihn nur in einem vertrauenswürdigen Netz betreiben.

Remote-Verwaltung heißt: Ein Node nimmt Anfragen nur von Identities in seiner Allow-Liste an. Wer sich per
Konfigurationsänderung aus dieser Liste streicht, sperrt sich aus; die Oberfläche warnt davor.

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

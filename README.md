# 3MF Splitter

[![Docker Build Verify](https://github.com/Gruudash/3mfsplitter-docker/actions/workflows/docker-verify.yml/badge.svg)](https://github.com/Gruudash/3mfsplitter-docker/actions/workflows/docker-verify.yml)

> ⚠️ **Work in Progress — nur für Testzwecke.**
> Dieses Projekt befindet sich aktiv im Aufbau und ist **nicht** für den
> produktiven Einsatz gedacht. Funktionen können sich jederzeit ändern,
> Fehler sind zu erwarten. Nutzung auf eigene Gefahr, insbesondere ohne
> vorherige Prüfung der exportierten Teile vor dem Druck.

Web-App zum Aufteilen von `.3mf`-Dateien (Bambu Studio, OrcaSlicer,
PrusaSlicer, Creality/Cura) in einzeln druckbare STL-Teile — inklusive
optionaler Steckverbindungen (Magnet, Steg/Zapfen, Schwalbenschwanz) für den
Wiederzusammenbau nach dem Druck.

## Warum

Multi-Material-/Multi-Color-3MF-Dateien lassen sich oft nicht am Stück
drucken (Druckbett-, Filament- oder Detailgrenzen). Automatisches Splitten
allein nach Farbgrenzen führt aber häufig dazu, dass die gedruckten Teile
sich nicht mehr sauber zusammenfügen lassen, weil Farbgrenzen im Modell
nicht immer mit sinnvollen Zusammenbau-Grenzen übereinstimmen. Diese App
lässt dich deshalb **manuell auswählen, wo getrennt wird**, statt blind
jeder Farbgrenze zu folgen.

## Funktionen

- **Datei einlesen**: `.3mf` per Drag & Drop oder Dateiauswahl, 3D-Vorschau
  im Browser (Three.js) — zusammengesetzte und explodierte Ansicht,
  Wireframe, Kamera-Werkzeuge, "Ebene wählen" zum Ausrichten vor dem Split
- **Auswahl treffen**: Kandidaten-Farbregionen per Checkbox abhaken und/oder
  per Freihand-Klick eine beliebige zusammenhängende Fläche als eigene
  Trennstelle markieren — alles nicht Ausgewählte bleibt automatisch ein
  Teil. Wird nichts ausgewählt, wird wie bisher automatisch nach Farbe
  gesplittet
- **Verbindungen**: optional Magnet-Löcher, Steg/Zapfen oder
  Schwalbenschwanz-Verbindungen an den Trennflächen erzeugen
- **Einfärben & Export**: jedem Teil eine Farbe zur Organisation zuweisen
  (nur Label/Dateiname + `colors.json`-Legende in der ZIP — STL selbst
  trägt keine Farbe) und als ZIP mit STL-Dateien herunterladen
- Unterstützt slicer-spezifische Farbcodierungen (Bambu/OrcaSlicer,
  PrusaSlicer, Standard-3MF-Colorgroups) inklusive MMU-Detailbemalung

## Schnellstart (Docker)

```bash
cp .env.example .env    # bei Bedarf anpassen (Port, Upload-Limit, CORS)
docker compose up --build
```

Danach im Browser: **http://localhost:8899** (Standardport, in `.env` über
`HOST_PORT` änderbar).

## Schnellstart (ohne Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Danach im Browser: **http://localhost:8000**

## Konfiguration

Siehe `.env.example`. Wichtigste Variablen:

| Variable | Bedeutung | Standard |
|---|---|---|
| `HOST_PORT` | Port auf dem Host (nur Docker Compose) | `8899` |
| `MAX_UPLOAD_MB` | Maximale Upload-Größe einer `.3mf`-Datei | `200` |
| `CORS_ALLOW_ORIGINS` | Erlaubte Origins, kommagetrennt oder `*` | `*` |

## API (Kurzüberblick)

| Endpunkt | Zweck |
|---|---|
| `GET /api/health` | Health-Check |
| `POST /api/info` | Metadaten/Farben einer `.3mf`-Datei ermitteln, ohne zu splitten |
| `POST /api/split` | Datei splitten (optional mit manueller Auswahl) und ZIP mit STL + `colors.json` zurückgeben |
| `POST /api/debug` | Interne Struktur einer `.3mf`-Datei zu Diagnosezwecken anzeigen |

## Bekannte Einschränkungen

- Feine, rekursiv unterteilte Bambu-Detailbemalung (sehr kleine
  Farbdetails) wird per Mehrheitsentscheid auf die dominante Farbe je
  Dreieck reduziert, nicht pixelgenau nachgebildet
- Getestet primär mit Bambu Studio/OrcaSlicer- und PrusaSlicer-Exporten;
  andere Slicer wurden weniger ausführlich geprüft

## Mitwirken

Issues und PRs willkommen — bitte im Hinterkopf behalten, dass sich API und
Datenformate während der aktiven Entwicklung noch ändern können.

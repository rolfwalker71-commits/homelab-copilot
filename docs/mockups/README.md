# UI-Mockups — Homelab Copilot

## `topology-overview-v1.png`

Zielbild für die **Infrastruktur-Topologie**: Master-Detail statt Expand-in-Grid.

### Problem (vorher)

- Gäste lagen in einem 3-Spalten-Raster.
- Docker wurde **inline** in einer Gast-Karte aufgeklappt → eine riesig hohe, schmale Spalte, Nachbarn leer.
- Große „Stack neu starten“-Buttons und flache Container-Listen zerstörten die Übersicht.

### Konzept (v1)

| Bereich | Inhalt |
|--------|--------|
| **Links** | Kompakte Host-Liste (Nodes → Guests), Suche, ein selektierter Eintrag |
| **Rechts** | Nur der gewählte Host: Kopfzeile (Status, IP, Terminal) + **App-/Compose-Stack-Karten** im Grid |
| **Oben** | Globale Suche / Aktualisieren (im Mockup) |

Docker wird **nicht** mehr in der Übersicht aufgeklappt. Stacks sind Karten (Titel, Status-Punkte, Service-Chips, kompakte Aktionen) — keine 31-Zeilen-Flatliste.

### Umsetzung

Die Live-UI folgt diesem Muster (`topo-shell`: Rail + Detail). Feinschliff (Status-Dots, Filter über Stacks) kann schrittweise nachziehen.

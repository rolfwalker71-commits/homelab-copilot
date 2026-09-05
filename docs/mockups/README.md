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

## `mobile/` — Begleit-App `/mobile`

Phone-first (≈390×844), Material You 3 Expressive, Homelab-Teal + Ochre. Default-Look ist **Dunkel**.

| Datei | Screen |
|--------|--------|
| `01-lage-dark.png` / `01-lage-light.png` | Lage (Zähler) |
| `02-hosts.png` | Host-Liste |
| `03-host-sheet.png` | Host-Sheet (Power, Updates, Desktop) |
| `04-hinweise.png` | Nur Störungen |
| `05-sichern.png` | Letzter Lauf + Backup starten |
| `06-confirm-backup.png` | Confirm Backup |
| `07-confirm-patch.png` | Confirm Einspielen |
| `08-confirm-power.png` | Confirm Guest-Power |
| `09-mehr.png` | Theme, Desktop, Logout |

Nicht im Scope der Begleit-App: Release-Upgrade, Wipe, Browse, Planner, Terminal, Inventar/Doku, volle Topologie-Rail.

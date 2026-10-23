# Humanize Website Content

Dieser Skill schreibt und überarbeitet deutschsprachige Website-Texte im Kontext der jeweiligen Seite. Er macht Sprache konkreter und natürlicher, erhält Fakten und technische Details und berücksichtigt SEO bei entsprechendem Auftrag.

## Installation

Installiere den vollständigen Ordner `humanize-website-content/` in einem Skill-Verzeichnis deines Agenten, etwa `<repository>/.agents/skills/` oder `~/.agents/skills/`. Weitere Installationswege stehen in der [Repository-README](../README.md#installation).

Der Textcheck benötigt Python 3.10 oder neuer. Zusätzliche Pakete, Zugangsdaten und Netzwerkzugriff sind nicht nötig. Der Skill selbst bleibt auch ohne Python nutzbar; melde dann den ausgelassenen Textcheck.

## Beispielaufrufe

```text
$humanize-website-content Überarbeite die Texte der Leistungsseite für Private AI. Erhalte alle technischen Aussagen und die bestehende Sie-Ansprache.
```

```text
$humanize-website-content Prüfe die Website-Texte in src/pages auf Floskeln und überarbeite sie im jeweiligen Seitenkontext.
```

```text
$humanize-website-content Erstelle auf Grundlage dieser Produktinformationen eine H1, Einleitung, Leistungsabschnitte, einen CTA, einen SEO-Title und eine Meta Description.
```

Der Skill fragt nach, wenn fehlende Informationen die Aussagen verändern würden. Bei begrenztem Kontext überarbeitet er konservativ. Er bearbeitet beauftragte Inhalte direkt und erhält Struktur, Logik, Platzhalter, Links und Tracking-Attribute.

## Aufbau

```text
humanize-website-content/
  SKILL.md                           # Workflow, Schreibregeln und Abschlusskriterien
  agents/openai.yaml                 # Codex-Anzeigename und Beispielaufruf
  references/human-writing-rules.md  # Stilbeispiele bei austauschbaren Texten
  references/seo.md                  # Detailregeln bei SEO-Aufträgen
  scripts/check_copy.py              # Statischer Textcheck ohne Schreibzugriff
  tests/test_check_copy.py           # Offline-Regressionstests
```

## Textcheck

Führe den Prüfer per vollständigem Skriptpfad aus dem Arbeitsverzeichnis des Website-Projekts aus. `<skill-dir>` bezeichnet den installierten Skill-Ordner:

```bash
python3 <skill-dir>/scripts/check_copy.py path/to/page.md
python3 <skill-dir>/scripts/check_copy.py src/pages
```

Mehrere Dateien, Verzeichnisse und stdin lassen sich kombinieren:

```bash
python3 <skill-dir>/scripts/check_copy.py page.md components/Hero.tsx
printf '%s\n' 'Wir prüfen Ihre Anforderungen.' | python3 <skill-dir>/scripts/check_copy.py -
```

Der Prüfer liest UTF-8, akzeptiert einen BOM und ändert keine Dateien. Treffer enthalten Dateipfad, Zeilennummer und betroffene Zeile. Er meldet:

- die Zeichen `—` und `–`
- konfigurierte deutsche Floskeln unabhängig von Großschreibung, einschließlich `ß`/`SS`, mit Wortgrenzen und variablen Leerzeichen innerhalb einer Zeile
- mehrere Leerzeichen oder Tabs innerhalb einer Zeile; führende Einrückung zählt nicht als Treffer

Bei Verzeichnissen berücksichtigt er `.md`, `.mdx`, `.txt`, `.html`, `.htm`, `.jsx`, `.tsx`, `.vue`, `.svelte`, `.astro`, `.json`, `.yaml` und `.yml`, auch mit großgeschriebener Endung. Einzeldateien werden unabhängig von ihrer Endung geprüft.

Rekursive Scans überspringen `.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, `build`, `dist`, `.next`, `.nuxt`, `.svelte-kit` und `.astro`. Sie folgen keinen Verzeichnissymlinks. Explizit angegebene Dateien oder Wurzelverzeichnisse werden trotzdem geprüft. Derselbe aufgelöste Dateipfad wird pro Aufruf nur einmal geprüft; die Dateisuche ist sortiert.

| Exitcode | Bedeutung |
| --- | --- |
| `0` | Alle gefundenen Eingaben gelesen, keine definierten Stiltreffer. |
| `1` | Alle gefundenen Eingaben gelesen, mögliche Stilprobleme vorhanden. |
| `2` | Aufruf-, Eingabe-, UTF-8- oder Lesefehler; die Prüfung ist unvollständig. Gilt auch, wenn weder stdin noch unterstützte Dateien geprüft werden konnten. |

Fehler stehen auf stderr; mögliche Stiltreffer und die Zusammenfassung auf stdout. Bei gemischten Eingaben werden lesbare Dateien weiter geprüft. Fehler haben Vorrang: Auch bei vorhandenen Stiltreffern endet ein unvollständiger Check mit `2`.

### Grenzen

Der Prüfer untersucht Rohtext zeilenweise. Er parst weder HTML noch JSX, JSON oder Markdown und unterscheidet Prosa nicht von Code, Kommentaren, Attributen oder Zitaten. Über mehrere Zeilen oder Tags verteilte Floskeln werden nicht erkannt; Unicode-Escapes und HTML-Entities werden nicht dekodiert. Gedankenstriche in Zahlenbereichen oder geschützten Namen werden ebenfalls gemeldet. Prüfe Treffer redaktionell, statt sie automatisch zu ersetzen.

Ein Treffer beweist keinen KI-Ursprung. Exitcode `0` bestätigt weder inhaltliche Richtigkeit noch natürlichen Stil oder gültige Quellcodesyntax. Nutze zusätzlich die relevanten Projektprüfungen.

## Entwicklung und Tests

Vom Skill-Verzeichnis aus:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
```

Die Tests prüfen deutsche Großschreibung und Wortgrenzen, Gedankenstriche, Einrückung, stdin, Exitcodes, Lese- und Verzeichnisfehler, Dateifilter, sortierte Suche, doppelte Pfade und Symlinks. Sie laufen offline mit der Python-Standardbibliothek. Der gemeinsame [CI-Workflow](../.github/workflows/skill-tests.yml) führt sie mit Python 3.10, 3.12 und 3.13 aus.

## Lizenz

MIT, siehe [LICENSE](../LICENSE).

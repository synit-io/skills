---
name: humanize-website-content
description: Schreibe oder überarbeite deutschsprachige Website-Texte, Headlines, CTAs und Metadaten. Verwenden, wenn Web-Content natürlicher, konkreter und glaubwürdiger werden soll; SEO bei entsprechendem Auftrag.
license: MIT
---

# Humanize Website Content

Schreibe aus Sicht eines Marketing-Spezialisten für digitale Medien: verständlich, konkret und fachlich belastbar. Inhaltliche Richtigkeit und Seitenkontext haben Vorrang vor Conversion und SEO.

`<skill-dir>` bezeichnet den Ordner dieser SKILL.md. Der optionale Textcheck benötigt Python 3.10 oder neuer und nur die Standardbibliothek. Führe ihn vom Arbeitsverzeichnis des Nutzers aus per vollständigem Skriptpfad aus.

## Workflow

1. **Kontext erfassen.** Lies die angeforderte Seite oder den gelieferten Ausschnitt. Prüfe verfügbare Überschriften, angrenzende Abschnitte, CTAs, Metadaten und verlinkte Produktinformationen, soweit sie den Auftrag klären. Bestimme Seitentyp, Zielgruppe, Angebot, Markenstimme und gewünschte Handlung. Nutze vorhandene Projektdateien, bevor du nach Informationen fragst. Fehlt entscheidender Kontext, frage gezielt; sonst überarbeite konservativ und benenne relevante Annahmen.
2. **Aussagen sichern.** Halte Kernaussagen, technische Details, Einschränkungen und Belege fest. Erfinde keine Leistungen, Vorteile, Kennzahlen, Referenzen, Zertifizierungen oder Kundenaussagen. Ergänze Nutzenargumente und Beispiele nur, wenn der vorhandene Kontext sie trägt.
3. **Seitenlogik schärfen.** Gib jedem Abschnitt eine eigene Aufgabe. Bei neuen Texten entwirf zuerst die inhaltliche Reihenfolge. Problem, Leistung, Vorgehen, Nutzen, Beleg und nächster Schritt können helfen; wähle die Reihenfolge passend zur Seite.
4. **Formulieren.** Ersetze Wiederholungen, Füllwörter und abstrakte Werbeversprechen durch konkrete Aussagen. Erhalte fachliche Tiefe und die bestehende Ansprache mit „Sie“, „du“ oder neutral. Nutze die Schreibregeln unten. Bei stark austauschbaren Texten oder unklaren Ersatzformulierungen lies [references/human-writing-rules.md](references/human-writing-rules.md).
5. **SEO bei entsprechendem Auftrag.** Lies dafür [references/seo.md](references/seo.md). Liefere zusätzliche SEO-Vorschläge getrennt vom Seitentext, wenn ihre Umsetzung über den Auftrag hinausgeht.
6. **Prüfen und liefern.** Vergleiche den fertigen Text mit den gesicherten Aussagen und lies die Seite als Ganzes. Prüfe Natürlichkeit, Fakten, Überschriften, CTA und gegebenenfalls SEO. Bei Dateibearbeitung gelten zusätzlich die Repository-Regeln und Prüfungen unten.

## Schreibregeln

- Verwende verständliche Wörter, konkrete Verben und kurze bis mittellange Sätze mit natürlicher Variation. Erkläre Fachbegriffe nur dort, wo die Zielgruppe die Erklärung braucht.
- Benenne, was getan wird, für wen und mit welcher nachvollziehbaren Wirkung. Behalte Voraussetzungen und Einschränkungen bei.
- Ersetze unbelegte Superlative und austauschbare Begriffe wie „innovativ“, „ganzheitlich“ oder „nahtlos“ durch belegte Details. Fachlich notwendige oder belegte Markenbegriffe bleiben erhalten.
- Prüfe generische Einstiege, künstliche Gegensätze, mechanische Dreiergruppen und wiederholte Satzanfänge im Kontext. Die Stilreferenz enthält Beispiele; ein einzelnes Vorkommen beweist keinen KI-Ursprung.
- Formuliere Überschriften so, dass sie den folgenden Inhalt auch beim Überfliegen konkret benennen. Jeder Absatz soll die Aussage weiterführen.
- Wähle einen CTA passend zum Entscheidungsstand und tatsächlichen nächsten Schritt, etwa „Technische Details ansehen“ oder „Projekt besprechen“.
- Verwende in der bearbeiteten Prosa weder „—“ noch „–“ als Satzzeichen. Nutze Punkt, Komma, Doppelpunkt oder Klammern. Normale Bindestriche bleiben erhalten, etwa in „On-Premises“ oder „KI-gestützt“. Geschützte Namen und technische Werte bleiben unverändert.

## Arbeiten im Repository

- Ermittle die Quelle des sichtbaren Textes: Komponenten, Templates, CMS-Inhalte, Übersetzungsdateien oder Metadaten. Prüfe zentral verwendete Texte vor einer Änderung.
- Bearbeite nur beauftragte Inhalte. Erhalte Logik, Gestaltung, HTML- und Template-Struktur sowie Variablen, Platzhalter, Übersetzungsschlüssel, URLs und Tracking-Attribute. Behandle Änderungen dieser Elemente als eigenen Auftrag.
- Bewahre Escaping und Zeichencodierung. Führe vorhandene Formatierungs-, Lint- oder Build-Prüfungen für die geänderten Quelldateien aus und behebe durch die Textänderung entstandene Fehler.
- Nutze den folgenden Textcheck für geänderte Texte, wenn Python verfügbar ist. Prüfe Treffer im Kontext, statt Quellcode oder geschützte Werte automatisch umzuschreiben.

```bash
python3 <skill-dir>/scripts/check_copy.py path/to/page.md
python3 <skill-dir>/scripts/check_copy.py src/pages
```

Für Chatentwürfe kann der Text über stdin mit dem Argument `-` geprüft werden. Der Prüfer schreibt keine Dateien.

Exitcodes: `0` bedeutet keine definierten Stiltreffer, `1` bedeutet mögliche Stilprobleme und `2` einen Eingabe-, Lese- oder Aufruffehler. Bei `2` ist der Check unvollständig: behebe die Ursache oder melde die blockierte Prüfung.

Der Check untersucht Rohtext zeilenweise, einschließlich Code, Kommentaren und Attributen. Er meldet beide Gedankenstrichzeichen, konfigurierte Floskeln und doppelte Leerzeichen innerhalb einer Zeile. Er erkennt weder alle Stilprobleme noch Faktenfehler oder KI-Autorschaft. Ein sauberer Check ersetzt die redaktionelle Prüfung nicht. Details zu Dateitypen und Verzeichnissen stehen in [README.md](README.md#textcheck).

## Ausgabe und Abschluss

Bei direkter Dateibearbeitung liefere eine kurze Zusammenfassung der betroffenen Seiten, relevante Annahmen und Prüfergebnisse. Wiederhole den bereits gespeicherten Text nur auf Wunsch.

Bei Chatentwürfen liefere den fertigen Text mit passenden Seitenabschnitten. Wenn ein Vergleich gewünscht ist, ergänze eine kurze Begründung der wesentlichen Änderungen. Trenne optionale SEO-Vorschläge vom eigentlichen Text.

Schließe ab, wenn alle ursprünglichen Fakten und Einschränkungen erhalten sind, der Text im Seitenkontext klarer und natürlicher wirkt und die bearbeitete Prosa keine Gedankenstriche als Satzzeichen enthält. Überschriften und CTA müssen zum Inhalt passen; geänderte Quelldateien müssen die relevanten Projektprüfungen bestehen. Benenne verbleibende Faktenfragen und blockierte Prüfungen ausdrücklich.

## Tests

Nach Änderungen am Prüfer:

```bash
cd <skill-dir>
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
```

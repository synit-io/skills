#!/usr/bin/env python3
"""Prüft Website-Texte auf typische KI-Floskeln und Gedankenstriche.

Verwendung:
    python3 scripts/check_copy.py path/to/file.md
    python3 scripts/check_copy.py content/
    cat text.txt | python3 scripts/check_copy.py -
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable

TEXT_SUFFIXES = {
    ".md", ".mdx", ".txt", ".html", ".htm", ".jsx", ".tsx",
    ".vue", ".svelte", ".astro", ".json", ".yaml", ".yml"
}

SKIP_DIRECTORIES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    "build", "dist", ".next", ".nuxt", ".svelte-kit", ".astro",
}

PHRASES = [
    "in der heutigen digitalen welt",
    "in einer zunehmend digitalisierten welt",
    "willkommen bei",
    "entdecken sie",
    "tauchen sie ein",
    "mehr als nur",
    "es geht nicht nur",
    "dabei steht",
    "im mittelpunkt",
    "gemeinsam gestalten wir die zukunft",
    "der schlüssel zum erfolg",
    "neue maßstäbe",
    "auf das nächste level",
    "potenziale voll ausschöpfen",
    "wir verstehen, dass",
    "ihre vision, unsere mission",
    "so individuell wie",
    "alles aus einer hand",
    "keine kompromisse",
    "die zukunft beginnt jetzt",
    "maßgeschneiderte lösung",
    "ganzheitlicher ansatz",
    "nahtlose integration",
    "modernste technologie",
]

PHRASE_PATTERNS = [
    (
        phrase,
        re.compile(
            r"(?<!\w)"
            + r"\s+".join(re.escape(word) for word in phrase.casefold().split())
            + r"(?!\w)"
        ),
    )
    for phrase in PHRASES
]

DASH_PATTERN = re.compile(r"[—–]")
MULTISPACE_PATTERN = re.compile(r"[ \t]{2,}")


def iter_files(paths: Iterable[str]) -> Iterable[Path]:
    def raise_walk_error(error: OSError) -> None:
        raise error

    for raw in paths:
        path = Path(raw)
        if path.is_file():
            yield path
        elif path.is_dir():
            for directory, subdirectories, files in os.walk(path, onerror=raise_walk_error):
                subdirectories[:] = sorted(
                    name for name in subdirectories if name not in SKIP_DIRECTORIES
                )
                for name in sorted(files):
                    candidate = Path(directory) / name
                    if candidate.suffix.lower() in TEXT_SUFFIXES:
                        yield candidate
        else:
            raise OSError(f"Pfad nicht gefunden oder keine reguläre Datei: {path}")


def check_text(text: str, label: str) -> list[str]:
    findings: list[str] = []

    for line_no, line in enumerate(text.splitlines(), start=1):
        lowered = line.casefold()

        if DASH_PATTERN.search(line):
            findings.append(
                f"{label}:{line_no}: Gedankenstrich gefunden: {line.strip()}"
            )

        for phrase, pattern in PHRASE_PATTERNS:
            if pattern.search(lowered):
                findings.append(
                    f"{label}:{line_no}: Mögliche KI-Floskel „{phrase}“: {line.strip()}"
                )

        if MULTISPACE_PATTERN.search(line.lstrip(" \t")):
            findings.append(
                f"{label}:{line_no}: Mehrfache Leerzeichen: {line.strip()}"
            )

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prüft Website-Texte auf Gedankenstriche und typische KI-Floskeln."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Dateien oder Verzeichnisse. Verwende '-' für stdin.",
    )
    args = parser.parse_args()

    all_findings: list[str] = []
    errors: list[str] = []
    seen: set[Path] = set()
    checked = 0
    normal_paths = [path for path in args.paths if path != "-"]

    if "-" in args.paths:
        try:
            all_findings.extend(check_text(sys.stdin.read(), "<stdin>"))
            checked += 1
        except (UnicodeError, OSError) as exc:
            errors.append(f"stdin konnte nicht gelesen werden: {exc}")

    for raw in normal_paths:
        try:
            for path in iter_files([raw]):
                try:
                    if not path.is_file():
                        raise OSError("Keine reguläre Datei")
                    resolved = path.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    text = path.read_text(encoding="utf-8-sig")
                except UnicodeError:
                    errors.append(f"Keine UTF-8-Textdatei: {path}")
                    continue
                except (OSError, RuntimeError) as exc:
                    errors.append(f"Datei konnte nicht gelesen werden: {path}: {exc}")
                    continue
                checked += 1
                all_findings.extend(check_text(text, str(path)))
        except OSError as exc:
            errors.append(f"Pfad konnte nicht geprüft werden: {raw}: {exc}")

    if not checked and not errors:
        errors.append("Keine unterstützten Textdateien gefunden.")

    if all_findings:
        print("\n".join(all_findings))
        print(f"\n{len(all_findings)} mögliche Stilprobleme gefunden.")

    if errors:
        for error in errors:
            print(f"FEHLER: {error}", file=sys.stderr)
        return 2

    if all_findings:
        return 1

    print("Keine definierten Stilprobleme gefunden.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

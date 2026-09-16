"""Compteur de frequence de mots (demo AgentWatch : petit programme code sous observation).

Usage : python wordfreq.py <fichier> [--top N] [--min-len L]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

_WORD_RE = re.compile(r"[a-zA-ZÀ-ſ]+(?:'[a-zA-Z]+)?")


def tokenize(text: str, min_len: int = 1) -> list[str]:
    """Mots en minuscules, lettres latines (accents inclus), longueur minimale."""
    return [w.lower() for w in _WORD_RE.findall(text) if len(w) >= min_len]


def top_words(text: str, top: int = 10, min_len: int = 1) -> list[tuple[str, int]]:
    """Les `top` mots les plus frequents, ordre : frequence decroissante puis alphabetique."""
    counts = Counter(tokenize(text, min_len))
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def format_rows(rows: list[tuple[str, int]], as_json: bool = False) -> str:
    """Rendu texte aligne, ou JSON (liste d'objets {word, count})."""
    if as_json:
        return json.dumps([{"word": w, "count": c} for w, c in rows], ensure_ascii=False)
    return "\n".join(f"{count:6d}  {word}" for word, count in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Frequence des mots d'un fichier texte.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--min-len", type=int, default=1)
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    args = parser.parse_args(argv)
    try:
        text = args.file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 1
    print(format_rows(top_words(text, args.top, args.min_len), as_json=args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

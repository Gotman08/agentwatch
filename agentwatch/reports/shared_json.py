"""Export JSON 2.0 optionnel, sans perte, avec structures partagees.

Le JSON 1.1 existant reste inchange. Les references sont reservees a cette
enveloppe versionnee ; meme un parametre utilisateur ressemblant a une reference
est echappe. La reconstruction restitue les valeurs et preuves du rapport 1.1.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Any

VERSION = "2.0"
ENCODING = "agentwatch-shared-json-v1"
REF = "$agentwatch_ref"


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    pool: dict[str, Any] = {}
    forced: set[str] = set()
    encoded_objects: dict[int, Any] = {}
    strings: dict[str, Any] = {}

    def encode(value):
        if isinstance(value, str) and len(value) >= 128:
            if value not in strings:
                key = hashlib.sha256(b"string\0" + value.encode("utf-8")).hexdigest()
                pool.setdefault(key, value)
                strings[value] = {REF: key}
            return strings[value]
        is_container = isinstance(value, (dict, list, tuple))
        if is_container and id(value) in encoded_objects:
            return encoded_objects[id(value)]
        if isinstance(value, dict):
            encoded = {k: encode(v) for k, v in value.items()}
            escape = set(value) == {REF}
        elif isinstance(value, (list, tuple)):
            encoded = [encode(v) for v in value]
            escape = False
        else:
            return value
        body = json.dumps(encoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(body) < 256 and not escape:
            encoded_objects[id(value)] = encoded
            return encoded
        key = hashlib.sha256(b"container\0" + body).hexdigest()
        pool.setdefault(key, encoded)
        if escape:
            forced.add(key)
        result = {REF: key}
        encoded_objects[id(value)] = result
        return result

    root = encode(report)
    # Compter les aretes du graphe partage, pas les copies du document d'origine.
    incoming: Counter = Counter()
    visited = set()

    def count(value):
        if isinstance(value, dict) and set(value) == {REF}:
            key = value[REF]
            incoming[key] += 1
            if key not in visited:
                visited.add(key)
                contents(pool[key], count)
        else:
            contents(value, count)

    def contents(value, visit):
        if isinstance(value, dict):
            for v in value.values():
                visit(v)
        elif isinstance(value, list):
            for v in value:
                visit(v)

    count(root)
    shared = {}

    def finish_contents(value):
        if isinstance(value, dict):
            for k, v in value.items():
                value[k] = finish(v)
        if isinstance(value, list):
            for i, v in enumerate(value):
                value[i] = finish(v)
        return value

    def finish(value):
        if isinstance(value, dict) and set(value) == {REF}:
            key = value[REF]
            if incoming[key] > 1 or key in forced:
                if key not in shared:
                    shared[key] = finish_contents(pool[key])
                return {REF: key}
            return finish_contents(pool[key])
        return finish_contents(value)

    encoded = finish(root)
    # Les fermetures recursives forment des cycles Python. Liberer leurs tables
    # AVANT la serialisation evite de retenir tout le graphe intermediaire.
    pool.clear()
    encoded_objects.clear()
    strings.clear()
    visited.clear()
    incoming.clear()
    return {"report_version": VERSION, "encoding": ENCODING,
            "expanded_report_version": report.get("report_version"),
            "report": encoded, "shared": shared}


def expand_report(document: dict[str, Any]) -> dict[str, Any]:
    """Deplier en rapport classique ; erreur explicite sur version, reference ou cycle invalides."""
    if document.get("report_version") != VERSION or document.get("encoding") != ENCODING:
        raise ValueError("unsupported shared report version or encoding")
    pool = document.get("shared")
    if not isinstance(pool, dict):
        raise ValueError("missing shared table")

    def decode_contents(value, active):
        if isinstance(value, dict):
            return {k: decode(v, active) for k, v in value.items()}
        if isinstance(value, list):
            return [decode(v, active) for v in value]
        return value

    def decode(value, active):
        if isinstance(value, dict) and set(value) == {REF}:
            key = value[REF]
            if not isinstance(key, str) or key not in pool:
                raise ValueError("unresolved shared reference")
            if key in active:
                raise ValueError("cyclic shared reference")
            return decode_contents(pool[key], active | {key})
        return decode_contents(value, active)

    report = decode(document.get("report"), set())
    if not isinstance(report, dict) or report.get("report_version") != document.get("expanded_report_version"):
        raise ValueError("expanded report version mismatch")
    return report

"""Export explicite, masque et en lecture seule des transcripts Claude Code.

Les evenements gardent leurs references; seuls les identifiants ecrits par Claude
et l'appartenance au dossier de session etablissent les liens entre fils.
Les mesures sont dedoublonnees et attribuees par le lecteur de transcripts existant.
Aucun contenu n'est importe dans le stockage AgentWatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO, Any, Iterator

from agentwatch.collector import privacy as P
from agentwatch.collector import transcripts as T
from agentwatch.collector.rollouts import iso_to_ns
from agentwatch.reports.inspect import Exporter, FLAGS

EXPORT_VERSION = "1.0"
_LINK_KEYS = ("uuid", "parentUuid", "agentId", "parentToolUseID", "requestId", "isSidechain")
_META_TYPES = {"file-history-snapshot", "queue-operation", "last-prompt", "custom-title", "agent-name",
               "agent-color", "tag", "pr-link"}


def session_files(cfg: dict[str, Any], session: str) -> list[Path]:
    """Trouve une racine par identifiant/prefixe non ambigu, puis ses sous-agents."""
    if not session or any(c in session for c in "*?[]/\\"):
        raise ValueError("identifiant de session Claude Code vide ou invalide")
    roots = sorted(p for p in T.claude_projects_dir(cfg).glob("*/*.jsonl") if p.stem.startswith(session))
    if not roots:
        raise ValueError(f"aucun transcript Claude Code pour la session {session!r}")
    if len(roots) != 1:
        raise ValueError(f"prefixe ambigu : {len(roots)} transcripts Claude Code")
    main, subs = T.find_transcripts(None, None, cfg, stored_path=str(roots[0]))
    return [main, *subs] if main is not None else []


def _ns(value: Any) -> int | None:
    try:
        return iso_to_ns(value)
    except (ValueError, OverflowError):
        return None


def _in_period(ns: int | None, since: int | None, until: int | None) -> bool:
    return ns is not None and (since is None or ns >= since) and (until is None or ns < until)


def _rows(path: Path) -> Iterator[tuple[dict[str, Any], Any, str | None]]:
    with path.open("rb") as fh:
        for line, raw in enumerate(fh, 1):
            src = {"file": path.name, "path": str(path), "line": line, "ordinal": None, "ts": None, "ns": None}
            if not raw.endswith(b"\n"):
                yield src, None, "incomplete"
                break
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                yield src, None, "invalid_json"
                continue
            if isinstance(obj, dict):
                stamp = obj.get("timestamp")
                src.update(ts=stamp if isinstance(stamp, str) else None, ns=_ns(stamp))
                if stamp is not None and not isinstance(stamp, str):
                    src["invalid_timestamp_type"] = type(stamp).__name__
            yield src, obj, None


def _blocks(obj: dict[str, Any]) -> list[Any]:
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return content if isinstance(content, list) else []


def _scan(path: Path) -> dict[str, Any]:
    """Index de references uniquement; aucun contenu de message conserve."""
    info: dict[str, Any] = {"path": path, "calls": {}, "results": {}, "uuids": {}, "agents": set(),
                            "parent_tools": set(), "complete_lines": set()}
    for src, obj, issue in _rows(path):
        if issue or not isinstance(obj, dict):
            continue
        info["complete_lines"].add(src["line"])
        if isinstance(obj.get("uuid"), str):
            info["uuids"][obj["uuid"]] = src
        if isinstance(obj.get("agentId"), str):
            info["agents"].add(obj["agentId"])
        if isinstance(obj.get("parentToolUseID"), str):
            info["parent_tools"].add(obj["parentToolUseID"])
        for block in _blocks(obj):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                info["calls"].setdefault(block["id"], src)
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                info["results"].setdefault(block["tool_use_id"], src)
    return info


class ClaudeExporter(Exporter):
    """Utilise le bornage, le masquage et les rendus existants pour les deux formats."""

    def _omit_reasoning(self, value: Any) -> Any:
        if isinstance(value, dict):
            kind = value.get("type")
            if kind == "redacted_thinking" or (kind == "thinking" and not self.reasoning):
                return {"type": kind, "omitted": True}
            return {k: self._omit_reasoning(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._omit_reasoning(v) for v in value]
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                nested = json.loads(value)
            except ValueError:
                return value
            filtered = self._omit_reasoning(nested)
            return json.dumps(filtered, ensure_ascii=False) if filtered != nested else value
        return value

    def _mask_tree(self, value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for key, child in value.items():
                name = P.mask_secrets(str(key), self.key)
                # La valeur d'une cle JSON sensible doit aussi passer dans le motif
                # key=value du masque commun (json.dumps interpose des guillemets).
                probe = str(key) + "=agentwatch-mask-probe"
                if child is not None and P.mask_secrets(probe, self.key) != probe:
                    secret = child if isinstance(child, str) else json.dumps(child, ensure_ascii=False, sort_keys=True)
                    # Toute la valeur est sensible, meme numerique ou structuree.
                    if not (isinstance(child, str) and child.startswith("<secret:") and child.endswith(">")):
                        child = "<secret:" + P.short_fingerprint(self.key, secret) + ">"
                out[name] = self._mask_tree(child)
            return out
        if isinstance(value, list):
            return [self._mask_tree(v) for v in value]
        if isinstance(value, str):
            # Les sorties d'outils contiennent parfois elles-memes du JSON.
            if value.lstrip().startswith(("{", "[")):
                try:
                    nested = json.loads(value)
                except ValueError:
                    pass
                else:
                    masked = self._mask_tree(nested)
                    if masked != nested:
                        return json.dumps(masked, ensure_ascii=False)
            return P.mask_secrets(value, self.key)
        return value

    def text(self, value: Any) -> tuple[str, list[str]]:
        filtered = self._omit_reasoning(value)
        masked = self._mask_tree(filtered)
        body, flags = super().text(masked)
        if masked != filtered and not any("[masque]" in f for f in flags):
            flags.insert(0, "[masque] secret(s)")
        if filtered != value:
            flags.append("[omis] raisonnement imbrique")
        return body, flags

    def emit(self, src: dict[str, Any], kind: str, title: str, data: dict[str, Any],
             flags: list[str] | None = None, *, text_fields: tuple[str, ...] = ()) -> None:
        flags = list(flags or [])
        safe: dict[str, Any] = {}
        for key, value in data.items():
            if key in text_fields or isinstance(value, str):
                safe[key], more = self.text(value)
                flags.extend(more)
            else:
                safe[key] = self._mask_tree(value)
        safe_src = self._mask_tree(src)
        title, more = self.text(title)
        flags.extend(more)
        flags = list(dict.fromkeys(flags))
        self.head(safe_src, title, flags)
        for key, value in safe.items():
            if value is not None and value != "":
                self.block(key, value)
        self.record(safe_src, kind, safe, flags)


def _links(obj: dict[str, Any], info: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    data = {k: obj[k] for k in _LINK_KEYS if k in obj}
    parent = obj.get("parentUuid")
    if isinstance(parent, str) and parent:
        data["parent_source"] = info["uuids"].get(parent)
    tool = obj.get("parentToolUseID")
    if isinstance(tool, str) and tool:
        data["parent_tool_sources"] = index["calls"].get(tool, [])
    return data


def _line(ex: ClaudeExporter, obj: Any, src: dict[str, Any], info: dict[str, Any], index: dict[str, Any]) -> None:
    flags = ["[horodatage non fiable] horodatage absent ou non interprete"] if src["ns"] is None else []
    if not isinstance(obj, dict):
        ex.emit(src, "non_compris", "Valeur JSON non objet", {"extrait": obj}, flags + ["[non compris]"], text_fields=("extrait",))
        return
    kind = obj.get("type")
    links = _links(obj, info, index)
    if kind in ("user", "assistant"):
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = msg.get("content")
        base = {**links, "role": kind, "message_id": msg.get("id"), "model": msg.get("model")}
        if isinstance(content, str):
            ex.emit(src, "message", f"Message {kind}", {**base, "text": content}, flags, text_fields=("text",))
        elif isinstance(content, list):
            for number, block in enumerate(content):
                bsrc = {**src, "block": number}
                bkind = block.get("type") if isinstance(block, dict) else None
                if bkind == "text":
                    ex.emit(bsrc, "message", f"Message {kind}", {**base, "text": block.get("text")}, flags, text_fields=("text",))
                elif bkind == "tool_use":
                    cid = block.get("id")
                    lookup = cid if isinstance(cid, str) else None
                    results = info["results"].get(lookup)
                    bflags = flags + (["[absent] resultat absent du transcript"] if results is None else [])
                    if not lookup:
                        bflags += ["[non compris] identifiant d'appel absent ou invalide"]
                    if results and not _in_period(results["ns"], ex.since, ex.until) and (ex.since is not None or ex.until is not None):
                        bflags += ["[omis] resultat hors periode ou sans horodatage exploitable"]
                    ex.emit(bsrc, "appel", f"Appel {block.get('name')}",
                            {**base, "call_id": cid, "name": block.get("name"), "input": block.get("input"),
                             "result_source": results, "attribution": ex.computed.get(lookup)}, bflags, text_fields=("input",))
                elif bkind == "tool_result":
                    cid = block.get("tool_use_id")
                    lookup = cid if isinstance(cid, str) else None
                    call = info["calls"].get(lookup)
                    bflags = flags + (["[absent] appel absent du transcript"] if call is None else [])
                    if not lookup:
                        bflags += ["[non compris] identifiant d'appel absent ou invalide"]
                    if call and not _in_period(call["ns"], ex.since, ex.until) and (ex.since is not None or ex.until is not None):
                        bflags += ["[omis] appel hors periode ou sans horodatage exploitable"]
                    elapsed = (src["ns"] - call["ns"]) / 1_000_000 if call and call["ns"] is not None and src["ns"] is not None else None
                    if elapsed is not None and elapsed < 0:
                        elapsed = None
                        bflags += ["[horodatage non fiable] resultat date avant l'appel"]
                    ex.emit(bsrc, "resultat", f"Resultat {cid} (is_error={block.get('is_error')})",
                            {**base, "call_id": cid, "is_error": block.get("is_error"), "content": block.get("content"),
                             "call_source": call, "elapsed_from_timestamps_ms": elapsed,
                             "attribution": ex.computed.get(lookup)}, bflags, text_fields=("content",))
                elif bkind in ("thinking", "redacted_thinking"):
                    data = {**base, "type": bkind}
                    if ex.reasoning and bkind == "thinking":
                        data["text"] = block.get("thinking")
                        bflags = flags
                    else:
                        bflags = flags + ["[omis] raisonnement brut" if bkind == "thinking" else "[chiffre] raisonnement masque par Claude"]
                    ex.emit(bsrc, "raisonnement", "Raisonnement", data, bflags, text_fields=("text",))
                elif bkind in ("image", "document"):
                    ex.emit(bsrc, "media", f"Contenu {bkind}", {**base, "type": bkind}, flags + ["[omis] donnees media"])
                else:
                    ex.emit(bsrc, "non_compris", f"Bloc de contenu {bkind!r}", {**base, "extrait": block}, flags + ["[non compris]"], text_fields=("extrait",))
        else:
            ex.emit(src, "message", f"Message {kind}", base, flags + ["[absent] contenu absent ou invalide"])
    elif kind == "system" and obj.get("subtype") == "compact_boundary":
        ex.emit(src, "compaction", "Frontiere de compaction", {**links, "metadata": obj.get("compactMetadata"),
                "content": obj.get("content")}, flags, text_fields=("metadata", "content"))
    elif kind == "summary":
        ex.emit(src, "resume", "Resume de session", {**links, "summary": obj.get("summary"), "leafUuid": obj.get("leafUuid")},
                flags, text_fields=("summary",))
    elif kind == "attachment":
        attachment = obj.get("attachment") if isinstance(obj.get("attachment"), dict) else {}
        subtype = attachment.get("type")
        ex.emit(src, "notification" if subtype == "queued_command" else "piece_jointe", f"Piece jointe {subtype}",
                {**links, "attachment_type": subtype, "attachment": attachment, "rendered": obj.get("rendered")},
                flags + ([] if subtype == "queued_command" else ["[non compris] type de piece jointe"]),
                text_fields=("attachment", "rendered"))
    elif kind == "system":
        ex.emit(src, "systeme", f"Evenement systeme {obj.get('subtype')}", {**links, "detail": obj}, flags, text_fields=("detail",))
    elif kind == "progress" or (isinstance(kind, str) and kind in _META_TYPES):
        ex.emit(src, "progression" if kind == "progress" else "metadonnee", f"Evenement {kind}",
                {**links, "detail": obj}, flags, text_fields=("detail",))
    else:
        ex.emit(src, "non_compris", f"Type de ligne {kind!r}", {**links, "extrait": obj},
                flags + ["[non compris]"], text_fields=("extrait",))


def export_session(cfg: dict[str, Any], home: str, session: str, out: IO[str], *, thread: str | None = None,
                   since: int | None = None, until: int | None = None, max_chars: int = 4000, reasoning: bool = False,
                   fmt: str = "markdown", conclusions: bool = True) -> dict[str, Any]:
    """Export du principal et de ses sous-agents; le stockage/hooks ne sont pas requis.

    La periode est [since, until). Une requete est datee a son premier bloc par
    parse_transcript; les evenements sans date restent visibles, mais leurs mesures
    sont exclues des totaux d'une periode et comptabilisees separement.
    """
    if fmt not in ("markdown", "jsonl"):
        raise ValueError("format d'export inconnu")
    if max_chars < 1:
        raise ValueError("max_chars doit etre positif")
    if since is not None and until is not None and since >= until:
        raise ValueError("periode vide ou inversee")
    files = session_files(cfg, session)
    root = files[0].stem
    infos = [_scan(p) for p in files]
    index: dict[str, Any] = {"calls": {}}
    for i, info in enumerate(infos):
        path = info["path"]
        info["thread_id"] = root if i == 0 else next(iter(info["agents"])) if len(info["agents"]) == 1 else path.stem
        for cid, src in info["calls"].items():
            index["calls"].setdefault(cid, []).append(src)
    if thread:
        infos = [i for i in infos if i["thread_id"].startswith(thread) or i["path"].stem.startswith(thread)]
        if len(infos) != 1:
            raise ValueError(f"fil Claude Code introuvable ou prefixe ambigu : {thread!r}")
    ex = ClaudeExporter(out, P.ensure_key(home), max_chars=max_chars, reasoning=reasoning, fmt=fmt, since=since, until=until)
    ex.w("# AgentWatch - export detaille d'une session Claude Code")
    ex.w(f"\n- Session : `{root}` ; export v{EXPORT_VERSION} ; transcripts lus en lecture seule ; texte borne a {max_chars} car.")
    ex.w("- MESURES : usage Claude, dedoublonne par requestId. Entree totale = input + cache_creation + cache_read; "
         "ni facturation ni economie constatee. CALCULE : attribution par appel, pas poids exact du resultat.")
    ex.w("- Liens : identifiants observes et dossier de session; aucun lien deduit des horaires. "
         "Les totaux de plusieurs sessions peuvent recouvrir un historique copie et ne doivent pas etre additionnes sans verification.")
    if since is not None or until is not None:
        ex.w(f"- Periode [{since}, {until}) en ns UTC; requetes datees au premier bloc. "
             "Evenements sans date affiches et signales; mesures sans date exclues du total de periode.")
    summary: dict[str, Any] = {"session": root, "client": "claude-code", "threads": [], "flags": {}, "kinds": {}}
    for info in infos:
        path = info["path"]
        parsed = T.parse_transcript(path)
        complete = info["complete_lines"]
        requests = [r for r in parsed["requests"] if not r.get("first_line") or r["first_line"] in complete]
        dated_unknown = sum(_ns(r.get("timestamp")) is None for r in requests)
        scoped = [r for r in requests if _in_period(_ns(r.get("timestamp")), since, until)] if since is not None or until is not None else requests
        scoped_parsed = {**parsed, "requests": scoped}
        measured = T.totals(scoped_parsed)
        input_parts = [measured.get(k) for k in ("input_tokens", "cache_creation_tokens", "cache_read_tokens")]
        measured["total_input_tokens"] = sum(input_parts) if all(isinstance(v, int) for v in input_parts) else None
        ex.computed = T.attribute_calls(scoped_parsed)
        ex.thread = {"thread_id": info["thread_id"]}
        calls_before, results_before = ex.kinds["appel"], ex.kinds["resultat"]
        ex.w(f"\n## Fil `{info['thread_id']}`\n")
        src = {"file": path.name, "path": str(path), "line": 0, "ordinal": None, "ts": None, "ns": None}
        ex.emit(src, "fil", "Source et liens du fil", {"path": str(path), "session": root,
                "association": "transcript principal" if path == files[0] else "dossier de la session (appel parent non deduit)",
                "agentIds": sorted(info["agents"]), "parentToolUseIDs": sorted(info["parent_tools"])})
        for event_src, obj, issue in _rows(path):
            if issue:
                flag = "[absent] derniere ligne incomplete ignoree" if issue == "incomplete" else "[non compris] ligne JSON invalide"
                ex.emit(event_src, issue, "Lecture du transcript", {}, [flag])
                continue
            ns = event_src["ns"]
            if ns is not None and not _in_period(ns, since, until):
                continue
            _line(ex, obj, event_src, info, index)
        for req in scoped:
            lines = req.get("source_lines") or ([req["first_line"]] if req.get("first_line") else [])
            rsrc = {**src, "line": lines[0] if lines else 0, "ts": req.get("timestamp"), "ns": _ns(req.get("timestamp"))}
            flags = ["[absent] mesure de tokens incomplete"] if any(v is None for v in req["usage"].values()) else []
            ex.emit(rsrc, "requete", "Tokens MESURES de la requete (une fois par requestId)",
                    {**req, "attribution_method": T.METHOD}, flags)
        ex.emit(src, "totaux", "Totaux MESURES du fil dans la periode", {**measured,
                "requests_unknown_timestamp": dated_unknown, "attribution_method": T.METHOD},
                ["[absent] certaines mesures de tokens manquent"] if measured.get("total_tokens") is None else [])
        summary["threads"].append({"thread_id": info["thread_id"], "path": str(path), "model": next((r.get("model") for r in scoped if r.get("model")), None),
                                   "calls": ex.kinds["appel"] - calls_before, "results": ex.kinds["resultat"] - results_before,
                                   "responses": len(scoped), "measured": measured, "requests_unknown_timestamp": dated_unknown})
    if conclusions:
        ex.w("\nConclusions des detecteurs : non executees par cet export des transcripts seuls; aucune conclusion ne filtre les evenements.")
    summary["flags"], summary["kinds"] = dict(ex.flags), dict(ex.kinds)
    ex.w("\n## Recapitulatif des signalements\n")
    for flag in FLAGS:
        ex.w(f"- {flag.strip('[]')} : {summary['flags'].get(flag, 0)}", content=True)
    return summary

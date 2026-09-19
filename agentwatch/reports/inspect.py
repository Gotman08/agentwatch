"""Export lisible d'une session Codex, relu dans ses rollouts a la demande : le detail, secrets masques.

# * AgentWatch ne stocke que des nombres, des identifiants et des empreintes. Pour consulter le DETAIL (consignes,
#   messages, arguments, scripts, resultats, erreurs, durees), `inspect` relit les rollouts eux-memes, en lecture
#   seule, et ecrit un fichier local explicite. Rien de ce detail n'entre dans le stockage d'AgentWatch.
# * Chaque evenement garde sa source : fichier du fil, numero de ligne, ordinal et horodatage ecrits par Codex.
# * Signalements explicites : [masque] secrets remplaces par <secret:empreinte> ; [tronque] texte coupe a
#   --max-chars (ou indice d'une coupe faite par Codex) ; [absent] donnee attendue qui manque ; [non compris] type
#   de ligne ou d'element que l'export ne sait pas interpreter (montre quand meme : champs et extrait masque) ;
#   [omis] contenu laisse de cote par defaut (raisonnement brut, instructions de base, historique remplace) ;
#   [chiffre] contenu illisible par construction ; [horodatage non fiable] fil reecrit d'un bloc.
# * Tokens : MESURES par Codex pour chaque reponse (token_usage_record ; token_count dans les anciens rollouts) ;
#   REPARTITION CALCULEE par AgentWatch pour chaque appel (prorata des tailles de sortie), lue dans son stockage.
# * Les conclusions des detecteurs sont en annexe, separees des evenements : elles ne retirent rien de l'export.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from typing import IO, Any

from agentwatch.collector import privacy as P
from agentwatch.collector import rollouts as R

EXPORT_VERSION = "1.0"
_CODEX_CUT_RE = re.compile(r"(?i)(\.\.\.\s*\[?\d+\s*(?:lines?|lignes?|chars?|caracteres?|tokens?)\s*(?:truncated|omitted|tronque)"
                           r"|output truncated|\btruncated\b\s*(?:output|\d)|<truncated>)")
_BULK_SPAN_NS = 2_000_000_000
_BULK_MIN_LINES = 20
FLAGS = ("[masque]", "[tronque]", "[absent]", "[non compris]", "[omis]", "[chiffre]", "[horodatage non fiable]")


def session_files(cfg: dict[str, Any], session: str) -> list[tuple[str, dict[str, Any]]]:
    """Rollouts d'une session (fil racine et sous-agents), lus dans leur en-tete : principal d'abord, puis par
    profondeur et date de creation. `session` : identifiant complet ou prefixe du fil racine ou d'un fil."""
    out = []
    for path in R.list_rollouts(cfg, None):
        meta = R.read_thread_meta(path) or {}
        tid = str(meta.get("thread_id") or R._thread_id_from_path(path) or "")
        root = str(meta.get("root_id") or tid)
        if root.startswith(session) or tid.startswith(session):
            out.append((path, meta))
    roots = {str(m.get("root_id") or "") for _, m in out}
    if len(roots) > 1:
        raise ValueError(f"prefixe ambigu : {len(roots)} sessions ({', '.join(sorted(r[:13] for r in roots))})")
    out.sort(key=lambda pm: (0 if not pm[1].get("parent_id") else 1, pm[1].get("depth") or 0, os.path.basename(pm[0])))
    return out


def _ts_bounds(path: str) -> tuple[int | None, int | None, int]:
    """(premier, dernier horodatage, nombre approximatif de lignes lues en tete) : fil reecrit d'un bloc ?"""
    first = last = None
    n = 0
    try:
        with open(path, "rb") as fh:
            for i, line in enumerate(fh):
                if i >= 50:
                    break
                n += 1
                try:
                    ns = R.iso_to_ns(json.loads(line).get("timestamp"))
                except ValueError:
                    continue
                if ns and first is None:
                    first = ns
            size = os.path.getsize(path)
            fh.seek(max(0, size - 262_144))
            for line in fh.read().splitlines()[-50:]:
                try:
                    ns = R.iso_to_ns(json.loads(line).get("timestamp"))
                except ValueError:
                    continue
                if ns:
                    last = ns
    except OSError:
        pass
    return first, last, n


def _fence(text: str) -> str:
    run = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, run + 1)


def _content_parts(content: Any) -> tuple[str, list[str]]:
    """Texte d'un contenu Codex (liste d'elements) et mentions des parties non textuelles (images)."""
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    other: list[str] = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("text"), str):
            texts.append(c["text"])
        elif c.get("type") in ("input_image", "image") or "image_url" in c:
            url = c.get("image_url")
            other.append(f"[omis] image ({len(url) if isinstance(url, str) else '?'} car. de donnees, non reproduite)")
        else:
            other.append(f"[non compris] partie de contenu de type {c.get('type')!r}")
    return "\n\n".join(texts), other


class Exporter:
    """Rendu Markdown (ou JSONL) d'une session, fil par fil, en flux."""

    def __init__(self, out: IO[str], key: bytes, *, max_chars: int = 4000, reasoning: bool = False, fmt: str = "markdown",
                 since: int | None = None, until: int | None = None, computed: dict[str, dict[str, Any]] | None = None) -> None:
        self.out, self.key, self.max_chars, self.reasoning, self.fmt = out, key, max_chars, reasoning, fmt
        self.since, self.until = since, until
        self.computed = computed or {}
        self.flags: Counter[str] = Counter()
        self.kinds: Counter[str] = Counter()
        self.call_lines: dict[str, str] = {}
        self.thread: dict[str, Any] = {}

    # ------------------------------------------------------------------ sortie
    def w(self, text: str = "", content: bool = False) -> None:
        """Une ligne de l'export. Les signalements sont comptes sur les lignes descriptives, jamais dans le contenu."""
        if not content:
            for f in FLAGS:
                if f in text:
                    self.flags[f] += text.count(f)
        if self.fmt == "markdown":
            self.out.write(text + "\n")

    def record(self, src: dict[str, Any], kind: str, data: dict[str, Any], flags: list[str]) -> None:
        self.kinds[kind] += 1
        if self.fmt == "jsonl":
            self.out.write(json.dumps({"source": src, "thread": self.thread.get("thread_id"), "kind": kind, "data": data,
                                       "flags": flags}, ensure_ascii=False) + "\n")

    def text(self, value: Any) -> tuple[str, list[str]]:
        """Texte masque puis borne ; drapeaux de ce qui a ete masque ou tronque."""
        if value is None:
            return "", []
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
        flags: list[str] = []
        masked = P.mask_secrets(s, self.key)
        n = masked.count("<secret:") - s.count("<secret:")
        if n > 0:
            flags.append(f"[masque] {n} secret(s)")
        if _CODEX_CUT_RE.search(s):
            flags.append("[tronque] indice d'une coupe faite par Codex dans le texte")
        if len(masked) > self.max_chars:
            flags.append(f"[tronque] {self.max_chars} car. affiches sur {len(masked)}")
            masked = masked[: self.max_chars]
        return masked, flags

    def block(self, label: str, value: Any) -> list[str]:
        """Bloc de texte (clos), avec ses drapeaux ; renvoie les drapeaux."""
        body, flags = self.text(value)
        if not body.strip():
            return flags
        fence = _fence(body)
        self.w(f"  {label}" + (" " + " ".join(flags) if flags else "") + " :")
        self.w(f"  {fence}text")
        for line in body.splitlines() or [""]:
            self.w("  " + line, content=True)
        self.w(f"  {fence}")
        return flags

    def head(self, src: dict[str, Any], title: str, flags: list[str] | None = None) -> None:
        ts = src.get("ts") or "?"
        self.w(f"- `L{src['line']}` {ts[11:23] if len(ts) >= 23 else ts} **{title}**" + (" " + " ".join(flags) if flags else ""))

    # ------------------------------------------------------------------ fil
    def thread_file(self, path: str, meta: dict[str, Any]) -> dict[str, Any]:
        first, last, n = _ts_bounds(path)
        bulk = bool(first and last and last - first < _BULK_SPAN_NS and n >= _BULK_MIN_LINES)
        self.thread = {"thread_id": meta.get("thread_id"), "path": path, "bulk": bulk, "open": {}, "responses": 0,
                       "measured": Counter(), "token_count": Counter(), "computed": Counter(), "calls": 0,
                       "recent": [], "pending_emit": [], "consumed": [], "model": None, "has_usage_record": False}
        who = ("fil principal" if not meta.get("parent_id") else
               f"sous-agent {meta.get('agent_nickname') or '?'} ({meta.get('agent_role') or 'role ?'})")
        self.w(f"## {who} : `{meta.get('thread_id')}`")
        self.w("")
        self.w(f"- Source : `{path}`")
        if meta.get("parent_id"):
            self.w(f"- Parent : `{meta.get('parent_id')}` ; profondeur {meta.get('depth')} ; chemin `{meta.get('agent_path') or '?'}`")
        if bulk:
            self.w("- [horodatage non fiable] toutes les lignes tiennent en moins de 2 s : fil reecrit d'un bloc par Codex ; "
                   "heures et durees reconstruites sans valeur, contenu intact")
        self.w("")
        with open(path, "rb") as fh:
            for lineno, raw in enumerate(fh, 1):
                if not raw.endswith(b"\n"):
                    self.w(f"- `L{lineno}` [absent] derniere ligne incomplete (en cours d'ecriture par Codex) : ignoree")
                    break
                try:
                    obj = json.loads(raw)
                except ValueError:
                    self.w(f"- `L{lineno}` [non compris] ligne JSON invalide ({len(raw)} octets)")
                    continue
                ns = R.iso_to_ns(obj.get("timestamp"))
                if not bulk and ns and ((self.since and ns < self.since) or (self.until and ns >= self.until)):
                    continue
                src = {"file": os.path.basename(path), "line": lineno, "ordinal": obj.get("ordinal"),
                       "ts": obj.get("timestamp"), "ns": ns}
                self.line(obj, src)
        self.thread_end()
        return self.thread

    def thread_end(self) -> None:
        t = self.thread
        for cid, info in t["open"].items():
            self.w(f"- `L{info['line']}` [absent] pas de sortie pour l'appel `{cid}` ({info['name']}) : appel ouvert ou "
                   "sortie hors de la periode exportee")
        m = t["measured"] if t["has_usage_record"] else t["token_count"]
        src_label = "token_usage_record" if t["has_usage_record"] else "token_count (ancien format)"
        self.w("")
        self.w(f"Totaux du fil : {t['calls']} appel(s) ; {t['responses']} reponse(s) du modele.")
        if m:
            self.w(f"- Tokens MESURES par Codex ({src_label}, une fois par reponse) : entree {m['input_tokens']} dont cache "
                   f"{m['cached_input_tokens']} (hors cache {m['input_tokens'] - m['cached_input_tokens']}), sortie "
                   f"{m['output_tokens']} dont raisonnement {m['reasoning_output_tokens']}.")
        else:
            self.w("- [absent] aucun releve de tokens dans ce fil.")
        c = t["computed"]
        if c:
            self.w(f"- Repartition CALCULEE par AgentWatch (somme des parts attribuees aux appels) : entree hors cache "
                   f"{c['uncached_input_tokens']}, sortie {c['output_tokens']} ; une part, pas une mesure : prorata des tailles "
                   "de sortie consommees ensemble, et de la sortie de la reponse emettrice entre ses appels.")
        self.w("")

    # ------------------------------------------------------------------ lignes
    def line(self, obj: dict[str, Any], src: dict[str, Any]) -> None:
        t = obj.get("type")
        p = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        handler = {
            "session_meta": self._session_meta, "turn_context": self._turn_context, "event_msg": self._event_msg,
            "response_item": self._response_item, "token_usage_record": self._usage_record, "compacted": self._compacted,
            "world_state": self._world_state, "inter_agent_communication_metadata": self._inter_agent_meta,
            "realtime_item": self._realtime,
        }.get(str(t))
        if handler is None:
            self._unknown(src, f"ligne de type {t!r}", p)
            return
        handler(p, src)

    def _unknown(self, src: dict[str, Any], what: str, payload: Any) -> None:
        keys = sorted(payload)[:20] if isinstance(payload, dict) else []
        self.head(src, f"{what}", ["[non compris]"])
        self.w(f"  champs : {', '.join(keys) or '-'}")
        self.block("extrait masque", payload)
        self.record(src, "non_compris", {"what": what, "keys": keys}, ["[non compris]"])

    def _session_meta(self, p: dict[str, Any], src: dict[str, Any]) -> None:
        if p.get("id") and self.thread.get("thread_id") and p.get("id") != self.thread["thread_id"]:
            self.head(src, "copie du session_meta du parent (ignoree : le sous-agent recopie l'historique de son parent)")
            self.record(src, "session_meta_copie", {"id": p.get("id")}, [])
            return
        base = p.get("base_instructions")
        btxt = base.get("text") if isinstance(base, dict) else base
        flags = [f"[omis] instructions de base de Codex ({len(btxt)} car.)"] if isinstance(btxt, str) else []
        self.head(src, f"Debut du fil (Codex {p.get('cli_version') or '?'}, origine {p.get('originator') or '?'}, "
                       f"source {p.get('thread_source') or '?'}, dossier `{R.clean_path(p.get('cwd')) or '?'}`)", flags)
        self.record(src, "session_meta", {"cli_version": p.get("cli_version"), "cwd": R.clean_path(p.get("cwd"))}, flags)

    def _turn_context(self, p: dict[str, Any], src: dict[str, Any]) -> None:
        self.thread["model"] = p.get("model") or self.thread.get("model")
        self.head(src, f"Contexte du tour `{p.get('turn_id') or '?'}` : modele `{p.get('model') or '?'}`, effort "
                       f"{p.get('effort') or '?'}, dossier `{R.clean_path(p.get('cwd')) or '?'}`")
        self.record(src, "turn_context", {k: p.get(k) for k in ("turn_id", "model", "effort")}, [])

    def _world_state(self, p: dict[str, Any], src: dict[str, Any]) -> None:
        state = p.get("state") if isinstance(p.get("state"), dict) else {}
        agents_md = (state.get("agents_md") or {}).get("text") if isinstance(state.get("agents_md"), dict) else None
        skills = (state.get("host_skills") or {}).get("body") if isinstance(state.get("host_skills"), dict) else None
        self.head(src, "Contexte injecte par Codex (AGENTS.md, skills)")
        flags = self.block(f"AGENTS.md ({len(agents_md)} car.)", agents_md) if isinstance(agents_md, str) else []
        if isinstance(skills, str):
            flags += self.block(f"skills ({len(skills)} car.)", skills)
        other = sorted(k for k in state if k not in ("agents_md", "host_skills"))
        if other:
            self.w(f"  autres parties de l'etat : {', '.join(other)} (non detaillees)")
        self.record(src, "world_state", {"agents_md_chars": len(agents_md) if isinstance(agents_md, str) else None}, flags)

    def _event_msg(self, p: dict[str, Any], src: dict[str, Any]) -> None:
        pt = p.get("type")
        if pt == "task_started":
            self.head(src, f"Tour demarre `{p.get('turn_id')}` (fenetre de contexte {p.get('model_context_window')})")
            self.record(src, "tour_debut", {"turn_id": p.get("turn_id")}, [])
        elif pt == "task_complete":
            self.head(src, f"Tour termine `{p.get('turn_id')}` : duree {p.get('duration_ms')} ms (mesuree par Codex), "
                           f"premier token {p.get('time_to_first_token_ms')} ms")
            self.record(src, "tour_fin", {"turn_id": p.get("turn_id"), "duration_ms": p.get("duration_ms")}, [])
        elif pt == "turn_aborted":
            self.head(src, f"Tour interrompu `{p.get('turn_id')}` : raison {p.get('reason')!r}, duree {p.get('duration_ms')} ms")
            self.record(src, "tour_interrompu", {"turn_id": p.get("turn_id"), "reason": p.get("reason")}, [])
        elif pt == "thread_settings_applied":
            s = p.get("thread_settings") if isinstance(p.get("thread_settings"), dict) else {}
            self.thread["model"] = s.get("model") or self.thread.get("model")
            self.head(src, f"Reglages du fil : modele `{s.get('model')}` ({s.get('model_provider_id')}), niveau "
                           f"{s.get('service_tier')}, effort {s.get('reasoning_effort')}, approbation {s.get('approval_policy')}, "
                           f"personnalite {s.get('personality')}")
            self.record(src, "reglages", {k: s.get(k) for k in ("model", "model_provider_id", "reasoning_effort", "approval_policy")}, [])
        elif pt == "token_count":
            self._token_count(p, src)
        elif pt == "item_completed":
            item = p.get("item") if isinstance(p.get("item"), dict) else {}
            self._item(item, p, src)
        else:
            self._unknown(src, f"evenement {pt!r}", p)

    # ------------------------------------------------------------------ elements d'actions
    def _computed_line(self, cid: str) -> None:
        u = self.computed.get(cid)
        if not u:
            return
        self.thread["computed"]["uncached_input_tokens"] += int(u.get("uncached_input_tokens") or 0)
        self.thread["computed"]["output_tokens"] += int(u.get("output_tokens") or 0)
        self.w(f"  tokens (repartition CALCULEE par AgentWatch, pas une mesure) : part d'entree hors cache "
               f"{u.get('uncached_input_tokens')} dans la reponse `{u.get('consumer_request_id')}`, part de sortie "
               f"{u.get('output_tokens')} de la reponse emettrice `{u.get('emitter_request_id')}`")

    def _duration(self, item: dict[str, Any], p: dict[str, Any]) -> str:
        d = R._duration_ms(item.get("duration"))
        if d is not None:
            return f"{d} ms (mesuree par Codex)"
        a, b = p.get("started_at_ms"), p.get("completed_at_ms")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return f"{int(b - a)} ms (debut et fin notes par Codex)" + (" [horodatage non fiable]" if self.thread["bulk"] else "")
        return "[absent] duree"

    def _item(self, item: dict[str, Any], p: dict[str, Any], src: dict[str, Any]) -> None:
        ty = item.get("type")
        iid = str(item.get("id") or "?")
        if ty == "CommandExecution":
            self.thread["calls"] += 1
            self.call_lines[iid] = f"{self.thread['thread_id']}:L{src['line']}"
            cmd = item.get("command")
            script = cmd[-1] if isinstance(cmd, list) and cmd else cmd
            code = item.get("exit_code")
            flags = [] if isinstance(code, int) else ["[absent] code de sortie"]
            self.head(src, f"Commande `{iid}` : {item.get('status')}, code de sortie {code}, {self._duration(item, p)}", flags)
            self.w(f"  dossier : `{R.clean_path(item.get('cwd')) or '?'}` ; interpreteur : "
                   f"`{os.path.basename(str(cmd[0])) if isinstance(cmd, list) and cmd else '?'}`")
            flags += self.block("script", script)
            flags += self.block("sortie", item.get("aggregated_output") if item.get("aggregated_output") is not None
                                else item.get("stdout"))
            self._computed_line(iid)
            self.record(src, "commande", {"id": iid, "exit_code": code, "status": item.get("status")}, flags)
        elif ty == "McpToolCall":
            self.thread["calls"] += 1
            self.call_lines[iid] = f"{self.thread['thread_id']}:L{src['line']}"
            self.thread["open"].pop(iid, None)
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            err = item.get("error")
            self.head(src, f"Outil MCP `{item.get('server')}/{item.get('tool')}` `{iid}` : {item.get('status')}, "
                           f"isError={result.get('isError')}, {self._duration(item, p)}")
            flags = self.block("arguments", item.get("arguments"))
            text, other = _content_parts(result.get("content"))
            flags += self.block("resultat", text)
            for o in other:
                self.w(f"  {o}")
            if result.get("structuredContent") is not None and not text:
                flags += self.block("resultat structure", result.get("structuredContent"))
            if err:
                flags += self.block("erreur", err)
            if not text and not err and result.get("structuredContent") is None:
                flags.append("[absent] resultat")
                self.w("  [absent] aucun resultat dans l'element")
            self._computed_line(iid)
            self.record(src, "mcp", {"id": iid, "server": item.get("server"), "tool": item.get("tool")}, flags)
        elif ty == "FileChange":
            self.thread["calls"] += 1
            self.call_lines[iid] = f"{self.thread['thread_id']}:L{src['line']}"
            changes = item.get("changes") if isinstance(item.get("changes"), dict) else {}
            self.head(src, f"Modification de fichiers `{iid}` : {item.get('status')}, {len(changes)} fichier(s)")
            flags: list[str] = []
            for path, ch in list(changes.items())[:50]:
                kind = ch.get("type") if isinstance(ch, dict) else "?"
                flags += self.block(f"{kind} `{R.clean_path(path)}`", (ch or {}).get("unified_diff") if isinstance(ch, dict) else None)
            self.record(src, "fichiers", {"id": iid, "files": len(changes)}, flags)
        elif ty in ("ImageView",):
            self.thread["calls"] += 1
            self.head(src, f"Image vue `{iid}` : `{R.clean_path(item.get('path'))}`")
            self.record(src, "image", {"id": iid}, [])
        elif ty in ("WebSearch", "Extension"):
            self.thread["calls"] += 1
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            self.head(src, f"Recherche web `{iid}` ({ty}) : requete {item.get('query') or action.get('query')!r}")
            flags = self.block("resultats", item.get("results")) if item.get("results") is not None else []
            self.record(src, "recherche_web", {"id": iid}, flags)
        elif ty == "SubAgentActivity":
            self.head(src, f"Sous-agent `{item.get('agent_thread_id')}` : {item.get('kind')} (chemin `{item.get('agent_path') or '?'}`)")
            self.record(src, "sous_agent", {"child": item.get("agent_thread_id"), "kind": item.get("kind")}, [])
        elif ty == "CollabAgentToolCall":
            self.head(src, f"Collaboration `{item.get('tool')}` : {item.get('status')}, de `{item.get('sender_thread_id')}` vers "
                           f"{', '.join('`' + str(x) + '`' for x in (item.get('receiver_thread_ids') or [])) or '-'}")
            if item.get("agents_states"):
                self.block("etats des agents", item.get("agents_states"))
            self.record(src, "collaboration", {"tool": item.get("tool"), "receivers": item.get("receiver_thread_ids")}, [])
        elif ty in ("AgentMessage", "UserMessage"):
            text, other = _content_parts(item.get("content"))
            if self._seen(text):
                self.head(src, f"Element {ty} : meme texte qu'un message deja affiche (doublon ecrit par Codex)")
                self.record(src, "doublon", {"type": ty}, [])
                return
            self.head(src, f"Element {ty}")
            flags = self.block("texte", text)
            self.record(src, "message_element", {"type": ty}, flags)
        elif ty == "Reasoning":
            summary = "\n".join(str(s.get("text") if isinstance(s, dict) else s) for s in item.get("summary_text") or [])
            raw = item.get("raw_content") or []
            raw_text = "\n".join(str(r.get("text") if isinstance(r, dict) else r) for r in raw)
            flags = [] if self.reasoning or not raw_text else [f"[omis] raisonnement brut ({len(raw_text)} car. ; --reasoning pour l'inclure)"]
            self.head(src, "Raisonnement (element)", flags)
            flags += self.block("resume", summary) if summary else []
            if self.reasoning and raw_text:
                flags += self.block("raisonnement brut", raw_text)
            self.record(src, "raisonnement", {"summary_chars": len(summary), "raw_chars": len(raw_text)}, flags)
        elif ty == "ContextCompaction":
            self.head(src, f"Element de compaction `{iid}`")
            self.record(src, "compaction_element", {"id": iid}, [])
        elif ty == "FunctionCallOutput":
            text = item.get("output")
            if self._seen(text if isinstance(text, str) else json.dumps(text)):
                self.head(src, f"Element FunctionCallOutput `{item.get('name')}` : meme sortie que celle deja affichee (doublon)")
                self.record(src, "doublon", {"type": ty}, [])
                return
            self.head(src, f"Element FunctionCallOutput `{item.get('namespace')}.{item.get('name')}`")
            flags = self.block("sortie", text)
            self.record(src, "sortie_fonction_element", {"name": item.get("name")}, flags)
        else:
            self._unknown(src, f"element {ty!r}", item)

    def _seen(self, text: str | None) -> bool:
        if not text:
            return False
        h = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        recent = self.thread["recent"]
        if h in recent:
            return True
        recent.append(h)
        del recent[:-40]
        return False

    # ------------------------------------------------------------------ elements de reponse
    def _response_item(self, p: dict[str, Any], src: dict[str, Any]) -> None:
        pt = p.get("type")
        if pt == "message":
            role = p.get("role")
            if role == "user":
                human, injected = R.split_injected(p.get("content"))
                text = "\n\n".join(human)
            else:
                text, _ = _content_parts(p.get("content"))
                injected = []
            self._seen(text)
            label = {"user": "Message de l'utilisateur", "assistant": "Message de l'agent", "developer": "Message developpeur",
                     "system": "Message systeme"}.get(str(role), f"Message ({role})")
            self.head(src, f"{label}" + (f" ({p.get('phase')})" if p.get("phase") else ""))
            flags = self.block("texte", text)
            for b in injected:
                self.w(f"  [omis] bloc injecte par Codex ({len(b)} car. ; AGENTS.md ou contexte d'environnement, "
                       "voir la ligne world_state)")
                flags.append("[omis] bloc injecte")
            if not text.strip() and not injected:
                self.w("  [absent] texte vide")
                flags.append("[absent] texte")
            self.record(src, "message", {"role": role, "phase": p.get("phase"), "chars": len(text)}, flags)
        elif pt == "agent_message":
            text, _ = _content_parts(p.get("content"))
            self._seen(text)
            self.head(src, f"Message entre agents : de `{p.get('author')}` a `{p.get('recipient')}`")
            flags = self.block("texte", text)
            self.record(src, "message_agents", {"author": p.get("author"), "recipient": p.get("recipient")}, flags)
        elif pt == "reasoning":
            summary = "\n".join(str(s.get("text") if isinstance(s, dict) else s) for s in p.get("summary") or [])
            enc = p.get("encrypted_content")
            flags = [f"[chiffre] {len(enc)} car. illisibles"] if isinstance(enc, str) and enc else []
            self.head(src, "Raisonnement", flags)
            flags += self.block("resume", summary) if summary else []
            self.record(src, "raisonnement", {"summary_chars": len(summary)}, flags)
        elif pt == "custom_tool_call":
            cid = str(p.get("call_id") or "?")
            self.thread["open"][cid] = {"line": src["line"], "name": p.get("name"), "ns": src.get("ns")}
            self.thread["pending_emit"].append(cid)
            self.head(src, f"Appel `{p.get('name')}` `{cid}` (l'agent ecrit un script)")
            flags = self.block("script", p.get("input"))
            self.record(src, "appel_script", {"call_id": cid, "name": p.get("name")}, flags)
        elif pt == "custom_tool_call_output":
            self._output(p, src, "Sortie du script")
        elif pt == "function_call":
            cid = str(p.get("call_id") or "?")
            self.thread["open"][cid] = {"line": src["line"], "name": p.get("name"), "ns": src.get("ns")}
            self.thread["pending_emit"].append(cid)
            self.call_lines[cid] = f"{self.thread['thread_id']}:L{src['line']}"
            name = (f"{p.get('namespace')}.{p.get('name')}" if p.get("namespace") and not str(p.get("namespace")).startswith("mcp__")
                    else f"{p.get('namespace') or ''}{'__' if p.get('namespace') else ''}{p.get('name')}")
            self.head(src, f"Appel de fonction `{name}` `{cid}`")
            try:
                args = json.loads(p.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = p.get("arguments")
            flags = self.block("arguments", args)
            self.thread["calls"] += 1
            self.record(src, "appel_fonction", {"call_id": cid, "name": name}, flags)
        elif pt == "function_call_output":
            self._output(p, src, "Sortie de la fonction")
        elif pt == "tool_search_call":
            self.thread["calls"] += 1
            self.head(src, f"Recherche d'outils `{p.get('call_id')}` : requete {((p.get('arguments') or {}).get('query'))!r}")
            self.record(src, "recherche_outils", {"call_id": p.get("call_id")}, [])
        elif pt == "tool_search_output":
            tools = p.get("tools") or []
            names = [str(t.get("name") if isinstance(t, dict) else t) for t in tools][:30]
            self.head(src, f"Outils trouves `{p.get('call_id')}` : {len(tools)} ({', '.join(names)})")
            self.record(src, "recherche_outils_resultat", {"call_id": p.get("call_id"), "tools": len(tools)}, [])
        elif pt == "web_search_call":
            self.thread["calls"] += 1
            action = p.get("action") if isinstance(p.get("action"), dict) else {}
            self.head(src, f"Recherche web : {action.get('query') or action.get('queries')!r} ({p.get('status')})")
            self.record(src, "recherche_web", {"status": p.get("status")}, [])
        elif pt == "image_generation_call":
            self.thread["calls"] += 1
            res = p.get("result")
            flags = [f"[omis] image generee ({len(res)} car. de donnees)"] if isinstance(res, str) else []
            self.head(src, f"Generation d'image ({p.get('status')})", flags)
            flags += self.block("consigne revisee", p.get("revised_prompt"))
            self.record(src, "image_generee", {}, flags)
        else:
            self._unknown(src, f"element de reponse {pt!r}", p)

    def _output(self, p: dict[str, Any], src: dict[str, Any], label: str) -> None:
        cid = str(p.get("call_id") or "?")
        info = self.thread["open"].pop(cid, None)
        self.thread["consumed"].append(cid)
        dur = ""
        if info and info.get("ns") and src.get("ns"):
            dur = f" ; {(src['ns'] - info['ns']) // 1_000_000} ms depuis l'appel (horodatages)"
            dur += " [horodatage non fiable]" if self.thread["bulk"] else ""
        out = p.get("output")
        text, other = _content_parts(out) if not isinstance(out, str) else (out, [])
        self.head(src, f"{label} `{cid}`" + dur + ("" if info else " [absent] appel correspondant hors de la periode ou du fil"))
        flags = self.block("sortie", text)
        for o in other:
            self.w(f"  {o}")
        if not text.strip() and not other:
            self.w("  [absent] sortie vide")
        self._computed_line(cid)
        self.record(src, "sortie", {"call_id": cid}, flags)

    # ------------------------------------------------------------------ tokens
    def _usage_record(self, p: dict[str, Any], src: dict[str, Any]) -> None:
        u = R._usage_numbers(p.get("usage"))
        t = self.thread
        t["has_usage_record"] = True
        t["responses"] += 1
        for k, v in u.items():
            t["measured"][k] += v
        emitted, consumed = t["pending_emit"], t["consumed"]
        self.head(src, f"Reponse {t['responses']} `{p.get('response_id')}` : tokens MESURES par Codex : entree {u['input_tokens']} "
                       f"dont cache {u['cached_input_tokens']} (hors cache {u['input_tokens'] - u['cached_input_tokens']}), sortie "
                       f"{u['output_tokens']} dont raisonnement {u['reasoning_output_tokens']}")
        if consumed or emitted:
            self.w(f"  consomme les sorties de : {', '.join('`' + c + '`' for c in consumed) or '-'} ; "
                   f"emet : {', '.join('`' + c + '`' for c in emitted) or '-'}")
        t["pending_emit"], t["consumed"] = [], []
        self.record(src, "reponse", {"response_id": p.get("response_id"), **u}, [])

    def _token_count(self, p: dict[str, Any], src: dict[str, Any]) -> None:
        info = p.get("info") if isinstance(p.get("info"), dict) else {}
        last = R._usage_numbers(info.get("last_token_usage"))
        total = R._usage_numbers(info.get("total_token_usage"))
        t = self.thread
        if not info.get("last_token_usage"):
            self.head(src, "Releve token_count sans usage (limites de debit seulement)")
            self.record(src, "token_count_vide", {}, [])
            return
        for k, v in last.items():
            t["token_count"][k] += v
        if not t["has_usage_record"]:
            t["responses"] += 1
        self.head(src, f"Releve token_count (MESURE par Codex, derniere reponse) : entree {last['input_tokens']} dont cache "
                       f"{last['cached_input_tokens']}, sortie {last['output_tokens']} ; cumul du fil : entree {total['input_tokens']}, "
                       f"sortie {total['output_tokens']}" + ("" if not t["has_usage_record"] else
                                                             " (meme mesure que le releve de reponse : format double)"))
        self.record(src, "token_count", {"last": last, "total": total}, [])

    def _compacted(self, p: dict[str, Any], src: dict[str, Any]) -> None:
        hist = p.get("replacement_history")
        flags = [f"[omis] historique remplace ({len(hist)} element(s))"] if isinstance(hist, list) else []
        self.head(src, f"Compaction : fenetre {p.get('window_number')}", flags)
        flags += self.block("resume ecrit par Codex", p.get("message"))
        self.record(src, "compaction", {"window": p.get("window_number")}, flags)

    def _inter_agent_meta(self, p: dict[str, Any], src: dict[str, Any]) -> None:
        self.head(src, f"Metadonnee du message entre agents : declenche un tour = {p.get('trigger_turn')}")
        self.record(src, "message_agents_meta", {"trigger_turn": p.get("trigger_turn")}, [])

    def _realtime(self, p: dict[str, Any], src: dict[str, Any]) -> None:
        self.head(src, f"Element temps reel `{p.get('id')}` ({p.get('type')}, session `{p.get('realtime_session_id')}`)")
        self.record(src, "temps_reel", {"type": p.get("type")}, [])


def export_session(cfg: dict[str, Any], home: str, session: str, out: IO[str], *, thread: str | None = None,
                   since: int | None = None, until: int | None = None, max_chars: int = 4000, reasoning: bool = False,
                   fmt: str = "markdown", conclusions: bool = True) -> dict[str, Any]:
    """Ecrit l'export d'une session ; retourne un resume (fils, drapeaux, types d'evenements)."""
    from agentwatch.collector.store import EventStore
    from agentwatch.core.session import load_session
    from agentwatch.core.timeslice import slice_view
    files = session_files(cfg, session)
    if not files:
        raise ValueError(f"aucun rollout pour la session {session!r}")
    root = str(files[0][1].get("root_id") or files[0][1].get("thread_id"))
    if thread:
        files = [(p, m) for p, m in files if str(m.get("thread_id") or "").startswith(thread)]
    key = P.ensure_key(home)
    store = EventStore(home, cfg)
    computed: dict[str, dict[str, Any]] = {}
    view = None
    try:
        view = load_session(store, "codex", root, cfg)
        if since or until:
            view = slice_view(view, since, until)
        computed = {c.call_id: c.usage for c in view.calls if c.call_id and isinstance(c.usage, dict)}
    except Exception:  # noqa: BLE001 - l'export reste possible sans le stockage (pas de repartition calculee)
        view = None
    ex = Exporter(out, key, max_chars=max_chars, reasoning=reasoning, fmt=fmt, since=since, until=until, computed=computed)
    ex.w("# AgentWatch - export detaille d'une session Codex")
    ex.w("")
    ex.w(f"- Session : `{root}` ; {len(files)} fil(s) ; export v{EXPORT_VERSION} ; texte borne a {max_chars} car. par champ ; "
         f"secrets masques (`<secret:empreinte>`) ; lu dans les rollouts, en lecture seule")
    if since or until:
        ex.w(f"- Periode : a partir de {since or 'debut'} jusqu'a {until or 'fin'} (ns UTC) ; les fils aux horodatages non fiables "
             "sont exportes en entier")
    ex.w("- Legende : [masque] secret remplace ; [tronque] texte coupe ; [absent] donnee attendue manquante ; [non compris] "
         "type non interprete (montre quand meme) ; [omis] laisse de cote par defaut ; [chiffre] illisible ; "
         "[horodatage non fiable] fil reecrit d'un bloc. Tokens MESURES = releves de Codex par reponse ; tokens CALCULES = "
         "parts attribuees aux appels par AgentWatch.", content=True)
    ex.w("")
    ex.w("## Arbre des agents")
    ex.w("")
    ex.w("| Fil | Role | Parent | Profondeur | Fichier source |")
    ex.w("|---|---|---|---|---|")
    for path, meta in files:
        role = "principal" if not meta.get("parent_id") else f"{meta.get('agent_nickname') or '?'} ({meta.get('agent_role') or '?'})"
        ex.w(f"| `{meta.get('thread_id')}` | {role} | `{meta.get('parent_id') or '-'}` | {meta.get('depth')} | `{os.path.basename(path)}` |")
    ex.w("")
    summary: dict[str, Any] = {"session": root, "threads": [], "flags": {}, "kinds": {}}
    for path, meta in files:
        t = ex.thread_file(path, meta)
        summary["threads"].append({"thread_id": t["thread_id"], "bulk": t["bulk"], "calls": t["calls"], "responses": t["responses"],
                                   "model": t["model"]})
    flags_seen = dict(ex.flags)
    ex.w("## Recapitulatif des signalements")
    ex.w("")
    for f in FLAGS:
        ex.w(f"- {f.strip('[]')} : {flags_seen.get(f, 0)}", content=True)
    ex.w("")
    if conclusions and view is not None:
        from agentwatch.detectors import run_detectors
        ex.w("## Annexe : conclusions des detecteurs (analyse, separee des evenements ; elle ne retire rien de l'export)")
        ex.w("")
        for f in run_detectors(view, cfg):
            where = [ex.call_lines.get(str(ref.get("call_id"))) for ref in f.call_refs[:6]]
            ex.w(f"- `{f.finding_id}` {f.rule_id} ({f.confidence}) : {f.title} ; appels : "
                 + (", ".join(w for w in where if w) or "voir l'identifiant"))
        ex.w("")
    summary["flags"], summary["kinds"] = flags_seen, dict(ex.kinds)
    return summary

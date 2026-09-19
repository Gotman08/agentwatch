"""Lecture passive des rollouts Codex : ce que les hooks verraient, et davantage, sans rien executer dans Codex.

# * Pourquoi : les hooks Codex s'executent DANS le client (deux processus par appel d'outil, message d'etat
#   affiche) et n'agissent qu'apres une approbation qui, constate le 2026-09-19, n'a ete enregistree nulle
#   part. Le rollout que Codex ecrit de toute facon contient chaque action d'outil (code de sortie, duree,
#   horodatages), l'usage en tokens de chaque reponse, les tours, les compactions et les sous-agents. Le lire
#   apres coup, en lecture seule, n'a aucun effet sur Codex.
# ! Confidentialite : les actions passent par l'adaptateur Codex et le masquage du hook (memes champs, memes
#   empreintes HMAC). Jamais de sortie, de prompt, de raisonnement ni de message en clair : longueurs et
#   empreintes seulement (paragraphes des messages : empreintes courtes, pour reperer une consigne repetee).
#   Le rollout n'est jamais modifie ni copie.
# * Format observe (codex-cli 0.153.4 a 0.155.0-alpha.9.2, rollouts du 2026-09-14 au 2026-09-19) : une ligne
#   JSON {timestamp, ordinal, type, payload}. Le modele appelle `exec` (code) ou une fonction ; les actions
#   imbriquees d'un `exec` (commande, appel MCP, patch, image, recherche) sont des lignes
#   event_msg/item_completed avec un identifiant `exec-<uuid>`, le meme que `tool_use_id` dans les hooks.
#   Une reponse du modele = un appel au plus ; `token_usage_record` suit les elements de la reponse.
# * Lecture incrementale : etat par rollout (octets lus, contexte en cours) dans <home>/import/ ; seules
#   les lignes completes nouvelles sont lues. Identifiants d'evenement derives du contenu : un reimport est
#   reconnu comme doublon a l'analyse.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from typing import Any

from agentwatch import CLIENT_CODEX
from agentwatch.collector import privacy as P
from agentwatch.collector.store import EventStore, atomic_write_bytes, atomic_write_json, longpath, session_key
from agentwatch.core import normalize as N
from agentwatch.core import schema as S

SOURCE = "codex:rollout"
STATE_DIRNAME = "import"
STATE_FILENAME = "codex-rollouts.json"
TOOL_ITEMS = ("CommandExecution", "McpToolCall", "FileChange", "ImageView", "Extension")
MESSAGE_ARGS = ("message", "task", "prompt", "instructions")
_PARAGRAPH_MIN_CHARS = 40
_MAX_PARAGRAPHS = 200
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z$")


# --------------------------------------------------------------------------- utilitaires
def iso_to_ns(ts: Any) -> int | None:
    """'2026-09-19T09:45:52.752Z' -> nanosecondes depuis l'epoque (UTC). None si le format differe."""
    if not isinstance(ts, str):
        return None
    m = _ISO_RE.match(ts)
    if not m:
        return None
    import calendar
    secs = calendar.timegm((int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(6)), 0, 0, 0))
    frac = (m.group(7) or "0").ljust(9, "0")[:9]
    return secs * 1_000_000_000 + int(frac)


def _ms_to_ns(v: Any) -> int | None:
    return int(v) * 1_000_000 if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def clean_path(p: Any) -> str | None:
    """Chemin tel que Codex l'ecrit -> chemin simple : prefixe \\\\?\\ retire, URL file:/// convertie."""
    if not isinstance(p, str) or not p:
        return None
    if p.startswith("\\\\?\\"):
        p = p[4:]
    if p.lower().startswith("file://"):
        from urllib.parse import unquote
        p = unquote(p[7:])
        if re.match(r"^/[A-Za-z]:", p):
            p = p[1:]
    return p


def _duration_ms(d: Any) -> int | None:
    if isinstance(d, dict) and isinstance(d.get("secs"), (int, float)):
        return int(d["secs"]) * 1000 + int(d.get("nanos") or 0) // 1_000_000
    return None


def _thread_id_from_path(path: str) -> str | None:
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path)
    return m.group(1) if m else None


# * Signature de similarite d'un paragraphe (detecteur F, consignes reformulees) : MinHash sur l'ensemble de ses
#   mots de 3 lettres ou plus, sans accents, tronques a 6 lettres (racine grossiere : touche, toucher, touchez), hors
#   mots vides. Chaque mot passe par un hachage CLE (blake2b, cle derivee de la cle HMAC locale) : la signature ne
#   revele rien du texte sans la cle ; avec la cle, on ne peut que verifier une hypothese, comme pour les empreintes.
#   32 valeurs de 16 bits : estimation de la similarite de Jaccard a +/- 0,09 pres.
SIG_K = 32
_SIG_P = (1 << 61) - 1
_WORD_RE = re.compile(r"[a-z0-9_]{3,}")
_STOP = frozenset("""les des une pour dans avec que qui pas par sur est sont aux ces cet cette mais tout tous
toutes plus moins son ses leur leurs nous vous ils elles elle lui eux the and for with that this are you not but
from have has was were will can your our its all any into then than there their them they what when which who
etre avoir fait faire comme aussi donc alors ainsi tres bien""".split())


def signature_params(key: bytes) -> tuple[bytes, list[tuple[int, int]]]:
    """(cle de hachage des mots, 32 permutations (a, b)) derivees de la cle locale."""
    import hashlib
    wkey = hashlib.blake2b(key, digest_size=32, person=b"aw-minhash-words").digest()
    params = []
    for i in range(SIG_K):
        h = hashlib.blake2b(key + i.to_bytes(2, "big"), digest_size=16, person=b"aw-minhash-perm").digest()
        params.append((int.from_bytes(h[:8], "big") % (_SIG_P - 1) + 1, int.from_bytes(h[8:], "big") % _SIG_P))
    return wkey, params


def paragraph_signature(norm: str, sig: tuple[bytes, list[tuple[int, int]]]) -> str | None:
    """Signature MinHash (128 caracteres hexadecimaux) d'un paragraphe normalise ; None s'il a moins de 4 mots utiles."""
    import hashlib
    import unicodedata
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", norm) if not unicodedata.combining(ch))
    words = {w[:6] for w in _WORD_RE.findall(plain) if w not in _STOP}
    if len(words) < 4:
        return None
    wkey, params = sig
    hs = [int.from_bytes(hashlib.blake2b(w.encode("utf-8"), key=wkey, digest_size=8).digest(), "big") for w in words]
    return "".join(f"{min((a * h + b) % _SIG_P for h in hs) & 0xFFFF:04x}" for a, b in params)


def signature_similarity(s1: str, s2: str) -> float:
    """Part des valeurs egales entre deux signatures : estimation de la similarite de Jaccard des mots."""
    n = len(s1) // 4
    if not n or len(s2) != len(s1):
        return 0.0
    return sum(1 for i in range(n) if s1[4 * i:4 * i + 4] == s2[4 * i:4 * i + 4]) / n


def paragraph_fingerprints(text: str, fp: Any, sig: tuple[bytes, list[tuple[int, int]]] | None = None,
                           sigs_out: list[str | None] | None = None) -> tuple[list[str], int, list[int]]:
    """Empreintes courtes des paragraphes (>= 40 caracteres, espaces normalises, casse ignoree), bornees,
    avec la longueur de chacun (un nombre, jamais le texte)."""
    out: list[str] = []
    sizes: list[int] = []
    for para in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        norm = " ".join(para.split()).lower()
        if norm.startswith("#") and "\n" not in para.strip():
            continue    # * un titre seul (ex. en-tete ajoute par l'application autour de fichiers colles) n'est pas une consigne
        if len(norm) >= _PARAGRAPH_MIN_CHARS:
            out.append(fp(norm)[:16])
            sizes.append(len(norm))
            if sig is not None and sigs_out is not None:
                sigs_out.append(paragraph_signature(norm, sig))
            if len(out) >= _MAX_PARAGRAPHS:
                break
    return out, len(text), sizes


# * Raison qu'un agent annonce dans ses commentaires ("Romeo n'est pas encore actif, je retente dans 30 s") :
#   des categories fixes, reconnues par motifs ; le texte n'est jamais conserve.
_DECLARED = (
    ("retry", re.compile(r"(?i)r[ée]essa|retent|relanc|nouvelle tentative|\bre-?try|try again|once more|encore une fois|"
                         r"[àa] nouveau|de nouveau|\bagain\b")),
    ("wait", re.compile(r"(?i)\battend(?:re|s|ons|ez|ant)\b|j'attends|patient|\bwait|\bpoll|\bsond|surveill|monitor|"
                        r"toutes les \d|every \d+|"
                        r"dans \d+ ?(?:s|sec|min)|in \d+ ?(?:s|sec|min)|en attendant|meanwhile|\bsleep")),
    ("unavailable", re.compile(r"(?i)pas (?:encore )?(?:actif|active|disponible|pr[êe]t|joignable|lanc[ée]|d[ée]marr[ée]|up\b)|"
                               r"indisponible|injoignable|not (?:yet )?(?:available|ready|running|up|reachable|responding)|"
                               r"unavailable|unreachable|offline|hors ligne|ne r[ée]pond pas|\bdown\b|connexion refus|"
                               r"connection refused")),
    ("in_progress", re.compile(r"(?i)(?:toujours|encore|est|sont|reste|restent) en cours|en cours d'ex[ée]cution|"
                               r"en cours de (?:compil|build|trait|charg|lanc|d[ée]marr|calcul|g[ée]n[ée]r|mesure|ex[ée]cution)|"
                               r"still (?:running|pending|queued|building|compiling|in progress)|"
                               r"pas (?:encore )?(?:fini|termin)|\bpending\b|\bqueued\b|en file")),
    ("verify", re.compile(r"(?i)v[ée]rifi|contr[ôo]l|confirm|\bcheck|valid|m'assurer|s'assurer|make sure|ensure|double-check")),
    ("after_change", re.compile(r"(?i)maintenant que|now that|apr[èe]s (?:avoir|la|le|ma|mon|ces|cette|ce) "
                                r"(?:modif|correct|chang|patch|[ée]dit|appliqu)|after (?:the|my|this) (?:change|fix|edit|patch)")),
    ("fix", re.compile(r"(?i)corrig|r[ée]par|\bfix|\bpatch")),
    ("explore", re.compile(r"(?i)regard|explor|cherch|inspect|examin|\blook|\bsearch|investig")),
)


def declared_intents(text: str) -> list[str]:
    """Categories de raison annoncees dans un message de l'agent (liste fixe, triee), jamais le texte."""
    probe = text[:4000]
    return sorted(label for label, rx in _DECLARED if rx.search(probe))


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = [c["text"] for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)]
        return "\n\n".join(parts)
    return ""


_INJECTED_PREFIXES = ("<", "# agents.md instructions", "# agents.md")


def split_injected(content: Any) -> tuple[list[str], list[str]]:
    """(blocs ecrits par l'utilisateur, blocs injectes par le client) d'un message "user" Codex.

    # * Observe (rollouts du 2026-09-15 au 2026-09-19) : le premier message d'un fil et celui d'une reprise
    #   portent, en blocs separes, les instructions AGENTS.md ("# AGENTS.md instructions for ...") et le contexte
    #   d'environnement (<environment_context>...). Ils se repetent par construction : ce ne sont pas des rappels.
    """
    human: list[str] = []
    injected: list[str] = []
    blocks = [content] if isinstance(content, str) else [c.get("text") for c in content or [] if isinstance(c, dict)]
    for b in blocks:
        if not isinstance(b, str) or not b.strip():
            continue
        head = b.lstrip()[:40].lower()
        (injected if head.startswith(_INJECTED_PREFIXES) or "<instructions>" in b[:400].lower() else human).append(b)
    return human, injected


def split_exact(total: int, weights: list[int]) -> list[int]:
    """Parts entieres proportionnelles aux poids dont la somme vaut exactement `total` (plus forts restes)."""
    if not weights:
        return []
    w = [max(0, int(x)) for x in weights]
    tw = sum(w)
    if tw == 0:
        w, tw = [1] * len(w), len(w)
    raw = [total * x / tw for x in w]
    parts = [int(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - parts[i], reverse=True)[: total - sum(parts)]:
        parts[i] += 1
    return parts


def _usage_numbers(u: Any) -> dict[str, int]:
    keys = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    return {k: int(u.get(k) or 0) for k in keys} if isinstance(u, dict) else {k: 0 for k in keys}


# --------------------------------------------------------------------------- decouverte
def sessions_dir(cfg: dict[str, Any]) -> str:
    from agentwatch.collector.health import codex_sessions_dir
    return str(codex_sessions_dir(cfg))


def list_rollouts(cfg: dict[str, Any], days: float | None = None, whole_sessions: bool = True) -> list[str]:
    """Rollouts modifies dans les `days` derniers jours (tous si None), du plus ancien au plus recent.

    # * Une session longue garde ses premiers sous-agents, termines, dans des fichiers plus anciens que la
    #   fenetre : constate le 2026-09-19, session commencee le 10, 41 fils sur 71 hors d'une fenetre de 7 jours.
    #   Une session touchee dans la fenetre est donc lue en entier (`whole_sessions`) ; on lit pour cela
    #   l'en-tete de chaque fichier (0,1 s pour 561 rollouts).
    """
    paths = glob.glob(os.path.join(sessions_dir(cfg), "*", "*", "*", "rollout-*.jsonl"))
    cutoff = time.time() - days * 86400 if days else None
    rows = []
    for p in paths:
        try:
            rows.append((os.path.getmtime(p), p))
        except OSError:
            continue
    if cutoff is None:
        return [p for _, p in sorted(rows)]
    recent = [(m, p) for m, p in rows if m >= cutoff]
    if whole_sessions and recent:
        roots = {_root_of(p) for _, p in recent} - {None}
        recent += [(m, p) for m, p in rows if m < cutoff and _root_of(p) in roots]
    return [p for _, p in sorted(recent)]


def _root_of(path: str) -> str | None:
    """Session (fil racine) d'un rollout, lue dans son session_meta ; a defaut, le fil du nom de fichier."""
    meta = read_thread_meta(path) or {}
    return meta.get("root_id") or _thread_id_from_path(path)


def read_thread_meta(path: str, max_lines: int = 40) -> dict[str, Any] | None:
    """Premier session_meta du fil lui-meme (un sous-agent recopie aussi celui de son parent)."""
    tid = _thread_id_from_path(path)
    try:
        with open(path, "rb") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                if b'"session_meta"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
                if o.get("type") == "session_meta" and (tid is None or p.get("id") == tid):
                    return _meta_from_payload(p, tid)
    except OSError:
        return None
    return None


def _meta_from_payload(p: dict[str, Any], tid: str | None) -> dict[str, Any]:
    src = p.get("source")
    spawn: dict[str, Any] = {}
    if isinstance(src, str) and src.startswith("{"):
        try:
            src = json.loads(src)
        except ValueError:
            pass
    if isinstance(src, dict):
        spawn = ((src.get("subagent") or {}).get("thread_spawn") or {}) if isinstance(src.get("subagent"), dict) else {}
    thread_id = p.get("id") if isinstance(p.get("id"), str) else tid
    parent = spawn.get("parent_thread_id") if isinstance(spawn.get("parent_thread_id"), str) else None
    root = p.get("session_id") if isinstance(p.get("session_id"), str) and p.get("session_id") else (parent or thread_id)
    return {"thread_id": thread_id, "root_id": root, "parent_id": parent,
            "depth": spawn.get("depth") if isinstance(spawn.get("depth"), int) else (0 if not parent else None),
            "agent_role": spawn.get("agent_role") if isinstance(spawn.get("agent_role"), str) else None,
            "agent_nickname": spawn.get("agent_nickname") if isinstance(spawn.get("agent_nickname"), str) else None,
            "agent_path": str(spawn.get("agent_path"))[:120] if spawn.get("agent_path") else None,
            "cwd": clean_path(p.get("cwd")), "cli_version": p.get("cli_version") if isinstance(p.get("cli_version"), str) else None,
            "originator": p.get("originator") if isinstance(p.get("originator"), str) else None,
            "thread_source": p.get("thread_source") if isinstance(p.get("thread_source"), str) else None,
            "started": p.get("timestamp") if isinstance(p.get("timestamp"), str) else None}


# --------------------------------------------------------------------------- analyse d'un rollout
class RolloutReader:
    """Transforme les lignes d'un rollout en evenements AgentWatch. Etat serialisable entre deux lectures."""

    def __init__(self, path: str, store: EventStore, cfg: dict[str, Any], key: bytes, state: dict[str, Any] | None = None) -> None:
        from agentwatch.adapters import AdapterContext, get_adapter
        self.path = path
        self.store = store
        self.cfg = cfg
        self.key = key
        self.fp = lambda s: P.fingerprint(key, s)
        st = dict(state or {})
        st.setdefault("offset", 0)
        st.setdefault("meta", None)
        st.setdefault("model", None)
        st.setdefault("cwd", None)
        st.setdefault("turn_id", None)
        st.setdefault("window", 0)
        st.setdefault("responses", 0)
        st.setdefault("open_execs", {})        # call_id -> {"emitter": response_id|None, "actions": [[item_id, size], ...]}
        st.setdefault("open_calls", {})        # call_id -> {"tool": str, "emitter": response_id|None}
        st.setdefault("pending_emit", [])      # appels emis depuis le dernier token_usage_record
        st.setdefault("to_consume", [])        # [[call_id, taille de sortie, "exec"|"fn"]] depuis la derniere reponse
        st.setdefault("done_execs", {})        # exec ferme en attente de sa reponse consommatrice : call_id -> actions
        st.setdefault("emitters", {})          # appel de haut niveau -> reponse emettrice
        st.setdefault("emit_tokens", {})       # appel de haut niveau -> tokens de sortie de sa reponse emettrice
        st.setdefault("emit_input", {})        # appel de haut niveau -> [entree totale, dont cache] de sa reponse emettrice
        st.setdefault("totals", _usage_numbers(None))
        st.setdefault("messages", 0)
        st.setdefault("execs_without_actions", 0)
        self.st = st
        self.events: list[dict[str, Any]] = []
        self.adapter = get_adapter(CLIENT_CODEX)
        self._ctx_cls = AdapterContext
        self._args_cache: dict[str, Any] = {}
        self._sig: tuple[bytes, list[tuple[int, int]]] | None = None

    # ------------------------------------------------------------------ identite du fil
    @property
    def meta(self) -> dict[str, Any]:
        m = self.st.get("meta")
        if not m:
            tid = _thread_id_from_path(self.path)
            m = {"thread_id": tid, "root_id": tid, "parent_id": None, "depth": 0, "agent_role": None,
                 "agent_nickname": None, "agent_path": None, "cwd": None}
            self.st["meta"] = m
        return m

    @property
    def session_id(self) -> str | None:
        return self.meta.get("root_id") or self.meta.get("thread_id")

    @property
    def agent_id(self) -> str | None:
        m = self.meta
        return m.get("thread_id") if m.get("root_id") and m.get("thread_id") != m.get("root_id") else None

    @property
    def agent_type(self) -> str | None:
        m = self.meta
        return (m.get("agent_role") or "subagent") if self.agent_id else None

    # ------------------------------------------------------------------ fabrication d'evenements
    def _event_id(self, *parts: Any) -> str:
        return P.sha256_hex("|".join(["rollout", str(self.meta.get("thread_id")), *[str(x) for x in parts]]).encode("utf-8"))[:32]

    def _finish(self, ev: dict[str, Any], ns: int | None, eid: str) -> None:
        from agentwatch.collector.ingest import sanitize_event
        ev["source"] = S.SOURCE_IMPORT
        ev["event_id"] = eid
        # * Toujours l'heure du rollout (jamais celle de l'import) : a defaut, celle de la derniere ligne lue.
        ns = ns if ns is not None else self.st.get("last_ns")
        if ns is not None:
            ev["received_time_ns"] = ns
            ev["received_time"] = S.now_iso(ns / 1e9)
        ev["evidence"]["import_source"] = SOURCE
        if self.agent_id and not ev.get("agent_id"):
            ev["agent_id"], ev["agent_type"] = self.agent_id, self.agent_type
        ev = sanitize_event(ev, self.key, self.cfg)
        self.events.append(ev)

    def _marker(self, phase: str, ns: int | None, eid_parts: tuple[Any, ...], meta: dict[str, Any] | None = None,
                hook_name: str | None = None) -> None:
        ev = S.empty_event()
        ev.update({"client": CLIENT_CODEX, "phase": phase, "session_id": self.session_id, "turn_id": self.st.get("turn_id"),
                   "model": self.st.get("model"), "cwd": self.st.get("cwd"), "project_dir": self._project(),
                   "hook_event_name": hook_name or f"rollout:{phase}"})
        if meta is not None:
            ev["session_meta"] = meta
        self._finish(ev, ns, self._event_id(phase, *eid_parts))

    def _project(self) -> str | None:
        return self.meta.get("cwd") or self.st.get("cwd")

    def _tool_events(self, call_id: str, tool: str, tool_input: Any, response: Any, start_ns: int | None, end_ns: int | None,
                     cwd: str | None, evidence: dict[str, Any], with_start: bool = True,
                     synthetic: bool = False) -> dict[str, Any] | None:
        """Un debut et une fin, fabriques comme des payloads de hook puis passes par l'adaptateur Codex.

        # ! `synthetic` : la reponse est fabriquee (image vue, patch) ; ses empreintes ne diraient rien du contenu
        #   reel et feraient passer deux resultats differents pour identiques. Elles sont retirees.
        """
        ctx = self._ctx_cls(self.cfg, self.fp, self._project())
        base = {"session_id": self.session_id, "turn_id": self.st.get("turn_id"), "cwd": cwd or self.st.get("cwd"),
                "model": self.st.get("model"), "tool_name": tool, "tool_use_id": call_id, "tool_input": tool_input}
        end_ev = None
        if with_start:
            ev = self.adapter.parse_hook_payload(dict(base, hook_event_name="PreToolUse"), ctx)
            ev["warnings"] = [w for w in ev["warnings"] if w != "tool_input missing"]
            ev["evidence"].pop("payload_keys", None)
            self._finish(ev, start_ns, self._event_id("start", call_id))
        if response is not _NO_END:
            ev = self.adapter.parse_hook_payload(dict(base, hook_event_name="PostToolUse", tool_response=response), ctx)
            ev["warnings"] = [w for w in ev["warnings"] if w not in ("tool_input missing",)]
            ev["evidence"].pop("payload_keys", None)
            ev["evidence"].update(evidence)
            if synthetic:
                ev["result_fingerprint"] = None
                ev["content_fingerprint"] = None
                ev["output_size_bytes"] = None
                ev["output_size_source"] = None
                ev["evidence"].pop("state_fp", None)
            self._finish(ev, end_ns, self._event_id("end", call_id))
            end_ev = ev
        return end_ev

    # ------------------------------------------------------------------ lecture
    def feed(self, raw: bytes, offset: int) -> None:
        """Traite une ligne complete (octets) situee a `offset` dans le fichier."""
        try:
            o = json.loads(raw)
        except ValueError:
            self.st["invalid_lines"] = self.st.get("invalid_lines", 0) + 1
            return
        if not isinstance(o, dict):
            return
        t = o.get("type")
        raw_p = o.get("payload")
        p: dict[str, Any] = raw_p if isinstance(raw_p, dict) else {}
        pt = p.get("type")
        ns = iso_to_ns(o.get("timestamp"))
        if ns is not None:
            self.st["last_ns"] = ns
        if t == "session_meta":
            self._on_session_meta(p, ns)
        elif t == "turn_context":
            self.st["turn_id"] = p.get("turn_id") if isinstance(p.get("turn_id"), str) else self.st.get("turn_id")
            self.st["model"] = p.get("model") if isinstance(p.get("model"), str) else self.st.get("model")
            self.st["cwd"] = clean_path(p.get("cwd")) or self.st.get("cwd")
        elif t == "token_usage_record":
            self._on_usage(p, ns)
        elif t == "compacted":
            self.st["window"] = int(p.get("window_number") or self.st.get("window", 0) + 1)
            self._marker(S.PHASE_COMPACT_END, ns, ("compacted", offset),
                         {"compact_type": "codex", "window": self.st["window"], "response_index": self.st["responses"],
                          "input_tokens_before": self.st.get("last_input")})
        elif t == "world_state":
            self._on_world_state(p, ns, offset)
        elif t == "event_msg":
            self._on_event_msg(p, pt, ns, offset)
        elif t == "response_item":
            self._on_response_item(p, pt, ns, offset)

    def _on_session_meta(self, p: dict[str, Any], ns: int | None) -> None:
        tid = _thread_id_from_path(self.path)
        if self.st.get("meta_seen") or (tid and p.get("id") != tid):
            return    # * copie du session_meta du parent dans un sous-agent : ignoree
        self.st["meta"] = _meta_from_payload(p, tid)
        self.st["meta_seen"] = True
        m = self.meta
        self.st["cwd"] = self.st.get("cwd") or m.get("cwd")
        info = {k: m.get(k) for k in ("cli_version", "originator", "thread_source")}
        if self.agent_id:
            info.update({"agent_id": self.agent_id, "agent_type": self.agent_type, "agent_nickname": m.get("agent_nickname"),
                         "agent_path": m.get("agent_path"), "parent_thread_id": m.get("parent_id"), "depth": m.get("depth")})
            self._marker(S.PHASE_SUBAGENT_START, ns, ("meta",), info, "rollout:thread_spawn")
        else:
            info["start_type"] = m.get("thread_source") or "rollout"
            self._marker(S.PHASE_SESSION_START, ns, ("meta",), info, "rollout:session_meta")

    def _on_world_state(self, p: dict[str, Any], ns: int | None, offset: int) -> None:
        state = p.get("state") if isinstance(p.get("state"), dict) else {}
        agents_md = (state.get("agents_md") or {}).get("text") if isinstance(state.get("agents_md"), dict) else None
        skills = (state.get("host_skills") or {}).get("body") if isinstance(state.get("host_skills"), dict) else None
        meta: dict[str, Any] = {"role": "context", "full": bool(p.get("full"))}
        if isinstance(agents_md, str):
            meta["agents_md_chars"] = len(agents_md)
            meta["agents_md_fp"] = self.fp(agents_md)[:16]
        if isinstance(skills, str):
            meta["skills_chars"] = len(skills)
        meta["chars"] = len(json.dumps(state, ensure_ascii=False))
        self._marker(S.PHASE_MESSAGE, ns, ("world_state", offset), meta, "rollout:world_state")

    def _on_event_msg(self, p: dict[str, Any], pt: Any, ns: int | None, offset: int) -> None:
        if pt == "task_started":
            self.st["turn_id"] = p.get("turn_id") if isinstance(p.get("turn_id"), str) else self.st.get("turn_id")
            self._marker(S.PHASE_TURN_START, ns, ("turn", p.get("turn_id")),
                         {"model_context_window": p.get("model_context_window"), "mode": p.get("collaboration_mode_kind")})
        elif pt == "task_complete":
            self._marker(S.PHASE_TURN_END, ns, ("turn_end", p.get("turn_id"), offset),
                         {"duration_ms": p.get("duration_ms"), "time_to_first_token_ms": p.get("time_to_first_token_ms")})
        elif pt == "turn_aborted":
            self._marker(S.PHASE_INTERRUPT, ns, ("abort", p.get("turn_id"), offset),
                         {"reason": p.get("reason") if isinstance(p.get("reason"), str) else None, "duration_ms": p.get("duration_ms")})
        elif pt == "item_completed":
            item = p.get("item") if isinstance(p.get("item"), dict) else {}
            if item.get("type") in TOOL_ITEMS:
                self._on_tool_item(item, p, ns)
            elif item.get("type") == "SubAgentActivity" and item.get("kind") in ("completed", "interrupted"):
                child = item.get("agent_thread_id")
                self._marker(S.PHASE_SUBAGENT_STOP, ns, ("subagent", child, item.get("id")),
                             {"agent_id": child, "kind": item.get("kind"), "agent_path": str(item.get("agent_path") or "")[:120] or None},
                             "rollout:subagent_activity")

    def _on_response_item(self, p: dict[str, Any], pt: Any, ns: int | None, offset: int) -> None:
        if pt == "custom_tool_call":
            cid = p.get("call_id")
            if isinstance(cid, str):
                self.st["open_execs"][cid] = {"emitter": None, "actions": [], "tool": p.get("name") or "exec"}
                self.st["pending_emit"].append(cid)
        elif pt == "custom_tool_call_output":
            cid = p.get("call_id")
            ex = self.st["open_execs"].pop(cid, None) if isinstance(cid, str) else None
            size = len(_content_text(p.get("output")))
            if ex is not None:
                if not ex["actions"]:
                    self.st["execs_without_actions"] += 1
                self._image_fingerprints(ex, p.get("output"), ns)
                self.st["done_execs"][cid] = ex
                self.st["to_consume"].append([cid, size, "exec"])
        elif pt == "function_call":
            self._on_function_call(p, ns)
        elif pt == "function_call_output":
            self._on_function_output(p, ns)
        elif pt == "message":
            role = p.get("role") if isinstance(p.get("role"), str) else "?"
            extra: dict[str, Any] = {"phase": p.get("phase"), "message_id": p.get("id")}
            if role == "user":
                # * Codex glisse dans des messages "user" du contexte qu'il injecte lui-meme (environnement, AGENTS.md) :
                #   seuls les blocs ecrits par l'utilisateur sont empreints ; les autres sont comptes a part.
                human, injected = split_injected(p.get("content"))
                text = "\n\n".join(human)
                extra.update({"injected_chars": sum(len(b) for b in injected), "injected_blocks": len(injected)})
            else:
                text = _content_text(p.get("content"))
            self._message(role, text, ns, ("message", p.get("id") or offset), extra)
        elif pt == "agent_message":
            text = _content_text(p.get("content"))
            self._message("agent", text, ns, ("agent_message", p.get("id") or offset),
                          {"message_id": p.get("id"), "author": str(p.get("author") or "")[:120] or None, "recipient": str(p.get("recipient") or "")[:120] or None})

    def _image_fingerprints(self, ex: dict[str, Any], output: Any, ns: int | None) -> None:
        """Empreinte du contenu de chaque image vue, si l'exec rend exactement une image par vue (dans l'ordre)."""
        views = ex.get("images") or []
        if not views or not isinstance(output, list):
            return
        images = [c.get("image_url") for c in output if isinstance(c, dict) and c.get("type") == "input_image"]
        if len(images) != len(views) or not all(isinstance(u, str) for u in images):
            return     # ? association incertaine : pas d'empreinte plutot qu'une empreinte fausse
        for iid, url in zip(views, images):
            ev = S.empty_event()
            ev.update({"client": CLIENT_CODEX, "phase": S.PHASE_OBSERVATION, "session_id": self.session_id, "call_id": iid,
                       "hook_event_name": "rollout_image", "tool_name": "view_image",
                       "content_fingerprint": {"method": "hmac-sha256-image", "value": self.fp(url), "chars": len(url)}})
            self._finish(ev, ns, self._event_id("image", iid))

    def _message(self, role: str, text: str, ns: int | None, eid: tuple[Any, ...], extra: dict[str, Any]) -> None:
        sigs: list[str | None] = []
        with_sig = role in ("user", "agent_instruction", "agent")
        if with_sig and self._sig is None:
            self._sig = signature_params(self.key)
        paras, chars, sizes = paragraph_fingerprints(text, self.fp, self._sig if with_sig else None, sigs)
        self.st["messages"] += 1
        meta = {"role": role, "chars": chars, "paragraphs": paras, "paragraph_chars": sizes, "turn_id": self.st.get("turn_id"),
                **{k: v for k, v in extra.items() if v is not None}}
        if with_sig and any(sigs):
            meta["paragraph_sigs"] = sigs
        if role == "assistant" and text:
            meta["declared"] = declared_intents(text)
        self._marker(S.PHASE_MESSAGE, ns, eid, meta, "rollout:message")

    # ------------------------------------------------------------------ appels
    def _parent_exec(self) -> str | None:
        opened = list(self.st["open_execs"])
        return opened[-1] if opened else None

    def _on_tool_item(self, item: dict[str, Any], p: dict[str, Any], ns: int | None) -> None:
        iid = item.get("id")
        if not isinstance(iid, str):
            return
        start_ns = _ms_to_ns(p.get("started_at_ms")) or ns
        end_ns = _ms_to_ns(p.get("completed_at_ms")) or ns
        ty = item.get("type")
        # * Un appel de fonction MCP (ex. mcp__cua_repl.js) produit AUSSI un item McpToolCall de meme identifiant :
        #   un seul appel. Son debut vient de function_call ; sa fin vient de l'item (statut et duree), la sortie
        #   de la fonction ne sert plus qu'a la consommation des tokens.
        fn = self.st["open_calls"].get(iid)
        if fn is not None:
            fn["item_end"] = True
        elif iid in self.st.setdefault("fn_closed", []):
            return
        parent = None if fn is not None else self._parent_exec()
        evidence: dict[str, Any] = {"rollout_item": ty, "exec_call_id": parent, "item_status": item.get("status")}
        cwd = clean_path(item.get("cwd"))
        dur = _duration_ms(item.get("duration"))
        size = 0
        if ty == "CommandExecution":
            cmd = item.get("command")
            script = cmd[-1] if isinstance(cmd, list) and cmd else cmd
            output = item.get("aggregated_output") if isinstance(item.get("aggregated_output"), str) else ""
            resp: dict[str, Any] = {"output": output}
            code = item.get("exit_code")
            if isinstance(code, int) and not isinstance(code, bool):
                resp["exit_code"] = code
            elif item.get("status") in ("failed", "declined"):
                resp["exit_code"] = 1
            if dur is not None:
                resp["duration_ms"] = dur
            if isinstance(cmd, list) and cmd:
                evidence["shell"] = os.path.basename(str(cmd[0]))[:40]
            size = len(output)
            self._tool_events(iid, "Bash", {"command": script if isinstance(script, str) else None, "workdir": cwd},
                              resp, start_ns, end_ns, cwd, evidence, with_start=fn is None)
        elif ty == "McpToolCall":
            server, tool = str(item.get("server") or "?"), str(item.get("tool") or "?")
            result = item.get("result")
            resp = dict(result) if isinstance(result, dict) else {}
            if item.get("status") == "failed" and "isError" not in resp:
                resp["isError"] = True
                err = item.get("error")
                if isinstance(err, dict) and isinstance(err.get("message"), str):
                    resp.setdefault("content", [{"type": "text", "text": err["message"]}])
            if dur is not None:
                resp["duration_ms"] = dur
            evidence["read_only_hint"] = item.get("readOnlyHint")
            size = len(json.dumps(resp.get("content"), ensure_ascii=False)) if resp.get("content") is not None else 0
            self._tool_events(iid, f"mcp__{server}__{tool}", item.get("arguments"), resp, start_ns, end_ns, cwd, evidence, with_start=fn is None)
        elif ty == "FileChange":
            changes = item.get("changes") if isinstance(item.get("changes"), dict) else {}
            headers = []
            diffs = []
            kinds: dict[str, int] = {}
            for path, ch in list(changes.items())[:50]:
                kind = (ch or {}).get("type") if isinstance(ch, dict) else None
                kinds[str(kind)] = kinds.get(str(kind), 0) + 1
                verb = {"add": "Add", "delete": "Delete"}.get(str(kind), "Update")
                headers.append(f"*** {verb} File: {clean_path(path)}")
                if isinstance(ch, dict) and isinstance(ch.get("unified_diff"), str):
                    diffs.append(ch["unified_diff"])
            patch = "*** Begin Patch\n" + "\n".join(headers) + "\n*** End Patch"
            resp = {"output": "", "exit_code": 0 if item.get("status") in ("completed", None) else 1}
            evidence["change_kinds"] = kinds
            self._tool_events(iid, "apply_patch", {"input": patch}, resp, start_ns, end_ns, cwd, evidence, with_start=fn is None,
                              synthetic=True)
            # * Taille et empreinte des diffs reels, pas de l'en-tete synthetique ci-dessus.
            diff_text = "\n".join(diffs)
            for ev in self.events[-2:]:
                if ev.get("call_id") == iid and isinstance(ev.get("params"), dict):
                    ev["params"]["patch_chars"] = len(diff_text)
                    ev["params"]["patch_fp"] = self.fp(diff_text)[:10] if diff_text else None
        elif ty == "ImageView":
            resp = {"output": "", "exit_code": 0}
            self._tool_events(iid, "view_image", {"path": clean_path(item.get("path"))}, resp, start_ns, end_ns, cwd, evidence,
                              with_start=fn is None, synthetic=True)
            if parent is not None and parent in self.st["open_execs"]:
                self.st["open_execs"][parent].setdefault("images", []).append(iid)
        elif ty == "Extension":
            kind = str(item.get("kind") or "extension")
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            tool = "web_search" if kind.startswith("web.") else kind
            results = item.get("results")
            resp = {"output": json.dumps(results, ensure_ascii=False) if results is not None else "", "exit_code": 0}
            evidence["extension_action"] = action.get("type") if isinstance(action.get("type"), str) else None
            size = len(resp["output"])
            self._tool_events(iid, tool, {"query": item.get("query")}, resp, start_ns, end_ns, cwd, evidence, with_start=fn is None)
        if fn is not None:
            return     # * consommation comptee a la sortie de la fonction
        if parent is not None and parent in self.st["open_execs"]:
            self.st["open_execs"][parent]["actions"].append([iid, size])
        elif parent is None:
            # * Action sans exec ouvert (fin differee d'un processus, appel direct) : consommee avec la reponse suivante.
            self.st["to_consume"].append([iid, size, "item"])

    def _on_function_call(self, p: dict[str, Any], ns: int | None) -> None:
        cid = p.get("call_id")
        if not isinstance(cid, str):
            return
        name = p.get("name") if isinstance(p.get("name"), str) else "?"
        ns_name = p.get("namespace") if isinstance(p.get("namespace"), str) else None
        tool = (f"{ns_name}__{name}" if ns_name.startswith("mcp__") else f"{ns_name}.{name}") if ns_name else name
        try:
            args = json.loads(p.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {"_raw_chars": len(str(p.get("arguments") or ""))}
        self._args_cache[cid] = args
        self.st["open_calls"][cid] = {"tool": tool, "emitter": None}
        self.st["pending_emit"].append(cid)
        self._tool_events(cid, tool, args, _NO_END, ns, None, None, {})
        if isinstance(args, dict):
            for k in MESSAGE_ARGS:
                if isinstance(args.get(k), str) and args[k].strip():
                    self._message("agent_instruction", args[k], ns, ("instruction", cid, k),
                                  {"message_id": cid, "tool": tool, "target": str(args.get("target") or args.get("task_name") or "")[:120] or None})
                    break

    def _on_function_output(self, p: dict[str, Any], ns: int | None) -> None:
        cid = p.get("call_id")
        if not isinstance(cid, str):
            return
        info = self.st["open_calls"].pop(cid, None) or {"tool": "?"}
        closed = self.st.setdefault("fn_closed", [])
        closed.append(cid)
        del closed[:-50]
        output = p.get("output")
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        self.st["to_consume"].append([cid, len(text), "fn"])
        if info.get("item_end"):
            return     # * fin deja donnee par l'item MCP de meme identifiant (statut, duree)
        # * Fonctions de collaboration et d'attente : sortie vide ou JSON, aucun code de sortie. Sans indice
        #   d'erreur, l'appel a abouti ; un texte d'erreur court (agent inconnu, cible invalide) en est un.
        try:
            j = json.loads(text) if text.lstrip().startswith("{") else None
        except ValueError:
            j = None
        err = None
        if isinstance(j, dict) and isinstance(j.get("error"), str):
            err = j["error"]
        elif j is None and info["tool"] != "wait" and _FN_ERROR_RE.search(text[:300]):
            # * `wait` rend la sortie d'une cellule exec encore en cours : un mot d'erreur y vient de la commande
            #   attendue, pas de l'attente ; son statut reste celui de l'attente (aboutie).
            err = text
        resp: dict[str, Any] = {"output": text, "success": err is None}
        if err is not None:
            resp["error"] = err
        evidence: dict[str, Any] = {"rollout_item": "function_call", "status_basis": "function output (no exit status): error text or none"}
        if isinstance(j, dict) and j.get("timed_out") is True:
            evidence["timed_out"] = True   # * attente arrivee a echeance : normal, pas un echec
        self._tool_events(cid, info["tool"], self._args_cache.pop(cid, None), resp, None, ns, None, evidence, with_start=False)

    # ------------------------------------------------------------------ tokens
    def _on_usage(self, p: dict[str, Any], ns: int | None) -> None:
        u = _usage_numbers(p.get("usage"))
        rid = p.get("response_id") if isinstance(p.get("response_id"), str) else f"resp-{self.st['responses']}"
        idx = self.st["responses"]
        self.st["responses"] += 1
        self.st["last_input"] = u["input_tokens"]
        # * Une ligne par reponse du modele : entree (dont cache), sortie, fenetre de contexte. Chaque reponse relit
        #   tout le contexte : c'est ce qui permet d'attribuer ce cout aux sorties d'outils qui y resident.
        rev = S.empty_event()
        rev.update({"client": CLIENT_CODEX, "phase": S.PHASE_USAGE, "session_id": self.session_id, "turn_id": self.st.get("turn_id"),
                    "hook_event_name": "rollout_response_usage", "model": self.st.get("model"),
                    "usage": {"scope": "response", "source": SOURCE, "thread_id": self.meta.get("thread_id"), "agent_id": self.agent_id,
                              "index": idx, "response_id": rid, "window": self.st.get("window", 0),
                              "consumed_outputs": len(self.st["to_consume"]), "emitted_calls": len(self.st["pending_emit"]),
                              **{k: u[k] for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")}}})
        self._finish(rev, ns, self._event_id("response-usage", idx, rid))
        for k, v in u.items():
            self.st["totals"][k] = self.st["totals"].get(k, 0) + v
        # * Consommation : l'entree non mise en cache de cette reponse contient les sorties apparues depuis la
        #   precedente ; elle est partagee entre elles au prorata de leur taille (au moins 1).
        uncached = max(0, u["input_tokens"] - u["cached_input_tokens"])
        consumed = self.st["to_consume"]
        shares = split_exact(uncached, [max(1, int(size)) for _, size, _ in consumed])
        for (cid, _size, kind), share in zip(consumed, shares):
            if kind == "exec":
                # * Un exec n'est pas un appel AgentWatch : sa part (entree consommee, sortie emettrice) est repartie
                #   sur ses actions imbriquees au prorata de la taille de leurs sorties.
                ex = self.st["done_execs"].pop(cid, None) or {"actions": [], "emitter": None, "out_tokens": None}
                acts = ex.get("actions") or []
                aw = [max(1, int(s)) for _, s in acts]
                out_tok = ex.get("out_tokens")
                in_parts = split_exact(share, aw)
                out_parts = split_exact(out_tok, aw) if isinstance(out_tok, int) else [None] * len(acts)
                for (iid, _s), a_in, a_out in zip(acts, in_parts, out_parts):
                    self._usage_obs(iid, a_in, a_out, ex.get("emitter"), rid, idx, ns, len(acts), ex.get("in_tokens"))
            else:
                self._usage_obs(cid, share, self.st["emit_tokens"].pop(cid, None), self.st["emitters"].pop(cid, None),
                                rid, idx, ns, 1, self.st.setdefault("emit_input", {}).pop(cid, None))
        self.st["to_consume"] = []
        # * Emission : la sortie de cette reponse est l'appel qu'elle vient d'emettre (un appel par reponse observe).
        pend = self.st["pending_emit"]
        for cid in pend:
            if cid in self.st["open_execs"]:
                self.st["open_execs"][cid]["emitter"] = rid
                self.st["open_execs"][cid]["out_tokens"] = u["output_tokens"] // max(1, len(pend))
                self.st["open_execs"][cid]["in_tokens"] = [u["input_tokens"], u["cached_input_tokens"]]
            else:
                self.st["emitters"][cid] = rid
                self.st["emit_tokens"][cid] = u["output_tokens"] // max(1, len(pend))
                self.st.setdefault("emit_input", {})[cid] = [u["input_tokens"], u["cached_input_tokens"]]
        self.st["pending_emit"] = []

    def _usage_obs(self, call_id: str, uncached: int, out_tok: int | None, emitter: str | None, consumer: str, idx: int,
                   ns: int | None, siblings: int, emit_in: list[int] | None = None) -> None:
        emit_in = emit_in if isinstance(emit_in, list) and len(emit_in) == 2 else [None, None]
        ev = S.empty_event()
        ev.update({"client": CLIENT_CODEX, "phase": S.PHASE_OBSERVATION, "session_id": self.session_id, "call_id": call_id,
                   "hook_event_name": "rollout_usage", "model": self.st.get("model"),
                   "usage": {"scope": "call", "source": SOURCE, "uncached_input_tokens": uncached, "output_tokens": out_tok,
                             "emitter_request_id": emitter, "consumer_request_id": consumer, "consumer_index": idx,
                             "window": self.st.get("window", 0), "siblings": siblings,
                             # * Entree totale de la reponse EMETTRICE (non partagee, dont cache) : le contexte relu pour
                             #   decider cet appel. A compter une fois par reponse (emitter_request_id).
                             "emitter_input_tokens": emit_in[0], "emitter_cached_input_tokens": emit_in[1],
                             "method": ("part de l'entree non mise en cache de la reponse qui a consomme la sortie (prorata des tailles) "
                                        "+ part de la sortie de la reponse emettrice (appels de haut niveau)")}})
        self._finish(ev, ns, self._event_id("usage", call_id, consumer))

    def session_usage_event(self) -> None:
        t = dict(self.st["totals"])
        if not self.st["responses"]:
            return
        ev = S.empty_event()
        ev.update({"client": CLIENT_CODEX, "phase": S.PHASE_USAGE, "session_id": self.session_id, "hook_event_name": "rollout_usage",
                   "model": self.st.get("model"),
                   "usage": {"scope": "thread", "source": SOURCE, "thread_id": self.meta.get("thread_id"), "agent_id": self.agent_id,
                             "requests": self.st["responses"], "windows": self.st.get("window", 0) + 1,
                             "messages": self.st.get("messages", 0), "execs_without_actions": self.st.get("execs_without_actions", 0),
                             **t}})
        eid = self._event_id("thread-usage", self.st["responses"], t.get("total_tokens"), self.st.get("messages", 0))
        if eid == self.st.get("usage_eid"):
            return      # * rien de nouveau depuis le dernier releve : pas de doublon
        self.st["usage_eid"] = eid
        self._finish(ev, None, eid)


_NO_END = object()
_FN_ERROR_RE = re.compile(r"(?i)\b(?:error|erreur|failed|echec|unknown agent|no such agent|not found|invalid|refused|denied)\b")


# --------------------------------------------------------------------------- import
def state_path(home: str) -> str:
    return os.path.join(home, STATE_DIRNAME, STATE_FILENAME)


def load_state(home: str) -> dict[str, Any]:
    try:
        with open(longpath(state_path(home)), encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(home: str, state: dict[str, Any]) -> None:
    os.makedirs(longpath(os.path.join(home, STATE_DIRNAME)), exist_ok=True)
    atomic_write_json(state_path(home), state)


def import_rollout(store: EventStore, cfg: dict[str, Any], key: bytes, path: str, prev: dict[str, Any] | None,
                   max_bytes: int = 0) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Lit les lignes completes nouvelles d'un rollout. Retourne (nouvel etat, evenements, resume)."""
    summary: dict[str, Any] = {"path": path, "bytes": 0, "lines": 0, "events": 0, "reset": False}
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return prev or {}, [], summary
    state = dict(prev or {})
    if state.get("offset", 0) > size:
        state = {}      # ? fichier tronque ou remplace : relecture complete (identifiants stables : pas de doublon utile)
        summary["reset"] = True
    reader = RolloutReader(path, store, cfg, key, state)
    if reader.st.get("offset", 0) == 0 and not reader.st.get("meta_seen"):
        meta = read_thread_meta(path)
        if meta:
            reader.st["meta"] = meta
    start = int(reader.st.get("offset", 0))
    limit = size if not max_bytes else min(size, start + max_bytes)
    pos = start
    with open(path, "rb") as fh:
        fh.seek(start)
        while pos < limit:
            line = fh.readline()
            if not line or not line.endswith(b"\n"):
                break     # * ligne en cours d'ecriture : relue la prochaine fois
            reader.feed(line, pos)
            pos += len(line)
            summary["lines"] += 1
    reader.st["offset"] = pos
    summary["bytes"] = pos - start
    if summary["lines"]:
        reader.session_usage_event()
    summary["events"] = len(reader.events)
    summary["session_id"] = reader.session_id
    summary["agent_id"] = reader.agent_id
    return reader.st, reader.events, summary


def write_events(store: EventStore, session_id: str | None, events: list[dict[str, Any]]) -> str | None:
    """Ecrit un lot d'evenements importes dans un segment JSONL de la session (ecriture atomique)."""
    if not events:
        return None
    seg_dir = os.path.join(store.segments_s, CLIENT_CODEX, session_key(session_id))
    os.makedirs(longpath(seg_dir), exist_ok=True)
    path = os.path.join(seg_dir, f"segment-{time.time_ns():020d}-{os.getpid()}-rollout.jsonl")
    data = b"".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" for e in events)
    atomic_write_bytes(path, data)
    return path


def pending_rollouts(home: str, paths: list[str]) -> list[str]:
    """Rollouts dont la taille depasse ce qui a deja ete lu (ou jamais lus)."""
    files = load_state(home).get("files", {})
    out = []
    for p in paths:
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        prev = files.get(os.path.normcase(os.path.abspath(p))) or {}
        if size != int(prev.get("offset", -1)):
            out.append(p)
    return out


def merge_segments(store: EventStore, session_id: str | None, max_segments: int = 30) -> int:
    """Fusionne les segments d'import d'une session au-dela de `max_segments` (le suivi en cree un par cycle).

    # * Ecriture atomique du segment fusionne, dedoublonne par event_id, puis suppression des anciens.
    """
    seg_dir = os.path.join(store.segments_s, CLIENT_CODEX, session_key(session_id))
    try:
        names = sorted(n for n in os.listdir(longpath(seg_dir)) if n.endswith("-rollout.jsonl"))
    except OSError:
        return 0
    if len(names) <= max_segments:
        return 0
    seen: set[str] = set()
    lines: list[bytes] = []
    for n in names:
        try:
            with open(longpath(os.path.join(seg_dir, n)), "rb") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        eid = json.loads(line).get("event_id")
                    except ValueError:
                        continue
                    if eid in seen:
                        continue
                    seen.add(eid)
                    lines.append(line)
        except OSError:
            return 0    # ? segment illisible : on ne fusionne pas plutot que de perdre des evenements
    atomic_write_bytes(os.path.join(seg_dir, f"segment-{time.time_ns():020d}-{os.getpid()}-rollout.jsonl"), b"\n".join(lines) + b"\n")
    for n in names:
        try:
            os.unlink(longpath(os.path.join(seg_dir, n)))
        except OSError:
            pass
    return len(names)


class background_priority:
    """Priorite d'arriere-plan le temps de l'import : processeur ET disque sous Windows
    (PROCESS_MODE_BACKGROUND_BEGIN), `nice` ailleurs. L'import ne dispute pas la machine a Codex."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.active = False

    def __enter__(self) -> background_priority:
        if not self.enabled:
            return self
        try:
            if os.name == "nt":
                import ctypes
                k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                self.active = bool(k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00100000))
            else:
                os.nice(10)
        except Exception:  # noqa: BLE001 - une priorite non modifiable n'empeche pas l'import
            self.active = False
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.active and os.name == "nt":
            try:
                import ctypes
                k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00200000)   # PROCESS_MODE_BACKGROUND_END
            except Exception:  # noqa: BLE001
                pass


class LiveDigest:
    """Resume de ce que Codex vient de faire, a partir des evenements d'une lecture (suivi en direct)."""

    def __init__(self, home: str) -> None:
        self.names: dict[str, str] = {}
        for st in (load_state(home).get("files") or {}).values():
            m = (st or {}).get("meta") or {}
            if m.get("thread_id"):
                self.names[m["thread_id"]] = (m.get("agent_nickname") or "principal") if m.get("parent_id") else "principal"
        # * Base des tokens : totaux deja lus de chaque fil (seul l'accroissement est ensuite affiche).
        self.tokens: dict[str, int] = {}
        for st in (load_state(home).get("files") or {}).values():
            m = (st or {}).get("meta") or {}
            if m.get("thread_id"):
                self.tokens[m["thread_id"]] = int(((st or {}).get("totals") or {}).get("total_tokens") or 0)
        self.reset()

    def reset(self) -> None:
        self.calls: dict[str, int] = {}
        self.failures = 0
        self.token_delta = 0
        self.turns_ended = 0
        self.interrupts = 0
        self.compactions = 0
        self.spawned: list[str] = []
        self.user_messages = 0

    def _who(self, ev: dict[str, Any]) -> str:
        aid = ev.get("agent_id")
        if not aid:
            return "principal"
        return self.names.get(aid) or str(aid)[:8]

    def feed(self, summ: dict[str, Any], events: list[dict[str, Any]]) -> None:
        for ev in events:
            ph = ev.get("phase")
            if ph == S.PHASE_END:
                who = self._who(ev)
                self.calls[who] = self.calls.get(who, 0) + 1
                if ev.get("status") == S.STATUS_ERROR:
                    self.failures += 1
            elif ph == S.PHASE_TURN_END:
                self.turns_ended += 1
            elif ph == S.PHASE_INTERRUPT:
                self.interrupts += 1
            elif ph == S.PHASE_COMPACT_END:
                self.compactions += 1
            elif ph == S.PHASE_SUBAGENT_START:
                meta = ev.get("session_meta") or {}
                if meta.get("agent_id"):
                    self.names[meta["agent_id"]] = meta.get("agent_nickname") or str(meta["agent_id"])[:8]
                self.spawned.append(str(meta.get("agent_nickname") or meta.get("agent_id") or "?"))
            elif ph == S.PHASE_MESSAGE and (ev.get("session_meta") or {}).get("role") == "user":
                self.user_messages += 1
            elif ph == S.PHASE_USAGE and (ev.get("usage") or {}).get("scope") != "response":
                u = ev.get("usage") or {}
                tid = str(u.get("thread_id") or "?")
                total = int(u.get("total_tokens") or 0)
                if tid in self.tokens:
                    self.token_delta += max(0, total - self.tokens[tid])
                self.tokens[tid] = max(total, self.tokens.get(tid, 0))

    def active(self) -> bool:
        return bool(self.calls or self.turns_ended or self.interrupts or self.compactions or self.spawned or self.user_messages)

    def line(self) -> str:
        n = sum(self.calls.values())
        parts = [f"+{n} appel(s)" + (f" dont {self.failures} en echec brut" if self.failures else ""),
                 f"+{self.token_delta} tokens"]
        if self.calls:
            parts.append(", ".join(f"{who} +{k}" for who, k in sorted(self.calls.items(), key=lambda kv: -kv[1])))
        for count, label in ((self.user_messages, "message(s) de l'utilisateur"), (self.turns_ended, "tour(s) termine(s)"),
                             (self.interrupts, "interruption(s)"), (self.compactions, "compaction(s)")):
            if count:
                parts.append(f"{count} {label}")
        if self.spawned:
            parts.append("sous-agent(s) lance(s) : " + ", ".join(self.spawned))
        return "Codex : " + " ; ".join(parts)


def import_rollouts(store: EventStore, cfg: dict[str, Any], paths: list[str], sink: Any = None) -> dict[str, Any]:
    """Import incremental d'une liste de rollouts ; l'etat est sauvegarde apres chaque fichier.

    `sink(resume, evenements)` recoit chaque lot ecrit (suivi en direct)."""
    home = store.home_s
    key = P.ensure_key(home)
    state = load_state(home)
    files = state.setdefault("files", {})
    out: dict[str, Any] = {"files": 0, "lines": 0, "events": 0, "bytes": 0, "sessions": {}, "errors": []}
    rcfg = cfg.get("rollouts") if isinstance(cfg.get("rollouts"), dict) else {}
    max_bytes = int(rcfg.get("max_bytes_per_run", 0) or 0)
    for path in paths:
        norm = os.path.normcase(os.path.abspath(path))
        prev = files.get(norm)
        try:
            new_state, events, summ = import_rollout(store, cfg, key, path, prev, max_bytes)
        except Exception as exc:  # noqa: BLE001 - un rollout illisible ne bloque pas les autres
            out["errors"].append(f"{os.path.basename(path)} : {type(exc).__name__}: {exc}")
            continue
        if summ.get("error"):
            out["errors"].append(f"{os.path.basename(path)} : {summ['error']}")
            continue
        if events:
            write_events(store, summ.get("session_id"), events)
            merge_segments(store, summ.get("session_id"), int(rcfg.get("max_segments", 30) or 30))
        files[norm] = new_state
        save_state(home, state)
        if sink is not None and events:
            sink(summ, events)
        if summ["lines"]:
            out["files"] += 1
            out["lines"] += summ["lines"]
            out["events"] += summ["events"]
            out["bytes"] += summ["bytes"]
            s = out["sessions"].setdefault(summ.get("session_id") or "?", {"threads": 0, "events": 0})
            s["threads"] += 1
            s["events"] += summ["events"]
    # * Trace du passage, pour verifier apres coup la derniere collecte et ses erreurs (`doctor`). Les erreurs des
    #   passages precedents sont gardees (20 au plus) avec leur date : une erreur passee ne disparait pas en silence.
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["last_run"] = {"time": now, "files_checked": len(paths), "files_read": out["files"], "lines": out["lines"],
                         "events": out["events"], "errors": len(out["errors"])}
    if out["events"]:
        state["last_import"] = {"time": now, "files": out["files"], "events": out["events"]}
    if out["errors"]:
        state["errors"] = ([{"time": now, "error": e[:300]} for e in out["errors"]] + list(state.get("errors") or []))[:20]
    try:
        save_state(home, state)
    except OSError as exc:
        out["errors"].append(f"etat de l'import non enregistre : {type(exc).__name__}: {exc}")
    return out

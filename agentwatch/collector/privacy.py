"""Confidentialite : masquage des secrets, empreintes HMAC, bornage des textes.

# * Les empreintes utilisent HMAC-SHA256 avec une cle locale (<home>/keys/hmac.key),
#   jamais exportee. Deux valeurs identiques donnent la meme empreinte, deux valeurs
#   differentes des empreintes differentes : on peut comparer sans stocker le contenu.
# ! Limite : la cle est lisible par tout processus de l'utilisateur ; l'empreinte
#   protege contre la lecture du contenu par un tiers sans la cle, pas contre un
#   processus local qui la possede. Voir docs/privacy.md.
# * Chemin chaud : HMAC calcule avec le module natif _sha2 (sans charger OpenSSL via
#   hashlib) ; repli sur hmac/hashlib si indisponible. Les deux donnent le meme resultat.
"""

from __future__ import annotations

import os
import re

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

try:  # CPython >= 3.12
    from _sha2 import sha256 as _sha256
except ImportError:  # pragma: no cover - autres implementations
    try:
        from _sha256 import sha256 as _sha256  # type: ignore[no-redef]
    except ImportError:
        _sha256 = None  # type: ignore[assignment]

KEY_DIRNAME = "keys"
KEY_FILENAME = "hmac.key"
_KEY_CACHE: dict[str, bytes] = {}

# * Un seul motif combine, applique en UNE passe : un remplacement n'est jamais
#   re-analyse (pas de masquage imbrique). L'ordre des alternatives compte.
_SECRET_RE = re.compile(
    r"(?P<pem>-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----)"
    r"|(?P<bearer_prefix>\b[Bb]earer\s+)(?P<bearer>[A-Za-z0-9_\-\.=]{12,})"
    r"|(?P<kv_prefix>(?<![<\w])(?:api[_\-]?key|secret(?:[_\-]?key)?|access[_\-]?token|refresh[_\-]?token"
    r"|auth[_\-]?token|token|password|passwd|pwd|authorization|client[_\-]?secret|private[_\-]?key)"
    r"\s*[:=]\s*[\"']?)(?!bearer\b)(?P<kv>[^\s\"'&;,<>]{6,})"
    r"|(?P<tok>\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
    r"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}\b"
    r"|\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"
    r"|\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"
    r"|\bAIza[0-9A-Za-z_\-]{30,}\b)"
    r"|(?<=://)(?P<userinfo>[^/\s:@]+:[^/\s@]+)(?=@)",
    re.S | re.I,
)
_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/home/|/Users/|~[\\/])")


def sha256_hex(data: bytes) -> str:
    if _sha256 is not None:
        return _sha256(data).hexdigest()
    import hashlib
    return hashlib.sha256(data).hexdigest()


def hmac_sha256_hex(key: bytes, msg: bytes) -> str:
    """HMAC-SHA256 (RFC 2104) sans dependre de hashlib quand _sha2 est disponible."""
    if _sha256 is None:
        import hashlib
        import hmac
        return hmac.new(key, msg, hashlib.sha256).hexdigest()
    if len(key) > 64:
        key = _sha256(key).digest()
    key = key.ljust(64, b"\0")
    ipad = bytes(b ^ 0x36 for b in key)
    opad = bytes(b ^ 0x5C for b in key)
    inner = _sha256(ipad + msg).digest()
    return _sha256(opad + inner).hexdigest()


def _read_key(key_path: str) -> bytes | None:
    try:
        with open(key_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return data if len(data) >= 32 else None


def _publish_key(tmp: str, key_path: str) -> None:
    """Rend `tmp` visible sous `key_path` SANS ecraser une cle deja creee par un autre hook.

    # ! os.replace ecraserait la cle d'un hook concurrent (ses empreintes deviendraient
    #   incomparables). Windows : os.rename echoue si la cible existe ; POSIX : os.link.
    """
    if os.name == "nt":
        os.rename(tmp, key_path)          # FileExistsError si la cle existe deja
    else:
        os.link(tmp, key_path)            # FileExistsError si la cle existe deja
        os.unlink(tmp)


def ensure_key(home: str | os.PathLike[str]) -> bytes:
    """Charge (ou cree) la cle HMAC locale et renvoie TOUJOURS celle qui est sur le disque.

    # ! Un hook ne doit jamais signer avec une cle non persistee : sinon ses empreintes ne
    #   seraient comparables a aucune autre. En cas d'echec durable, on leve OSError et
    #   l'ingestion journalise un incident au lieu d'ecrire un evenement incoherent.
    """
    home_s = os.fspath(home)
    cached = _KEY_CACHE.get(home_s)
    if cached:
        return cached
    key_dir = os.path.join(home_s, KEY_DIRNAME)
    if os.name == "nt" and os.path.isabs(key_dir) and not key_dir.startswith("\\\\?\\"):
        key_dir = "\\\\?\\" + os.path.abspath(key_dir)   # * chemins > 260 caracteres
    key_path = os.path.join(key_dir, KEY_FILENAME)
    last_error: OSError | None = None
    for attempt in range(6):
        data = _read_key(key_path)
        if data:
            _KEY_CACHE[home_s] = data
            return data
        tmp = key_path + f".tmp-{os.getpid()}-{os.urandom(2).hex()}"
        try:
            os.makedirs(key_dir, exist_ok=True)
            # ! O_BINARY : sans lui, Windows ouvre le descripteur en mode texte et os.write
            #   transforme chaque octet 0x0A en 0x0D 0x0A (cle de 33 octets constatee).
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            try:
                os.write(fd, os.urandom(32))
            finally:
                os.close(fd)
            try:
                _publish_key(tmp, key_path)
            except FileExistsError:
                pass                      # ? un autre hook a gagne la course : on relira sa cle
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            if os.name != "nt":
                try:
                    os.chmod(key_path, 0o600)
                except OSError:
                    pass
        except OSError as exc:            # ? antivirus, dossier verrouille : nouvel essai
            last_error = exc
            _sleep_backoff(attempt)
            continue
        data = _read_key(key_path)        # * toujours relire : c'est la cle reellement partagee
        if data:
            _KEY_CACHE[home_s] = data
            return data
        _sleep_backoff(attempt)
    raise OSError(f"cle HMAC illisible ou impossible a creer : {key_path} ({last_error})")


def _sleep_backoff(attempt: int) -> None:
    import time
    time.sleep(0.01 * (attempt + 1))


def fingerprint(key: bytes, value: str | bytes) -> str:
    """Empreinte HMAC-SHA256 hex (64 caracteres)."""
    if isinstance(value, str):
        value = value.encode("utf-8", errors="surrogateescape")
    return hmac_sha256_hex(key, value)


def short_fingerprint(key: bytes, value: str) -> str:
    return fingerprint(key, value)[:10]


def mask_secrets(text: str, key: bytes) -> str:
    """Remplace les secrets reconnus par <secret:xxxxxxxxxx> (empreinte HMAC courte)."""
    if not text:
        return text

    def _repl(m: re.Match[str]) -> str:
        for group, prefix_group in (("bearer", "bearer_prefix"), ("kv", "kv_prefix")):
            val = m.group(group)
            if val is not None:
                return (m.group(prefix_group) or "") + "<secret:" + short_fingerprint(key, val) + ">"
        for group in ("pem", "tok", "userinfo"):
            val = m.group(group)
            if val is not None:
                return "<secret:" + short_fingerprint(key, val) + ">"
        return m.group(0)

    return _SECRET_RE.sub(_repl, text)


def bounded(text: str | None, limit: int) -> tuple[str | None, bool]:
    """Tronque un texte a `limit` caracteres. Retourne (texte, tronque?)."""
    if text is None:
        return None, False
    if len(text) <= limit:
        return text, False
    return text[:limit] + "...", True


def mask_home(path: str | None, home: str | None) -> str | None:
    """Remplace le prefixe du dossier utilisateur par ~ (lisibilite, moins de PII)."""
    if not path or not home:
        return path
    norm_home = home.replace("\\", "/").rstrip("/")
    norm_path = path.replace("\\", "/")
    fold = os.name == "nt"
    head = norm_path.lower() if fold else norm_path
    want = norm_home.lower() if fold else norm_home
    if head.startswith(want):
        rest = norm_path[len(norm_home):]
        if rest == "" or rest.startswith("/"):
            return "~" + rest
    return path


def sanitize_text(value: Any, key: bytes, limit: int) -> tuple[str | None, bool]:
    """Chaine masquee et bornee ; les non-chaines sont converties de facon sure."""
    if value is None:
        return None, False
    if not isinstance(value, str):
        value = repr(value)
    masked = mask_secrets(value, key)
    return bounded(masked, limit)


def scrub_tree(value: Any, key: bytes, max_chars: int, home: str | None = None) -> Any:
    """Masque recursivement un JSON (dict/list/str), borne les chaines, masque le home
    dans les chaines qui ressemblent a des chemins absolus."""
    if isinstance(value, str):
        out = sanitize_text(value, key, max_chars)[0]
        if home and out and _ABS_PATH_RE.match(out):
            out = mask_home(out, home)
        return out
    if isinstance(value, dict):
        return {str(k): scrub_tree(v, key, max_chars, home) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_tree(v, key, max_chars, home) for v in value]
    return value

"""Tests : masquage des secrets, empreintes, normalisation des chemins et commandes."""

from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import unittest
from pathlib import Path

from agentwatch.collector import privacy as P
from agentwatch.core import normalize as N

KEY = b"k" * 32


class PrivacyTests(unittest.TestCase):
    def test_hmac_matches_hashlib(self) -> None:
        for n in (0, 5, 64, 65, 300):
            key = os.urandom(n) if n else b""
            msg = os.urandom(200)
            self.assertEqual(P.hmac_sha256_hex(key, msg), hmac.new(key, msg, hashlib.sha256).hexdigest())

    def test_masks_common_secrets_without_collision(self) -> None:
        a = P.mask_secrets("curl -H 'Authorization: Bearer sk-AAAAAAAAAAAAAAAAAAAAAAAA' https://x", KEY)
        b = P.mask_secrets("curl -H 'Authorization: Bearer sk-BBBBBBBBBBBBBBBBBBBBBBBB' https://x", KEY)
        self.assertNotIn("sk-AAAA", a)
        self.assertNotIn("sk-BBBB", b)
        self.assertIn("<secret:", a)
        self.assertNotEqual(a, b, "deux secrets differents ne doivent pas donner la meme identite")
        self.assertEqual(a, P.mask_secrets("curl -H 'Authorization: Bearer sk-AAAAAAAAAAAAAAAAAAAAAAAA' https://x", KEY))

    def test_masks_kv_aws_pem_userinfo(self) -> None:
        text = ("API_KEY=abcdef123456 password: hunter2hunter2 AKIAABCDEFGHIJKLMNOP "
                "https://user:pw@host/x -----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY----- ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ12")
        out = P.mask_secrets(text, KEY)
        for leaked in ("abcdef123456", "hunter2hunter2", "AKIAABCDEFGHIJKLMNOP", "user:pw", "ghp_ABCDEF", "abc\n"):
            self.assertNotIn(leaked, out)
        self.assertNotIn("<secret:<secret:", out, "pas de masquage imbrique")

    def test_placeholder_not_remasked(self) -> None:
        once = P.mask_secrets("token=abcdefghijk", KEY)
        self.assertEqual(once, P.mask_secrets(once, KEY))

    def test_mask_home(self) -> None:
        self.assertEqual(P.mask_home("C:\\Users\\me\\proj\\a.py", "C:\\Users\\me"), "~/proj/a.py")
        self.assertEqual(P.mask_home("/home/me", "/home/me"), "~")
        self.assertEqual(P.mask_home("/home/meow/x", "/home/me"), "/home/meow/x")

    def test_key_created_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k1 = P.ensure_key(Path(tmp))
            P._KEY_CACHE.clear()
            k2 = P.ensure_key(Path(tmp))
            self.assertEqual(k1, k2)
            self.assertEqual(len(k1), 32)

    def test_key_bytes_are_written_verbatim(self) -> None:
        """Regression : sous Windows, un descripteur en mode texte transformait 0x0A en 0x0D0A."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(P.os, "urandom", side_effect=lambda n: b"\n" * n):
            P._KEY_CACHE.clear()
            key = P.ensure_key(Path(tmp))
            self.assertEqual(key, b"\n" * 32)
            self.assertEqual((Path(tmp) / "keys" / "hmac.key").read_bytes(), b"\n" * 32)
        P._KEY_CACHE.clear()

    def test_atomic_write_preserves_newlines(self) -> None:
        from agentwatch.collector.store import atomic_write_bytes
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "seg.jsonl"
            payload = b'{"a":1}\n{"b":2}\r\n\x00\n'
            atomic_write_bytes(target, payload)
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(list(Path(tmp).glob("*.tmp-*")))


class NormalizeTests(unittest.TestCase):
    def test_categorize(self) -> None:
        self.assertEqual(N.categorize_tool("claude-code", "Read"), ("read", None, None))
        self.assertEqual(N.categorize_tool("codex", "apply_patch"), ("edit", None, None))
        self.assertEqual(N.categorize_tool("codex", "mcp__srv__tool"), ("mcp", "srv", "tool"))
        self.assertEqual(N.categorize_tool("codex", "unknown_tool_x")[0], "unknown")

    def test_normalize_path_relative_to_project(self) -> None:
        proj = "C:\\proj" if os.name == "nt" else "/proj"
        self.assertEqual(N.normalize_path(os.path.join(proj, "src", "a.py"), proj, proj), "src/a.py")
        self.assertEqual(N.normalize_path("src/b.py", proj, proj), "src/b.py")
        self.assertEqual(N.normalize_path(proj, proj, proj), ".")
        outside = "D:\\other\\x.py" if os.name == "nt" else "/other/x.py"
        self.assertEqual(N.normalize_path(outside, proj, proj), outside.replace("\\", "/"))

    @unittest.skipUnless(os.name == "nt", "formes MSYS/WSL : Windows seulement")
    def test_msys_and_wsl_paths_map_to_the_project(self) -> None:
        proj = "C:\\Users\\me\\proj"
        self.assertEqual(N.normalize_path("/c/Users/me/proj/src/a.py", proj, proj), "src/a.py")
        self.assertEqual(N.normalize_path("/mnt/c/Users/me/proj/src/a.py", proj, proj), "src/a.py")
        self.assertEqual(N.normalize_path("src/a.py", proj, "/c/Users/me/proj"), "src/a.py")
        self.assertEqual(N.normalize_path("/c/other/x.py", proj, proj), "C:/other/x.py")
        self.assertEqual(N.normalize_path("/usr/bin/env", proj, proj), "/usr/bin/env")

    def test_shell_classification_is_conservative(self) -> None:
        self.assertEqual(N.classify_shell("cat a.txt | grep x")["kind"], "read")
        self.assertEqual(N.classify_shell("git status && git log -3")["kind"], "read")
        self.assertEqual(N.classify_shell("git commit -m x")["kind"], "write")
        self.assertEqual(N.classify_shell("cat a.txt > b.txt")["kind"], "write")
        self.assertEqual(N.classify_shell("sed -i 's/a/b/' f")["kind"], "write")
        self.assertEqual(N.classify_shell("sed -n '1,5p' f")["kind"], "read")
        self.assertEqual(N.classify_shell("python run.py")["kind"], "run")
        self.assertEqual(N.classify_shell("mystery-binary --flag")["kind"], "unknown")
        self.assertIn("a.txt", N.classify_shell("cat a.txt")["paths"])
        self.assertEqual(N.classify_shell("rtk git status")["heads"], ["git"])

    def test_patch_paths_and_error_signature(self) -> None:
        self.assertEqual(N.extract_patch_paths("*** Begin Patch\n*** Update File: a/b.py\n*** Add File: c.py\n*** End Patch"), ["a/b.py", "c.py"])
        sig = N.make_error_signature("Exit code 7: curl: (7) Failed to connect to C:\\x\\y port 443\nmore")
        self.assertEqual(sig, "Exit code <n>: curl: (<n>) Failed to connect to <path> port <n>")
        self.assertIsNone(N.make_error_signature("   "))

    def test_strip_url(self) -> None:
        self.assertEqual(N.strip_url("https://h.com/p/a?token=abc#f"), "https://h.com/p/a")


if __name__ == "__main__":
    unittest.main()

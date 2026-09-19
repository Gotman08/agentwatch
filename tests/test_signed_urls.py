"""URL presignees : signature et identifiants masques, jamais une URL en guise de tete de commande.

Constate le 2026-09-19 dans un rollout Codex : des URL d'envoi GCS presignees (x-goog-signature,
x-goog-credential) conservees en clair dans la commande, la cible et les tetes de commande.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentwatch import CLIENT_CODEX
from agentwatch.collector.privacy import mask_secrets
from agentwatch.core.normalize import classify_shell
from agentwatch.selftest import Synth

KEY = b"k" * 32
GCS = ("https://storage.googleapis.com/uploads/51d8d79d-6bf9?X-Goog-Algorithm=GOOG4-RSA-SHA256"
       "&X-Goog-Credential=svc%40proj.iam.gserviceaccount.com%2F20260910%2Fauto%2Fstorage%2Fgoog4_request"
       "&X-Goog-Date=20260910T214029Z&X-Goog-Expires=60&X-Goog-SignedHeaders=host"
       "&X-Goog-Signature=8bd7853eb33479ce8bc2f237afd312af50616726febfc002bfcca41b6c72729409929fcc2260")
S3 = ("https://b.s3.amazonaws.com/o?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIDEXAMPLE%2F20260910%2Fus"
      "&X-Amz-Security-Token=IQoJb3JpZ2luX2VjEXAMPLETOKEN&X-Amz-Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7")
SAS = "https://acct.blob.core.windows.net/c/b?sv=2021-08-06&se=2026-09-10T22%3A00Z&sp=r&sig=Zm9vYmFyYmF6cXV4c2VjcmV0c2ln"
CF = "https://d111.cloudfront.net/x.mp4?Expires=1789000000&Signature=nITfLd9SLbL7ZpTcbJ6Bk6Me2Wp4&Key-Pair-Id=K2JCJMDEHXQW5F"


class SignedUrlTests(unittest.TestCase):
    def test_signatures_and_credentials_are_masked(self) -> None:
        for url, secrets in ((GCS, ("8bd7853eb334", "gserviceaccount")), (S3, ("5d672d79c15b", "AKIDEXAMPLE", "IQoJb3JpZ2lu")),
                             (SAS, ("Zm9vYmFyYmF6",)), (CF, ("nITfLd9SLbL7",))):
            masked = mask_secrets(f'curl -X PUT --data-binary @a.png "{url}"', KEY)
            for s in secrets:
                self.assertNotIn(s, masked, url[:30])
            self.assertIn("<secret:", masked)
            self.assertIn("storage.googleapis.com" if url is GCS else "https://", masked)   # * l'hote reste lisible

    def test_url_is_never_a_command_head(self) -> None:
        info = classify_shell(f'$u = 1\n"{GCS}"\ncurl -X PUT $u')
        self.assertIn("<url>", info["heads"])
        self.assertFalse(any("goog" in h.lower() for h in info["heads"]))

    def test_stored_event_keeps_no_signature(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            s = Synth(Path(d), client=CLIENT_CODEX, session_id="u")
            s.session_start(); s.user_prompt()
            s.bash(f'curl -X PUT --data-binary @icon.png "{GCS}"', "ok")
            blob = "".join(p.read_text(encoding="utf-8") for p in Path(d).rglob("*.json"))
            self.assertNotIn("8bd7853eb334", blob)
            self.assertNotIn("gserviceaccount", blob)


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import hemis_session


class HemisSessionEncryptionTests(unittest.TestCase):
    def test_session_is_encrypted_and_round_trips(self):
        key = Fernet.generate_key().decode("ascii")
        session_data = {"access_token": "secret-access-token", "refresh_token": "secret-refresh-token"}

        with patch.object(hemis_session, "HEMIS_SESSION_KEY", key):
            ciphertext = hemis_session.encrypt_hemis_session(session_data)
            restored = hemis_session.decrypt_hemis_session(ciphertext)

        self.assertNotIn("secret-access-token", ciphertext)
        self.assertNotIn("secret-refresh-token", ciphertext)
        self.assertEqual(restored, session_data)

    def test_session_cannot_be_decrypted_with_another_key(self):
        first_key = Fernet.generate_key().decode("ascii")
        other_key = Fernet.generate_key().decode("ascii")
        with patch.object(hemis_session, "HEMIS_SESSION_KEY", first_key):
            ciphertext = hemis_session.encrypt_hemis_session({"cookie": "secret-cookie"})

        with patch.object(hemis_session, "HEMIS_SESSION_KEY", other_key):
            with self.assertRaisesRegex(ValueError, "cannot be decrypted"):
                hemis_session.decrypt_hemis_session(ciphertext)

    def test_missing_encryption_key_fails_closed(self):
        with patch.object(hemis_session, "HEMIS_SESSION_KEY", None):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                hemis_session.encrypt_hemis_session({"cookie": "secret-cookie"})


if __name__ == "__main__":
    unittest.main()

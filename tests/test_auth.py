import unittest

from tradingbot.auth import hash_password, normalize_email, validate_password_hash, verify_password


class TestOwnerPasswords(unittest.TestCase):
    def test_passwords_use_independent_salts_and_reject_wrong_passwords(self):
        password = "A-test-password-with-enough-length"
        first, second = hash_password(password), hash_password(password)
        self.assertNotEqual(first, second)
        self.assertNotIn(password, first)
        self.assertTrue(verify_password(password, first))
        self.assertFalse(verify_password("wrong-password", first))

    def test_invalid_credentials_and_unsupported_hashes_fail_closed(self):
        for value in ("short", "x" * 257):
            with self.assertRaises(ValueError):
                hash_password(value)
        for value in ("", "scrypt$2$8$1$00$00", "scrypt$32768$8$3$00$00", "not-a-hash"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_password_hash(value)

    def test_owner_email_normalizes_and_rejects_invalid_addresses(self):
        self.assertEqual(normalize_email(" Owner@Example.com "), "owner@example.com")
        for email in ("", "owner", "owner@", "@example.com", "owner @example.com"):
            with self.subTest(email=email), self.assertRaises(ValueError):
                normalize_email(email)

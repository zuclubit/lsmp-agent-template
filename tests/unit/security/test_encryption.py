"""
Unit tests for the EncryptionService and supporting classes.

Tests AES-256-GCM encrypt/decrypt round-trips, password-based encryption,
envelope encryption, field-level encryption, PII field detection,
batch record operations, HMAC utilities, key derivation, and key rotation.

Module under test: security/encryption/encryption_service.py
Security standards: AES-256-GCM, PBKDF2-HMAC-SHA256, HKDF-SHA256
Pattern: AAA (Arrange / Act / Assert), no I/O dependencies.
"""

from __future__ import annotations

import os
import secrets

import pytest
from cryptography.exceptions import InvalidTag

from encryption_service import (
    AES_KEY_SIZE,
    FIELD_CIPHER_PREFIX,
    GCM_NONCE_SIZE,
    GCM_TAG_SIZE,
    SALT_SIZE,
    SENSITIVE_MIGRATION_FIELDS,
    AESGCMCipher,
    DataKey,
    EncryptedData,
    EncryptionAlgorithm,
    EncryptionService,
    KeyDerivation,
    KeyType,
    get_encryption_service,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def master_key() -> bytes:
    """Deterministic 32-byte master key for reproducible tests."""
    return b"\x01" * AES_KEY_SIZE


@pytest.fixture()
def alt_key() -> bytes:
    """A different 32-byte key to verify key-dependent decryption."""
    return b"\x02" * AES_KEY_SIZE


@pytest.fixture()
def service(master_key: bytes) -> EncryptionService:
    return EncryptionService(master_key=master_key, master_key_id="test-master-v1")


@pytest.fixture()
def keyless_service() -> EncryptionService:
    """Service with no master key — for direct encrypt() tests."""
    return EncryptionService()


# ===========================================================================
# 1. AESGCMCipher — low-level encrypt / decrypt
# ===========================================================================


class TestAESGCMCipherEncrypt:
    """AESGCMCipher.encrypt() produces (ciphertext, nonce, tag) tuples."""

    def test_encrypt_returns_three_tuple(self, master_key):
        ct, nonce, tag = AESGCMCipher.encrypt(master_key, b"hello world")
        assert isinstance(ct, bytes)
        assert isinstance(nonce, bytes)
        assert isinstance(tag, bytes)

    def test_nonce_length_is_12_bytes(self, master_key):
        _, nonce, _ = AESGCMCipher.encrypt(master_key, b"data")
        assert len(nonce) == GCM_NONCE_SIZE

    def test_tag_length_is_16_bytes(self, master_key):
        _, _, tag = AESGCMCipher.encrypt(master_key, b"data")
        assert len(tag) == GCM_TAG_SIZE

    def test_ciphertext_differs_from_plaintext(self, master_key):
        plaintext = b"secret data"
        ct, _, _ = AESGCMCipher.encrypt(master_key, plaintext)
        assert ct != plaintext

    def test_same_plaintext_produces_different_ciphertext_each_call(self, master_key):
        ct1, nonce1, _ = AESGCMCipher.encrypt(master_key, b"same data")
        ct2, nonce2, _ = AESGCMCipher.encrypt(master_key, b"same data")
        assert nonce1 != nonce2  # random nonces → different ciphertext

    def test_wrong_key_size_raises_value_error(self):
        with pytest.raises(ValueError, match="Key must be"):
            AESGCMCipher.encrypt(b"short", b"data")

    def test_custom_nonce_is_used(self, master_key):
        nonce = b"\xaa" * GCM_NONCE_SIZE
        _, used_nonce, _ = AESGCMCipher.encrypt(master_key, b"data", nonce=nonce)
        assert used_nonce == nonce


class TestAESGCMCipherDecrypt:
    """AESGCMCipher.decrypt() recovers plaintext and validates authentication."""

    def test_decrypt_recovers_plaintext(self, master_key):
        plaintext = b"hello cryptography"
        ct, nonce, tag = AESGCMCipher.encrypt(master_key, plaintext)
        recovered = AESGCMCipher.decrypt(master_key, ct, nonce, tag)
        assert recovered == plaintext

    def test_decrypt_with_aad_succeeds(self, master_key):
        aad = b"context:field:ssn"
        ct, nonce, tag = AESGCMCipher.encrypt(master_key, b"123-45-6789", aad=aad)
        recovered = AESGCMCipher.decrypt(master_key, ct, nonce, tag, aad=aad)
        assert recovered == b"123-45-6789"

    def test_decrypt_with_wrong_aad_raises_invalid_tag(self, master_key):
        ct, nonce, tag = AESGCMCipher.encrypt(master_key, b"data", aad=b"correct-aad")
        with pytest.raises(InvalidTag):
            AESGCMCipher.decrypt(master_key, ct, nonce, tag, aad=b"wrong-aad")

    def test_decrypt_with_tampered_ciphertext_raises_invalid_tag(self, master_key):
        ct, nonce, tag = AESGCMCipher.encrypt(master_key, b"data")
        tampered_ct = bytes([ct[0] ^ 0xFF]) + ct[1:]
        with pytest.raises(InvalidTag):
            AESGCMCipher.decrypt(master_key, tampered_ct, nonce, tag)

    def test_decrypt_with_wrong_key_raises_invalid_tag(self, master_key, alt_key):
        ct, nonce, tag = AESGCMCipher.encrypt(master_key, b"secret")
        with pytest.raises(InvalidTag):
            AESGCMCipher.decrypt(alt_key, ct, nonce, tag)

    def test_decrypt_wrong_key_size_raises_value_error(self, master_key):
        ct, nonce, tag = AESGCMCipher.encrypt(master_key, b"data")
        with pytest.raises(ValueError, match="Key must be"):
            AESGCMCipher.decrypt(b"shortkey", ct, nonce, tag)


# ===========================================================================
# 2. KeyDerivation
# ===========================================================================


class TestKeyDerivationPBKDF2:
    """KeyDerivation.derive_pbkdf2() produces a key and salt."""

    def test_returns_key_and_salt(self):
        key, salt = KeyDerivation.derive_pbkdf2("password123")
        assert len(key) == AES_KEY_SIZE
        assert len(salt) == SALT_SIZE

    def test_same_password_same_salt_produces_same_key(self):
        salt = os.urandom(SALT_SIZE)
        key1, _ = KeyDerivation.derive_pbkdf2("pw", salt=salt)
        key2, _ = KeyDerivation.derive_pbkdf2("pw", salt=salt)
        assert key1 == key2

    def test_same_password_different_salt_produces_different_key(self):
        key1, _ = KeyDerivation.derive_pbkdf2("pw")
        key2, _ = KeyDerivation.derive_pbkdf2("pw")
        assert key1 != key2  # random salts

    def test_accepts_bytes_password(self):
        key, salt = KeyDerivation.derive_pbkdf2(b"bytes-password")
        assert len(key) == AES_KEY_SIZE

    def test_custom_key_length(self):
        key, _ = KeyDerivation.derive_pbkdf2("pw", key_length=16)
        assert len(key) == 16


class TestKeyDerivationHKDF:
    """KeyDerivation.derive_hkdf() produces purpose-specific derived keys."""

    def test_derived_key_is_32_bytes(self, master_key):
        key = KeyDerivation.derive_hkdf(master_key, info=b"migration:field:ssn")
        assert len(key) == AES_KEY_SIZE

    def test_different_info_produces_different_keys(self, master_key):
        k1 = KeyDerivation.derive_hkdf(master_key, info=b"field:email")
        k2 = KeyDerivation.derive_hkdf(master_key, info=b"field:phone")
        assert k1 != k2

    def test_same_ikm_same_info_produces_same_key(self, master_key):
        k1 = KeyDerivation.derive_hkdf(master_key, info=b"context")
        k2 = KeyDerivation.derive_hkdf(master_key, info=b"context")
        assert k1 == k2

    def test_custom_key_length(self, master_key):
        key = KeyDerivation.derive_hkdf(master_key, info=b"x", key_length=16)
        assert len(key) == 16


# ===========================================================================
# 3. EncryptionService.encrypt / decrypt (direct key)
# ===========================================================================


class TestEncryptionServiceDirectEncrypt:
    """EncryptionService.encrypt() and .decrypt() with explicit key."""

    def test_returns_encrypted_data_object(self, keyless_service, master_key):
        result = keyless_service.encrypt("hello", master_key, key_id="k1")
        assert isinstance(result, EncryptedData)

    def test_algorithm_is_aes_256_gcm(self, keyless_service, master_key):
        result = keyless_service.encrypt("hello", master_key, key_id="k1")
        assert result.algorithm == EncryptionAlgorithm.AES_256_GCM

    def test_key_id_stored(self, keyless_service, master_key):
        result = keyless_service.encrypt("hello", master_key, key_id="my-key-2025")
        assert result.key_id == "my-key-2025"

    def test_ciphertext_not_equal_to_plaintext_bytes(self, keyless_service, master_key):
        result = keyless_service.encrypt("secret", master_key, key_id="k1")
        assert result.ciphertext != b"secret"

    def test_decrypt_returns_plaintext_bytes(self, keyless_service, master_key):
        enc = keyless_service.encrypt("hello world", master_key, key_id="k1")
        plaintext = keyless_service.decrypt(enc, master_key)
        assert plaintext == b"hello world"

    def test_decrypt_with_wrong_key_raises(self, keyless_service, master_key, alt_key):
        enc = keyless_service.encrypt("secret", master_key, key_id="k1")
        with pytest.raises(InvalidTag):
            keyless_service.decrypt(enc, alt_key)

    def test_accepts_bytes_plaintext(self, keyless_service, master_key):
        enc = keyless_service.encrypt(b"bytes input", master_key, key_id="k1")
        assert keyless_service.decrypt(enc, master_key) == b"bytes input"

    def test_aad_stored_on_encrypted_data(self, keyless_service, master_key):
        aad = b"migration-context"
        enc = keyless_service.encrypt("val", master_key, key_id="k1", aad=aad)
        assert enc.aad == aad


# ===========================================================================
# 4. EncryptedData.serialize / deserialize
# ===========================================================================


class TestEncryptedDataSerialization:
    """EncryptedData.serialize() and .deserialize() preserve all fields."""

    def test_round_trip_serialization(self, keyless_service, master_key):
        enc = keyless_service.encrypt("my secret", master_key, key_id="test-key")
        serialized = enc.serialize()
        restored = EncryptedData.deserialize(serialized)
        assert restored.ciphertext == enc.ciphertext
        assert restored.nonce == enc.nonce
        assert restored.tag == enc.tag
        assert restored.key_id == enc.key_id
        assert restored.algorithm == enc.algorithm

    def test_serialized_is_string(self, keyless_service, master_key):
        enc = keyless_service.encrypt("value", master_key, key_id="k1")
        assert isinstance(enc.serialize(), str)

    def test_salt_preserved_through_serialization(self):
        enc = EncryptedData(
            ciphertext=b"ct",
            nonce=b"n" * GCM_NONCE_SIZE,
            tag=b"t" * GCM_TAG_SIZE,
            salt=b"s" * SALT_SIZE,
            key_id="pbkdf2",
            algorithm=EncryptionAlgorithm.AES_256_GCM,
        )
        restored = EncryptedData.deserialize(enc.serialize())
        assert restored.salt == enc.salt


# ===========================================================================
# 5. Password-based encryption
# ===========================================================================


class TestPasswordBasedEncryption:
    """encrypt_with_password() / decrypt_with_password() round-trips."""

    def test_round_trip_with_string_password(self, keyless_service):
        enc = keyless_service.encrypt_with_password("sensitive data", "P@ssw0rd!")
        result = keyless_service.decrypt_with_password(enc, "P@ssw0rd!")
        assert result == b"sensitive data"

    def test_salt_stored_on_encrypted_data(self, keyless_service):
        enc = keyless_service.encrypt_with_password("data", "password")
        assert enc.salt is not None
        assert len(enc.salt) == SALT_SIZE

    def test_wrong_password_raises_invalid_tag(self, keyless_service):
        enc = keyless_service.encrypt_with_password("data", "correct-password")
        with pytest.raises(InvalidTag):
            keyless_service.decrypt_with_password(enc, "wrong-password")

    def test_missing_salt_raises_value_error(self, keyless_service, master_key):
        enc = keyless_service.encrypt("data", master_key, key_id="k1")
        # No salt present on direct-encrypt EncryptedData
        with pytest.raises(ValueError, match="Salt required"):
            keyless_service.decrypt_with_password(enc, "password")

    def test_accepts_bytes_password(self, keyless_service):
        enc = keyless_service.encrypt_with_password("data", b"bytes-pw")
        result = keyless_service.decrypt_with_password(enc, b"bytes-pw")
        assert result == b"data"


# ===========================================================================
# 6. Envelope encryption
# ===========================================================================


class TestEnvelopeEncryption:
    """envelope_encrypt() and envelope_decrypt() round-trips."""

    def test_round_trip(self, service):
        enc = service.envelope_encrypt("confidential")
        plaintext = service.envelope_decrypt(enc)
        assert plaintext == b"confidential"

    def test_encrypted_data_key_present(self, service):
        enc = service.envelope_encrypt("data")
        assert enc.encrypted_data_key is not None
        assert len(enc.encrypted_data_key) > 0

    def test_key_id_is_master_key_id(self, service):
        enc = service.envelope_encrypt("data")
        assert enc.key_id == "test-master-v1"

    def test_wrong_context_raises_on_decrypt(self, service):
        enc = service.envelope_encrypt("data", context="context-A")
        with pytest.raises((InvalidTag, Exception)):
            service.envelope_decrypt(enc, context="context-B")

    def test_missing_master_key_raises_runtime_error(self, keyless_service):
        with pytest.raises(RuntimeError, match="Master key required"):
            keyless_service.envelope_encrypt("data")

    def test_missing_dek_raises_value_error(self, service, master_key):
        enc = service.encrypt("data", master_key, key_id="k1")
        with pytest.raises(ValueError, match="No encrypted data key"):
            service.envelope_decrypt(enc)

    def test_two_encryptions_of_same_plaintext_differ(self, service):
        enc1 = service.envelope_encrypt("same data")
        enc2 = service.envelope_encrypt("same data")
        assert enc1.ciphertext != enc2.ciphertext  # random DEK each time


# ===========================================================================
# 7. Field-level encryption
# ===========================================================================


class TestFieldLevelEncryption:
    """encrypt_field() and decrypt_field() for individual string values."""

    def test_encrypt_field_returns_prefixed_string(self, service):
        result = service.encrypt_field("email", "jane@example.com")
        assert result.startswith(FIELD_CIPHER_PREFIX)

    def test_encrypted_value_differs_from_original(self, service):
        result = service.encrypt_field("phone", "5551234567")
        assert result != "5551234567"

    def test_decrypt_field_recovers_original(self, service):
        original = "jane.doe@government.gov"
        encrypted = service.encrypt_field("email", original)
        recovered = service.decrypt_field("email", encrypted)
        assert recovered == original

    def test_decrypt_non_encrypted_value_returns_as_is(self, service):
        """Non-prefixed values are returned unchanged (passthrough)."""
        result = service.decrypt_field("email", "plain@text.com")
        assert result == "plain@text.com"

    def test_empty_value_returned_as_is(self, service):
        assert service.encrypt_field("email", "") == ""
        assert service.encrypt_field("email", None) is None

    def test_different_fields_produce_different_ciphertext(self, service):
        """HKDF-derived field keys must differ per field_name."""
        enc_email = service.encrypt_field("email", "user@example.com")
        enc_phone = service.encrypt_field("phone", "user@example.com")
        # Both encrypt the same value but with different field keys
        assert enc_email != enc_phone

    def test_is_field_encrypted_true_for_encrypted(self, service):
        encrypted = service.encrypt_field("ssn", "123-45-6789")
        assert service.is_field_encrypted(encrypted) is True

    def test_is_field_encrypted_false_for_plaintext(self, service):
        assert service.is_field_encrypted("plain-text") is False
        assert service.is_field_encrypted("") is False

    def test_field_key_cache_hit(self, service):
        """Calling encrypt_field twice for the same field reuses cached key."""
        service.encrypt_field("email", "a@b.com")
        service.encrypt_field("email", "c@d.com")
        assert "email" in service._field_key_cache


# ===========================================================================
# 8. encrypt_record / decrypt_record
# ===========================================================================


class TestRecordEncryption:
    """encrypt_record() and decrypt_record() on dict payloads."""

    def test_sensitive_fields_encrypted(self, service):
        record = {"Name": "Acme", "SSN": "123-45-6789", "Phone": "5551234567"}
        encrypted = service.encrypt_record(record, sensitive_fields=["SSN", "Phone"])
        assert service.is_field_encrypted(encrypted["SSN"])
        assert service.is_field_encrypted(encrypted["Phone"])

    def test_non_sensitive_fields_not_encrypted(self, service):
        record = {"Name": "Acme", "SSN": "123-45-6789"}
        encrypted = service.encrypt_record(record, sensitive_fields=["SSN"])
        assert not service.is_field_encrypted(encrypted["Name"])

    def test_decrypt_record_restores_original(self, service):
        original = {"Name": "Test Corp", "TaxId__c": "98-7654321", "Website": "acme.com"}
        encrypted = service.encrypt_record(original, sensitive_fields=["TaxId__c"])
        decrypted = service.decrypt_record(encrypted, sensitive_fields=["TaxId__c"])
        assert decrypted["TaxId__c"] == "98-7654321"
        assert decrypted["Name"] == "Test Corp"

    def test_null_sensitive_field_not_encrypted(self, service):
        record = {"SSN": None, "Name": "Acme"}
        encrypted = service.encrypt_record(record, sensitive_fields=["SSN"])
        assert encrypted["SSN"] is None

    def test_original_record_not_mutated(self, service):
        original = {"SSN": "secret"}
        _ = service.encrypt_record(original, sensitive_fields=["SSN"])
        assert original["SSN"] == "secret"

    def test_missing_sensitive_field_ignored(self, service):
        record = {"Name": "Acme"}
        result = service.encrypt_record(record, sensitive_fields=["SSN"])
        assert result == {"Name": "Acme"}


# ===========================================================================
# 9. HMAC utilities
# ===========================================================================


class TestHMACUtilities:
    """compute_hmac() and verify_hmac() for data integrity."""

    def test_compute_hmac_returns_bytes(self, master_key):
        tag = EncryptionService.compute_hmac(master_key, b"data to authenticate")
        assert isinstance(tag, bytes)
        assert len(tag) > 0

    def test_verify_hmac_succeeds_for_valid_data(self, master_key):
        data = b"authentic data"
        tag = EncryptionService.compute_hmac(master_key, data)
        assert EncryptionService.verify_hmac(master_key, data, tag) is True

    def test_verify_hmac_fails_for_tampered_data(self, master_key):
        data = b"original data"
        tag = EncryptionService.compute_hmac(master_key, data)
        assert EncryptionService.verify_hmac(master_key, b"tampered data", tag) is False

    def test_verify_hmac_fails_for_different_key(self, master_key, alt_key):
        data = b"data"
        tag = EncryptionService.compute_hmac(master_key, data)
        assert EncryptionService.verify_hmac(alt_key, data, tag) is False

    def test_same_key_and_data_produces_same_hmac(self, master_key):
        tag1 = EncryptionService.compute_hmac(master_key, b"data")
        tag2 = EncryptionService.compute_hmac(master_key, b"data")
        assert tag1 == tag2


# ===========================================================================
# 10. hash_pii
# ===========================================================================


class TestHashPII:
    """hash_pii() produces one-way pseudonymised hashes."""

    def test_returns_hex_hash_and_salt(self):
        hex_hash, salt = EncryptionService.hash_pii("jane@example.com")
        assert isinstance(hex_hash, str)
        assert isinstance(salt, bytes)
        assert len(salt) == SALT_SIZE

    def test_hash_is_not_reversible(self):
        value = "secret-ssn-123"
        hex_hash, _ = EncryptionService.hash_pii(value)
        assert value not in hex_hash

    def test_same_value_same_salt_produces_same_hash(self):
        salt = os.urandom(SALT_SIZE)
        h1, _ = EncryptionService.hash_pii("value", salt=salt)
        h2, _ = EncryptionService.hash_pii("value", salt=salt)
        assert h1 == h2

    def test_different_salts_produce_different_hashes(self):
        h1, _ = EncryptionService.hash_pii("value")
        h2, _ = EncryptionService.hash_pii("value")
        assert h1 != h2  # random salts

    def test_different_values_same_salt_differ(self):
        salt = os.urandom(SALT_SIZE)
        h1, _ = EncryptionService.hash_pii("alice@example.com", salt=salt)
        h2, _ = EncryptionService.hash_pii("bob@example.com", salt=salt)
        assert h1 != h2


# ===========================================================================
# 11. generate_key / generate_key_id
# ===========================================================================


class TestKeyGeneration:
    """EncryptionService.generate_key() and generate_key_id() utilities."""

    def test_generate_key_is_32_bytes(self):
        key = EncryptionService.generate_key()
        assert len(key) == AES_KEY_SIZE

    def test_generated_keys_are_unique(self):
        k1 = EncryptionService.generate_key()
        k2 = EncryptionService.generate_key()
        assert k1 != k2

    def test_generate_key_id_starts_with_key_prefix(self):
        kid = EncryptionService.generate_key_id()
        assert kid.startswith("key-")

    def test_generated_key_ids_are_unique(self):
        id1 = EncryptionService.generate_key_id()
        id2 = EncryptionService.generate_key_id()
        assert id1 != id2


# ===========================================================================
# 12. SENSITIVE_MIGRATION_FIELDS registry
# ===========================================================================


class TestSensitiveFieldRegistry:
    """SENSITIVE_MIGRATION_FIELDS contains expected PII / financial fields."""

    def test_ssn_in_registry(self):
        assert "SSN" in SENSITIVE_MIGRATION_FIELDS

    def test_bank_account_in_registry(self):
        assert "BankAccountNumber__c" in SENSITIVE_MIGRATION_FIELDS

    def test_registry_is_list_of_strings(self):
        assert isinstance(SENSITIVE_MIGRATION_FIELDS, list)
        assert all(isinstance(f, str) for f in SENSITIVE_MIGRATION_FIELDS)

    def test_registry_not_empty(self):
        assert len(SENSITIVE_MIGRATION_FIELDS) > 0

    @pytest.mark.parametrize("field", [
        "SSN", "TaxId__c", "CreditCardNumber__c", "PasswordHash__c", "ApiKey__c",
    ])
    def test_critical_fields_present(self, field):
        assert field in SENSITIVE_MIGRATION_FIELDS


# ===========================================================================
# 13. DataKey dataclass
# ===========================================================================


class TestDataKey:
    """DataKey stores metadata alongside key bytes."""

    def test_datakey_stores_key_bytes(self, master_key):
        dk = DataKey(
            key_id="dk-001",
            key_bytes=master_key,
            key_type=KeyType.DATA_KEY,
        )
        assert dk.key_bytes == master_key

    def test_datakey_created_at_is_set(self, master_key):
        dk = DataKey(key_id="dk-001", key_bytes=master_key, key_type=KeyType.FIELD_KEY)
        assert dk.created_at is not None

    def test_default_algorithm_is_aesgcm(self, master_key):
        dk = DataKey(key_id="dk-001", key_bytes=master_key, key_type=KeyType.DATA_KEY)
        assert dk.algorithm == EncryptionAlgorithm.AES_256_GCM


# ===========================================================================
# 14. get_encryption_service factory
# ===========================================================================


class TestGetEncryptionServiceFactory:
    """get_encryption_service() factory reads config from env vars."""

    def test_factory_returns_encryption_service_from_env(self, monkeypatch, master_key):
        import base64
        encoded = base64.b64encode(master_key).decode()
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY_B64", encoded)
        svc = get_encryption_service()
        assert isinstance(svc, EncryptionService)

    def test_factory_raises_for_wrong_key_length(self, monkeypatch):
        import base64
        short_key = base64.b64encode(b"short").decode()
        monkeypatch.setenv("ENCRYPTION_MASTER_KEY_B64", short_key)
        with pytest.raises(ValueError, match="Master key must be"):
            get_encryption_service()

    def test_factory_falls_back_to_dev_seed_when_no_env(self, monkeypatch):
        monkeypatch.delenv("ENCRYPTION_MASTER_KEY_B64", raising=False)
        monkeypatch.delenv("DEV_ENCRYPTION_SEED", raising=False)
        # Should not raise; uses static dev salt + default seed
        svc = get_encryption_service()
        assert isinstance(svc, EncryptionService)

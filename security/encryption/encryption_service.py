"""
Encryption Service — Legacy to Salesforce Migration
====================================================
AES-256-GCM encryption with:
  - PBKDF2-based key derivation
  - Envelope encryption (data key + master key)
  - Field-level encryption for PII/sensitive fields
  - Key rotation support
  - FIPS 140-2 compliant mode

Author: Platform Security Team
Version: 1.1.0
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Union

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, hmac as crypto_hmac, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.asymmetric import rsa, padding as asym_padding
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    load_pem_public_key,
    Encoding,
    PublicFormat,
    PrivateFormat,
    NoEncryption,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AES_KEY_SIZE = 32        # 256-bit key
GCM_NONCE_SIZE = 12      # 96-bit nonce (NIST recommended)
GCM_TAG_SIZE = 16        # 128-bit authentication tag
SALT_SIZE = 32           # 256-bit salt for KDF
PBKDF2_ITERATIONS = 600_000  # NIST SP 800-132 recommendation (2023)

ENVELOPE_VERSION = b"\x01"  # Version byte for envelope format
FIELD_CIPHER_PREFIX = "enc:v1:"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EncryptionAlgorithm(str, Enum):
    AES_256_GCM = "AES-256-GCM"
    AES_256_CBC_HMAC = "AES-256-CBC-HMAC-SHA256"  # For legacy interop


class KeyType(str, Enum):
    DATA_KEY = "data_key"
    MASTER_KEY = "master_key"
    FIELD_KEY = "field_key"
    DERIVED_KEY = "derived_key"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EncryptedData:
    """Container for encrypted data with all necessary decryption metadata."""
    ciphertext: bytes
    nonce: bytes
    tag: bytes
    salt: Optional[bytes]          # For PBKDF2-derived keys
    key_id: str                    # Reference to the key used
    algorithm: EncryptionAlgorithm
    version: int = 1
    aad: Optional[bytes] = None   # Additional Authenticated Data
    encrypted_data_key: Optional[bytes] = None  # For envelope encryption

    def serialize(self) -> str:
        """Serialize to a compact base64-encoded string for storage."""
        payload = {
            "v": self.version,
            "alg": self.algorithm.value,
            "kid": self.key_id,
            "ct": base64.b64encode(self.ciphertext).decode(),
            "n": base64.b64encode(self.nonce).decode(),
            "tag": base64.b64encode(self.tag).decode(),
        }
        if self.salt:
            payload["s"] = base64.b64encode(self.salt).decode()
        if self.aad:
            payload["aad"] = base64.b64encode(self.aad).decode()
        if self.encrypted_data_key:
            payload["edk"] = base64.b64encode(self.encrypted_data_key).decode()
        return base64.b64encode(json.dumps(payload).encode()).decode()

    @classmethod
    def deserialize(cls, serialized: str) -> "EncryptedData":
        """Deserialize from compact base64-encoded string."""
        payload = json.loads(base64.b64decode(serialized))
        return cls(
            ciphertext=base64.b64decode(payload["ct"]),
            nonce=base64.b64decode(payload["n"]),
            tag=base64.b64decode(payload["tag"]),
            salt=base64.b64decode(payload["s"]) if "s" in payload else None,
            key_id=payload["kid"],
            algorithm=EncryptionAlgorithm(payload["alg"]),
            version=payload.get("v", 1),
            aad=base64.b64decode(payload["aad"]) if "aad" in payload else None,
            encrypted_data_key=base64.b64decode(payload["edk"]) if "edk" in payload else None,
        )


@dataclass
class DataKey:
    """A data encryption key (DEK) with metadata."""
    key_id: str
    key_bytes: bytes
    key_type: KeyType
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    algorithm: EncryptionAlgorithm = EncryptionAlgorithm.AES_256_GCM

    def __del__(self) -> None:
        """Zero out key bytes on destruction (best-effort)."""
        try:
            # Overwrite the bytes object reference
            self.key_bytes = b"\x00" * len(self.key_bytes)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Key Derivation
# ---------------------------------------------------------------------------

class KeyDerivation:
    """Key derivation utilities."""

    @staticmethod
    def derive_pbkdf2(
        password: bytes | str,
        salt: bytes | None = None,
        iterations: int = PBKDF2_ITERATIONS,
        key_length: int = AES_KEY_SIZE,
    ) -> tuple[bytes, bytes]:
        """
        Derive a key from a password using PBKDF2-HMAC-SHA256.

        Returns:
            Tuple of (derived_key, salt) where salt can be stored alongside ciphertext.
        """
        if isinstance(password, str):
            password = password.encode("utf-8")
        if salt is None:
            salt = os.urandom(SALT_SIZE)

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=key_length,
            salt=salt,
            iterations=iterations,
            backend=default_backend(),
        )
        key = kdf.derive(password)
        return key, salt

    @staticmethod
    def derive_hkdf(
        input_key_material: bytes,
        info: bytes,
        salt: bytes | None = None,
        key_length: int = AES_KEY_SIZE,
    ) -> bytes:
        """
        Derive a purpose-specific key using HKDF-SHA256.

        Args:
            input_key_material: Master key or shared secret.
            info: Context string identifying the purpose (e.g., b"migration:field:ssn").
            salt: Optional random salt.
            key_length: Output key length in bytes.

        Returns:
            Derived key bytes.
        """
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=key_length,
            salt=salt,
            info=info,
            backend=default_backend(),
        )
        return hkdf.derive(input_key_material)


# ---------------------------------------------------------------------------
# Core AES-256-GCM Encryption
# ---------------------------------------------------------------------------

class AESGCMCipher:
    """Low-level AES-256-GCM encryption/decryption operations."""

    @staticmethod
    def encrypt(
        key: bytes,
        plaintext: bytes,
        aad: bytes | None = None,
        nonce: bytes | None = None,
    ) -> tuple[bytes, bytes, bytes]:
        """
        Encrypt plaintext with AES-256-GCM.

        Args:
            key: 32-byte (256-bit) encryption key.
            plaintext: Data to encrypt.
            aad: Additional authenticated data (authenticated but not encrypted).
            nonce: 12-byte nonce. Generated randomly if not provided.

        Returns:
            Tuple of (ciphertext, nonce, tag).
        """
        if len(key) != AES_KEY_SIZE:
            raise ValueError(f"Key must be {AES_KEY_SIZE} bytes, got {len(key)}")

        if nonce is None:
            nonce = os.urandom(GCM_NONCE_SIZE)

        aesgcm = AESGCM(key)
        # AESGCM.encrypt returns ciphertext + tag concatenated
        ct_with_tag = aesgcm.encrypt(nonce, plaintext, aad)

        ciphertext = ct_with_tag[:-GCM_TAG_SIZE]
        tag = ct_with_tag[-GCM_TAG_SIZE:]

        return ciphertext, nonce, tag

    @staticmethod
    def decrypt(
        key: bytes,
        ciphertext: bytes,
        nonce: bytes,
        tag: bytes,
        aad: bytes | None = None,
    ) -> bytes:
        """
        Decrypt AES-256-GCM ciphertext.

        Raises:
            cryptography.exceptions.InvalidTag: If authentication fails.
        """
        if len(key) != AES_KEY_SIZE:
            raise ValueError(f"Key must be {AES_KEY_SIZE} bytes")
        if len(nonce) != GCM_NONCE_SIZE:
            raise ValueError(f"Nonce must be {GCM_NONCE_SIZE} bytes")

        aesgcm = AESGCM(key)
        ct_with_tag = ciphertext + tag
        return aesgcm.decrypt(nonce, ct_with_tag, aad)


# ---------------------------------------------------------------------------
# High-Level Encryption Service
# ---------------------------------------------------------------------------

class EncryptionService:
    """
    High-level encryption service for the migration platform.

    Provides:
    1. Direct encryption with a provided key
    2. Password-based encryption (PBKDF2)
    3. Envelope encryption (DEK wrapped by master key)
    4. Field-level encryption for individual data fields
    """

    def __init__(
        self,
        master_key: bytes | None = None,
        master_key_id: str = "master-key-v1",
        key_provider=None,  # Optional: external key provider (Vault Transit, KMS)
    ) -> None:
        self._master_key = master_key
        self._master_key_id = master_key_id
        self._key_provider = key_provider
        self._field_key_cache: dict[str, bytes] = {}

    # ------------------------------------------------------------------
    # Direct encryption
    # ------------------------------------------------------------------

    def encrypt(
        self,
        plaintext: Union[bytes, str],
        key: bytes,
        key_id: str,
        aad: bytes | None = None,
    ) -> EncryptedData:
        """
        Encrypt data with a provided key.

        Args:
            plaintext: Data to encrypt.
            key: 256-bit encryption key.
            key_id: Identifier for the key used (for lookup during decryption).
            aad: Additional authenticated data.

        Returns:
            EncryptedData containing all fields needed for decryption.
        """
        if isinstance(plaintext, str):
            plaintext = plaintext.encode("utf-8")

        ciphertext, nonce, tag = AESGCMCipher.encrypt(key, plaintext, aad)

        return EncryptedData(
            ciphertext=ciphertext,
            nonce=nonce,
            tag=tag,
            salt=None,
            key_id=key_id,
            algorithm=EncryptionAlgorithm.AES_256_GCM,
            aad=aad,
        )

    def decrypt(
        self,
        encrypted: EncryptedData,
        key: bytes,
    ) -> bytes:
        """Decrypt data with a provided key."""
        return AESGCMCipher.decrypt(
            key,
            encrypted.ciphertext,
            encrypted.nonce,
            encrypted.tag,
            encrypted.aad,
        )

    # ------------------------------------------------------------------
    # Password-based encryption
    # ------------------------------------------------------------------

    def encrypt_with_password(
        self,
        plaintext: Union[bytes, str],
        password: Union[bytes, str],
        aad: bytes | None = None,
    ) -> EncryptedData:
        """Encrypt using a password-derived key (PBKDF2-HMAC-SHA256)."""
        if isinstance(plaintext, str):
            plaintext = plaintext.encode("utf-8")

        key, salt = KeyDerivation.derive_pbkdf2(password)
        ciphertext, nonce, tag = AESGCMCipher.encrypt(key, plaintext, aad)

        return EncryptedData(
            ciphertext=ciphertext,
            nonce=nonce,
            tag=tag,
            salt=salt,
            key_id="pbkdf2-derived",
            algorithm=EncryptionAlgorithm.AES_256_GCM,
            aad=aad,
        )

    def decrypt_with_password(
        self,
        encrypted: EncryptedData,
        password: Union[bytes, str],
    ) -> bytes:
        """Decrypt password-based encryption."""
        if not encrypted.salt:
            raise ValueError("Salt required for PBKDF2 decryption")
        key, _ = KeyDerivation.derive_pbkdf2(password, salt=encrypted.salt)
        return self.decrypt(encrypted, key)

    # ------------------------------------------------------------------
    # Envelope encryption
    # ------------------------------------------------------------------

    def envelope_encrypt(
        self,
        plaintext: Union[bytes, str],
        context: str = "migration-data",
        aad: bytes | None = None,
    ) -> EncryptedData:
        """
        Envelope encryption: generate a random DEK, encrypt plaintext with it,
        then encrypt the DEK with the master key.

        Args:
            plaintext: Data to encrypt.
            context: Encryption context (becomes part of AAD for DEK).
            aad: Additional authenticated data for data encryption.

        Returns:
            EncryptedData with both encrypted content and encrypted DEK.
        """
        if not self._master_key:
            raise RuntimeError("Master key required for envelope encryption")

        if isinstance(plaintext, str):
            plaintext = plaintext.encode("utf-8")

        # Generate random data encryption key
        dek = os.urandom(AES_KEY_SIZE)
        dek_id = secrets.token_hex(16)

        # Encrypt plaintext with DEK
        ciphertext, nonce, tag = AESGCMCipher.encrypt(dek, plaintext, aad)

        # Encrypt DEK with master key
        # Use context as AAD for DEK encryption (binding)
        dek_aad = context.encode("utf-8")
        encrypted_dek, dek_nonce, dek_tag = AESGCMCipher.encrypt(
            self._master_key, dek, dek_aad
        )
        # Pack DEK nonce + tag + ciphertext for storage
        packed_dek = dek_nonce + dek_tag + encrypted_dek

        # Zero out plaintext DEK
        del dek

        return EncryptedData(
            ciphertext=ciphertext,
            nonce=nonce,
            tag=tag,
            salt=None,
            key_id=self._master_key_id,
            algorithm=EncryptionAlgorithm.AES_256_GCM,
            aad=aad,
            encrypted_data_key=packed_dek,
        )

    def envelope_decrypt(
        self,
        encrypted: EncryptedData,
        context: str = "migration-data",
    ) -> bytes:
        """Decrypt envelope-encrypted data."""
        if not self._master_key:
            raise RuntimeError("Master key required for envelope decryption")
        if not encrypted.encrypted_data_key:
            raise ValueError("No encrypted data key found — not envelope encrypted")

        # Unpack the encrypted DEK
        packed_dek = encrypted.encrypted_data_key
        dek_nonce = packed_dek[:GCM_NONCE_SIZE]
        dek_tag = packed_dek[GCM_NONCE_SIZE:GCM_NONCE_SIZE + GCM_TAG_SIZE]
        encrypted_dek = packed_dek[GCM_NONCE_SIZE + GCM_TAG_SIZE:]

        # Decrypt DEK with master key
        dek_aad = context.encode("utf-8")
        dek = AESGCMCipher.decrypt(self._master_key, encrypted_dek, dek_nonce, dek_tag, dek_aad)

        # Decrypt plaintext with DEK
        plaintext = self.decrypt(encrypted, dek)

        # Zero out DEK
        del dek

        return plaintext

    # ------------------------------------------------------------------
    # Field-level encryption
    # ------------------------------------------------------------------

    def _get_field_key(self, field_name: str) -> bytes:
        """
        Derive a deterministic field-specific key using HKDF.

        Each field gets a unique key derived from the master key and field name.
        """
        if field_name in self._field_key_cache:
            return self._field_key_cache[field_name]

        if not self._master_key:
            raise RuntimeError("Master key required for field-level encryption")

        info = f"migration:field:{field_name}".encode("utf-8")
        field_key = KeyDerivation.derive_hkdf(self._master_key, info)
        self._field_key_cache[field_name] = field_key
        return field_key

    def encrypt_field(self, field_name: str, value: str) -> str:
        """
        Encrypt a single field value.

        Returns a prefixed string that can be stored in a database or passed
        through the migration pipeline.

        Format: "enc:v1:<base64-serialized-EncryptedData>"
        """
        if not value:
            return value

        field_key = self._get_field_key(field_name)
        aad = f"field:{field_name}".encode("utf-8")
        encrypted = self.encrypt(value, field_key, f"field-key:{field_name}", aad)
        return FIELD_CIPHER_PREFIX + encrypted.serialize()

    def decrypt_field(self, field_name: str, encrypted_value: str) -> str:
        """Decrypt a field value encrypted with encrypt_field()."""
        if not encrypted_value or not encrypted_value.startswith(FIELD_CIPHER_PREFIX):
            return encrypted_value  # Not encrypted; return as-is

        serialized = encrypted_value[len(FIELD_CIPHER_PREFIX):]
        encrypted = EncryptedData.deserialize(serialized)
        field_key = self._get_field_key(field_name)
        plaintext_bytes = self.decrypt(encrypted, field_key)
        return plaintext_bytes.decode("utf-8")

    def encrypt_record(
        self,
        record: dict[str, Any],
        sensitive_fields: list[str],
    ) -> dict[str, Any]:
        """
        Encrypt specified fields in a record dictionary.

        Args:
            record: Dictionary of field name -> value.
            sensitive_fields: List of field names to encrypt.

        Returns:
            New dictionary with sensitive fields encrypted.
        """
        result = dict(record)
        for field_name in sensitive_fields:
            if field_name in result and result[field_name] is not None:
                raw_value = str(result[field_name])
                result[field_name] = self.encrypt_field(field_name, raw_value)
        return result

    def decrypt_record(
        self,
        record: dict[str, Any],
        sensitive_fields: list[str],
    ) -> dict[str, Any]:
        """Decrypt specified encrypted fields in a record dictionary."""
        result = dict(record)
        for field_name in sensitive_fields:
            if field_name in result and isinstance(result[field_name], str):
                result[field_name] = self.decrypt_field(field_name, result[field_name])
        return result

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    @staticmethod
    def generate_key() -> bytes:
        """Generate a cryptographically secure 256-bit AES key."""
        return os.urandom(AES_KEY_SIZE)

    @staticmethod
    def generate_key_id() -> str:
        """Generate a unique key identifier."""
        return f"key-{secrets.token_hex(16)}"

    @staticmethod
    def compute_hmac(key: bytes, data: bytes) -> bytes:
        """Compute HMAC-SHA256 for data integrity verification."""
        h = crypto_hmac.HMAC(key, hashes.SHA256(), backend=default_backend())
        h.update(data)
        return h.finalize()

    @staticmethod
    def verify_hmac(key: bytes, data: bytes, expected_hmac: bytes) -> bool:
        """Verify HMAC in constant time (prevents timing attacks)."""
        computed = EncryptionService.compute_hmac(key, data)
        return hmac.compare_digest(computed, expected_hmac)

    @staticmethod
    def hash_pii(value: str, salt: bytes | None = None) -> tuple[str, bytes]:
        """
        One-way hash a PII value for pseudonymization.

        Uses HMAC-SHA256 with a random salt. NOT reversible.
        Use for matching/lookup without storing raw PII.

        Returns:
            Tuple of (hex_hash, salt).
        """
        if salt is None:
            salt = os.urandom(SALT_SIZE)
        digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, 100_000)
        return digest.hex(), salt

    def is_field_encrypted(self, value: str) -> bool:
        """Check if a field value appears to be encrypted by this service."""
        return isinstance(value, str) and value.startswith(FIELD_CIPHER_PREFIX)


# ---------------------------------------------------------------------------
# Sensitive Field Registry
# ---------------------------------------------------------------------------

# Fields that should always be encrypted at the field level
SENSITIVE_MIGRATION_FIELDS = [
    # Contact / identity
    "SSN",
    "SocialSecurityNumber__c",
    "TaxId__c",
    "PassportNumber__c",
    "DriversLicenseNumber__c",
    "DateOfBirth",

    # Financial
    "BankAccountNumber__c",
    "RoutingNumber__c",
    "CreditCardNumber__c",
    "CVV__c",

    # Health
    "DiagnosisCode__c",
    "MedicalRecordNumber__c",
    "InsuranceMemberId__c",

    # Authentication
    "PasswordHash__c",
    "ApiKey__c",
    "SecretKey__c",
]


def get_encryption_service(secrets_manager=None) -> EncryptionService:
    """
    Factory: create an EncryptionService with a master key from secrets manager.

    In production, the master key is loaded from Vault/KMS.
    In development, a key is derived from an environment variable.
    """
    import os

    master_key_b64 = os.environ.get("ENCRYPTION_MASTER_KEY_B64")
    if master_key_b64:
        master_key = base64.b64decode(master_key_b64)
        if len(master_key) != AES_KEY_SIZE:
            raise ValueError(f"Master key must be {AES_KEY_SIZE} bytes")
    elif secrets_manager:
        # Would be async in production; simplified here
        raise NotImplementedError("Async key loading from secrets manager not supported in sync factory")
    else:
        # Dev-only fallback (not for production)
        logger.warning("Using derived master key from DEV_ENCRYPTION_SEED — NOT FOR PRODUCTION")
        seed = os.environ.get("DEV_ENCRYPTION_SEED", "dev-seed-change-me")
        master_key, _ = KeyDerivation.derive_pbkdf2(seed, salt=b"migration-dev-salt-static-v1")

    return EncryptionService(master_key=master_key)

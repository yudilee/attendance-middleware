import base64
import hashlib
from cryptography.fernet import Fernet
from app.config import settings

def _get_fernet_key(key_str: str) -> bytes:
    """Ensure the key is a valid 32-byte URL-safe base64 key."""
    hashed = hashlib.sha256(key_str.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(hashed)

# Initialize Fernet using our derived key
_key = _get_fernet_key(settings.encryption_key)
_cipher = Fernet(_key)

def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string using Fernet, returning a string with 'enc:' prefix."""
    if not plaintext:
        return ""
    encrypted = _cipher.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return f"enc:{encrypted}"

def decrypt_value(ciphertext: str) -> str:
    """Decrypt an encrypted string (must start with 'enc:'). Returns original plaintext."""
    if not ciphertext:
        return ""
    if not ciphertext.startswith("enc:"):
        return ciphertext
    try:
        raw_ciphertext = ciphertext[4:]  # Strip 'enc:'
        decrypted = _cipher.decrypt(raw_ciphertext.encode("utf-8")).decode("utf-8")
        return decrypted
    except Exception:
        # Gracefully handle decryption failure, returning the ciphertext itself
        return ciphertext

def is_encrypted(value: str) -> bool:
    """Check if the value is encrypted (starts with 'enc:')."""
    return bool(value and value.startswith("enc:"))

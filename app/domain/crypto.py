"""AES-256-CBC + PKCS7 decryption for the grummer gateway (pure, no I/O)."""
import base64

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_BLOCK_BITS = algorithms.AES.block_size  # 128


class DecryptError(Exception):
    """Raised when a grummer payload cannot be decrypted (bad key, IV, or padding)."""


def load_key(hex_string: str) -> bytes:
    """Parse the 32-byte AES-256 key from its hex representation."""
    key = bytes.fromhex(hex_string.strip())
    if len(key) != 32:
        raise ValueError(f"AES-256 key must be 32 bytes, got {len(key)}")
    return key


def decrypt_grummer(iv_b64: str, ciphertext_b64: str, key: bytes) -> bytes:
    """Decrypt a base64 IV + ciphertext. Returns the plaintext bytes (still JSON, unparsed)."""
    try:
        iv = base64.b64decode(iv_b64)
        ciphertext = base64.b64decode(ciphertext_b64)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(_BLOCK_BITS).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except Exception as exc:  # base64 / block-size / padding failures all mean "undecryptable"
        raise DecryptError(str(exc)) from exc

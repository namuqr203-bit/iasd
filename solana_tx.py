"""Minimal Solana helpers: base58, keypair loading, and signing a
partially-signed transaction (legacy or versioned) without heavy SDKs."""

import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\0" * pad + full


class Keypair:
    def __init__(self, seed: bytes):
        self._sk = Ed25519PrivateKey.from_private_bytes(seed)
        self.public_key = self._sk.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @classmethod
    def from_secret(cls, secret: str) -> "Keypair":
        """Accepts a Phantom-style base58 secret key or a JSON byte array
        (solana-keygen format)."""
        secret = secret.strip()
        raw = bytes(json.loads(secret)) if secret.startswith("[") else b58decode(secret)
        if len(raw) not in (32, 64):
            raise ValueError("Private key must be 32 or 64 bytes")
        kp = cls(raw[:32])
        if len(raw) == 64 and raw[32:] != kp.public_key:
            raise ValueError("Private key does not match its public key")
        return kp

    @property
    def address(self) -> str:
        return b58encode(self.public_key)

    @property
    def secret_b58(self) -> str:
        """64-byte secret key in base58 — the format Phantom/Solflare import."""
        seed = self._sk.private_bytes(serialization.Encoding.Raw,
                                      serialization.PrivateFormat.Raw,
                                      serialization.NoEncryption())
        return b58encode(seed + self.public_key)

    def sign(self, msg: bytes) -> bytes:
        return self._sk.sign(msg)


def _compact_u16(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def build_transfer_tx(kp: Keypair, to_address: str, lamports: int, blockhash: str) -> str:
    """Signed legacy SystemProgram transfer, returned as base64 wire bytes."""
    to = b58decode(to_address)
    if len(to) != 32:
        raise ValueError("Bad destination address")
    system_program = bytes(32)
    data = (2).to_bytes(4, "little") + int(lamports).to_bytes(8, "little")
    msg = (
        bytes([1, 0, 1])                        # header: 1 signer, 0 ro-signed, 1 ro-unsigned
        + _compact_u16(3) + kp.public_key + to + system_program
        + b58decode(blockhash).rjust(32, b"\0")
        + _compact_u16(1)                       # one instruction
        + bytes([2]) + _compact_u16(2) + bytes([0, 1])
        + _compact_u16(len(data)) + data
    )
    tx = _compact_u16(1) + kp.sign(msg) + msg
    return base64.b64encode(tx).decode()


def _read_compact_u16(buf: bytes, off: int):
    val, shift = 0, 0
    while True:
        b = buf[off]
        off += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, off
        shift += 7


def sign_serialized_tx(tx_b64: str, kp: Keypair) -> str:
    """Adds kp's signature to a base64 wire transaction, keeping any
    signatures already present (e.g. Slotana's build signature)."""
    raw = bytearray(base64.b64decode(tx_b64))
    num_sigs, off = _read_compact_u16(raw, 0)
    sig_start = off
    msg_start = off + 64 * num_sigs
    msg = bytes(raw[msg_start:])

    p = 1 if msg[0] & 0x80 else 0  # versioned message prefix
    num_required = msg[p]
    num_keys, k = _read_compact_u16(msg, p + 3)
    keys = [msg[k + 32 * i: k + 32 * (i + 1)] for i in range(num_keys)]

    try:
        idx = keys.index(kp.public_key)
    except ValueError:
        raise ValueError("Wallet is not part of this transaction")
    if idx >= num_required or idx >= num_sigs:
        raise ValueError("Wallet is not a required signer of this transaction")

    pos = sig_start + 64 * idx
    raw[pos:pos + 64] = kp.sign(msg)
    return base64.b64encode(bytes(raw)).decode()

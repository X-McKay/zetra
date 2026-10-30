"""AES-256-GCM payload codec (13.9): the Temporal server and UI never see task content."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import os
from collections.abc import Sequence
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    DataConverter,
    DefaultFailureConverterWithEncodedAttributes,
    PayloadCodec,
)

ENCODING = b"binary/encrypted"


class AesGcmCodec(PayloadCodec):
    def __init__(self, key: bytes, tenant: str):
        if len(key) != 32:
            raise ValueError("payload key must be 32 bytes")
        self._aead = AESGCM(key)
        self._aad = tenant.encode()  # a payload cannot be replayed into another tenant
        self.key_id = hashlib.sha256(key).hexdigest()[:16].encode()

    @classmethod
    def from_file(cls, path: str | Path, tenant: str) -> AesGcmCodec:
        raw = Path(path).read_bytes().strip()
        key = raw if len(raw) == 32 else base64.b64decode(raw, validate=True)
        return cls(key, tenant)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out = []
        for p in payloads:
            nonce = os.urandom(12)
            sealed = self._aead.encrypt(nonce, p.SerializeToString(), self._aad)
            out.append(
                Payload(
                    metadata={"encoding": ENCODING, "encryption-key-id": self.key_id},
                    data=nonce + sealed,
                )
            )
        return out

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out = []
        for p in payloads:
            if p.metadata.get("encoding") != ENCODING:
                out.append(p)
                continue
            if p.metadata.get("encryption-key-id") != self.key_id:
                raise ValueError("payload encrypted under another key")
            plain = self._aead.decrypt(p.data[:12], p.data[12:], self._aad)
            out.append(Payload.FromString(plain))
        return out


def data_converter(codec: PayloadCodec) -> DataConverter:
    # Failure messages and stack traces are encoded too, so an error string cannot leak content.
    return dataclasses.replace(
        DataConverter.default,
        payload_codec=codec,
        failure_converter_class=DefaultFailureConverterWithEncodedAttributes,
    )

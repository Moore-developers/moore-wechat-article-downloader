#!/usr/bin/env python3
"""WeChat Channels video header decryption helpers.

The downloaded mp4 is XOR-obfuscated in the first 128 KiB when decodeKey is
present. The key stream is generated from a numeric WxIsaac64 seed.
"""

from __future__ import annotations

from pathlib import Path


MASK64 = (1 << 64) - 1
WX_KEY_BYTES = 131072


def _u64(value: int) -> int:
    return value & MASK64


def _mix(values: list[int]) -> list[int]:
    a, b, c, d, e, f, g, h = values
    a = _u64(a - e)
    f = _u64(f ^ (h >> 9))
    h = _u64(h + a)
    b = _u64(b - f)
    g = _u64(g ^ _u64(a << 9))
    a = _u64(a + b)
    c = _u64(c - g)
    h = _u64(h ^ (b >> 23))
    b = _u64(b + c)
    d = _u64(d - h)
    a = _u64(a ^ _u64(c << 15))
    c = _u64(c + d)
    e = _u64(e - a)
    b = _u64(b ^ (d >> 14))
    d = _u64(d + e)
    f = _u64(f - b)
    c = _u64(c ^ _u64(e << 20))
    e = _u64(e + f)
    g = _u64(g - c)
    d = _u64(d ^ (f >> 17))
    f = _u64(f + g)
    h = _u64(h - d)
    e = _u64(e ^ _u64(g << 14))
    g = _u64(g + h)
    return [_u64(item) for item in (a, b, c, d, e, f, g, h)]


class WxIsaac64:
    """Small ISAAC64 implementation matching the public WxIsaac64 analysis."""

    def __init__(self, seed: int) -> None:
        self.mem = [0] * 256
        self.mem[0] = seed & MASK64
        self.a = 0
        self.b = 0
        self.c = 0
        self._init_memory()

    def _init_memory(self) -> None:
        values = [
            0x647C4677A2884B7C,
            0xB9F8B322C73AC862,
            0x8C0EA5053D4712A0,
            0xB29B2E824A595524,
            0x82F053DB8355E0CE,
            0x48FE4A0FA5A09315,
            0xAE985BF2CBFC89ED,
            0x98F5704F6C44C0AB,
        ]
        for _ in range(2):
            for index in range(0, 256, 8):
                values = [_u64(values[offset] + self.mem[index + offset]) for offset in range(8)]
                values = _mix(values)
                self.mem[index : index + 8] = values

    def _ind(self, value: int) -> int:
        return self.mem[(value & ((256 - 1) << 3)) >> 3]

    def block(self) -> list[int]:
        self.c = _u64(self.c + 1)
        a = self.a
        b = _u64(self.b + self.c)
        result = [0] * 256
        for index in range(256):
            if index & 3 == 0:
                mixed = _u64(~(a ^ _u64(a << 21)))
            elif index & 3 == 1:
                mixed = _u64(a ^ (a >> 5))
            elif index & 3 == 2:
                mixed = _u64(a ^ _u64(a << 12))
            else:
                mixed = _u64(a ^ (a >> 33))
            x = self.mem[index]
            a = _u64(mixed + self.mem[(index + 128) & 255])
            y = _u64(self._ind(x) + a + b)
            self.mem[index] = y
            b = _u64(self._ind(y >> 8) + x)
            result[index] = b
        self.a = a
        self.b = b
        return result

    def bytes(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            for word in self.block():
                out.extend(word.to_bytes(8, "little"))
                if len(out) >= size:
                    break
        return bytes(out[:size])


def decode_key_seed(decode_key: str) -> int:
    value = str(decode_key or "").strip()
    if not value or not value.isdigit():
        raise ValueError("decodeKey must be a decimal uint64 string")
    seed = int(value, 10)
    if seed < 0 or seed > MASK64:
        raise ValueError("decodeKey is outside uint64 range")
    return seed


def decryption_array(decode_key: str, size: int = WX_KEY_BYTES) -> bytes:
    seed = decode_key_seed(decode_key)
    # The Emscripten bridge reverses the generated buffer before XOR.
    return WxIsaac64(seed).bytes(size)[::-1]


def xor_decrypt_file_header(path: Path, decode_key: str, size: int = WX_KEY_BYTES) -> int:
    key = decryption_array(decode_key, size)
    with path.open("r+b") as fh:
        chunk = fh.read(len(key))
        if not chunk:
            return 0
        decrypted = bytes(left ^ right for left, right in zip(chunk, key))
        fh.seek(0)
        fh.write(decrypted)
        return len(decrypted)

from __future__ import annotations

CAESAR_SHIFT = 7


def caesar_encrypt_filename(name: str, shift: int = CAESAR_SHIFT) -> str:
    chars = []
    for char in name:
        chars.append(_shift_char(char, shift))
    return "".join(chars)


def _shift_char(char: str, shift: int) -> str:
    if "a" <= char <= "z":
        return chr((ord(char) - ord("a") + shift) % 26 + ord("a"))
    if "A" <= char <= "Z":
        return chr((ord(char) - ord("A") + shift) % 26 + ord("A"))
    return char

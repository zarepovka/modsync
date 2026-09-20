"""Small, bounded parser for Valve's nested KeyValues text format."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..exceptions import DiscoveryMetadataError

_MAX_TEXT_LENGTH = 4 * 1024 * 1024
_MAX_TOKENS = 100_000
_MAX_DEPTH = 32


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    line: int
    structural: bool = False


def _tokenize(text: str) -> list[_Token]:
    if len(text) > _MAX_TEXT_LENGTH:
        raise DiscoveryMetadataError("Valve metadata is too large")
    tokens: list[_Token] = []
    index = 0
    line = 1
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            line += char == "\n"
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
            continue
        if char in "{}":
            tokens.append(_Token(char, line, True))
            index += 1
        elif char == '"':
            start_line = line
            index += 1
            value: list[str] = []
            while index < length:
                char = text[index]
                if char == '"':
                    index += 1
                    break
                if char == "\\":
                    index += 1
                    if index >= length:
                        raise DiscoveryMetadataError(
                            f"Unterminated escape in Valve metadata at line {start_line}"
                        )
                    escaped = text[index]
                    replacements = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
                    if escaped in replacements:
                        value.append(replacements[escaped])
                    else:
                        value.extend(("\\", escaped))
                    index += 1
                    continue
                if ord(char) < 32 and char not in "\t\r\n":
                    raise DiscoveryMetadataError(
                        f"Control character in Valve metadata at line {line}"
                    )
                line += char == "\n"
                value.append(char)
                index += 1
            else:
                raise DiscoveryMetadataError(
                    f"Unterminated string in Valve metadata at line {start_line}"
                )
            tokens.append(_Token("".join(value), start_line))
        else:
            start = index
            while index < length and not text[index].isspace() and text[index] not in '{}"':
                if ord(text[index]) < 32:
                    raise DiscoveryMetadataError(
                        f"Control character in Valve metadata at line {line}"
                    )
                index += 1
            if start == index:
                raise DiscoveryMetadataError(f"Unexpected token at line {line}")
            tokens.append(_Token(text[start:index], line))
        if len(tokens) > _MAX_TOKENS:
            raise DiscoveryMetadataError("Valve metadata contains too many tokens")
    return tokens


def parse_keyvalues(text: str) -> dict[str, Any]:
    """Parse nested Valve KeyValues data, rejecting malformed structure safely."""
    tokens = _tokenize(text.lstrip("\ufeff"))
    position = 0

    def parse_object(depth: int, closing: bool) -> dict[str, Any]:
        nonlocal position
        if depth > _MAX_DEPTH:
            raise DiscoveryMetadataError("Valve metadata is nested too deeply")
        result: dict[str, Any] = {}
        while position < len(tokens):
            token = tokens[position]
            if token.structural and token.value == "}":
                if not closing:
                    raise DiscoveryMetadataError(
                        f"Unexpected closing brace in Valve metadata at line {token.line}"
                    )
                position += 1
                return result
            if token.structural:
                raise DiscoveryMetadataError(
                    f"Expected a key in Valve metadata at line {token.line}"
                )
            key = token.value
            if not key:
                raise DiscoveryMetadataError(f"Empty Valve metadata key at line {token.line}")
            position += 1
            if position >= len(tokens):
                raise DiscoveryMetadataError(f"Missing value for {key!r} at line {token.line}")
            value_token = tokens[position]
            if value_token.structural and value_token.value == "{":
                position += 1
                value: Any = parse_object(depth + 1, True)
            elif value_token.structural:
                raise DiscoveryMetadataError(
                    f"Missing value for {key!r} at line {value_token.line}"
                )
            else:
                value = value_token.value
                position += 1
            folded = key.casefold()
            if any(existing.casefold() == folded for existing in result):
                raise DiscoveryMetadataError(f"Duplicate Valve metadata key: {key}")
            result[key] = value
        if closing:
            raise DiscoveryMetadataError("Unclosed object in Valve metadata")
        return result

    parsed = parse_object(0, False)
    if position != len(tokens):
        raise DiscoveryMetadataError("Trailing Valve metadata tokens")
    return parsed


def get_key(mapping: dict[str, Any], key: str) -> Any:
    """Fetch a KeyValues key case-insensitively."""
    folded = key.casefold()
    for candidate, value in mapping.items():
        if candidate.casefold() == folded:
            return value
    return None

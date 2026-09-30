"""Strict Postcard payload codec for the pinned native WUJI schema snapshot.

This handles payloads only: no WUJI packet header, session, transport, or device
operation is performed. Struct order comes from matching IDL declarations in
the official Python stub or explicit pinned native disassembly. schema_info()
distinguishes executed native fixtures from static order evidence. Unknown
schemas are rejected.

Primitive rules: https://postcard.jamesmunns.com/wire-format
"""
from __future__ import annotations

from collections.abc import Mapping
import operator
import struct

from ._schema_contract import NAMES, NATIVE_FIXTURE_ORDER, NATIVE_VERIFIED, SCHEMAS, STATIC_NATIVE_ORDER, UNSUPPORTED
from ._types import WujiException, _to_native


class CodecError(WujiException, ValueError):
    """Malformed or schema-incompatible payload."""


class UnsupportedSchemaError(CodecError):
    """No established structural encoding exists for this schema."""


def _schema_id(schema):
    if isinstance(schema, str):
        name = schema.removeprefix("wuji.")
        if name in NAMES:
            return NAMES[name]
        try:
            return int(schema, 0)
        except ValueError:
            raise UnsupportedSchemaError(f"Unknown WUJI schema: {schema}") from None
    try:
        return operator.index(schema)
    except TypeError:
        raise UnsupportedSchemaError("Schema must be a numeric id or declared type name") from None


def schema_info(schema):
    """Describe exactly what supports an encoding claim for this schema."""
    schema_id = _schema_id(schema)
    name = next((name for name, candidate in NAMES.items() if candidate == schema_id), None)
    descriptor = SCHEMAS.get(schema_id)
    static_order = STATIC_NATIVE_ORDER.get(schema_id)
    fixture_order = NATIVE_FIXTURE_ORDER.get(schema_id)
    return {
        "schema_id": schema_id, "name": name, "supported": descriptor is not None,
        "native_fixture_verified": schema_id in NATIVE_VERIFIED,
        "order_source": ("native_parser_fixture" if fixture_order else
                         "pinned_linux_arm64_static_disassembly" if static_order else
                         "pinned_python_stub" if descriptor and descriptor[0] == "struct" else "postcard_spec"),
        "static_native_order_evidence": static_order,
        "native_fixture_order_evidence": fixture_order,
        "unsupported_reason": UNSUPPORTED.get(schema_id, None if descriptor else "unknown schema"),
        "little_endian_supported": descriptor is not None and _le_size(descriptor) is not None,
        "little_endian_native_fixture_verified": False,
    }


def _descriptor(schema, require_verified):
    schema_id = _schema_id(schema)
    if schema_id not in SCHEMAS:
        reason = UNSUPPORTED.get(schema_id, "unknown schema")
        raise UnsupportedSchemaError(f"Schema {schema_id}: {reason}")
    descriptor = SCHEMAS[schema_id]
    if require_verified and descriptor[0] == "struct" and schema_id not in NATIVE_VERIFIED:
        raise UnsupportedSchemaError(f"Schema {schema_id}: native field-order fixture is not available")
    return descriptor


def _varint(value):
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7f) | 0x80)
        value >>= 7
    output.append(value)
    return output


def _integer(value, kind):
    try:
        value = operator.index(value)
    except TypeError:
        raise CodecError(f"{kind} requires an integer") from None
    signed = kind.startswith("int")
    bits = int(kind[3:] if signed else kind[4:])
    minimum = -(1 << (bits - 1)) if signed else 0
    maximum = (1 << (bits - (1 if signed else 0))) - 1
    if not minimum <= value <= maximum:
        raise CodecError(f"{kind} out of range: {value}")
    if bits == 8:
        return bytes([value & 0xff])
    if signed:
        value = (value << 1) ^ (value >> (bits - 1))
    return _varint(value)


def _encode(descriptor, value, out):
    kind = descriptor[0]
    if kind.startswith(("uint", "int")):
        out.extend(_integer(value, kind))
    elif kind in {"float", "double"}:
        if isinstance(value, (str, bytes, bytearray)):
            raise CodecError(f"{kind} requires a real number")
        try:
            out.extend(struct.pack("<f" if kind == "float" else "<d", value))
        except (OverflowError, TypeError, struct.error) as error:
            raise CodecError(str(error)) from error
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise CodecError("boolean requires bool")
        out.append(int(value))
    elif kind == "string":
        if not isinstance(value, str):
            raise CodecError("string requires str")
        data = value.encode("utf-8")
        out.extend(_varint(len(data)))
        out.extend(data)
    elif kind == "array":
        if isinstance(value, str) or not hasattr(value, "__len__"):
            raise CodecError("array requires a sized sequence")
        child, fixed = descriptor[1:]
        if fixed is not None and len(value) != fixed:
            raise CodecError(f"fixed array needs exactly {fixed} elements, received {len(value)}")
        if fixed is None:
            out.extend(_varint(len(value)))
        for element in value:
            _encode(child, element, out)
    elif kind == "struct":
        _, name, fields = descriptor
        if not isinstance(value, Mapping):
            raise CodecError(f"{name} requires a mapping or modeled value")
        expected = {field for field, _ in fields}
        if set(value) != expected:
            raise CodecError(f"{name} fields differ: missing={sorted(expected - set(value))}, extra={sorted(set(value) - expected)}")
        for field, child in fields:
            _encode(child, value[field], out)
    else:
        raise UnsupportedSchemaError(f"Unhandled descriptor: {kind}")


def encode(schema, value, *, require_verified=False):
    """Encode a payload; opt into requiring native-fixture-tested struct order."""
    descriptor = _descriptor(schema, require_verified)
    output = bytearray()
    _encode(descriptor, _to_native(value), output)
    return bytes(output)


class _Reader:
    def __init__(self, data, max_items):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise CodecError("payload must be bytes-like")
        self.data = memoryview(data).cast("B")
        self.offset = 0
        self.max_items = max_items

    def take(self, count):
        if count < 0 or count > len(self.data) - self.offset:
            raise CodecError(f"truncated payload at byte {self.offset}")
        start = self.offset
        self.offset += count
        return self.data[start:self.offset]

    def varint(self, bits):
        value = 0
        for index in range((bits + 6) // 7):
            byte = self.take(1)[0]
            value |= (byte & 0x7f) << (index * 7)
            if value >= 1 << bits:
                raise CodecError(f"varint exceeds uint{bits}")
            if byte < 0x80:
                return value
        raise CodecError(f"varint exceeds maximum encoded length for uint{bits}")

    def decode(self, descriptor):
        kind = descriptor[0]
        if kind.startswith(("uint", "int")):
            signed = kind.startswith("int")
            bits = int(kind[3:] if signed else kind[4:])
            if bits == 8:
                value = self.take(1)[0]
                return value - 256 if signed and value >= 128 else value
            value = self.varint(bits)
            return (value >> 1) ^ -(value & 1) if signed else value
        if kind in {"float", "double"}:
            return struct.unpack("<f" if kind == "float" else "<d", self.take(4 if kind == "float" else 8))[0]
        if kind == "boolean":
            value = self.take(1)[0]
            if value not in (0, 1):
                raise CodecError(f"invalid boolean: {value}")
            return bool(value)
        if kind == "string":
            count = self.varint(64)
            try:
                return bytes(self.take(count)).decode("utf-8")
            except UnicodeDecodeError as error:
                raise CodecError("invalid UTF-8 string") from error
        if kind == "array":
            child, fixed = descriptor[1:]
            count = self.varint(64) if fixed is None else fixed
            if count > self.max_items:
                raise CodecError(f"array length {count} exceeds decoder limit {self.max_items}")
            return [self.decode(child) for _ in range(count)]
        if kind == "struct":
            return {name: self.decode(child) for name, child in descriptor[2]}
        raise UnsupportedSchemaError(f"Unhandled descriptor: {kind}")


def decode(schema, payload, *, require_verified=False, max_items=1_000_000):
    """Decode exactly one raw payload, rejecting trailing bytes and truncation.

    Returns mappings for structs. Python's semantic SDK views (for example
    uint32 status_word -> StatusWord) are a separate conversion in the facade.
    """
    if not isinstance(max_items, int) or max_items < 0:
        raise CodecError("max_items must be a nonnegative integer")
    descriptor = _descriptor(schema, require_verified)
    reader = _Reader(payload, max_items)
    result = reader.decode(descriptor)
    if reader.offset != len(reader.data):
        raise CodecError(f"trailing bytes after payload: {len(reader.data) - reader.offset}")
    return result


def _le_size(descriptor):
    """Return an unambiguous raw width; reject strings, vectors and all structs."""
    kind = descriptor[0]
    if kind.startswith(("uint", "int")):
        return int(kind[3:] if kind.startswith("int") else kind[4:]) // 8
    if kind in ("float", "double", "boolean"):
        return {"float": 4, "double": 8, "boolean": 1}[kind]
    if kind == "array" and descriptor[2] is not None:
        child_size = _le_size(descriptor[1])
        if child_size is not None:
            return child_size * descriptor[2]
    return None


def _le_descriptor(schema):
    descriptor = _descriptor(schema, False)
    if _le_size(descriptor) is None:
        raise UnsupportedSchemaError(
            f"Schema {_schema_id(schema)}: little-endian mode supports only numeric/bool "
            "scalars and fixed arrays of those scalars; struct and variable layouts are unverified"
        )
    return descriptor


def _encode_le(descriptor, value, output):
    kind = descriptor[0]
    if kind.startswith(("uint", "int")):
        _integer(value, kind)  # Share strict numeric type and bounds checks.
        output.extend(operator.index(value).to_bytes(_le_size(descriptor), "little", signed=kind.startswith("int")))
    elif kind in ("float", "double", "boolean"):
        _encode(descriptor, value, output)  # These have identical scalar representations.
    elif kind == "array" and _le_size(descriptor) is not None:
        if isinstance(value, str) or not hasattr(value, "__len__"):
            raise CodecError("fixed array requires a sized sequence")
        child, count = descriptor[1:]
        if len(value) != count:
            raise CodecError(f"fixed array needs exactly {count} elements, received {len(value)}")
        for item in value:
            _encode_le(child, item, output)
    else:
        raise UnsupportedSchemaError(f"Unverified little-endian descriptor: {kind}")


def encode_le(schema, value):
    """Encode an unambiguous serde-format-2 scalar/fixed array at full byte width.

    This does not infer packed struct layout or variable-array conventions.
    The current schema snapshot exposes scalar IDs; it has no top-level fixed
    array IDs. Native byte fixtures have not yet verified this format.
    """
    descriptor = _le_descriptor(schema)
    output = bytearray()
    _encode_le(descriptor, _to_native(value), output)
    return bytes(output)


def _decode_le(descriptor, reader):
    kind = descriptor[0]
    if kind.startswith(("uint", "int")):
        return int.from_bytes(reader.take(_le_size(descriptor)), "little", signed=kind.startswith("int"))
    if kind in ("float", "double", "boolean"):
        return reader.decode(descriptor)
    if kind == "array" and _le_size(descriptor) is not None:
        child, count = descriptor[1:]
        if count > reader.max_items:
            raise CodecError(f"array length {count} exceeds decoder limit {reader.max_items}")
        return [_decode_le(child, reader) for _ in range(count)]
    raise UnsupportedSchemaError(f"Unverified little-endian descriptor: {kind}")


def decode_le(schema, payload, *, max_items=1_000_000):
    """Decode full-width little-endian values, with exact payload length checks."""
    if not isinstance(max_items, int) or max_items < 0:
        raise CodecError("max_items must be a nonnegative integer")
    descriptor = _le_descriptor(schema)
    reader = _Reader(payload, max_items)
    expected = _le_size(descriptor)
    if len(reader.data) != expected:
        raise CodecError(f"little-endian payload requires {expected} bytes, received {len(reader.data)}")
    return _decode_le(descriptor, reader)

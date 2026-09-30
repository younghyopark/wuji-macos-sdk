"""Postcard specification vectors and independent native SDK oracle fixtures.

Specification: https://postcard.jamesmunns.com/wire-format
Native fixtures: scripts/probe_device.py, pinned WUJI Mac CLI 2026.8.31.
"""
import json
from pathlib import Path
import unittest

from wuji_sdk import _codec

ROOT = Path(__file__).resolve().parents[1]


class PrimitivePostcardTests(unittest.TestCase):
    def test_unsigned_varint_spec_vectors(self):
        for number, encoded in [
            (0, "00"), (127, "7f"), (128, "8001"), (16383, "ff7f"),
            (16384, "808001"), (16385, "818001"), (65535, "ffff03"),
        ]:
            with self.subTest(number=number):
                self.assertEqual(_codec.encode("uint16", number).hex(), encoded)
                self.assertEqual(_codec.decode("uint16", bytes.fromhex(encoded)), number)

    def test_signed_varint_spec_vectors(self):
        for number, encoded in [
            (0, "00"), (-1, "01"), (1, "02"), (63, "7e"), (-64, "7f"),
            (64, "8001"), (-65, "8101"), (32767, "feff03"), (-32768, "ffff03"),
        ]:
            with self.subTest(number=number):
                self.assertEqual(_codec.encode("int16", number).hex(), encoded)
                self.assertEqual(_codec.decode("int16", bytes.fromhex(encoded)), number)

    def test_eight_bit_values_do_not_use_varints(self):
        self.assertEqual(_codec.encode("uint8", 255), b"\xff")
        self.assertEqual(_codec.encode("int8", -1), b"\xff")
        self.assertEqual(_codec.decode("uint8", b"\xff"), 255)
        self.assertEqual(_codec.decode("int8", b"\xff"), -1)

    def test_float_spec_vectors_and_utf8_byte_length(self):
        self.assertEqual(_codec.encode("float", -32.005859375).hex(), "000600c2")
        self.assertEqual(_codec.encode("double", -32.005859375).hex(), "00000000c00040c0")
        self.assertEqual(_codec.encode("String", "é").hex(), "02c3a9")
        self.assertEqual(_codec.decode("String", bytes.fromhex("02c3a9")), "é")

    def test_noncanonical_varint_is_allowed_within_type_bound(self):
        self.assertEqual(_codec.decode("uint16", bytes.fromhex("808000")), 0)
        for invalid in ("80808000", "ffff07", "ffff8300"):
            with self.subTest(payload=invalid), self.assertRaises(_codec.CodecError):
                _codec.decode("uint16", bytes.fromhex(invalid))

    def test_invalid_ranges_truncation_and_trailing_bytes(self):
        for schema, value in [("uint8", 256), ("uint16", -1), ("int8", -129), ("int16", 32768)]:
            with self.subTest(schema=schema, value=value), self.assertRaises(_codec.CodecError):
                _codec.encode(schema, value)
        for schema, payload in [("uint32", b"\x80"), ("float", b"\0" * 3), ("Boolean", b"\x02"), ("uint8", b"\1\2")]:
            with self.subTest(schema=schema), self.assertRaises(_codec.CodecError):
                _codec.decode(schema, payload)

    def test_unknown_struct_order_is_never_guessed(self):
        with self.assertRaises(_codec.UnsupportedSchemaError):
            _codec.encode("missing_schema", {})
        with self.assertRaises(_codec.UnsupportedSchemaError):
            _codec.decode(289, b"")  # FbMotionFrame is absent from the Python stub.


class NativeOracleTests(unittest.TestCase):
    def test_python_matches_native_serialization(self):
        fixtures = []
        for name in ("native-codec-fixtures.json", "schema-oracle-fixtures.json"):
            fixtures.extend(json.loads((ROOT / "tests" / "data" / name).read_text()))
        self.assertGreaterEqual(len(fixtures), 10)
        for fixture in fixtures:
            with self.subTest(schema=fixture["type"]):
                wire = bytes.fromhex(fixture["postcard_hex"])
                self.assertEqual(_codec.encode(fixture["schema_id"], fixture["value"], require_verified=True), wire)
                self.assertEqual(_codec.decode(fixture["schema_id"], wire, require_verified=True), fixture["value"])

    def test_joint_commands_are_exactly_twenty_fixed_entries(self):
        joints = [{"position": 0.0, "velocity": 0.0, "effort": 0.0} for _ in range(20)]
        wire = _codec.encode("JointCommandFrame", {"seq": 300, "joints": joints}, require_verified=True)
        self.assertEqual(len(wire), 2 + 20 * 3 * 4)
        self.assertEqual(wire[:2], bytes.fromhex("ac02"))
        with self.assertRaises(_codec.CodecError):
            _codec.encode("JointCommandFrame", {"seq": 300, "joints": joints[:-1]})

    def test_verified_receive_and_provisional_tactile_are_distinct(self):
        for schema in ("JointStateFrame", "JointDiagnosticsFrame", "ImuData", "HandCommunicationDiagnostics"):
            info = _codec.schema_info(schema)
            self.assertTrue(info["supported"])
            self.assertTrue(info["native_fixture_verified"])
        self.assertFalse(_codec.schema_info("FingertipSensorData")["native_fixture_verified"])
        with self.assertRaises(_codec.UnsupportedSchemaError):
            _codec.decode("FingertipSensorData", b"", require_verified=True)

    def test_missing_measurements_cannot_be_encoded(self):
        with self.assertRaises(_codec.CodecError):
            _codec.encode("JointCommand", {"position": 0.0, "velocity": 0.0})

    def test_internal_realtime_order_uses_native_parser_proof(self):
        for schema in (290, 291):
            info = _codec.schema_info(schema)
            self.assertTrue(info["native_fixture_verified"])
            self.assertEqual(info["order_source"], "native_parser_fixture")
            self.assertIsNone(info["static_native_order_evidence"])
        self.assertTrue(_codec.schema_info(334)["native_fixture_verified"])


class InternalFingertipProtocolTests(unittest.TestCase):
    """Static native field order plus Postcard spec, not executed Linux fixtures.

    Linux serializer 0x1e94dc emits finger u8 then offset u16; deserializer
    0x1e9a34 reads finger, offset, total, chunk. See hand2-semantics.md for the
    pinned library hash and call-chain evidence. Unequal values expose order.
    """

    def test_request_wire_order_and_full_u16_range(self):
        for value, wire in [({"offset": 0, "finger": 0}, "0000"),
                            ({"offset": 300, "finger": 4}, "04ac02"),
                            ({"offset": 65535, "finger": 255}, "ffffff03")]:
            with self.subTest(value=value):
                self.assertEqual(_codec.encode(336, value).hex(), wire)
                self.assertEqual(_codec.decode("FingertipInfoReq", bytes.fromhex(wire)), value)

    def test_chunk_order_count_prefix_and_empty_terminator(self):
        for value, wire in [
            ({"chunk": [0, 1, 128, 255], "total": 1234, "offset": 300, "finger": 4},
             "04ac02d20904000180ff"),
            ({"chunk": [], "total": 65535, "offset": 65535, "finger": 3},
             "03ffff03ffff0300"),
        ]:
            with self.subTest(value=value):
                self.assertEqual(_codec.encode(337, value).hex(), wire)
                self.assertEqual(_codec.decode("FingertipInfoChunk", bytes.fromhex(wire)), value)

    def test_static_evidence_does_not_claim_executed_native_fixture(self):
        for schema in (336, 337):
            info = _codec.schema_info(schema)
            self.assertTrue(info["supported"])
            self.assertEqual(info["order_source"], "pinned_linux_arm64_static_disassembly")
            self.assertEqual(info["static_native_order_evidence"]["library_sha256"],
                             "5cebdbec82df966899cfbd8f8361c4faf60ea2f651f0bc520a3b4ffdfa8d79cf")
            if not info["native_fixture_verified"]:
                with self.assertRaises(_codec.UnsupportedSchemaError):
                    _codec.decode(schema, b"", require_verified=True)

    def test_malformed_chunks_and_missing_selector_fields_reject(self):
        for wire in ("04ac02d20904000180", "04ac02d20900ff", "04ac02d20980"):
            with self.subTest(wire=wire), self.assertRaises(_codec.CodecError):
                _codec.decode(337, bytes.fromhex(wire))
        for value in ({"finger": 0}, {"finger": 0, "offset": 65536},
                      {"finger": 0, "offset": 0, "total": 0}):
            with self.subTest(value=value), self.assertRaises(_codec.CodecError):
                _codec.encode(336, value)


class LittleEndianTests(unittest.TestCase):
    """Full-width primitive examples; these are not native-oracle fixtures."""

    def test_full_width_integer_scalars_are_not_postcard_varints(self):
        fixtures = [
            ("uint8", 255, "ff"), ("int8", -128, "80"),
            ("uint16", 300, "2c01"), ("int16", -300, "d4fe"),
            ("uint32", 0x12345678, "78563412"), ("int32", -2, "feffffff"),
            ("uint64", 0x0102030405060708, "0807060504030201"),
            ("int64", -2**63, "0000000000000080"),
        ]
        for schema, value, payload in fixtures:
            with self.subTest(schema=schema):
                self.assertEqual(_codec.encode_le(schema, value).hex(), payload)
                self.assertEqual(_codec.decode_le(schema, bytes.fromhex(payload)), value)
        self.assertNotEqual(_codec.encode_le("uint16", 300), _codec.encode("uint16", 300))

    def test_float_and_boolean_scalar_bytes(self):
        for schema, value, payload in [("float", -1.5, "0000c0bf"), ("double", 1.0, "000000000000f03f"),
                                       ("Boolean", False, "00"), ("Boolean", True, "01")]:
            with self.subTest(schema=schema, value=value):
                self.assertEqual(_codec.encode_le(schema, value).hex(), payload)
                self.assertEqual(_codec.decode_le(schema, bytes.fromhex(payload)), value)

    def test_fixed_array_helper_adds_no_count_prefix(self):
        # No current native top-level schema has a fixed-array shape. Check the
        # generic helper explicitly without assigning a fabricated schema ID.
        descriptor = ("array", ("int16",), 3)
        output = bytearray()
        _codec._encode_le(descriptor, [-1, 300, -300], output)
        self.assertEqual(output.hex(), "ffff2c01d4fe")
        self.assertEqual(_codec._decode_le(descriptor, _codec._Reader(output, 3)), [-1, 300, -300])
        with self.assertRaises(_codec.CodecError):
            _codec._encode_le(descriptor, [1, 2], bytearray())
        with self.assertRaises(_codec.CodecError):
            _codec._decode_le(descriptor, _codec._Reader(output, 2))

    def test_structs_strings_and_variable_arrays_reject(self):
        for schema in ("JointCommand", "JointCommandFrame", "String", 17, 18, 20):
            with self.subTest(schema=schema):
                self.assertFalse(_codec.schema_info(schema)["little_endian_supported"])
                with self.assertRaises(_codec.UnsupportedSchemaError):
                    _codec.encode_le(schema, [])
                with self.assertRaises(_codec.UnsupportedSchemaError):
                    _codec.decode_le(schema, b"")
        self.assertFalse(_codec.schema_info("uint16")["little_endian_native_fixture_verified"])

    def test_invalid_ranges_lengths_and_boolean_values_reject(self):
        for schema, value in [("uint8", 256), ("uint16", -1), ("int16", 32768), ("uint64", 2**64), ("Boolean", 2)]:
            with self.subTest(schema=schema), self.assertRaises(_codec.CodecError):
                _codec.encode_le(schema, value)
        for schema, payload in [("uint16", b"\x01"), ("uint16", b"\x01\x00\x00"), ("Boolean", b"\x02")]:
            with self.subTest(schema=schema), self.assertRaises(_codec.CodecError):
                _codec.decode_le(schema, payload)


if __name__ == "__main__":
    unittest.main()

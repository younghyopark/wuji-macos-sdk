# Python API contract inventory

Pinned `wuji-sdk==2026.8.31` stub SHA-256: `10cd4fb8f366492e9b489bf9647306a355baaec36f5c0cd1656265572d09bcaa`.

This inventory separates data-model coverage from native operation coverage. It is not a claim that all upstream examples run on macOS.

| Category | Classes |
| --- | ---: |
| exception | 2 |
| modeled_enum | 8 |
| modeled_value | 79 |
| native_facade_required | 86 |
| typed_dictionary | 13 |

The stub defines 188 distinct classes, 3 aliases/type variables, and 1 top-level functions. Repeated extension declarations are merged. The JSON companion records every field, property, method signature, overload, async modifier, source line, and example import/call observation.

## Explicit limitations

- No class categorized native_facade_required is implemented by this generator.
- The stub references CalibrationResult and JointDiagnostics without defining either class.
- PointCloud.__len__ is declared but its behavior is not specified; the model raises NotImplementedError. point_count() is implemented from documented stride semantics.
- Python model repr strings, mutation rules, numeric widths, and all native PyO3 edge-case conversions have not been checked against a running Linux SDK.
- TransportType has named Python variants only; its native Python integer discriminants are not inferred from C or declaration order.
- TransportType includes Zenoh and CppNative from the pinned native Mac SDK; the vendor Python stub only lists Usb and Udp.
- Enum values for DeviceType/Handedness/HandModel use the pinned C contract; equality and formatting beyond the documented integer-compatible enums remain unverified against PyO3.
- Example call inventory is static and may include unrelated methods sharing an SDK method name; it does not demonstrate hardware execution.

## Value-model use

`scripts/generate_api.py` reads the hash-verified wheel without importing it and emits `src/wuji_sdk/_types.py`. Only constructors declared by the official stub are public. Native bridges can call the private `_from_native(ClassOrName, decoded_mapping)` to recursively materialize records. Missing telemetry fields raise errors; no success state, timestamp, measurement, or joint is synthesized. `_to_native(value)` converts modeled values to plain data and is not a wire serializer.

`JointCommand`, `MitParam`, `FrameHeader`, `Quaternion`, `Vector3F64`, `Pose`, `HwVersion`, `ErrorStatus`, `ErrorLogEntry`, `EmfPose`, `EmfPoseArray`, `ConnectOptions`, and `LowPass` have declared constructors. `ImuData.raw` marks orientation as unavailable according to the [official offline-pipeline guide](https://docs.wuji.tech/docs/en/wuji-glove/latest/offline-pipeline/). `PointCloud.point_count` follows the [documented point stride](https://docs.wuji.tech/docs/en/wuji-glove/latest/sdk-data-reference/tactile/).

## Hand 2 example imports

All required data symbols below are in the generated value module. `SdkManager`, `WujiHand2`, and `set_log_level` require the native-backed package facade. This is an import contract, not proof of running the examples on hardware.

| Original example | Required top-level SDK names |
| --- | --- |
| `wuji_hand_2/0.subscribe_callback.py` | `DeviceType`, `JointStateFrame`, `SdkManager`, `WujiHand2` |
| `wuji_hand_2/1.subscribe_async.py` | `DeviceType`, `JointStateFrame`, `SdkManager`, `WujiHand2` |
| `wuji_hand_2/2.publish.py` | `DeviceType`, `JointCommand`, `SdkManager`, `WujiHand2` |
| `wuji_hand_2/3.fingertip_typed.py` | `DeviceType`, `SdkManager`, `set_log_level` |
| `wuji_hand_2/4.set_stream_rate.py` | `DeviceType`, `SdkManager` |
| `wuji_hand_2/5.export_flash_logs.py` | `DeviceType`, `SdkManager` |
| `wuji_hand_2/6_opposition/opposition.py` | `JointCommand`, `SdkManager` |
| `wuji_hand_2/7.mit_sweep.py` | `JointCommand`, `SdkManager` |

## Postcard payload codec

`_codec.py` currently encodes/decodes 64 of the 110 native schemas; 46 reject with explicit missing-order/type reasons. It handles payloads only. Unknown schemas never fall back to sorted JSON keys.

Native serializer fixtures verify `MitParams`, `EffortLimitVec`, `ControlModeVec`, `JointCommandFrame`, and `JointStateFrame`, including the nested `MitParam`, `JointCommand`, `JointStateEntry`, and `FrameHeader` layouts. The 20 command entries form a fixed array without a length prefix. The receive fixture preserves sparse IDs 0 and 23 and independent joint-count/array-length fields. Remaining object order comes from the matching Python IDL declaration and is provisional. `schema_info()` reports the distinction; `require_verified=True` rejects structs without native fixtures.

Executed native parser fixtures additionally verify `RealtimeStreamFrame`/`RealtimeStreamEntry` (291/290) and `FingertipSensorInfo` (334). The realtime types are internal and absent from the public Python stub; their explicit order is recorded in `NATIVE_FIXTURE_ORDER`. Their distinct-field fixture is also the input to the native virtual `joint_states` pipeline test. Parser fixtures prove that the pinned native parser accepts and interprets the exact bytes; they are not represented as serializer output.

The separate `probe_schema_oracles.py` adds actual native serializer fixtures for `ImuData` (261) and `JointDiagnosticsFrame` (332), including its nested entry (331), and a native parser oracle for every numeric field of `HandCommunicationDiagnostics` (282). Distinct values cover all covariance elements, summary fields, five fingers and five nodes per finger. The installed-wheel suite recompiles the small research adapter and repeats these against the installed native image. `FingertipSensorData` (335) remains provisional: no direct retained native parser/serializer oracle has been established.

Internal schemas 336 `FingertipInfoReq` and 337 `FingertipInfoChunk` use field order recovered from the pinned Linux ARM64 serializer/deserializer, recorded with its hash and addresses in `STATIC_NATIVE_ORDER`. They expose no fabricated public Python classes. Exact-byte static vectors cover their integer widths, order, and chunk length prefix, but are separate from native Mac oracle fixtures. See `research/hand2-semantics.md`.

Primitive vectors follow the [Postcard wire specification](https://postcard.jamesmunns.com/wire-format). Strict decoding checks integer bounds, boolean values, truncation, and trailing bytes. Wire `status_word` integers remain integers in decoded mappings; conversion to the Python `StatusWord` view belongs to the facade.

## Little-endian payload codec

`encode_le`/`decode_le` support numeric and boolean scalar schema IDs 1–11 at their full native width. Internal fixed-array helpers add no length prefix; the pinned schema snapshot currently has no top-level fixed-array IDs. Strings, variable arrays, and every struct reject explicitly because their format-2 layout has not been established. Scalar tests check byte widths, signedness, bounds, and exact payload length; native format-2 fixture verification remains outstanding.

# Hand 2 semantics recovered from the pinned C SDK

The Linux evidence below is static inspection of the official ARM64 C SDK 2026.8.31; that library was not loaded or executed. Later sections explicitly identify separate executed Mac payload oracles and localhost protocol fixtures. None of these tests accessed physical hardware.

Artifact: [official C SDK release](https://github.com/wuji-technology/wuji-sdk/releases/tag/v2026.8.31), pinned in `artifacts.lock.json` as `sdk-c-linux-arm64`.

- Archive SHA-256: `efa4b1c86f2733aca0ab0bec9e4c1118f4b226b95cc3e9a6ccd1cba76f74e4c3`.
- Extracted `libwuji_sdk_c.so` SHA-256: `5cebdbec82df966899cfbd8f8361c4faf60ea2f651f0bc520a3b4ffdfa8d79cf`.
- Local extraction and disassembly: `build/hand2-semantics/`.
- Addresses are virtual addresses in that exact ELF unless explicitly labeled as Mac image offsets.
- The matching C declarations/documentation are in `build/abi-inspection/wuji_sdk.h`; the Python type contract is the pinned `__init__.pyi` recorded in `research/api-coverage.json`.

## Joint indexing and names

The native Hand 2 constructor at `0x270144` builds twenty joint handles, in increasing flat index `i=0..19`. The body at `0x270190..0x2701c0` computes:

```python
finger = ("thumb", "index", "middle", "ring", "pinky")[i // 4]
slot = i % 4 + 1
prefix = f"{finger}/S{slot}"
```

The lookup at `0xc75134` contains bytes `00 01 02 03 04`, but these are finger-enum discriminants, not printed integers: the first format argument's function pointer (GOT `0x1580518`, relocated to `0x29dd20`) is the finger enum's Display, a jump table over the literals `thumb`, `index`, `middle`, `ring`, `pinky` at `0xc73990`. The embedded format program at `0xc6b9fd` places literal `/S` between the two arguments. The resource joiner `0x24a500`, via `0x2c3140`, uses format program `0xc6ef49` with literal `/` between prefix and resource. Device-relative per-joint resource paths therefore range from `thumb/S1/...` through `pinky/S4/...`.

An earlier revision of this note read the discriminant bytes as the printed bus number and derived `0/S1/...`. A read-only capability listing of a live Hand 2 (firmware 2.5.1) confirms the finger-name form: it advertises `thumb/S1/motor_enable`, `thumb/S1/od/aux/set_origin_ctrl`, and `thumb/tactile/tactile_state`, and a native GET of `0/S1/mit_params` fails with `Path not found`. Per-finger tactile resources use the same finger names (`{finger}/tactile/...`).

`wuji_hand_2_joint_label` at `0x192538`, using `0x19943c`, returns:

```python
finger = ("thumb", "index", "middle", "ring", "pinky")[i // 4]
label = f"{finger}_S{i % 4 + 1}"
```

The exact finger names are pointer/length pairs at `0x1491a20`; the format program at `0xc6445a` has literal `_S`. Valid labels are `thumb_S1` through `pinky_S4`. The wrapper rejects indices greater than 19. `wuji_hand_2_finger_name` at `0x191064` uses the same table and rejects indices greater than 4.

These dense indices differ from telemetry IDs: the C header and native function `0x270d9c` give `nid = bus * 5 + node - 1`, leaving a tactile slot on every bus. Do not index a twenty-element command array directly using sparse telemetry `nid` values.

## Whole-hand and masked actions

| Python operation | Whole hand (`joints=None`) | Selected joint `i` |
| --- | --- | --- |
| `enable` | empty raw SET to `motor_enable` | empty raw SET to `{prefix}/motor_enable` |
| `disable` | empty raw SET to `motor_disable` | empty raw SET to `{prefix}/motor_disable` |
| `clear_fault` | empty raw SET to `clear_fault` | empty raw SET to `{prefix}/clear_fault` |
| `set_origin` | empty raw SET to `set_origin` | raw SET to `{prefix}/od/aux/set_origin_ctrl`, payload `01 00 00 00` |
| `clear_origin` | empty raw SET to `clear_origin` | raw SET to `{prefix}/od/aux/set_origin_ctrl`, payload `02 00 00 00` |
| `emergency_stop` | empty raw SET to `emergency_stop` | no masked form |

These Hand 2 convenience methods call the raw SET implementation, not the generic SDK `execute` implementation. Generic C `wuji_dev_execute` at `0x18d0dc` calls the distinct function `0x23a9c4`, which checks executable resource metadata; the Hand 2 convenience paths call raw setter `0x23ace0`. Preserve this distinction even though both represent device actions. The empty SET payload contains neither an encoded JSON null nor a Postcard unit discriminator. Masked origin operations use a full-width little-endian u32, not a one-byte Postcard integer.

The C wrappers are at `0x190bc0` (enable), `0x190a78` (disable), `0x190798` (clear fault), `0x192adc` (set origin), `0x190840` (clear origin), and `0x190b20` (emergency stop). A null mask chooses the whole-hand branch. For a nonnull mask, `0x18a9d4` reads exactly twenty bytes and converts each to `byte != 0`. The native masked helpers reject a mask length other than twenty, visit selected joints in ascending flat index, and return immediately on the first failed selected operation. An all-zero mask does no requests and succeeds. `None` and an all-ones mask therefore select different request strategies.

Whole-hand callees load exact path literals at:

| Operation | Callee | Literal address / byte count |
| --- | --- | --- |
| set origin | `0x270124` | `0xc78656` / 10 |
| clear fault | `0x270134` | `0xc77bc0` / 11 |
| clear origin | `0x270308` | `0xc78660` / 12 |
| emergency stop | `0x270464` | `0xc7866c` / 14 |
| enable | `0x270dd4` | `0xc77be1` / 12 |
| disable | `0x270de4` | `0xc77bed` / 13 |

All route through `0x270df4` and `0x2ba74c` to raw setter `0x23ace0` with payload length zero. Selected enable/disable/clear-fault helpers use `0x24a534`, `0x24a59c`, and `0x24a42c` respectively, joining the same action names to the per-joint prefix. The selected set/clear-origin helpers are `0x24a3c8` and `0x24a494`; literal `0xc77baa` is exactly `od/aux/set_origin_ctrl`. Helper `0x2c6fe0` writes u32 1; the clear helper writes u32 2 directly. Both call `0x2b9218`, which passes a pointer to that u32 and length four to `0x23ace0`.

The header documents origin changes as runtime offsets without a Flash write. Do not append `save_params` to these operations.

## Effort limits

The shared device-level resource path is exactly `effort_limit`, verified independently at the SET helper `0x197750` (literal `0xc6498e`, length 12) and GET helper `0x278c44` (literal `0xc77bd5`, length 12).

The setter algorithm is:

1. A scalar broadcasts by constructing a twenty-element float vector. A per-joint argument supplies exactly twenty floats in dense finger-major order.
2. Validate every value as finite and nonnegative before the request. Invalid values abort the operation; no valid prefix of the vector is sent.
3. Send one typed root SET containing `EffortLimitVec { joints: values }` to `effort_limit`.

Evidence: scalar C wrapper `0x192778` calls `0x17709c`; per-joint wrapper `0x192834` copies eighty bytes then calls `0x176fa4`. Both helpers compare the vector length to twenty, validate each float, and route through `0x197750`. The error format at `0xc63293` explicitly describes the nonnegative finite requirement. This is one aggregate SET, not twenty independent joint requests. Its Postcard payload layout is separately verified by the native `EffortLimitVec` fixture.

The GET algorithm is:

```python
values = get("effort_limit")["joints"]
if len(values) != 20:
    raise DecodeError("expected 20 entries")
return [None if math.isnan(value) else value for value in values]
```

Evidence: `0x278c44` requires length twenty before conversion; conversion `0x1f3a54` uses `fcmp s0,s0` and `cset vc` to select `Some` exactly when the value is not NaN. It does not classify infinity or zero as offline. The C wrapper `0x191100` turns these option values into its separate output-array/online-bitmask convention: valid values are copied, offline slots are zero, and bit `i` is set only for a present entry. The Python list should preserve `None`; do not copy the C-only zero fill into the Python API.

## MIT gains

Both getters and setters use the exact root path `mit_params`: SET literal `0xc6496a` and GET literal `0xc77bcb`, both ten bytes. The aggregate value is `MitParams { joints: [{kp, kd}, ...] }` in dense twenty-joint order.

`wuji_hand_2_set_all_mit_params` (`0x192924`) interleaves the two twenty-element C arrays and calls `0x174f58`. That helper also supports a scalar pair by expanding it to twenty entries; it rejects other lengths. It validates all twenty pairs before issuing one typed SET at `0x175000`. Validator `0x28fbcc` rejects NaN/infinity in either field and then rejects negative `kp` or `kd`. Negative zero passes the comparison. No per-joint requests or control-mode changes are appended. The native `MitParams` Postcard fixture independently verifies its field order and vector representation.

The GET helper at `0x2605a8` requires exactly twenty entries. Conversion `0x1f3814` compares each pair with `fcmp kp,kd`, then uses the ordered-condition flag: if either value is NaN, the entire pair becomes `None`; otherwise it becomes `MitParam(kp,kd)`. Infinity by itself remains a present returned value. The C wrapper at `0x191250` separately translates absent pairs to NaN/NaN and clears the corresponding output bitmap bit. Python should retain the option-list semantics.

## Online joint count and diagnostics schema

The C count wrapper at `0x192638` calls `0x270f74`, whose typed GET literal is `comm_diag/hand` at `0xc747ef` (14 bytes). The converted result has five fingers. The count routine `0x181054`, reached through `0x183a7c`, sums the `online` fields of the first four node diagnostics in every finger:

```python
diag = get("comm_diag/hand")
count = sum(node["online"] for finger in diag["fingers"] for node in finger["nodes"][:4])
```

The native contract has exactly five fingers and exactly five nodes per finger; the fifth node is excluded from the joint count. Do not count all five nodes or infer online state from an effort value. The machine routine adds the integer online values, rather than recounting truthiness; the documented flags are 0/1 and output count is 0..20.

Native schema 282 (`wuji.HandCommunicationDiagnostics`) matches the stub's field names exactly: top-level `fingers`, nested `FingerCommunicationDiagnostics.nodes`, and `NodeDiagnostics.online` (u8). No alias or invented `online_count` field is needed. The schema has fixed arrays of five for `fingers`, `nodes`, and `transfer_stats`. Its Postcard object order remains derived from the matching stub and does not yet have an independent native serialization fixture. The Linux static count algorithm does not eliminate that separate wire-layout verification gap.

## Status-word decoding

`wuji_hand_2_decode_joint_status` at `0x1908e8` implements the following directly; no SDK state or resource lookup is involved:

```python
ext_state = status_word & 0x3
enabled = ext_state == 2                 # C-only convenience field
position_limit_active = bool(status_word & (1 << 4))
velocity_limit_active = bool(status_word & (1 << 5))
current_limit_active = bool(status_word & (1 << 6))
```

The Python `StatusWord` contract includes `ext_state` and the three limit flags, plus derived `ext_state_name`; it has no declared `enabled` field. The documented state names are 0 Init, 1 Ready, 2 Enabled, 3 Stopped. Bits 2, 3, and 7..31 do not change these exposed fields.

## Limits and follow-up evidence

### Tactile calibration and fresh status

The C tactile calibration wrapper at `0x19323c` calls `0x270d20`, which visits finger indices 0 through 4 in order. Each call to `0x270770` constructs `{finger}/tactile/tactile_calibrate` (format program `0xc6ef01`) and calls generic execute `0x23a9c4`. The first failed finger aborts the sequence. This method does not poll for completion. The matching Python stub and C header require unloaded sensor surfaces; a successful return means that commands were enqueued.

The C status wrapper at `0x1932dc` validates a finger index in 0..4 and calls `0x270474`. Python accepts the corresponding lowercase finger name. The internal algorithm is:

```python
execute(f"{finger}/tactile/tactile_query_status")
for attempt in range(50):
    state = get_u8(f"{finger}/tactile/tactile_state")
    if state == 255:
        sleep(0.020)
        continue
    if state not in (0, 1):
        raise InvalidDataError()
    model = get_u8(f"{finger}/tactile/tactile_type")
    if model not in (0, 1):
        raise InvalidDataError()
    return TactileStatus(model=model, state=state)
raise TimeoutError()
```

The error names in this pseudocode describe the conditions, not verified Python exception classes. Format-program addresses are `0xc6eeb0` (query action), `0xc6eed0` (state), and `0xc6eee9` (model). The loop initializes its counter to 51 and decrements before each request at `0x270514`, allowing fifty GETs. A state of 255 sleeps for exactly 20,000,000 ns at `0x270558..0x270564`, including after the last failed poll, for approximately one second of polling sleep plus request durations. A valid state is returned immediately; `Calibrating` is a valid result, not a reason to wait until Ready. The header defines state 0 Ready / 1 Calibrating and model 0 Standard / 1 Thumb.

Native helper `0x28fad8` translates two internal error variants (tags `0x800000000000001d` and `0x800000000000001e`) to a finger-specific offline error. This audit has not established the source enum names for those two variants. Other request errors propagate. Do not infer an offline finger from state 255, which specifically means that fresh status has not arrived.

### Fingertip metadata

`wuji_hand_2_get_fingertip_info` at `0x191f30` validates finger 0..4 and calls `0x270888`. That method uses the following device-level paths, including literal dots:

| Request | Path | Serialization |
| --- | --- | --- |
| raw SET | `fingertip.info_req` | Postcard schema 336, `FingertipInfoReq` |
| raw GET | `fingertip.info_chunk` | Postcard schema 337, `FingertipInfoChunk` |

The request path is at `0xc7867a`, length 18; the reply path is at `0xc7868c`, length 20. These internal schema definitions are present in the extracted native schema snapshot even though their classes are absent from the Python stub.

The request wire order is `finger: u8`, then `offset: u16` as an unsigned Postcard varint. Serializer `0x1e94dc`, reached through `0x1b8c60`, emits the native struct's byte at offset two with `0x2022fc`, then its u16 at offset zero with `0x1fedc0`; native memory layout is not wire field order. The reply deserializer `0x1e9a34`, reached through `0x1b86f0`, reads `finger: u8`, `offset: u16`, `total: u16`, then `chunk: Vec<u8>` in that order. These orders were initially established by static code. The actual retained Mac helper now independently exercises both layouts against the localhost mock: finger 2, offsets 0/17/34/51/55, total 55, three 17-byte chunks, a four-byte chunk, and an empty terminator. See `scripts/probe_mock_device.py --resources --stream --fingertip` and `research/abi-bridge.md`.

The transfer starts at offset zero. Each iteration sends a selector SET and performs a chunk GET. It rejects a reply whose finger or offset differs from the request (`0x27099c..0x2709b0`). An empty chunk ends the transfer; otherwise it appends the chunk bytes and advances the offset by the chunk byte count. The implementation reads the low sixteen bits of the vector length, adds them to the u16 offset, and rejects a resulting value above 65535 (`0x2709c8..0x2709d0`). The `total` field is not used to decide completion in this implementation. There is no polling sleep or retry on mismatched selectors in this method. Request, parsing, mismatch, and offset errors propagate.

After the empty terminator chunk, it deserializes the concatenated bytes with `0x1b8728` as schema 334, `FingertipSensorInfo`. Its fields are header, digest, model, device_type, rate_hz, and format. The final metadata layout now has an executed Mac native parser fixture at image offset `0xa9b37c`; both Python encoding and decoding match its exact bytes. The internal chunk schemas additionally have the executed native localhost transfer described above, independently of the seven standalone payload fixtures. JSON property sorting is not evidence of Postcard order.

The Mac image separately retains the entire native `WujiHand2::get_fingertip_info` helper at image offset `0xb1c5b0`. The Python backend calls its C bridge, parses the returned JSON, and releases that bridge-owned allocation in a `finally` block, including parser/error paths. It does not substitute a local reconstruction of the chunk transfer. The metadata ownership tests inject a C-call substitute explicitly; they do not establish transfer from a physical fingertip.

### Remaining methods

The pinned C ABI does not export the Python single-joint `effort_limit`, `error_code`, or `status_word` resource accessors. The live Hand 2 capability listing advertises them directly under the joint prefix: `{prefix}/effort_limit` (float, GET/SET), `{prefix}/error_code` (u16, GET), and `{prefix}/status_word` (u32, GET), all Postcard. The Python accessors use exactly those advertised resources through the generic schema-checked GET/SET path; they are not OD guesses.

`wuji_hand_2_export_flash_logs` at `0x190c68` does exist, but the wrapper drives a larger async export implementation. Embedded literals establish `flash_log.head`, `flash_log.chunk_req`, and `flash_log.chunk`, along with request-echo/session checks, stale/foreign-response errors, status-byte decoding, and temporary JSONL output. These are protocol clues, not a complete transfer algorithm. The matching C header requires a full ring export, frame decoding into timestamp_ms/level/target/message, overwrite and concurrent-export timeout handling, and a newly written file per call. It documents request-echo concurrency detection only for firmware 2.6.0 onward. This audit did not derive the request/reply layouts, retry bounds, frame decoder, or atomic output behavior. Keep the high-level method explicitly unavailable until those are implemented and tested. The header's default directory is `~/.wuji/logs`; repo containment requires an explicit destination rather than changing HOME.

This audit does not provide `DeviceInfo` string-offset or `nodes()` implementations: neither a matching C export nor declaration exists in the pinned C library/header. It does not substitute guessed node descriptors from resource names. Device status, resources, publisher availability, and transport success still require their own native evidence.

The fifth native codec fixture, generated separately from the pinned Mac serializer at image offset `0xa6a1d0`, now verifies `JointStateFrame` and nested `JointStateEntry`/`FrameHeader` payload order byte-for-byte in Python. That establishes the receive payload layout for that schema; it does not establish receipt from a physical hand. See `tests/data/native-codec-fixtures.json` and `tests/test_codec.py`.

Two additional executed native parser fixtures bring the fixture count to seven: internal `RealtimeStreamFrame`/`RealtimeStreamEntry` (schema 291/290, parser `0xa9b3d0`) and `FingertipSensorInfo` (334, parser `0xa9b37c`). The realtime fields have distinct values, including sparse IDs and multibyte integer values, and are checked against the native parser's output fields. The internal realtime types are supported as decoded mappings without inventing public Python value classes absent from the stub. Native serializer output and native parser acceptance are tracked separately in the fixture records.

### Additional executed Mac telemetry oracles

`scripts/probe_schema_oracles.py` builds the separate research adapter `native/schema_oracle.c`, then runs it with all network access denied. It adds three payload fixtures in `tests/data/schema-oracle-fixtures.json`, bringing the total across both fixture files to ten. The image is the hash-pinned Mac CLI artifact `61fba3eb0cb32de58cf9098cab9ca9afe8b892ac21e64d2d96280b53490f6d50`. Every address in this subsection is an offset from that image's loaded base.

| Schema | Actual native operation | Evidence checked |
| --- | --- | --- |
| 261 `ImuData` | Postcard serializer `0xafaa10` | Header, distinct quaternion components, all 27 covariance entries, angular velocity and linear acceleration |
| 332 `JointDiagnosticsFrame` | Postcard serializer `0xa6a390` | Header, independent count/vector fields, sparse joint IDs, distinct status/error/current/temperature/voltage/communication fields, and every communication-summary field |
| 282 `HandCommunicationDiagnostics` | Postcard parser `0xa9b420` | Every numeric field in five fingers, each with five transfer-stat records and five node records; truncated inputs reject natively |

For schema 261 the borrowed native value is 336 bytes: Header at 0, quaternion at 40, orientation covariance at 72, angular velocity at 144, its covariance at 168, linear acceleration at 240, and its covariance at 264. Quaternion serializer `0xafa218` and Vector3 serializer `0xafa250` read full-width f64 components; the exact native output matches both directions of the Python codec. All values are exactly representable binary fractions.

For schema 332 the borrowed frame is 128 bytes: Header at 0, the joint Vec at 40, communication summary at 64, and count at 120. Entry serializer `0xa6a2a4` reads a 24-byte native entry and writes wire fields in this order: nid, status_word, current, vbus_v_fb, mcu_temp_c_fb, error_code_current, comm_response_rate_pct, comm_timeout_total. Summary serializer `0xafa430` writes age_ms, tactile_online_mask, e2e_received, e2e_lost, e2e_reordered, e2e_duplicates, e2e_window_loss_x100, rpc_total, rpc_retries, rpc_timeouts, comm_get_failures, the fixed five-element response-rate array, the fixed five-element timeout array, and sdk_dropped. The checked fixture verifies that ordering, including schema 331's nested entry. Native status_word remains an integer on the wire.

The schema 282 parser returns a success tag at byte 0 and five 224-byte finger values starting at byte 4. Finger parser `0xa54be4` stores five 24-byte node records at finger offset 0, seven u32 totals at offsets 120..144, tx/rx kbps at 148/152, five 12-byte transfer records at 156, and three u16 rates at 216/218/220. Node parser `0xa9e110` stores four u32 counters at 0/4/8/12, request_per_sec at 16, and node_type/online/response_rate_pct bytes at 18/19/20. Transfer parser `0xa9df24` stores tx_total at 0, timeout_total at 4, tx_per_sec at 8, and timeout_rate_pct at 10. The oracle reads and checks each meaningful field independently, ignoring padding. Its payload is Python-encoded and accepted/interpreted by the original native parser; it is deliberately not labeled as native serializer output.

`tests/test_schema_oracles_native.py` repeats the three oracles against the installed wheel's native image. Loading the research probe does not alter `sys.path`, so isolated wheel verification continues to use the installed Python codec. The standalone reproduction uses `scripts/probe_schema_oracles.py`; `--installed` selects an installed package without adding the source tree. No manager or transport is initialized by these payload tests.

`FingertipSensorData` is schema **335**, not 333 (`FactoryEntryInfo`). Its current codec order remains provisional: this bounded audit did not find a direct retained serializer/parser oracle for it. Typed method existence or resource subscription alone does not prove its payload decoding. The separate schema 334 metadata parser and chunk-transfer proof do not establish schema 335 compatibility.

Reproduce the wrapper inspection without executing the SDK:

```sh
objdump -T build/hand2-semantics/wuji-sdk-c-2026.8.31-aarch64-linux-gnu/lib/libwuji_sdk_c.so
objdump -d --disassemble-symbols=wuji_hand_2_enable,wuji_hand_2_set_all_effort_limit,wuji_hand_2_get_all_mit_params,wuji_hand_2_online_joints_count build/hand2-semantics/wuji-sdk-c-2026.8.31-aarch64-linux-gnu/lib/libwuji_sdk_c.so
```

`objdump` labels stripped internal callees relative to a preceding export such as `wuji_version`; those displayed symbol names are not evidence about the internal function's identity. The call destinations and instruction/data bytes cited above are the evidence.

### Joint-command publisher protocol

The following is static evidence from the pinned Linux C library, not execution of the Linux binary or a physical-device test. The Mac bridge must separately validate its native memory layout and retained functions. In particular, payload field order cannot be inferred from Rust memory layout.

Creation follows C `0x1923d0` → `0x18a8bc` → Hand2 helper `0x26e054` → typed publisher `0x1a60a4`. It looks up the literal `joint_command` (address `0xc785a7`, length 13), validates schema 315 through `0x5c03ec`, and performs actual publisher startup through `0x5a703c`. Schema zero is accepted as unspecified by that validation helper; an incompatible nonzero schema is rejected. The Linux resource entry has `resource_id: u16` at byte 56, `node_id: u16` at byte 58, schema at byte 60, and serde format at byte 63. The startup log at `0xd7ca48` labels the first of these fields `resource_id`; the Mac native capability-cache borrow was separately verified against a mock child-node resource and agrees with these ID offsets.

Startup poll `0x580be8` constructs an opcode `0x30` request and accepts opcode `0x31`. The header node ID comes from the resource entry; the current native session ID comes from the connection. The request body is Postcard:

```text
u8  1
u16 startup_counter_old_value
u16 resource_id
u16 0
```

All u16 fields above use unsigned Postcard varints. Native request struct bytes 0/2/4/6 hold the counter/resource/zero/version, respectively; serializer `0x569bcc` emits the byte at offset 6 first, then u16 fields at 0, 2, and 4 through varint routine `0x572110`. The startup counter uses wrapping atomic u16 fetch-add(1), proven by `0xc61fd0`, independently of the transport packet counter and command-frame counter. The native pointer chain is `DeviceConnection+0x68` → Arc read lock at `Arc+0x10` → guard data; the startup atomic is at guard data + `0x64`. The initial value of that counter is not established by this Linux trace.

The startup response has a fixed three-byte prefix `status: u16 LE` and a byte of payload metadata. A nonzero status fails. After that prefix, `0x540740`/`0x569c38` parse exactly three u16 Postcard values without an array-length prefix. The caller returns the second value as the stream ID; it does not validate the other two values here. This matches the shape of schema/stream/rate replies already used by native subscribe tests, but the names of the first and third values are inferred from that related protocol. The negotiated ID is saved at publisher byte `0x80`, and its node ID is registered in the native stream map. The publisher also retains a connection clone and a lease; it does not use the resource ID as the stream ID.

Send follows `0x27d6d0` → `0x264cdc` → `0x264d54` → `DeviceConnection::publish` at `0x5a9f44` → concrete future poll `0x5811d0`. The Hand2 helper validates all 60 position/velocity/effort floats as finite. After validation, it atomically increments its own u32 command sequence at publisher byte `0x90`, starting at zero, and puts the old value in `JointCommandFrame`. A send failure after that point still consumes the command sequence. The existing native schema-315 fixture proves the command payload is `seq: u32 varint` plus exactly 20 triples of f32 values when the resource format is Postcard; JSON follows the actual resource metadata.

For serialized command payloads of at most 1400 bytes, the native publish packet is:

```text
18-byte WujiHeader: bytes 57 10 50 00, current session ID,
                   shared transport packet sequence, reserved/checksum field 0,
                   registered node ID, full payload length
payload: stream_id u16 LE | Unix timestamp_us u64 LE | serialized command payload
native two-byte tail CRC
```

The **ten-byte stream prefix is fixed little endian in this client-to-device path**. `0x581458` calls `0xbe9d8c`, which writes the u16 to bytes 0..1 and u64 to bytes 2..9 and returns 10. The header begins `57 10 50 00` at `0x581420..0x581444`; frame encoder `0x59791c` only updates the payload length before native header encoder `0xbe8f48` copies those four bytes verbatim. This differs from the independently verified device-to-client localhost stream fixture, whose prefix is Postcard. Do not silently substitute that receive fixture's format for the publish prefix.

Timestamp generation is Unix seconds × 1,000,000 + nanoseconds / 1,000, or zero if the clock predates the epoch (`0x5812a0..0x5813cc`). The transport packet sequence is another atomic u32 fetched from the Arc at `DeviceConnection+0x50`, atomic at Arc+16 (`0x5816f8`, `0x5d93f4`). It is shared with actual native requests and must not be replaced by an independent Python packet sequence. The native stream map provides the node ID for the negotiated stream and rejects missing streams (`0x59c3c0`). Frames larger than this limit take a separate fragmentation path through `0x5b9c58`; its full protocol has not been established here.

Close is active native cleanup, not only allocation release. C close `0x194fb8` ultimately calls publisher drop `0x2a5e70`; if its closed flag is false and the backend is native, it obtains its lease and schedules a cancellation future with the stream ID. Under an existing runtime it spawns; otherwise it blocks on `0x5aa308`. That future (`0x582c30`) waits for a background channel permit, then queues the u16 stream ID through `0x5b8858` → `0x5b8674` → `0x5bce04`. It does not wait for a device cancellation acknowledgement at this level.

The background consumer was then independently traced in the pinned **Mac** image: `CancelHandler` task poll at image `0x959138` calls actual future poll `0x955c20`. It skips missing/already-closed streams, or decrements the stream's reference count when greater than one (`0x955f48..0x956010`). For the last user, it reads the registered node ID at stream entry byte 14 and fetch-adds the same shared startup/cancellation u16 counter (`0x956018..0x956038`). The cancel request has opcode `0x34`, expects `0x35`, and its Postcard body is `u8 1`, `u16 counter_old_value`, `u16 stream_id`. Native request memory offsets are counter 0, stream 2, version 4; native serializer `0x99da18` → `0x969ce4` emits that wire order. The request header begins `57 10 34 00` (`0x9560ac`), with the registered node ID and current session/transport sequence. The reply is checked for kind `0x35` at `0x9563b8`; status is parsed by native `0x9dbb14`. A nonzero device status is logged, then cleanup still continues (`0x956454..0x9564b4`): it closes the local stream hub and marks the native stream entry closed. A direct replacement RPC can explicitly wait for acknowledgement for its own unique publisher handle, but that is stricter than the original background-close timing and error handling. Shared subscription/publisher entries require the native reference-count behavior.

The separate native bridge work has now implemented and exercised that bounded test in `scripts/probe_mock_device.py --resources --stream --publisher`: a nonzero resource node and distinct negotiated stream ID, two typed commands plus a generic payload, native packet CRC/header/session, shared startup and transport counters, independent command sequence, finite-value rejection, size rejection, and actual cancellation. The probe also exercises a rejected cancellation followed by a fresh publisher lifecycle. These tests establish transport/protocol behavior against a localhost fixture, not successful motion of a real Hand2. The original SDK's fragmentation path remains outside the bounded bridge implementation.

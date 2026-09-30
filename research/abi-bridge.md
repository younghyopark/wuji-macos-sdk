# SDK ABI inspection, 2026.8.31

The native CLI contains usable Rust SDK exports. It does **not** contain the
public C wrapper entry points from the Linux SDK. Reusing a Rust method therefore
requires a bridge whose layout and calling convention have been checked against
this exact binary; including `wuji_sdk.h` does not establish that bridge.

Run the static inspection from this repository:

```sh
python3 scripts/inspect_sdk_artifacts.py
```

It verifies the three archives against `artifacts.lock.json`, verifies the
extracted CLI against its archive, extracts the C header and Python type stubs,
reads both Mach-O and ELF dynamic exports, and saves bounded disassembly under
`build/abi-inspection/`. It never loads an SDK library, initializes a manager,
scans, or connects to hardware. It needs Python 3 and Apple Command Line Tools.

## Artifacts and symbol lookup

- Official CLI: `.artifacts/cli-macos-arm64/2026.8.31/wuji`, executable SHA256
  `e65fc23d0739487eb41d36ad9af85fd0fa799f4a888385bd29a7e0ab5176b905`.
- Experimental converted copy: `build/native-reuse/libwuji_cli_reuse.dylib`.
  Its conversion and pure codec probes are separate from this inspection.
- Linux C archive: `.artifacts/sdk-c-linux-arm64/2026.8.31/wuji-sdk-c-2026.8.31-aarch64-linux-gnu.tar.gz`;
  public header `include/wuji_sdk.h`, shared object `lib/libwuji_sdk_c.so`.
- Linux CPython archive: `.artifacts/sdk-python-linux-arm64/2026.8.31/wuji_sdk-2026.8.31-cp312-cp312-manylinux_2_34_aarch64.whl`;
  Python API contract in `wuji_sdk/__init__.pyi`.

`nm -gU` sees only the executable-header symbol. Apple's
`xcrun dyld_info -exports` sees 64,437 dynamic export entries, including Rust SDK
methods. `dlsym` takes the displayed Mach-O name with **one** leading underscore
removed. Reported offsets are relative to the image, not fixed runtime addresses.
The raw export list and lookup names are saved for reproduction.

## Small offline C functions

These are documented in the shipped C header and present in the Linux C shared
object. None is exported by the Mac CLI, including after conversion to a dylib.

| Function | Known C signature / behavior |
| --- | --- |
| `wuji_version` | `const char *(void)`; process-lifetime NUL-terminated version string |
| `wuji_last_error` | `const char *(void)`; C-wrapper error text |
| `wuji_hand_2_joint_id_from_bus_node` | `uint8_t(uint8_t bus_id, uint8_t node_id)`; `bus_id * 5 + node_id - 1`; node zero is invalid |
| `wuji_hand_2_decode_joint_status` | `WujiStatus(uint32_t, WujiJointStatus *)`; local status decoding |
| `wuji_hand_2_joint_label` | `WujiStatus(uint8_t, char *, size_t, size_t *)`; local index-to-label lookup |
| `wuji_hand_2_finger_name` | `WujiStatus(uint8_t, char *, size_t, size_t *)`; local finger-name lookup |
| `wuji_hand_2_describe_error` | `WujiStatus(uint16_t, WujiErrorInfo *)`; static fault-catalog lookup |

The firmware joint ID is sparse: bus 4, node 4 gives 23. It is not the dense
0–19 joint index used by the command arrays. The header explicitly distinguishes
these two spaces. Fault code zero means no active error and is absent from the
fault catalog. Reimplementing these small helpers is possible but would not prove
that native SDK code had executed.

An additional exported pure Rust candidate is `Pose::identity` at offset
`0x9d952c`:

```text
_RNvMNtNtNtCs7G3C2LVP3LI_9wuji_core7schemas9handwrite4poseNtNtNtB6_9generated4pose4Pose8identity
```

This is a no-input constructor, but its return layout must be determined from
the code, not assumed from `WujiPose`. The separately developed header codec
probe is a more useful first native-execution test.

## Manager and Hand 2 operations

The smallest manager entry is:

```text
_RNvMNtCsdhiNM91e48J_8wuji_sdk7managerNtB2_10SdkManager8instance
```

It is at image offset `0xb2a5cc`. Disassembly and isolated execution establish
`fn instance() -> &'static SdkManager`: no input registers are consumed and the
function resolves a global OnceLock before returning a pointer. This remains a
version-specific Rust ABI, not a vendor-supported C declaration. First use
initializes global state. `native/manager_bridge.c` exposes a C facade and
redirects the SDK's filesystem paths before calling it.

The following exact dlsym names are available:

| Operation | Image offset | dlsym name |
| --- | --- | --- |
| Manager scan | `0xb28bac` | `_RNvMNtCsdhiNM91e48J_8wuji_sdk7managerNtB2_10SdkManager4scan` |
| Manager connect | `0xb28f94` | `_RNvMNtCsdhiNM91e48J_8wuji_sdk7managerNtB2_10SdkManager7connect` |
| Raw byte get | `0xad059c` | `_RNvMNtCsdhiNM91e48J_8wuji_sdk6deviceNtB2_10WujiDevice13get_raw_bytes` |
| Generic get | `0xad1904` | `_RNvMNtCsdhiNM91e48J_8wuji_sdk6deviceNtB2_10WujiDevice7get_raw` |
| Generic set | `0xad19c8` | `_RNvMNtCsdhiNM91e48J_8wuji_sdk6deviceNtB2_10WujiDevice7set_raw` |
| Raw subscribe | `0xad06f0` | `_RNvMNtCsdhiNM91e48J_8wuji_sdk6deviceNtB2_10WujiDevice13subscribe_raw` |
| Execute action | `0xad1724` | `_RNvMNtCsdhiNM91e48J_8wuji_sdk6deviceNtB2_10WujiDevice7execute` |
| Typed Hand 2 wrapper | `0xb1c1bc` | `_RNvMs8_NtNtNtCsdhiNM91e48J_8wuji_sdk7devices11wuji_hand_24handNtB5_9WujiHand211from_device` |

The manager and raw device entry points are synchronous. ARM64 assembly shims
place indirect results in `x8` and preserve Rust pointer/length arguments. Their
concrete result, target, options, and device layouts are recorded next to the C
bridge implementation. `WujiDevice` is an owned 56-byte value containing a name
and reference-counted client/parameter state. It is not a single Arc pointer.
The bridge retains the native image for process lifetime, copies returned JSON
into C allocations, and calls this image's matching drops for Rust values.

`scripts/probe_manager.py --scan --connect-errors` verifies manager singleton
identity, native log filters, default user lookup/list/switch, missing-user
errors, empty scan, connect validation errors, unavailable transport errors,
disconnect, JSON validation, and unchanged process HOME. Its child process
denies all network access and IOKit access. Empty scan is the native result
inside that sandbox, not evidence that an attached device was searched.

`scripts/probe_mock_device.py --resources` verifies a successful real native
connection against an explicitly simulated UDP device bound to 127.0.0.1. Its
OS sandbox permits localhost traffic and repository writes only and denies
IOKit. The native SDK negotiates discovery, session, timing, node types, and
nodes; reads the fixture's advertised uint32 resource as 42; sets it to 123456;
and reads back 123456. Metadata, firmware, node and resource introspection,
missing-resource GET/SET/action/subscription errors, native connection status,
disconnect, and idempotent clone release also pass. Packet and result evidence
is saved in `build/mock-device-probe/report.json`.

Adding `--stream` also advertises a physical `realtime_stream` resource. The
native SDK derives its `joint_states` and `joint_diagnostics` virtual resource
descriptors from that capability. Physical and virtual subscription creation,
empty polling, a nonempty two-joint physical frame, native conversion to the
matching JointStateFrame bytes, and close/unsubscribe all pass. The injected
RealtimeStreamFrame and expected JointStateFrame were separately checked by
native Postcard deserialization and serialization in `scripts/probe_device.py`.
This validates the actual native virtual stream pipeline, while all supplied
measurements still come from the fixture.

Adding `--fingertip` exercises the retained native `get_fingertip_info` helper
through `fingertip.info_req` SET and `fingertip.info_chunk` GET transactions.
For finger 2 the fixture returns four data chunks followed by an empty chunk;
the native helper requests offsets 0, 17, 34, 51, and 55, assembles the native
schema-334 sample, and returns its exact model, digest, header, type, rate, and
format JSON. Both internal chunk schemas therefore have executed Mac protocol
evidence as well as the earlier static field-order analysis. Capability
listings expose the equivalent slash-separated paths, `fingertip/info_req`
and `fingertip/info_chunk`, and the nonempty node path `fingertip`.

The probe also sends a valid inert heartbeat through the SDK's actual
MessageActor `SendFrame` queue, awaits native transport completion, and checks
that the localhost fixture received those exact bytes. Physical and virtual
subscription rate setters preserve native unsupported-capability errors because
this fixture does not advertise rate control; successful rate changes are not
claimed.

The fixture is importable as `MockWujiUDP(library_path,
advertise_resources=True)` from `scripts/probe_mock_device.py`. The context
manager exposes `.address`, `.serial`, and `.scalar_value`; the only scalar
resource is `fixture_value`. With `advertise_resources=False`, it advertises
empty resource and node pages unless `advertise_stream=True` is set. Stream
mode adds `.emit_stream(payload, timestamp=0)` for explicitly injected packets;
the module exports `REALTIME_SAMPLE` and `JOINT_STATES_SAMPLE` fixture bytes.
`advertise_fingertip=True` enables the chunk exchange and exposes its recorded
selectors in `.fingertip_requests`; `FINGERTIP_INFO_SAMPLE` and
`FINGERTIP_INFO_VALUE` are the corresponding raw and decoded reference values.
This fixture exercises the actual binary and
transport; it does not establish physical Hand 2 compatibility, USB behavior,
motion-control correctness, or nonempty discovery behavior.

## State containment

Static byte inspection finds these environment variable literals in the CLI:
`WUJI_LOGS_DIR`, `WUJI_TELEMETRY_DISABLE`, `WUJI_NO_UPDATE_CHECK`, `WUJI_API_URL`,
`WUJI_ZENOH_PROTO`, `WUJI_ZENOH_CHUNK`, and `WUJI_UPDATE_REPO`. It also finds
`.wuji`, `HOME`, `storage.json`, `registry.toml`, calibration paths, and telemetry
queue paths. The public `WujiInitOptions` contains only `int32_t log_level`.

There were no literals for `WUJI_HOME`, `WUJI_SDK_HOME`, `WUJI_DATA_DIR`, or
`WUJI_CONFIG_DIR`. This limited negative search is not proof that no state-root
override exists. `WUJI_LOGS_DIR` alone cannot be assumed to redirect user
registries, telemetry queues, and calibration files. The bridge redirects these paths before initialization
by replacing exactly one lazy `getenv` import in this loaded SDK image's writable
`__DATA` section. It returns the requested state root for the SDK's `HOME`
lookup and a contained `.wuji/logs` path for `WUJI_LOGS_DIR`; it also returns `1`
for update-check and telemetry opt-outs. Other environment lookups delegate to
libc. Process HOME and other libraries' environment access are unchanged. No
code/text pages, vendor artifact, or global environment variable are modified.

Native manager initialization creates
`<state_root>/.wuji/sdk/users/registry.toml`. Successful sandboxed initialization
and repeated user operations demonstrate these writes stay inside the chosen
repository state directory. The SDK singleton fixes its library and state root
for the process; a different second initialization is rejected. The image and
its worker runtimes remain loaded until process exit.

`wuji_set_log_level` operates before manager initialization and updates the
actual native `log::MAX_LOG_LEVEL_FILTER` and `tracing_core::metadata::MAX_LEVEL`
atomics. Off/error/warn/warning/info/debug/trace spellings were checked against
the atomic values; invalid levels leave them unchanged. The CLI's output
subscriber is not installed by these SDK entry points, so this controls filters
without promising terminal or file log output.

## Native user management and paths

`scripts/probe_manager.py --users` exercises native create/update/delete and
tactile path resolution with network and IOKit denied and all writes contained
in the repository. The bridge calls the retained native registry code; it does
not implement its own user database. The current Python stub and Linux C SDK
provide the public field names and semantics, while disassembly and executed
calls establish this pinned Mac binary's ABI.

| Native function | Mac offset | Verified ABI and behavior |
| --- | --- | --- |
| User create | `0xad3ab4` | Borrowed name, external ID, description pointer/length pairs; native UUID, timestamps, validation, directory creation, and registry write |
| User update | `0xad4324` | Borrowed ID and optional name, then pointers to external-ID and description changes; native duplicate validation and persistence |
| Manager user lock | `0xb28190` | Native manager mutex guard held across create/update and dropped using `0xb25b2c` |
| Manager delete user | `0xb27274` | Native deletion and parameter reload; deleting the current user switches to default |
| Tactile directory | `0xad424c` | Borrowed user ID and serial; native safe-segment validation |
| Tactile run directory | `0xad4d6c` | Same input ABI; returns native calibration-run directory |
| Tactile model paths | `0xad51a0` | Same input ABI; returns native directory, safetensors path, and npz path |

Create/update return a 152-byte UserInfo on success or a `cap=-1` sentinel with
SdkError at offset 8. Update's nested optional string has three machine words:
tag 0 leaves the field unchanged; tag 1 with a null pointer clears it; tag 1
with a pointer and length sets it. The bridge's public `clear_fields` values
are 1 for description and 2 for external ID. It formats native UserInfo through
the retained serializer and drops all UserInfo, path, error, and guard values
with this image's corresponding destructors.

The probe checks quoted/newline descriptions, external IDs, rename/set/unchanged/
clear updates, duplicate and empty-name errors, missing users, invalid serial
paths, all three resolved path forms, switch-to-created-user followed by its
deletion, automatic return to default, and refusal to delete default. It removes
only the user it creates and confirms that process HOME remains unchanged.

The pinned 2026.8.31 Python stub does not contain
`set_glove_imu_global_transform` or `get_glove_imu_global_transform`, and no
matching Mac export was found. These older names are not implemented by guessing
calibration storage. Whole user-data ZIP export/preview/import entry points have
not yet been recovered, although lower-level native ZIP and manifest routines
remain in the image; their presence alone does not establish a usable public ABI.

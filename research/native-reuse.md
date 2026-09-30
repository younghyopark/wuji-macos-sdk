# Native CLI reuse investigation

Verified 2026-09-10 on Apple Silicon macOS. This is an experimental reuse of
compiled vendor code, **not a source rebuild of `wuji-sdk`**. No hardware was
accessed. Pure ABI probes deny all networking; transport probes allow only
explicit localhost UDP fixtures and deny IOKit access.

## Result

The official CLI executable can be converted into a loadable dylib without
rewriting its machine code. The C/ARM64 bridge now calls actual native SDK
manager, resource, codec, physical subscription, virtual subscription, and
transport functions from that dylib. These are release-specific private ABI
adapters, not a stable Rust ABI or a source rebuild.

Completed validation includes:

- Native header encode/decode and malformed-header errors.
- 110 native JSON schemas; seven unequal-field codec fixtures, including a
  20-joint command, real-time input, derived joint state, and fingertip metadata.
- Native broadcast-channel creation, empty/lagged/frame/closed states, and
  destruction, plus unit oneshot completion/error/cancellation ownership.
- Real native connection to an explicit localhost UDP fixture, capability and
  node discovery, info, scalar GET/SET/readback, disconnect, and handle release.
- Physical real-time subscription and the SDK's own virtual `joint_states`
  pipeline. Injected real-time bytes and received derived state bytes match
  native parser/serializer fixtures exactly. SUB/UNSUB use actual SDK transport.
- The whole native fingertip-info method performs chunk selection and retrieval,
  handles a terminating empty chunk, and produces the expected structured data.
- A retained native `MessageActor::SendFrame` command sends through the SDK's
  transport and acknowledges completion; a localhost fixture observed the exact
  inert heartbeat frame. Generic native request-frame roundtrips also pass.

The publisher path now also passes localhost validation: real PUB negotiation,
negotiated stream ID, two native-serialized joint command frames with distinct
sequence values, an unchanged generic payload, and cancellation. Native shared
packet and stream-request counters preserve coordination with subscriptions.
Invalid nonfinite commands, oversized payloads, duplicate opens, and cancellation
errors have explicit tests. Physical Hand 2 interoperability, actuator motion,
and firmware flashing have not been tested.

## Artifact and binary evidence

- Official release: [wuji-cli v2026.8.31](https://github.com/wuji-technology/wuji-cli/releases/tag/v2026.8.31).
- Local input: `.artifacts/cli-macos-arm64/2026.8.31/wuji`.
- Input SHA256: `e65fc23d0739487eb41d36ad9af85fd0fa799f4a888385bd29a7e0ab5176b905`.
- Original is thin ARM64 Mach-O `MH_EXECUTE`, PIE, minimum macOS 11.0, built
  against SDK 15.5. It has no separately linked Wuji dylib; `otool -L` shows
  macOS system libraries and frameworks.
- `nm -gU` shows only `__mh_execute_header`. **That is not the entire exported
  interface.** `dyld_info -exports` reads the retained export trie and reports
  **64,437** exports. Of these, 8,612 names contain `wuji_sdk`, 4,018 contain
  `wuji_client`, 3,862 contain `wuji_core`, and 162 contain `wuji_hand_2`.
  These substring counts overlap and are not API counts.
- Export examples include Hand 2 handlers, `WujiHand2::from_device`,
  `JointCommandFrame` serializers, SDK manager internals, and UDP transport.
- No `PyInit` export or `wuji_sdk_` C-ABI prefix was found. The CLI is not the
  native Python extension, and the official C wrapper is not present here.
- Embedded source paths identify `wuji-sdk-dev` commit prefix `89869f5`.

The proof preserves the official executable and writes a derived copy only.
Its derived SHA256 after ad hoc signing was
`2fbb622c6d59264942ff726db620febde0eaeebf522be9466122e791ab3f1d9a`.
`codesign --verify --strict` passed on that copy.

## Reproduce the offline proof

From the repository root, with Apple's command-line development tools:

```sh
python3 scripts/experimental_macho.py \
  .artifacts/cli-macos-arm64/2026.8.31/wuji \
  build/native-reuse/libwuji_cli_reuse.dylib --probe
```

The script refuses an input whose SHA256 differs from the pinned artifact.
It changes only the Mach-O container metadata and code signature:

1. Changes `MH_EXECUTE` to `MH_DYLIB` and clears `MH_PIE`.
2. Makes `__PAGEZERO` zero-sized, preserving its segment index. Removing the
   segment would shift indices used by existing dyld fixups.
3. Removes `LC_MAIN` and `LC_LOAD_DYLINKER` and adds `LC_ID_DYLIB` within the
   existing load-command region.
4. Re-signs the derived artifact ad hoc.

`--probe` compiles `native/header_bridge.c` and `native/header_bridge.S` under
`build/native-reuse/`, then launches a fresh Python process through
`sandbox-exec` with `(deny network*)`. It invokes only pure byte codecs. No SDK
manager is initialized and no device API is called.

Expected probe output includes:

```json
{
  "dlopen": "ok",
  "wuji_core_header_roundtrip": "ok",
  "wire_hex": "5710123478563412efcdab90112266558877",
  "decode_error_cases": 3,
  "output_bounds": "ok",
  "hardware_access": "none",
  "network": "denied by sandbox-exec"
}
```

The synthetic bytes are inert test data, not a validated command packet.

## Small C interface for packaging

```c
int wuji_header_roundtrip(
    const char *library_path,
    const unsigned char *input, size_t input_len,
    unsigned char *output, size_t output_len,
    char *error, size_t error_len);
```

- `0`: native decode and encode succeeded; exactly 18 output bytes written.
- `1`, `2`, `3`: native truncated-header, magic, or version error.
- `-1`: invalid arguments or output capacity below 18 bytes.
- `-2`, `-3`: load or symbol-resolution failure.
- `-4`: unexpected native result.

Output remains untouched on failures. Optional error text is NUL terminated
when its buffer capacity is positive. The wrapper intentionally keeps the
private Rust object layout inside C/assembly and does not guess semantic names
for header fields.

Build independently with:

```sh
xcrun clang -arch arm64 -dynamiclib -std=c11 -Wall -Wextra -Werror \
  native/header_bridge.c native/header_bridge.S \
  -o build/native-reuse/libwuji_header_bridge.dylib
```

## Version-specific ABI evidence

`WujiHeader::decode` starts at image-relative offset `0x009D92E4`; `encode`
starts at `0x009D9384`. Their full exported names are in `header_bridge.c`.
In ARM64 disassembly, both use register `x8` for their aggregate return address.
The assembly shim supplies that address while keeping a stable C entry point.

For this binary, successful decode writes a result tag at offset 0 and a
20-byte header object at offset 4. Encode consumes that object, returns its
result tag at offset 0, and writes the output length at offset 8. These layouts
were checked against disassembly and exercised; they are not a portable ABI.
The [Rust type-layout reference](https://doc.rust-lang.org/stable/reference/type-layout.html)
does not guarantee a fixed field order for the default representation, and the
[Rust function ABI documentation](https://doc.rust-lang.org/std/primitive.fn.html#abi-compatibility)
requires compatible signatures when calling function pointers.

## Additional reproducible probes

```sh
python3 scripts/probe_device.py
python3 scripts/probe_mock_device.py --resources --stream --fingertip --publisher
```

The first compiles the device bridge and runs native codecs and ownership
checks in a subprocess with all network access denied. It writes recovered
schemas and codec fixtures under `build/device-probe/`. The second binds a
fixture exclusively to `127.0.0.1`, permits only localhost networking in its
sandbox, and rejects hardware IOKit access. Its in-memory scalar and synthetic
sensor values do not represent physical hardware behavior.

## Device ABI and ownership evidence

The public bridge declarations are in `native/device_bridge.h`; private layouts
stay in C and assembly. `native/rust_format.c` provides actual Rust Display
formatting via a C implementation of the pinned `fmt::Write` vtable.

- `WujiDevice` owns 56 bytes. Its client Arc is at offset24. Successful manager
  connect returns an owned record, not an Arc pointer. Native clone/drop paths
  independently establish the representation.
- `WujiHand2::from_device` at `0xB1C1BC` consumes that device and builds20 joint
  wrappers in a1656-byte record. Its full body only constructs names and clones
  device handles; virtual-topic activation does not depend on this constructor.
- Actual capability descriptors include native schema IDs, access flags and
  serialization formats. `ResourceEntry` stores resource ID at56, node ID at58,
  and schema ID at60; a mock resource with unequal node/resource IDs verifies
  this mapping. Metadata is borrowed only while its native read guard is held.
- Physical raw subscriptions own64 bytes. Their native broadcast receiver is
  at32 and actual stream ID at56. Virtual subscriptions use the retained native
  global subscription hub, its untyped byte receiver, and rate lease. Hub
  release, receiver drop and rate-Arc drop follow the native lease destructor.
- The native Hand2 registry contains `joint_states` (330) and
  `joint_diagnostics` (332), dependent on `realtime_stream` (291). The native
  virtual topic descriptor uses Postcard. The validated state conversion is
  performed by native SDK code, not by a Python approximation.
- `MessageActor::run` at `0x9528B4` retains `SendFrame`:48-byte command, tag1,
  owned unit-result oneshot sender at8, native `bytes::Bytes` at16. Construction
  uses native `Bytes::copy_from_slice`, channel allocation and queue send. Its
  real transport result is observed through the owned completion channel;
  timeout closes that receiver so a later native send can drop its result.
- Connection access uses the same native RwLock read guard as SDK device calls.
  MessageManager's packet sequence Arc is at80 (0x50), with native AtomicU32 at
  Arc+16; initialization and existing native callers establish this field.

No arbitrary symbol is treated as callable solely because its name exists.
Aggregate returns use the assembly x8 return adapter, result tags and object
sizes are checked against disassembly, and ownership paths are tested in
isolated child processes. These checks reduce the experimental surface but do
not turn the private ABI into a vendor-supported interface.

## Publisher (CLI 2026.9.22)

CLI 2026.8.31 kept no callable Hand 2 publisher, so the earlier bridge rebuilt
the UDP publish protocol (startup, framing and cancellation) from the Linux C
SDK's machine code and enforced its own limits: one publisher per resource and
payloads of at most 1400 bytes. CLI 2026.9.22 links the SDK's typed publisher,
and the bridge now calls it directly (`native/device_bridge.c`, native
publisher section):

- `wuji_client::manager::Device::publish::<JointCommandFrame>` (`0xC3C14C`)
  validates schema 315 and negotiates the stream itself; the result is the
  0x90-byte `Publisher` inline, or UINT64_MAX with a `ClientError` at +8.
- `JointCommandPublisher::send` (`0xBB9790`) checks all 60 floats are finite,
  stamps the u32 command sequence at +0x90 and serializes with the resource's
  own format. Generic payloads go through `Publisher::send_raw` (`0xBB8364`).
- `JointCommandPublisher::close` (`0xBB993C`) consumes the publisher.

The port inherits upstream semantics. Publishers on one stream are
reference-counted, so the last one to close sends the cancel. Sends return once
the frame is queued, not when it reaches the wire. Cancellation runs in the
background, so `close()` always releases the handle, and a device-rejected
cancel is logged natively rather than returned. Fragmentation belongs to the
SDK. The localhost UDP fixture (`scripts/probe_mock_device.py`,
`tests/test_loopback_native.py`) and the Zenoh peer checks verify the exact
wire bytes.

The 2026.9.22 `ConnectOptions` gained a flag byte at +0x28, which pushed the
capability-query attempt count from +0x28 to +0x29 (default 3). The query budget
is that count times the u16 at +0x24 (1000 ms). Leaving +0x29 at zero makes
every connect fail with "No reply from Zenoh queryable".


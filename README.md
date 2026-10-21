# wuji-macos

Experimental Apple Silicon bindings for **WUJI Hand 2**, targeting the public
`wuji_sdk` Python API. Downloads, derived binaries, environments, SDK state,
and test output stay in this repository.

## Status

The wheel now provides `import wuji_sdk`, the Hand 2 Python classes and resource
interfaces, native SDK manager initialization, discovery, connection handling,
and SDK user selection. It is **not yet a complete SDK replacement**. A Python
method existing is not evidence that its native operation works.

| Layer | Evidence and remaining limits |
| --- | --- |
| Python API | Hand 2 example imports and callback/async/publisher ownership tested with an explicitly injected test backend. Full SDK inventory still has missing classes and methods. |
| Native manager | Real initialization, discovery, native connection errors, disconnect-all, user CRUD/selection, and tactile storage-path lookup tested. Successful native handshake and disconnect proven against a localhost UDP protocol fixture. |
| Native resource operations | Installed Python API reads and writes a fixture resource over the actual native transport. Device info and node/resource metadata are exercised end to end. Fingertip sensor metadata is unavailable: SDK 2026.9.22 removed `WujiHand2::get_fingertip_info`. |
| Payloads | Native header codec; native command/MIT/effort/control-mode and read-frame fixtures; strict Python Postcard codec. Unsupported write layouts are rejected. |
| Streaming | A native physical realtime stream feeds the SDK's actual Hand 2 virtual joint-state pipeline, then reaches Python `recv_async()` with verified fields. Rate dispatch is implemented; successful rate negotiation still needs a capable fixture or device. |
| Commands | Publishing calls the SDK's own `Device::publish::<JointCommandFrame>` and `JointCommandPublisher`, which own UDP negotiation, serialization, sequence counters and cancellation. Exact wire bytes pass a localhost UDP fixture and a real Zenoh peer, including all 101 commands from the original MIT sweep. Hand 2 masks, gains, origin and tactile dispatch are checked against pinned native behavior. Motor control was exercised on a physical hand with the 2026.8.31 build; the 2026.9.22 build has been checked live read-only (connect, 1 kHz joint states, publisher open/close). |
| Other SDK features | Glove, Hand 1, recording, offline pipelines, and retargeting do not yet have complete macOS bindings. |

The exact Python surface is recorded in
[the runtime coverage report](research/api-runtime-coverage.md), with per-method
information in its JSON companion. Missing native dispatch raises
`NativeCapabilityError`; the runtime never substitutes simulated devices.

Live validation against a physical Hand 2 over Zenoh is summarized in the
Commands row above. The current
machine is macOS 26.5.2 with ARM64 Python 3.14.4. macOS 11.0 is the binary
deployment target; older macOS and other Python versions are not run-tested.

Current API limits include flash-log export,
device log configuration and parameter persistence, user-data ZIP operations,
global topics, and the other-device features listed above. Native errors are
reported as `WujiException`; the specialized `NodeOfflineError` mapping is not
complete. Publishers follow upstream behavior: publishers on one stream are
reference-counted, sends are queued rather than awaited on the wire, and
cancellation runs in the background, so `close()` always releases the handle.

Joint feed-forward compensation (SDK 2026.9.22, firmware 2.10.0+) is bound as
upstream: `FeedForwardMode`, `hand.set_constant(mode, joints=None)`,
`hand.set_position_table(on, joints=None)`, and per joint
`hand.joint(k).constant().set(mode)` / `.position_table().set(on)`. Like the
Linux C SDK, each joint gets a raw SET of a little-endian u32 on
`{finger}/S{n}/od/action_set_constant` or `od/action_set_position_table`, in
index order, stopping at the first failure. The firmware applies it in RAM and
does not support reading it back. Firmware 2.10.0 accepted both on a live hand.

## Build and verify

Requires native ARM64 macOS, Python 3.11+ for build scripts, and Apple's Xcode
command-line tools. The resulting package requires Python 3.10+.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --cache-dir .cache/pip -r requirements-build.txt
.venv/bin/python scripts/build_macos.py
.venv/bin/python scripts/verify_wheel.py
```

The wheel is written to
`dist/wuji_macos-0.1.0-py3-none-macosx_11_0_arm64.whl`.
The verifier installs that artifact into `.venv-verify/`, runs an offline suite
with network and IOKit access denied, then runs a separate native protocol suite
with only localhost network access. Both restrict filesystem writes to this
repository. Results are saved to `build/verification.json`. The Zenoh fixture
uses a pinned `eclipse-zenoh` wheel installed only in the verification environment.
These synthetic peers are test code; the installed SDK uses the native transport
and does not require the Python Zenoh package.

This distribution is named `wuji-macos`, but its SDK import is `wuji_sdk`.
Use a fresh environment; do not install it alongside the Linux `wuji-sdk`
distribution, which owns the same import namespace.

## Python examples

The upstream Python examples are copied into
[examples/python](examples/python/), including the unchanged Hand 2 replay data.
[examples/upstream.json](examples/upstream.json) pins their source commit and
individual SHA-256 hashes and records the local opposition pacing fix. Its
original source is retained in `examples/upstream_sources/opposition.py`.
Their upstream README describes the API, not the
current completion status of this macOS port.

For a first read-only connection test from this checkout:

```sh
.venv-verify/bin/python examples/macos/inspect_hand2.py
```

The inspection script prints device metadata and five joint-state frames.
Use `--address IP:50001` or `--sn SERIAL` to select a specific hand, and
`--frames 0` for metadata only. It disables device sharing for this inspection.

Read-only Hand 2 examples:

```sh
.venv-verify/bin/python examples/python/wuji_hand_2/0.subscribe_callback.py
.venv-verify/bin/python examples/python/wuji_hand_2/1.subscribe_async.py
```

Both originals have been run through actual native discovery under the offline
sandbox and exit with `No devices found`. The connected subscription API also
passes the local native protocol fixture. The original scripts have not been
run on hardware. Example 7's unchanged command generator and send loop are
exercised against a synthetic Zenoh peer, including all 101 command payloads.
Examples 2, 6, and 7 enable motors; physical motion remains unvalidated.
Example 5 requires flash-log export, which is
still explicitly unavailable in this build.

### Joint PD controller

[examples/macos/pd_controller.py](examples/macos/pd_controller.py) is a minimal
joint PD controller with an aiofranka/aioflexiv-style API (`WujiController`,
`start`/`stop`, `set_freq`, `set("q_desired", q)`, `move`). The PD law runs in
the hand's firmware (MIT mode): the script sets kp/kd/effort limit once and a
1 kHz thread streams position, velocity-feedforward, and effort targets as one
native `float32[60]`, reading `joint_states` as raw bytes decoded with `struct`.
The loop disables the hand on stale feedback, a missing or faulted joint, or
excessive tracking error; reported joint velocity is not used for safety
because it can be nonzero while the joint is stationary.

Without `--confirm-motion` it is a dry run that connects and runs the loop
without changing gains, enabling, or sending:

```sh
python examples/macos/pd_controller.py SERIAL
```

**This enables and moves the hand.** Move joint 16 by 5 degrees and back, then
hold for 3 seconds with the vendor example gains and a 0.5 A limit:

```sh
python examples/macos/pd_controller.py SERIAL --confirm-motion SERIAL \
  --joint 16 --delta-deg 5 --kp 3 --kd 0.05 --effort-limit 0.5
```

### Opposition replay timing

The vendor opposition example slept 1 ms **after** constructing and sending
each command. The macOS copy now uses a 1 kHz monotonic deadline schedule and
prints its measured cadence after cleanup. Run the usual command:

```sh
python examples/python/wuji_hand_2/6_opposition/opposition.py
```

The scheduler preserves every recorded position and the original zero
velocity/effort commands. After a full-period overrun it shifts the schedule
instead of sending a catch-up burst. macOS scheduling is still best effort;
inspect the measured interval tails.

For a short 5,000-frame capture with detailed timestamps, run
`python examples/macos/profile_opposition.py --frames 5000`. It uses the fixed
loop and writes `build/opposition-timing/live-sender.json`. `--mode original`
explicitly selects the saved vendor timing baseline for comparisons. The
diagnostic-only option `--spin-us 100` uses additional CPU for the final wait;
the normal script does not busy-wait.

For a connected communication-health capture that does not move the hand, run:

```sh
python scripts/benchmark_hand2_connected.py --duration-s 10
```

The read-only default records host delivery cadence, device timestamp cadence,
telemetry sequence gaps/duplicates/reordering, native receiver lag, joint and
SDK communication counters, and baseline/final internal finger-bus diagnostics.
Use `--telemetry both` to correlate state and diagnostic projections from each
device frame. It preserves the current device stream rate. To explicitly request
and record a 1 kHz telemetry rate for the run, add
`--request-telemetry-hz 1000`; the script restores the device default during
cleanup.

The same tool can instrument the opposition command path, but this mode enables
and moves the hand. It requires the exact discovered serial twice:

```sh
python scripts/benchmark_hand2_connected.py \
  --mode opposition \
  --sn SERIAL \
  --confirm-motion SERIAL \
  --request-telemetry-hz 1000
```

With `--frames` omitted, active mode replays the complete recording like the
ordinary opposition example; add `--frames 5000` for a shorter run. Active mode
requires the explicit 1 kHz telemetry request for both watchdog streams and
verifies their warmup cadence before enabling. It then requires fresh 20-joint
state and diagnostics, checks the documented sparse-NID mapping, joint ranges,
replay step size, faults, and distance to the first target. During motion it
checks every unseen diagnostic frame and stops on a sequence gap, stale feedback,
a missing/disabled joint, a new motor communication timeout, a low response
rate, excessive tracking error, a limit, or a fault. Cleanup is only reported
successful after a new diagnostic frame confirms that no motor remains enabled;
an unconfirmed normal disable escalates to the device emergency stop.

For the small all-joint control benchmark, use `sine` mode. **This enables and
moves every motor.** The default trajectory is a 5-degree peak (10-degree
peak-to-peak), 0.5 Hz sine wave centered on the hand's freshly measured pose:

```sh
python scripts/benchmark_hand2_connected.py \
  --mode sine \
  --sn SERIAL \
  --confirm-motion SERIAL \
  --request-telemetry-hz 1000
```

The run first requires 10 clean seconds from both 1 kHz telemetry streams. It
then snapshots all 20 joint positions, verifies at least 10 degrees of room to
each documented limit, and generates 10,001 targets over exactly 10,000 one-ms
intervals. A two-second raised-cosine ramp at each end makes the command begin
and finish at the measured pose. All joints receive the same offset in phase.
For this first-debug test the script temporarily applies `kp=3`, `kd=0.05`, and
a 0.5 A effort limit, verifies all 20 readbacks, and restores the original
values only after fresh telemetry confirms the motors are disabled.

Sine mode uses absolute 1 ms deadlines with bounded catch-up: safety checks and
target construction consume the time before each deadline, and no two sends may
start less than 0.8 ms apart. It aborts before a send that would be at least 1 ms
late. It also stops for a send call lasting at least 1 ms, a 20 ms feedback stall,
any sequence or communication-counter anomaly, a joint response rate below 80%,
more than 2 degrees of tracking error, more than 7 degrees of measured excursion
from center, or speed above 0.55 rad/s. Keep the hand secured, the entire pinch
zone clear, cables slack, and an independent power disconnect within reach.
Python cyclic garbage collection is disabled during this short active capture
and restored in cleanup; reference counting remains active. This choice and the
before/after GC state are recorded because cyclic-GC pauses can batch otherwise
lossless 1 kHz telemetry on macOS. Use `--disable-cyclic-gc` with read-only mode
to measure the same tuned host condition without enabling motors.

Each run writes `session.json`, `summary.json`, `commands.csv`,
`telemetry.csv`, and `comm_diag.jsonl` under
`build/connected-timing/<UTC timestamp>/`. Publisher-return timing and healthy
feedback establish host/SDK behavior, not firmware receipt or application of
each command; the current device API exposes no command acknowledgement. State
and diagnostic rows with the same sequence and device timestamp are correlated
views of one device frame, not independent confirmation of two links.

For a separate passive observer, run:

```sh
.venv-verify/bin/python examples/macos/monitor_joint_commands.py
```

It subscribes to the command topic
without enabling or commanding the hand. Its timestamps include observer and
network delay and are not firmware arrival timestamps.

Run a hardware-free comparison of the same replay workload through the actual
native Zenoh publisher with a separate localhost receiver:

```sh
.venv-verify/bin/python scripts/benchmark_opposition_timing.py --samples 5000
```

The command adapter caches the fixed generated field annotations rather than
resolving them again for every joint command. Validation and float32 conversion
remain in place. Measurements and limits are described in
[the timing investigation](research/opposition-timing.md).

SDK storage defaults to `.state/` when run from this checkout. Outside the
checkout, set `WUJI_MACOS_STATE_DIR` to an explicit storage directory:

```sh
export WUJI_MACOS_STATE_DIR="$PWD/.state"
```

The bridge redirects only the reused SDK image's home/log-path lookup. It does
not change the process `HOME`, global Python installation, or shell configuration.
Imports alone do not initialize the SDK manager or open hardware.

## How this build works

The public SDK repository distributes examples and binary releases, without
its Rust implementation/build manifests or a PyPI source distribution. The
Mac CLI already contains many compiled SDK components. This project converts
a **copy** of a checksum-pinned CLI executable into a loadable Mach-O library,
then compiles small C/ARM64 adapters for recovered Rust calling conventions.
It is binary reuse, not a source build of the vendor's PyO3 extension.

Every installed native library is checksum-verified before loading. Rust
containers are handled and freed in the native bridge; Python sees a checked
C ABI. The original executable is unchanged. Updating the pinned CLI requires
reviewing the ABI again. The CLI does not retain every Python SDK call path,
so broad API parity cannot be assumed from the presence of its Rust core.

## Layout

- `artifacts.lock.json`: upstream artifact versions, URLs, and SHA-256 hashes.
- `scripts/`: fetching, Mach-O conversion, binding generation, build, verification, and native probes.
- `native/`: manager, device, codec, and Rust formatting adapters.
- `src/wuji_sdk/`: public Python interface, value types, codecs, and native dispatch.
- `src/wuji_macos/`: native-library integrity/provenance and offline header probe.
- `examples/`: unchanged upstream scripts/data and their provenance.
- `tests/`: native tests, payload fixtures, and explicitly isolated Python contract tests.
- `research/`: source availability, ABI evidence, and API coverage.
- `.artifacts/`, `.cache/`, `.venv*/`, `.state/`, `build/`, `dist/`: ignored local output.

See [native reuse](research/native-reuse.md),
[ABI investigation](research/abi-bridge.md), and
[source availability](research/source-availability.md).

Upstream: [Mac CLI release](https://github.com/wuji-technology/wuji-cli/releases/tag/v2026.9.22),
[SDK repository](https://github.com/wuji-technology/wuji-sdk).

# Public source and protocol audit

Checked 2026-09-10. Scope: **WUJI Hand 2**, SDK/CLI release `2026.8.31`, Apple Silicon macOS. No hardware connections or motion commands were made.

## Source-build status

No complete, publicly accessible Hand 2 SDK implementation was found in this audit. That prevents a conventional source rebuild today; it does not establish a macOS platform incompatibility.

| Surface checked | Result |
| --- | --- |
| Official `wuji-sdk` current branches | `main` (`9db1fe521af94c011ac18cc1a88635dd0338d585`) and `badges` contain examples, documentation, license, and coverage badge only. |
| Existing public SDK checkout history | All 34 locally reachable commits: 77 unique historical paths; no Rust source, Cargo manifest, or protocol schema files. The only top-level history is `.gitignore`, `CHANGELOG.md`, `LICENSE`, `README.md`, `coverage.svg`, and `examples/`. |
| Three public forks | `yanlasuo666/wuji-sdk`, `KevinMi2023p/wuji-sdk`, `SuveenE/wuji-sdk` have the same example-only core. KevinMi2023p's `uv-setup` branch adds demos and build/environment files that consume the prebuilt SDK; its `pyproject.toml` explicitly declares `package = false`. |
| Official `wuji-cli` | All six listed public branches contain documentation, installer scripts, and agent skills, without the Rust implementation. |
| Official `wuji-studio` | Current main tree has README and changelog only. |
| PyPI | Across all 180 `wuji-sdk` release files returned by the JSON API, zero source distributions. Current release has ten CPython 3.10–3.14 Linux wheels covering x86_64 and aarch64. |
| crates.io | Searching `wuji` returned no SDK/core/client crates. |

Evidence: [SDK branches](https://api.github.com/repos/wuji-technology/wuji-sdk/branches?per_page=100), [SDK main tree](https://api.github.com/repos/wuji-technology/wuji-sdk/git/trees/main?recursive=1), [fork inventory](https://api.github.com/repos/wuji-technology/wuji-sdk/forks?per_page=100), [fork example project](https://github.com/KevinMi2023p/wuji-sdk/blob/uv-setup/pyproject.toml), [CLI branches](https://api.github.com/repos/wuji-technology/wuji-cli/branches?per_page=100), [Studio tree](https://api.github.com/repos/wuji-technology/wuji-studio/git/trees/main?recursive=1), [PyPI metadata](https://pypi.org/pypi/wuji-sdk/json), [crate search](https://crates.io/api/v1/crates?q=wuji&per_page=100).

PyPI's public provenance names `wuji-technology/wuji-sdk-dev`, tag `v2026.8.31`, commit `4912b39eb512e358d50fbff5054fa82b2ef1fee9`. The unauthenticated [repository API](https://api.github.com/repos/wuji-technology/wuji-sdk-dev) returned 404. This demonstrates lack of public access, without distinguishing a private repository from other reasons for a 404. No credentials or access controls were bypassed. [PyPI release provenance](https://pypi.org/project/wuji-sdk/2026.8.31/#files)

## What the Hand 2 documentation exposes

The official reference describes the application API, resource discovery, 20-entry position/velocity/effort commands, and device sharing through `ConnectOptions(enable_bridge=True)`. It does not provide a complete Ethernet packet layout or transport codec. Host-to-device examples use UDP address port 50001. SDK-enabled sharing can reach other hosts, so a Mac-native Zenoh client is a plausible alternate front end if the shared resource protocol is validated. [Hand 2 reference](https://docs.wuji.tech/docs/en/wuji-hand/latest/sdk-reference/), [shared connection behavior](https://docs.wuji.tech/docs/en/wuji-sdk/latest/device-connection/)

The public `wujihandpy` implementation targets the original USB-connected hand. It is not the missing Hand 2 SDK. Its **host-side Zenoh bridge** is nevertheless relevant because WUJI explicitly documents compatibility with the common `wuji-sdk` client. [Bridge architecture](https://github.com/wuji-technology/wujihandpy/blob/main/bridge/README.md)

## Shared Zenoh protocol evidence

The public bridge provides these concrete conventions:

- Liveliness token: `wuji/{serial}/@alive`; discovery uses `wuji/**`.
- Capability query: `wuji/{serial}/@capability`.
- Resource key: `wuji/{serial}/{resource_path}`.
- GET: query without a payload; reply contains UTF-8 JSON.
- SET: query with a UTF-8 JSON payload; successful example reply is the JSON string `"ok"`.
- Streaming publish: a Zenoh `put` on a publishable resource key.
- Capability resources advertise `path`, `schema_id`, `can_get`, `can_set`, `can_sub`, `can_pub`, `can_exec`, `internal`, `serde_format`, and `json_schema`. The public USB-hand bridge explicitly advertises `serde_format: "json"`.

These are confirmed for the public bridge implementation, not yet for every current Hand 2 operation. Its `build_capability()` describes itself as following the SDK protocol. Its Hand 1 resource names, 5×4 arrays, and timestamp envelopes must not be reused as Hand 2 schemas. [Implementation, capability builder](https://github.com/wuji-technology/wujihandpy/blob/main/bridge/python/hand_zenoh_bridge.py#L247), [query handling](https://github.com/wuji-technology/wujihandpy/blob/main/bridge/python/hand_zenoh_bridge.py#L595)

Read-only string inspection of the downloaded official Mac CLI confirms the same protocol family, with additional machinery:

| Observed binary text | Implication or limit |
| --- | --- |
| `wuji/*/@alive`, `/@capability`, `/@status`, `/@node/*`, `/@time_sync` | Shared discovery, capability, node, status, and time-sync keys are compiled into the Mac binary. |
| `wuji_client::zenoh::bridge`, `wuji_client::zenoh::proxy` | Both bridge and proxy components appear in the native build. Presence does not prove each CLI command enables them. |
| `kind=summary`, `ZenohNodeTypePage`, `ZenohNodeListPage`, `ZenohResourceInfo` | Current capability negotiation supports paging/node types beyond the simple public USB bridge example. Exact query parameters still need observation. |
| `can_pub`, `can_exec`, `serde_format`, `json_schema`, `encoding` | Metadata includes operation permissions and serialization information. |
| `Unknown data format ... defaulting to Postcard` | JSON cannot be assumed for all Hand 2 resource payloads. Postcard decoding requires the actual schema/layout. |
| Zenoh source paths at version `1.9.0` | The native CLI includes Zenoh 1.9.0 components. |

Artifact inspected: `.artifacts/cli-macos-arm64/2026.8.31/wuji`, fetched from the [official CLI release](https://github.com/wuji-technology/wuji-cli/releases/tag/v2026.8.31). These are binary observations and inferences, not verified public ABI guarantees.

Matching `eclipse-zenoh==1.9.0` has an official CPython 3.9+ ABI3 universal2 wheel including arm64 macOS. A Mac client need not rebuild Zenoh. [PyPI release files](https://pypi.org/project/eclipse-zenoh/1.9.0/#files)

## Concrete next Zenoh experiment

This is an alternative to direct binary/native-wrapper work, not a completed control implementation.

1. Resolve SDK/CLI state paths so all generated data stays inside this repository; do not start a manager until that is established.
2. In a repository-local environment, run a **loopback-only fake device** with explicit Zenoh endpoints and multicast scouting disabled. Advertise an unmistakable synthetic serial, a static readable number, and a synthetic stream. Do not import `wujihandpy` or open hardware.
3. Run the Mac CLI against the fake bridge to capture capability requests and prove discovery/get/sub interoperability. Expand the mock to current paged capability only when observed requests require it. Assert exact values and clean shutdown.
4. Use a Python Zenoh client against the same mock to validate GET, SET, and publish bytes. JSON-only success validates the bridge transport but does not establish Hand 2 Postcard compatibility.
5. After a real hand is available and containment works, start only a read-only native subscription. Observe whether it advertises `@alive`; query capability, all pages, resource encodings, and joint-state schema from the Python client. Archive those responses with CLI/firmware/hardware versions.
6. Implement Hand 2 commands only from confirmed metadata/codec behavior, with no inferred Hand 1 layouts. Until then, high-rate motion via Zenoh remains unverified.

No Zenoh package was installed or bridge started during this audit. No public Hand 2 capability dump or complete wire schema was found in the inspected sources. A separate build from the vendor source, a native wrapper around usable symbols in the Mac CLI, and a validated Zenoh front end remain distinct paths with different completeness claims.

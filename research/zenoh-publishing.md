# Hand 2 Zenoh publishing

The SDK publisher has two transport implementations. The original macOS bridge
only supported the UDP frame implementation, so its `backend != 0` check returned
status 2 without a diagnostic for a connected Zenoh hand. That is a bridge gap,
not evidence that Ethernet or macOS is unsupported.

## Pinned upstream binary evidence

Source artifact: the official Linux ARM64 C SDK 2026.8.31, library SHA-256
`5cebdbec82df966899cfbd8f8361c4faf60ea2f651f0bc520a3b4ffdfa8d79cf`.
Addresses below are ELF virtual addresses in that library.

- Hand 2 typed publisher constructor `0x1a60a4` validates schema 315. Its Zenoh
  branch at `0x1a60e8` clones a 64-byte proxy, a resource-path String, and a runtime
  handle. It creates no stream lease and sends no UDP PUB request. Unlike the
  bounded UDP bridge, this branch can have multiple publisher handles for one key.
- Raw publisher send `0x264d54` checks its first u64 for the Zenoh variant's `-1`
  discriminant. It forwards `(publisher + 8, payload pointer, payload length)` to
  `0x5b2dc4`; the other variant calls `DeviceConnection::publish` at `0x5a9f44`.
- The synchronous Zenoh send clones its proxy/path and owns a copy of the input
  bytes. Its runtime path is `0x542788 -> 0x541cec -> 0x566c64 -> 0x545c60 ->
  0x54a448 -> 0x58a1ec -> 0x57e5e0`.
- The final future at `0x57e5e0` constructs the key with `0x5c2164` from the
  proxy serial string and resource path. It obtains the existing Session through
  the proxy's Arc at byte `0x38`, then uses the Session at Arc + 16.
- At `0x57e648` the native Session publisher builder is made by `0x550c9c`.
  At `0x57e664`, `0x6b4ba4` converts the owned payload into ZBytes. The final
  publication builder contains its 88-byte publisher builder at byte `0x48` and
  32-byte ZBytes at `0xa0`. Encoding (`0xc0`, 40 bytes), timestamp (`0`),
  attachment (`0x20`), and source info (`0xe8`) are all absent. There is no QoS
  override. Native IntoFuture at `0x673250` performs the publication.

Consequently, Zenoh puts the schema-serialized command bytes directly on the
resource key. It does not include the UDP stream ID, UDP frame header/CRC, or UDP
timestamp prefix. The typed command's own sequence and 20 position/velocity/effort
triples remain part of schema 315 and are still produced by the native codec.

The retained Mac equivalents are Session publisher builder `0x98e658`, ZBytes
conversion `0xc686e4`, and publication-builder IntoFuture `0xc66028`. The bridge
uses the full native Session.put constructor at `0xa37780`, which initializes
those owning fields and native defaults, followed by IntoFuture. It reuses the
already-connected session.

The public Python `Publisher` stub exposes `dtype`, `send`, `send_async`, and
`close`; it has no public `stream_id`. Zero for a Zenoh publisher's internal
bridge stream ID is therefore only a bridge sentinel, not a negotiated ID.

## Public source cross-check and fixture dependency

The official [WUJI host bridge implementation](https://github.com/wuji-technology/wujihandpy/blob/main/bridge/python/hand_zenoh_bridge.py)
also publishes resource data directly on `wuji/{serial}/{resource_path}`. That
bridge targets Hand 1 and is used only to cross-check shared Zenoh key conventions;
its hand-specific arrays and timestamp envelopes are not used for Hand 2 commands.

The loopback fixture uses the official `eclipse-zenoh==1.9.0` Python wheel, matching
the native SDK's Zenoh version. Its universal2 CPython ABI3 release includes arm64
macOS. The downloaded wheel SHA-256 is
`6f66059b12e1ec53c70bc25192b0e74502751759064726dbb153ed6dd8f4dc8b`, checked
against [PyPI release metadata](https://pypi.org/pypi/eclipse-zenoh/1.9.0/json).
This dependency is for the synthetic test peer, not the production SDK package.

`scripts/probe_zenoh_publisher.py` binds its peer only on loopback, disables
multicast and gossip scouting and shared memory, and records received bytes.
Its runner denies non-loopback networking and IOKit and confines file writes to
this repository. A successful fixture publication establishes native SDK protocol
behavior; it does not establish physical hand motion.

The test-only `native/zenoh_fixture_bridge.c` selects the native Zenoh connection
method explicitly for a synthetic serial and a loopback endpoint. This avoids
multicast discovery in the sandbox. It is not linked into the wheel. Resource
lookup, device cloning, schema serialization, publishing, and cleanup use the
production device bridge and retained native SDK.

The offline native probe repeatedly exercises key validation, payload ownership,
error formatting, and cleanup under a network-denied sandbox. Python errors now
include the publisher operation and resource path even when a native failure
supplies no error text. A regression test also covers an operation waiting on a
closed publisher whose native pointer is reused before it acquires its lock.
Installed-wheel results are recorded in `build/verification.json`.

The Zenoh fixture uses TCP on `127.0.0.1:7447`. The pinned SDK recognizes port
7447 when selecting an explicit Zenoh endpoint; `WUJI_ZENOH_PROTO=tcp` selects
the fixture's transport and is restored afterward. Only a UDP port 7446 bind is
additionally permitted for native scouting initialization; non-loopback inbound
and outbound traffic remains denied. Exact `@capability` and `@status`
queryables are necessary because Zenoh treats `@` chunks as verbatim and wildcard
queryables do not match them.

Installed Python tests check Postcard and JSON commands, independent sequence
counters, asynchronous sends, and all 101 exact payloads generated and sent by
the unchanged `7.mit_sweep.py` functions. They never run its motor-enable path.
A separate live check in `build/live-zenoh-publisher-verification.json` connects
to the actual Hand 2, reads 20 online joints, opens/closes its publisher, and
disconnects. That live check sends no joint commands.

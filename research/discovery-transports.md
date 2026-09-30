# Native discovery transport compatibility

The pinned vendor Python stub lists only `TransportType.Usb` and `.Udp`.
The pinned Mac SDK's native enum also has `Zenoh` and `CppNative`. Its Display
routine at image offset `0x9acc00`, jump table at `0x43c281a`, and names at
`0x43c29a0` establish all four variants. Native `DiscoveredDevice::from` at
`0x967eb8` copies the transport byte to record offset 145; the C bridge's
mapping was correct.

The generated Python enum originally rejected a real Zenoh discovery record.
`scripts/generate_api.py` now preserves all four named variants, and
`tests/test_discovery.py` exercises the complete public `SdkManager.scan()`
conversion with mixed, nonempty native-format records. Numeric Python enum
values are not inferred from the C header, whose USB/UDP order differs.
Unrecognized native transport values still raise explicitly.

## Live check on 2026-09-10

The rebuilt and reinstalled wheel successfully converted the Hand 2 advertised
at `192.168.1.111:7447` over Zenoh. The read-only inspection then reached
connection negotiation and timed out fetching capabilities. Direct UDP at
`192.168.1.111:50001` also timed out. No successful hardware connection or motor
command was performed.

The active hand-side USB adapter was `en15` (`USB 10/100/1000 LAN 2`), with a
self-assigned `169.254.69.74/16` address. The route to `192.168.1.111` instead
used the campus interface `en7`. The hand-side adapter needs an address on the
hand's subnet; interface identifiers must be rechecked on another machine.
A temporary address for this observed direct cable connection can be added
by its administrator:

```sh
sudo /sbin/ifconfig en15 inet 192.168.1.10 netmask 255.255.255.0 alias
```

This command was not applied by the agent: noninteractive `sudo` reported that
an administrator password was required. Persistent IPv4 settings can instead
be configured using [Apple's TCP/IP settings](https://support.apple.com/guide/mac-help/mh14129/mac).

The local diagnostic output is retained in ignored files
`build/discovery-before-fix.json`, `build/live-hand2-inspection.json`, and
`build/live-hand2-udp-inspection.json`. The installed offline and localhost
verification suites passed after the enum fix. The localhost node-list test
also now checks membership without assuming native map iteration order.

## After the adapter configuration change

A later read-only check from the user's `wuji` Conda environment connected to
the same Hand 2 by its discovered serial and read 20 online joints. The next
failure, `Native bridge returned 2`, occurred when opening `joint_command` for
publishing: the C publisher bridge accepted only the direct UDP backend and
rejected the connected Zenoh backend without an error message. The reproduction
is recorded in `build/mit-sweep-repro.json`; it did not enable motors or send
joint commands. See [Zenoh publishing](zenoh-publishing.md) for the fix and
transport validation.

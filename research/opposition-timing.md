# Hand 2 opposition timing investigation

## What the example schedules

The pinned example uses `read -> build 20 JointCommand objects -> send ->
sleep(0.001)`. The period includes all of that work and sleep overshoot; it is
not a 1 kHz deadline schedule. It also discards each replay frame's microsecond
timestamp. Python explicitly documents that sleep can exceed its requested
duration due to scheduling: [time.sleep](https://docs.python.org/3/library/time.html#time.sleep).

Both replay files have 30,000 finite, distinct frames and match the pinned
upstream files. Their recorded timestamp intervals have median 1,000 us and p99
1,129 us. The largest adjacent position changes are 0.004747 rad (right) and
0.006931 rad (left); no step exceeds 0.01 rad. This rules out repeated low-rate
frames in the files, but does not establish smooth physical motion. See
`build/opposition-timing/replay-data-analysis.json` and its reproducer.

## Measurements and scope

`scripts/benchmark_opposition_timing.py` imports only the vendor example's
functions and sends its original positions through the installed SDK to a
synthetic Zenoh peer. The receiver runs in a separate process so the sender and
receiver do not share a Python GIL. All network traffic is restricted to
localhost and IOKit is denied. Raw send-start, send-return, callback timestamps,
sequence numbers, source hashes, and distributions are retained in JSON.

The initial 5,000-frame run with the previous wheel measured 409.0 Hz for the
original loop: median interval 2.441 ms, p99 3.183 ms, maximum 10.286 ms.
The same run with deadline pacing measured 999.98 Hz: median 1.000 ms, p99
1.053 ms, maximum 1.314 ms. The receiver observed all sequence numbers in order
in both cases. These are local test observations, not a real-time guarantee or
the hand's physical arrival timings. CPU load and power/scheduling state can
affect comparisons. Baseline data: `build/opposition-timing/before-cache.json`.

An offline component profile found the old adapter took about 295 us to build
20 joint commands, mostly from repeatedly resolving their fixed annotations.
Caching that metadata reduced the same construction to about 31 us in a
process-local experiment. Native encoding and its library lookup took about
17 us. Production caching preserves coercion; unresolved forward references
retry and user-defined classes remain dynamic. The profile reproducer and
component results are in `build/opposition-timing/`.

After installing the cache change in the verification environment, the actual
patched `opposition.py` loop sent the first 5,000 right-hand frames through the
native publisher at 996.77 Hz. Sender intervals had median 1.000 ms, p99
1.136 ms, and maximum 8.070 ms; four intervals exceeded 2 ms. The separate
receiver observed all 5,000 sequences in order at 996.84 Hz. This fixes the
systematic slow pacing but does not eliminate rare long stalls. Evidence:
`build/opposition-timing/fixed-script.json`. The final wheel was verified and
installed in the user's `wuji` Conda environment; physical replay of this fixed
version is a separate validation step.

## What changed and what still needs observation

The replay files remain byte-identical. The SDK now caches fixed annotation
metadata, and the ordinary `opposition.py` uses deadline pacing with a measured
cadence summary printed after cleanup. Its original source and both hashes are
retained in `examples/upstream_sources/opposition.py` and `examples/upstream.json`.
`examples/macos/profile_opposition.py` adds detailed sender timestamps; it uses
the fixed loop by default, while `--mode original` selects the saved vendor
baseline. No gains, trajectories, requested velocities/efforts, or Zenoh QoS
settings change. Deadline pacing keeps all frames and shifts the schedule after
full-period overruns rather than trying to catch up with a burst. The diagnostic
wrapper's optional final spin trades CPU for tighter wakeups; it is not enabled
in the normal script.

`monitor_joint_commands.py` only subscribes. Peer mode connects to this hand's
UDP endpoint; client mode did not. A callback interval is an observation at the
Mac, not a firmware timestamp, and missing observations do not prove that the
hand missed commands. A second subscriber can itself add traffic. The sender
and observer reports must therefore be interpreted together.

The original-loop live capture on the right hand sent 5,000 commands at
464.71 Hz. Sender intervals had median 2.104 ms, p99 3.036 ms, and maximum
10.368 ms. It completed cleanup successfully. The passive observer was stopped
before this successful run: the SDK capability query had timed out while it
was running, and the SDK then connected successfully on its own. This single
comparison does not establish causality, so subsequent tests use sender-only
instrumentation. Evidence: `build/opposition-timing/live-original-before-cache.json`
and `connection-without-observer.json`.

Native Zenoh PUT completion is not a device acknowledgement. The pinned SDK
uses upstream defaults that allow batching and drop under congestion. These
match the vendor command path and were left unchanged. Timing/loss observations
are needed before attributing jitter to those mechanisms.
[Zenoh publisher source](https://raw.githubusercontent.com/eclipse-zenoh/zenoh/1.9.0/zenoh/src/api/builders/publisher.rs),
[session implementation](https://raw.githubusercontent.com/eclipse-zenoh/zenoh/1.9.0/zenoh/src/api/session.rs).

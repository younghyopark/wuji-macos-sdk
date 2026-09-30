# Python runtime API inspection

Static inspection of imported Python attributes and source assignments, without native initialization. Presence is not signature equivalence or functioning hardware support.

Pinned stub SHA-256: `10cd4fb8f366492e9b489bf9647306a355baaec36f5c0cd1656265572d09bcaa`. Reproduce with `PYTHONPATH=src .venv/bin/python tests/test_example_contract.py --write-report`.

The imported package exposes 130 of 188 upstream class names. There are 5 missing declared methods on the present classes. The JSON report lists every class, method, signature, field, and source hash; it also distinguishes inherited builtin defaults and source-only instance assignments.

## Hand 2 example import contract

Only SDK import AST nodes are executed by the test. Example main functions, motion code, and other imports are never executed.

| Original example | Missing SDK imports |
| --- | --- |
| `examples/python/wuji_hand_2/0.subscribe_callback.py` | None |
| `examples/python/wuji_hand_2/1.subscribe_async.py` | None |
| `examples/python/wuji_hand_2/2.publish.py` | None |
| `examples/python/wuji_hand_2/3.fingertip_typed.py` | None |
| `examples/python/wuji_hand_2/4.set_stream_rate.py` | None |
| `examples/python/wuji_hand_2/5.export_flash_logs.py` | None |
| `examples/python/wuji_hand_2/6_opposition/opposition.py` | None |
| `examples/python/wuji_hand_2/7.mit_sweep.py` | None |

## Offline lifecycle validation

`tests/test_example_contract.py` explicitly constructs a manager with a test-only backend that rejects unexpected calls. It checks callback delivery, callback and stream errors, sparse telemetry IDs, async receive and cancellation, publisher validation, async send, idempotent stream closure, and garbage collection of an idle callback handle with worker/native-subscription closure. Native backend initialization is guarded against during these tests. These results establish Python adapter behavior only.

## Native and hardware coverage

This inspection does not assess native dispatch or hardware behavior. See the native bridge tests and codec fixtures for their narrower, independent evidence. No hardware coverage is inferred from a present attribute or a passing injected-backend test.

## Missing public classes

`AlertStream`, `DeviceDiagnosticsResource`, `EffortLimitResource`, `EmfPosesResource`, `FirmwareVersionResource`, `HandJointAnglesResource`, `HandModelPathResource`, `HandSideResource`, `HandSkeletonResource`, `ImuDataIndexResource`, `ImuDataMiddleResource`, `ImuDataPalmResource`, `ImuDataPinkyResource`, `ImuDataRingResource`, `ImuDataThumbResource`, `ImuIndexResource`, `ImuMiddleResource`, `ImuPalmResource`, `ImuPinkyResource`, `ImuRingResource`, `ImuThumbResource`, `InputVoltageResource`, `MetricsStream`, `MitParamsResource`, `OfflineHandJointAnglesResource`, `OfflineHandSkeletonResource`, `OfflineImuDataResource`, `OfflineTipPosesResource`, `PortResource`, `ProductSnResource`, `RealtimeController`, `RecordingHandle`, `RetargetSession`, `SnResource`, `StatusStream`, `TactileBinaryResource`, `TactileDeviceInfoResource`, `TactileDiagnosticsResource`, `TactileHandednessResource`, `TactilePointCloudResource`, `TactilePressureFrameResource`, `TactileResidualResource`, `TactileResource`, `TactileStatusResource`, `TactileZonesResource`, `TemperatureResource`, `TfResource`, `TfStaticResource`, `TipPosesResource`, `TopicRecorder`, `VersionResource`, `WujiGlove`, `WujiGloveOfflinePipeline`, `WujiHand`, `WujiHandFingerHandle`, `WujiHandJointHandle`, `WujiHandJointStatesResource`, `WujiTactileGlove`

## Missing methods on present classes

`SdkManager.export_user_data`, `SdkManager.import_user_data`, `SdkManager.preview_user_data`, `SdkManager.tf`, `SdkManager.tf_static`

## Contract limitations

Attribute existence is weaker than compatibility. The report retains actual and stub signatures without claiming they match, and does not call constructors, properties, native methods, or telemetry transformations. Type-only names and methods with explicit unsupported behavior remain visible as such in the value-model coverage report. `T` is an upstream typing helper, not a device operation.

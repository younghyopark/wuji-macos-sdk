"""Offline Hand 2 API contracts; never execute an example's device or motion code.

The scripted backend is injected explicitly and is defined only in this test.
To refresh the inspection report without native initialization, run:
    PYTHONPATH=src python tests/test_example_contract.py --write-report
"""
from __future__ import annotations

import ast
import asyncio
import collections
import copy
import gc
import hashlib
import inspect
import json
from pathlib import Path
import sys
import threading
import time
import unittest
import weakref
from unittest.mock import patch

import wuji_sdk as sdk


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples/python/wuji_hand_2"
FRAME = {
    "header": {"seq": 27, "timestamp_us": 1234567, "frame_id": "fixture_hand"},
    "num_joints": 2,
    "joints": [
        {"nid": 1, "position": 0.125, "velocity": 0.25, "effort": 0.5},
        {"nid": 18, "position": -0.125, "velocity": -0.25, "effort": -0.5},
    ],
}


def example_imports(path):
    """Keep only SDK imports and inspect names; do not evaluate other AST nodes."""
    tree = ast.parse(path.read_text(), filename=str(path))
    nodes, required, aliases = [], set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "wuji_sdk":
            if any(alias.name == "*" for alias in node.names):
                raise AssertionError(f"Unbounded import in {path}")
            nodes.append(node)
            required.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imports = [alias for alias in node.names if alias.name == "wuji_sdk"]
            if imports:
                nodes.append(ast.Import(names=imports))
                aliases.update(alias.asname or alias.name for alias in imports)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            required.add(node.attr)
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    return module, sorted(required)


class _ScriptedBackend:
    """A fail-closed call recorder with finite, explicitly supplied frames."""

    def __init__(self, frames=()):
        self.calls = []
        self.frames = collections.deque(copy.deepcopy(frames))
        self._lock = threading.Lock()

    def call(self, scope, handle, operation, **arguments):
        with self._lock:
            self.calls.append((scope, handle, operation, copy.deepcopy(arguments)))
            key = (scope, operation)
            if key == ("manager", "open"):
                return "fixture-manager"
            if key == ("manager", "scan"):
                return [{"sn": "EXPLICIT_TEST_FIXTURE", "address": "fixture://hand",
                         "device_type": 2, "transport_type": "Udp"}]
            if key == ("manager", "connect"):
                return {"handle": "fixture-hand", "metadata": {
                    "device_name": arguments["device_name"], "serial_number": arguments["sn"],
                    "device_type": 2,
                }}
            if key == ("manager", "disconnect_all"):
                return None
            if key == ("device", "resource"):
                resource, action = arguments["resource"], arguments["action"]
                if (resource, action) == ("joint_states", "subscribe"):
                    return {"handle": "fixture-sub", "dtype": "JointStateFrame", "stream_id": 7}
                if (resource, action) == ("joint_command", "publish"):
                    return {"handle": "fixture-pub", "dtype": "JointCommandFrame"}
                if (resource, action) == ("online_joints_count", "get"):
                    return 2
            if key == ("subscription", "recv"):
                item = self.frames.popleft() if self.frames else None
                if isinstance(item, Exception):
                    raise item
                return item
            if key == ("subscription", "set_rate"):
                return arguments["frequency_hz"]
            if key in (("subscription", "close"), ("publisher", "send"), ("publisher", "close")):
                return None
            raise AssertionError(f"Unexpected test-backend call: {key} {arguments}")

    def operations(self, scope, operation):
        with self._lock:
            return [call for call in self.calls if call[0] == scope and call[2] == operation]


class ExampleImports(unittest.TestCase):
    def test_all_original_hand2_sdk_imports_resolve_without_initializing_native(self):
        paths = sorted(EXAMPLES.rglob("*.py"))
        self.assertEqual(len(paths), 8)
        with patch("wuji_sdk._backend.default_backend", side_effect=AssertionError("Native initialization forbidden")):
            for path in paths:
                with self.subTest(example=path.relative_to(EXAMPLES)):
                    module, names = example_imports(path)
                    exec(compile(module, str(path), "exec"), {})
                    self.assertTrue(names)
                    for name in names:
                        self.assertIn(name, sdk.__all__)
                        self.assertTrue(hasattr(sdk, name), name)


class InjectedBackendLifecycle(unittest.TestCase):
    def setUp(self):
        guard = patch("wuji_sdk._backend.default_backend", side_effect=AssertionError("Native initialization forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.backend = _ScriptedBackend()
        self.manager = sdk.SdkManager(self.backend)
        self.addCleanup(self.manager.disconnect_all)
        discovered = self.manager.scan()
        self.assertEqual(discovered[0].device_type, sdk.DeviceType.WujiHand2)
        self.hand = self.manager.connect(sn=discovered[0].sn, device_name="fixture_hand")
        self.assertIsInstance(self.hand, sdk.WujiHand2)

    def test_callback_materializes_sparse_frame_and_closes_once(self):
        self.backend.frames.append(copy.deepcopy(FRAME))
        received, arrived = [], threading.Event()

        def callback(frame):
            received.append(frame)
            arrived.set()

        sub = self.hand.joint_states().subscribe_with_callback(callback)
        self.addCleanup(sub.close)
        self.assertTrue(arrived.wait(1), "Callback did not receive fixture")
        sub.close()
        sub.close()
        self.assertFalse(sub.is_active)
        self.assertEqual(sub.received_count, 1)
        self.assertEqual(sub.error_count, 0)
        self.assertIsInstance(received[0], sdk.JointStateFrame)
        self.assertEqual([joint.nid for joint in received[0].joints], [1, 18])
        self.assertEqual(len(self.backend.operations("subscription", "close")), 1)

    def test_dropping_idle_callback_handle_stops_worker_and_closes_subscription(self):
        sub = self.hand.joint_states().subscribe_with_callback(lambda frame: None)
        worker = sub._thread
        reference = weakref.ref(sub)
        # A strong cleanup reference would hide the very leak being checked.
        def cleanup():
            alive = reference()
            if alive is not None:
                alive.close()
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 1
        while not self.backend.operations("subscription", "recv") and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.backend.operations("subscription", "recv"))
        del sub
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            gc.collect()
            if reference() is None and not worker.is_alive():
                break
            time.sleep(0.005)
        self.assertIsNone(reference(), "Callback worker kept its public owner alive")
        self.assertFalse(worker.is_alive(), "Dropped callback handle left a running worker")
        self.assertEqual(len(self.backend.operations("subscription", "close")), 1)

    def test_callback_error_is_reported_and_next_frame_still_arrives(self):
        self.backend.frames.extend([copy.deepcopy(FRAME), copy.deepcopy(FRAME)])
        errors, arrived = [], threading.Event()
        count = 0

        def callback(frame):
            nonlocal count
            count += 1
            if count == 1:
                raise ValueError("deliberate callback failure")
            arrived.set()

        sub = self.hand.joint_states().subscribe_with_callback(callback, errors.append)
        self.addCleanup(sub.close)
        self.assertTrue(arrived.wait(1), "Stream stopped after callback exception")
        sub.close()
        self.assertEqual(errors, ["deliberate callback failure"])
        self.assertEqual(sub.received_count, 2)
        self.assertEqual(sub.error_count, 1)

    def test_stream_error_reaches_callback_error_handler_and_closes(self):
        self.backend.frames.append(sdk.WujiException("fixture stream failed"))
        errors, failed = [], threading.Event()

        def on_error(error):
            errors.append(error)
            failed.set()

        sub = self.hand.joint_states().subscribe_with_callback(lambda frame: None, on_error)
        self.addCleanup(sub.close)
        self.assertTrue(failed.wait(1))
        sub.close()
        self.assertEqual(errors, ["fixture stream failed"])
        self.assertEqual(sub.error_count, 1)
        self.assertFalse(sub.is_active)
        self.assertEqual(len(self.backend.operations("subscription", "close")), 1)

    def test_stream_lag_is_reported_and_delivery_continues(self):
        self.backend.frames.extend([sdk.StreamLaggedError(3), copy.deepcopy(FRAME)])
        errors, arrived = [], threading.Event()

        def callback(frame):
            arrived.set()

        sub = self.hand.joint_states().subscribe_with_callback(callback, errors.append)
        self.addCleanup(sub.close)
        self.assertTrue(arrived.wait(1), "Stream stopped after a lag")
        self.assertTrue(sub.is_active)
        sub.close()
        self.assertEqual(errors, ["Stream lagged by 3 frame(s)"])
        self.assertEqual((sub.received_count, sub.error_count), (1, 1))
        self.assertEqual(len(self.backend.operations("subscription", "close")), 1)

    def test_async_receive_skips_empty_polls_and_keeps_stream_rate_result(self):
        self.backend.frames.extend([None, copy.deepcopy(FRAME)])
        sub = self.hand.joint_states().subscribe()
        self.addCleanup(sub.close)
        self.assertEqual(sub.set_rate(200), 200)

        async def receive():
            return await asyncio.wait_for(sub.recv_async(), timeout=1)

        frame = asyncio.run(receive())
        self.assertIsInstance(frame, sdk.JointStateFrame)
        self.assertEqual(frame.header.seq, 27)
        sub.close()
        with self.assertRaises(sdk.WujiException):
            sub.recv()

    def test_async_receive_can_be_cancelled_without_fabricating_a_frame(self):
        sub = self.hand.joint_states().subscribe()
        self.addCleanup(sub.close)

        async def cancel_receive():
            task = asyncio.create_task(sub.recv_async())
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(cancel_receive())
        self.assertTrue(self.backend.operations("subscription", "recv"))
        self.assertFalse(self.backend.operations("subscription", "close"))
        sub.close()

    def test_typed_publisher_validates_before_send_and_closes_once(self):
        publisher = self.hand.joint_command().publish()
        self.addCleanup(publisher.close)
        commands = [sdk.JointCommand(i / 32, -i / 64, 0.25) for i in range(20)]
        with self.assertRaises(ValueError):
            publisher.send(commands[:19])
        with self.assertRaises(TypeError):
            publisher.send([{}] * 20)
        bad = commands[:]
        bad[0] = sdk.JointCommand(float("nan"), 0, 0)
        with self.assertRaises(ValueError):
            publisher.send(bad)
        self.assertFalse(self.backend.operations("publisher", "send"))
        publisher.send(commands)
        asyncio.run(publisher.send_async(commands))
        sent = self.backend.operations("publisher", "send")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][3]["data"][19], {"position": 19 / 32, "velocity": -19 / 64, "effort": 0.25})
        publisher.close()
        publisher.close()
        self.assertEqual(len(self.backend.operations("publisher", "close")), 1)
        with self.assertRaises(sdk.WujiException):
            publisher.send(commands)


def _member(obj, name):
    """Inspect descriptors without calling properties or constructing native objects."""
    for owner in obj.__mro__:
        if name in vars(owner):
            value = vars(owner)[name]
            underlying = value.__func__ if isinstance(value, (staticmethod, classmethod)) else value
            try:
                signature = str(inspect.signature(underlying))
            except (TypeError, ValueError):
                signature = None
            return {"present": True, "owner": f"{owner.__module__}.{owner.__name__}",
                    "descriptor": type(value).__name__, "signature": signature,
                    "is_async": inspect.iscoroutinefunction(underlying),
                    "inherited_builtin_default": owner in (object, dict, list, tuple, int, str, float)}
    return {"present": False}


def write_runtime_coverage():
    contract = json.loads((ROOT / "research/api-coverage.json").read_text())
    source_paths = sorted((ROOT / "src/wuji_sdk").glob("*.py"))
    init_assignments = {}
    for path in source_paths:
        for cls in ast.walk(ast.parse(path.read_text())):
            if not isinstance(cls, ast.ClassDef):
                continue
            names = set()
            for node in ast.walk(cls):
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name) and node.value.id == "self":
                    names.add(node.attr)
            init_assignments[cls.name] = names
    classes = {}
    for name, expected in contract["classes"].items():
        obj = vars(sdk).get(name)
        present = isinstance(obj, type)
        item = {"present": present, "exported": name in sdk.__all__,
                "contract_category": expected["implementation_category"],
                "native_validation": "not_assessed_by_this_report", "hardware_verified": False,
                "methods": {}, "attributes": {}}
        if present:
            item["runtime_module"] = obj.__module__
        for method, overloads in expected["methods"].items():
            result = _member(obj, method) if present else {"present": False}
            result["stub_signatures"] = [entry["signature"] for entry in overloads]
            result["contract_behavior_verified"] = False
            item["methods"][method] = result
        for attr in expected["attributes"] | expected["properties"]:
            result = _member(obj, attr) if present else {"present": False}
            if present and not result["present"]:
                owners = [owner.__name__ for owner in obj.__mro__ if attr in init_assignments.get(owner.__name__, ())]
                if owners:
                    result.update({"present": None, "evidence": "source_assigns_instance_attribute", "owners": owners})
                elif attr in getattr(obj, "__annotations__", {}):
                    result.update({"present": None, "evidence": "type_annotation_only"})
            item["attributes"][attr] = result
        classes[name] = item
    functions = {name: {"present": callable(vars(sdk).get(name)), "exported": name in sdk.__all__}
                 for name in contract["top_level_functions"]}
    aliases = {name: {"present": name in vars(sdk), "exported": name in sdk.__all__}
               for name in contract["aliases"]}
    examples = {}
    for path in sorted(EXAMPLES.rglob("*.py")):
        _, required = example_imports(path)
        examples[str(path.relative_to(ROOT))] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "required_sdk_names": required, "missing_sdk_names": [name for name in required if name not in vars(sdk)],
            "executed": False, "hardware_verified": False}
    missing_classes = [name for name, value in classes.items() if not value["present"]]
    missing_methods = [f"{name}.{method}" for name, value in classes.items() if value["present"]
                       for method, member in value["methods"].items() if not member["present"]]
    all_methods = [member for cls in classes.values() for member in cls["methods"].values()]
    report = {
        "scope": "Static inspection of imported Python attributes and source assignments, without native initialization. Presence is not signature equivalence or functioning hardware support.",
        "stub_sha256": contract["stub_sha256"],
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths},
        "reproduce": "PYTHONPATH=src .venv/bin/python tests/test_example_contract.py --write-report",
        "counts": {"required_classes": len(classes), "present_classes": len(classes) - len(missing_classes),
                   "required_method_names_including_dunders": len(all_methods),
                   "present_method_names_including_inherited_defaults": sum(member["present"] for member in all_methods),
                   "missing_methods_on_present_classes": len(missing_methods)},
        "native_coverage": {"assessed": False, "hardware_verified": False,
                            "evidence_location": ["README.md", "research/api-coverage.md", "tests/test_manager_native.py", "tests/test_codec.py"],
                            "note": "Native operation dispatch and byte fixtures require independent evidence; injected-backend tests do not establish either."},
        "offline_lifecycle_test_scope": ["explicit injected backend only", "callback delivery and error propagation", "async empty poll and cancellation", "publisher validation, async send and idempotent close"],
        "missing_classes": missing_classes, "missing_methods_on_present_classes": missing_methods,
        "classes": classes, "top_level_functions": functions, "aliases": aliases, "hand2_examples": examples,
    }
    (ROOT / "research/api-runtime-coverage.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    lines = ["# Python runtime API inspection", "", report["scope"], "",
             f"Pinned stub SHA-256: `{report['stub_sha256']}`. Reproduce with `{report['reproduce']}`.", "",
             f"The imported package exposes {report['counts']['present_classes']} of {len(classes)} upstream class names. "
             f"There are {len(missing_methods)} missing declared methods on the present classes. "
             "The JSON report lists every class, method, signature, field, and source hash; it also distinguishes inherited builtin defaults and source-only instance assignments.", "",
             "## Hand 2 example import contract", "", "Only SDK import AST nodes are executed by the test. Example main functions, motion code, and other imports are never executed.", "",
             "| Original example | Missing SDK imports |", "| --- | --- |"]
    for path, entry in examples.items():
        lines.append(f"| `{path}` | {', '.join('`' + name + '`' for name in entry['missing_sdk_names']) or 'None'} |")
    lines += ["", "## Offline lifecycle validation", "",
              "`tests/test_example_contract.py` explicitly constructs a manager with a test-only backend that rejects unexpected calls. It checks callback delivery, callback and stream errors, sparse telemetry IDs, async receive and cancellation, publisher validation, async send, idempotent stream closure, and garbage collection of an idle callback handle with worker/native-subscription closure. Native backend initialization is guarded against during these tests. These results establish Python adapter behavior only.", "",
              "## Native and hardware coverage", "",
              "This inspection does not assess native dispatch or hardware behavior. See the native bridge tests and codec fixtures for their narrower, independent evidence. No hardware coverage is inferred from a present attribute or a passing injected-backend test.", "",
              "## Missing public classes", "", ", ".join(f"`{name}`" for name in missing_classes) or "None.", "",
              "## Missing methods on present classes", "", ", ".join(f"`{name}`" for name in missing_methods) or "None.", "",
              "## Contract limitations", "",
              "Attribute existence is weaker than compatibility. The report retains actual and stub signatures without claiming they match, and does not call constructors, properties, native methods, or telemetry transformations. Type-only names and methods with explicit unsupported behavior remain visible as such in the value-model coverage report. `T` is an upstream typing helper, not a device operation.", ""]
    (ROOT / "research/api-runtime-coverage.md").write_text("\n".join(lines))
    print(json.dumps(report["counts"], sort_keys=True))


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-report"]:
        write_runtime_coverage()
    else:
        unittest.main()

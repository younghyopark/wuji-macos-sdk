"""Offline tests of the pinned Python value contract; no manager or hardware."""
import inspect
import unittest
from unittest.mock import patch

from wuji_sdk import _types as types


class PublicConstructors(unittest.TestCase):
    def test_joint_command_signature_and_payload(self):
        signature = inspect.signature(types.JointCommand)
        self.assertEqual(list(signature.parameters), ["position", "velocity", "effort"])
        self.assertTrue(all(p.default is inspect.Parameter.empty for p in signature.parameters.values()))
        command = types.JointCommand(0.125, velocity=2, effort=-0.25)
        self.assertEqual(types._to_native(command), {
            "position": 0.125, "velocity": 2.0, "effort": -0.25,
        })
        with self.assertRaises(TypeError):
            types.JointCommand(0, 0)
        with self.assertRaises(TypeError):
            types.JointCommand("0.1", 0, 0)

    def test_connection_defaults_and_time_sync_contract(self):
        options = types.ConnectOptions()
        self.assertEqual(types._to_native(options), {
            "timeout_ms": 1000, "retry_count": 3, "enable_bridge": True,
            "auto_time_sync_interval_ms": 30000,
        })
        self.assertIsNone(types.ConnectOptions(auto_time_sync_interval_ms=None).auto_time_sync_interval_ms)
        self.assertEqual(types.ConnectOptions(auto_time_sync_interval_ms=100).auto_time_sync_interval_ms, 100)
        with self.assertRaises(ValueError):
            types.ConnectOptions(auto_time_sync_interval_ms=99)
        with self.assertRaises(TypeError):
            types.ConnectOptions(timeout_ms=1.2)

    def test_schema_constructors_from_upstream_offline_example(self):
        header = types.FrameHeader(seq=1, timestamp_us=0)
        self.assertEqual(header.frame_id, "")
        pose = types.Pose([0.05, 0, 0], types.Quaternion(0, 0, 0, 1))
        frame = types.EmfPoseArray(header, [types.EmfPose(pose, 1.0) for _ in range(5)])
        self.assertEqual(len(frame), 5)
        self.assertIsInstance(frame.poses[0].pose.orientation, types.Quaternion)
        self.assertEqual(types.MitParam(3, 0.05).kp, 3.0)
        self.assertEqual(types.HwVersion(2, 0, 0).major, 2)

    def test_raw_imu_marks_orientation_unavailable(self):
        frame = types.ImuData.raw(
            types.FrameHeader(1, 100), types.Vector3F64(0, 0, 0), types.Vector3F64(0, 0, 9.8),
        )
        self.assertEqual(frame.orientation_covariance[0], -1.0)
        self.assertEqual(len(frame.orientation_covariance), 9)
        self.assertEqual(frame.linear_acceleration.z, 9.8)


class AnnotationResolution(unittest.TestCase):
    def setUp(self):
        types._cached_annotations.cache_clear()
        self.addCleanup(types._cached_annotations.cache_clear)

    def test_repeated_commands_resolve_once_without_relaxing_coercion(self):
        with patch.object(types, "get_type_hints", wraps=types.get_type_hints) as resolve:
            commands = [types.JointCommand(index, 0, -index) for index in range(20)]
            self.assertEqual(resolve.call_count, 1)
            self.assertTrue(all(type(command.position) is float for command in commands))
            self.assertEqual(commands[-1].effort, -19.0)
            with self.assertRaises(TypeError):
                types.JointCommand("0.1", 0, 0)
            self.assertEqual(resolve.call_count, 1)

    def test_unresolved_forward_reference_is_retried(self):
        # CalibrationResult is absent from the pinned upstream stub. Preserve
        # its raw payload until the reference is defined, then enforce it.
        raw = types._from_native(types.CalibrationOutcome, {"status": "ok", "result": "raw"})
        self.assertEqual(raw.result, "raw")
        with patch.object(types, "CalibrationResult", float, create=True):
            resolved = types._from_native(types.CalibrationOutcome, {"status": "ok", "result": 2})
            self.assertIs(type(resolved.result), float)
            self.assertEqual(resolved.result, 2.0)
            with self.assertRaises(TypeError):
                types._from_native(types.CalibrationOutcome, {"status": "ok", "result": "raw"})

    def test_user_defined_annotations_remain_dynamic(self):
        class DynamicRecord(types._Value):
            __slots__ = ("value",)
            __annotations__ = {"value": float}

        value = types._from_native(DynamicRecord, {"value": 2})
        self.assertIs(type(value.value), float)
        DynamicRecord.__annotations__["value"] = int
        value = types._from_native(DynamicRecord, {"value": 2})
        self.assertIs(type(value.value), int)
        with self.assertRaises(TypeError):
            types._from_native(DynamicRecord, {"value": 2.5})


class NativeRecordMaterialization(unittest.TestCase):
    def test_joint_frame_preserves_online_joint_ids_and_measurements(self):
        data = {
            "header": {"seq": 27, "timestamp_us": 1234567, "frame_id": "right_hand"},
            "num_joints": 2,
            "joints": [
                {"nid": 1, "position": 0.125, "velocity": 0.25, "effort": 0.5},
                {"nid": 18, "position": -0.125, "velocity": -0.25, "effort": -0.5},
            ],
        }
        frame = types._from_native("JointStateFrame", data)
        self.assertIsInstance(frame.header, types.FrameHeader)
        self.assertIsInstance(frame.joints[0], types.JointStateEntry)
        self.assertEqual([entry.nid for entry in frame.joints], [1, 18])
        self.assertEqual(len(frame), 2)
        self.assertEqual(types._to_native(frame), data)

    def test_missing_measurements_are_rejected(self):
        with self.assertRaisesRegex(TypeError, "missing=.*effort"):
            types._from_native(types.JointStateEntry, {"nid": 0, "position": 0.0, "velocity": 0.0})
        with self.assertRaises(TypeError):
            types.JointStateFrame()

    def test_fingertip_payload_stays_raw_and_self_describing(self):
        payload = bytes([0, 127, 128, 255])
        frame = types._from_native(types.FingertipSensorData, {
            "header": {"seq": 1, "timestamp_us": 3, "frame_id": "thumb"},
            "info_digest": 123, "data": payload,
        })
        self.assertEqual(bytes(frame.data), payload)
        self.assertEqual(len(frame), 4)
        self.assertEqual(frame.info_digest, 123)

    def test_status_and_discovery_metadata(self):
        status = types._from_native(types.StatusWord, {
            "ext_state": 2, "position_limit_active": True,
            "velocity_limit_active": False, "current_limit_active": False,
        })
        self.assertEqual(status.ext_state_name, "Enabled")
        discovered = types._from_native(types.DiscoveredDevice, {
            "sn": "SYNTHETIC_TEST", "address": "127.0.0.1:50001",
            "transport_type": "Udp", "device_type": 2,
        })
        self.assertEqual(discovered.device_type, types.DeviceType.WujiHand2)
        self.assertEqual(discovered.transport_type, types.TransportType.Udp)
        resource = types._from_native("Resource", {
            "path": "joint_command", "can_get": False, "can_set": False,
            "can_sub": False, "can_pub": True, "can_exec": False,
        })
        self.assertEqual(resource.path, "joint_command")
        self.assertTrue(resource.can_pub)

    def test_typed_dictionary_and_exception_contract(self):
        self.assertEqual(types._from_native(types.ErrorInfo, {"code": 0x1234}), {"code": 0x1234})
        self.assertTrue(issubclass(types.NodeOfflineError, types.WujiException))
        self.assertIn("device", str(types.WujiException("device unavailable")))

    def test_integer_compatible_enums(self):
        self.assertEqual(types.ControlMode.Mit, 1)
        self.assertEqual(types.ControlMode.Current, 4)
        self.assertEqual(types.TactileType.Thumb, 1)
        self.assertEqual(types.TactileState.Calibrating, 1)
        self.assertEqual(types._to_native(types.HandModel.WujiHand2), 1)

    def test_point_count_uses_byte_stride(self):
        cloud = types._from_native(types.PointCloud, {
            "header": {"seq": 1, "timestamp_us": 1, "frame_id": ""},
            "frame_id": "wrist", "point_stride": 16, "fields": [], "data": [0] * 48,
        })
        self.assertEqual(cloud.point_count(), 3)
        with self.assertRaises(NotImplementedError):
            len(cloud)


if __name__ == "__main__":
    unittest.main()

"""RealSense driver contract tests; no camera or RealSense SDK is required."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest


@pytest.fixture
def driver(monkeypatch):
    sdk = ModuleType("pyrealsense2")
    sdk.camera_info = SimpleNamespace(name="name", serial_number="serial", firmware_version="firmware")
    sdk.stream = SimpleNamespace(color="color", depth="depth")
    sdk.format = SimpleNamespace(rgb8="rgb8", z16="z16")
    sdk.context = Mock()
    sdk.pipeline = Mock()
    sdk.config = Mock()
    sdk.context.return_value.query_devices.return_value = [_device("123")]
    monkeypatch.setitem(sys.modules, "pyrealsense2", sdk)

    # Load an isolated module so a fake SDK cannot leak into other tests/imports.
    path = Path(__file__).parents[1] / "camera" / "drivers" / "realsense.py"
    spec = importlib.util.spec_from_file_location("_test_realsense_driver", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, sdk


def _device(serial):
    values = {"name": "Test RealSense", "serial": serial, "firmware": "test"}
    return SimpleNamespace(get_info=values.__getitem__)


def _frames(color, depth=None):
    frames = Mock()
    frames.get_color_frame.return_value = None if color is None else Mock(get_data=Mock(return_value=color))
    frames.get_depth_frame.return_value = None if depth is None else Mock(get_data=Mock(return_value=depth))
    return frames


def test_single_camera_collects_rgb_without_requesting_depth(driver):
    module, sdk = driver
    sensor = module.RealSenseSensor()
    sdk.config.return_value.enable_device.assert_called_once_with("123")
    sdk.config.return_value.enable_stream.assert_called_once_with("color", 640, 480, "rgb8", 30)

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    frames = _frames(rgb)
    sdk.pipeline.return_value.wait_for_frames.return_value = frames
    result = sensor.read()

    assert set(result["images"]) == {"ego_view"}
    assert set(result["timestamps"]) == {"ego_view"}
    np.testing.assert_array_equal(result["images"]["ego_view"], rgb)
    frames.get_depth_frame.assert_not_called()


@pytest.mark.parametrize("selection", [{"device_id": "200"}, {"id": 1}])
def test_selects_requested_camera_with_unsorted_devices(driver, selection):
    module, sdk = driver
    sdk.context.return_value.query_devices.return_value = [_device("200"), _device("100")]
    module.RealSenseSensor(**selection)
    sdk.config.return_value.enable_device.assert_called_once_with("200")


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        ({}, "Multiple RealSense devices"),
        ({"device_id": "missing"}, "serial 'missing' not found"),
        ({"id": -1}, "Invalid RealSense device index"),
        ({"id": 2}, "Invalid RealSense device index"),
    ],
)
def test_invalid_or_ambiguous_selection_reports_available_serials(driver, selection, message):
    module, sdk = driver
    sdk.context.return_value.query_devices.return_value = [_device("200"), _device("100")]
    with pytest.raises(ValueError, match=message) as error:
        module.RealSenseSensor(**selection)
    assert "100, 200" in str(error.value)
    sdk.pipeline.assert_not_called()


def test_rejects_conflicting_serial_and_index(driver):
    module, sdk = driver
    with pytest.raises(ValueError, match="not both"):
        module.RealSenseSensor(device_id="123", id=0)
    sdk.pipeline.assert_not_called()


def test_no_devices_has_clear_error(driver):
    module, sdk = driver
    sdk.context.return_value.query_devices.return_value = []
    with pytest.raises(RuntimeError, match="No RealSense devices found"):
        module.RealSenseSensor()
    sdk.pipeline.assert_not_called()


def test_optional_depth_preserves_uint16_data_and_custom_stream_settings(driver):
    module, sdk = driver
    config = module.RealSenseConfig(
        color_image_dim=(320, 240), depth_image_dim=(640, 480), fps=15, enable_depth=True
    )
    sensor = module.RealSenseSensor(config=config, mount_position="left_wrist")
    sdk.config.return_value.enable_stream.assert_any_call("color", 320, 240, "rgb8", 15)
    sdk.config.return_value.enable_stream.assert_any_call("depth", 640, 480, "z16", 15)
    assert sdk.config.return_value.enable_stream.call_count == 2

    rgb = np.zeros((240, 320, 3), dtype=np.uint8)
    depth = np.full((480, 640), 1200, dtype=np.uint16)
    sdk.pipeline.return_value.wait_for_frames.return_value = _frames(rgb, depth)
    result = sensor.read()

    assert set(result["images"]) == {"left_wrist", "left_wrist_depth"}
    assert result["timestamps"]["left_wrist"] == result["timestamps"]["left_wrist_depth"]
    assert result["images"]["left_wrist_depth"].dtype == np.uint16
    np.testing.assert_array_equal(result["images"]["left_wrist_depth"], depth)


@pytest.mark.parametrize(
    ("enable_depth", "color", "depth"),
    [
        (False, None, None),
        (False, np.empty((0, 0, 3), dtype=np.uint8), None),
        (True, np.zeros((2, 2, 3), dtype=np.uint8), None),
        (True, np.zeros((2, 2, 3), dtype=np.uint8), np.empty((0, 0), dtype=np.uint16)),
    ],
)
def test_missing_or_empty_required_frames_are_dropped(driver, enable_depth, color, depth):
    module, sdk = driver
    sensor = module.RealSenseSensor(config=module.RealSenseConfig(enable_depth=enable_depth))
    sdk.pipeline.return_value.wait_for_frames.return_value = _frames(color, depth)
    assert sensor.read() is None


def test_default_configs_are_independent(driver):
    module, _ = driver
    first = module.RealSenseSensor()
    second = module.RealSenseSensor()
    first._realsense_config.enable_depth = True
    assert second._realsense_config.enable_depth is False


def test_depth_observation_space_matches_sensor_data(driver, monkeypatch):
    module, _ = driver
    # Gym is optional on the camera server.
    monkeypatch.setattr(module, "gym", pytest.importorskip("gymnasium"))
    rgb_sensor = module.RealSenseSensor()
    assert set(rgb_sensor.observation_space()) == {"color_image"}

    sensor = module.RealSenseSensor(config=module.RealSenseConfig(enable_depth=True))
    depth_space = sensor.observation_space()["depth_image"]
    assert depth_space.shape == (480, 640)
    assert depth_space.dtype == np.uint16
    assert depth_space.contains(np.full((480, 640), 65535, dtype=np.uint16))


def test_pipeline_start_failure_chains_cause_and_attempts_cleanup(driver):
    module, sdk = driver
    original = RuntimeError("USB disconnected")
    sdk.pipeline.return_value.start.side_effect = original
    sdk.pipeline.return_value.stop.side_effect = RuntimeError("Pipeline is not running")
    with pytest.raises(RuntimeError, match="Failed to start RealSense sensor 123") as error:
        module.RealSenseSensor()
    assert error.value.__cause__ is original
    sdk.pipeline.return_value.stop.assert_called_once()


def test_server_start_failure_stops_pipeline(driver, monkeypatch):
    module, sdk = driver
    original = OSError("Address already in use")
    monkeypatch.setattr(module.RealSenseSensor, "start_server", Mock(side_effect=original))
    stop_server = Mock()
    monkeypatch.setattr(module.RealSenseSensor, "stop_server", stop_server)
    with pytest.raises(RuntimeError) as error:
        module.RealSenseSensor(run_as_server=True)
    assert error.value.__cause__ is original
    stop_server.assert_called_once()
    sdk.pipeline.return_value.stop.assert_called_once()


def test_close_stops_pipeline_even_if_server_cleanup_fails(driver, monkeypatch):
    module, sdk = driver
    monkeypatch.setattr(module.RealSenseSensor, "start_server", Mock())
    monkeypatch.setattr(
        module.RealSenseSensor, "stop_server", Mock(side_effect=RuntimeError("Server already stopped"))
    )
    sensor = module.RealSenseSensor(run_as_server=True)
    with pytest.raises(RuntimeError, match="Server already stopped"):
        sensor.close()
    sensor.close()
    sdk.pipeline.return_value.stop.assert_called_once()


def test_standalone_server_publishes_flat_image_message(driver, monkeypatch):
    module, _ = driver
    monkeypatch.setattr(module.RealSenseSensor, "start_server", Mock())
    sensor = module.RealSenseSensor(run_as_server=True)
    frame = {"timestamps": {"ego_view": 1.0}, "images": {"ego_view": np.zeros((2, 2, 3), dtype=np.uint8)}}
    sensor.read = Mock(side_effect=[None, frame, KeyboardInterrupt])
    sensor.send_message = Mock()
    with pytest.raises(KeyboardInterrupt):
        sensor.run_server()
    sensor.send_message.assert_called_once_with(sensor.serialize(frame))

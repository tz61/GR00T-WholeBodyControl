import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
import tyro

from gear_sonic.camera.composed_camera import ComposedCameraConfig, ComposedCameraSensor


@pytest.mark.parametrize(
    "device_ids",
    [(None, None), ("123", None), ("123", "123")],
)
def test_multiple_realsense_cameras_require_unique_serials(device_ids):
    with pytest.raises(ValueError, match="RealSense"):
        ComposedCameraConfig(
            ego_view_camera="realsense",
            ego_view_device_id=device_ids[0],
            left_wrist_camera="realsense",
            left_wrist_device_id=device_ids[1],
        )


def test_realsense_cli_passes_serial_mount_and_stream_options(monkeypatch):
    config = tyro.cli(
        ComposedCameraConfig,
        args=[
            "--ego-view-camera", "realsense",
            "--ego-view-device-id", "00123",
            "--left-wrist-camera", "realsense",
            "--left-wrist-device-id", "00456",
            "--fps", "15",
            "--realsense-enable-depth",
        ],
    )
    driver = ModuleType("gear_sonic.camera.drivers.realsense")
    driver.RealSenseConfig = SimpleNamespace
    driver.RealSenseSensor = Mock()
    monkeypatch.setitem(sys.modules, driver.__name__, driver)

    # Exercise factory wiring without starting camera threads or opening sockets.
    server = ComposedCameraSensor.__new__(ComposedCameraSensor)
    server.config = config
    for mount, camera in server._get_camera_configs().items():
        server._instantiate_camera(mount, camera["camera_type"], camera["device_id"])

    calls = driver.RealSenseSensor.call_args_list
    assert len(calls) == 2
    assert [(call.kwargs["mount_position"], call.kwargs["device_id"]) for call in calls] == [
        ("ego_view", "00123"),
        ("left_wrist", "00456"),
    ]
    for call in calls:
        assert call.kwargs["config"].fps == 15
        assert call.kwargs["config"].enable_depth is True


def test_single_realsense_defaults_to_rgb_without_serial():
    config = ComposedCameraConfig(ego_view_camera="realsense")
    assert config.ego_view_device_id is None
    assert config.realsense_enable_depth is False


def test_mixed_camera_types_do_not_require_realsense_serial():
    ComposedCameraConfig(ego_view_camera="realsense", left_wrist_camera="oak")

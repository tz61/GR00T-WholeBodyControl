"""Intel RealSense camera driver.

Requires the ``pyrealsense2`` SDK — install with::

    pip install pyrealsense2

See https://github.com/IntelRealSense/librealsense for hardware-specific instructions.
"""

from contextlib import suppress
from dataclasses import dataclass
import time
from typing import Any

import numpy as np

try:
    import gymnasium as gym
except ImportError:
    gym = None  # type: ignore[assignment]

import pyrealsense2 as rs

from gear_sonic.camera.sensor import Sensor
from gear_sonic.camera.sensor_server import (
    CameraMountPosition,
    ImageMessageSchema,
    SensorServer,
)


@dataclass
class RealSenseConfig:
    """Configuration for the RealSense camera."""

    depth_image_dim: tuple[int, int] = (640, 480)
    color_image_dim: tuple[int, int] = (640, 480)
    fps: int = 30
    mount_position: str = CameraMountPosition.EGO_VIEW.value
    enable_depth: bool = False


class RealSenseSensor(Sensor, SensorServer):
    """Sensor for Intel RealSense depth cameras."""

    def __init__(
        self,
        run_as_server: bool = False,
        port: int = 5555,
        config: RealSenseConfig | None = None,
        id: int | None = None,
        mount_position: str = CameraMountPosition.EGO_VIEW.value,
        device_id: str | None = None,
    ):
        config = config if config is not None else RealSenseConfig()
        devices = sorted(
            rs.context().query_devices(), key=lambda device: device.get_info(rs.camera_info.serial_number)
        )
        if not devices:
            raise RuntimeError("No RealSense devices found")

        serials = [device.get_info(rs.camera_info.serial_number) for device in devices]
        available = ", ".join(serials)
        if device_id is not None and id is not None:
            raise ValueError("Specify either RealSense device_id (serial) or id (index), not both")
        if device_id is not None:
            if device_id not in serials:
                raise ValueError(
                    f"RealSense serial {device_id!r} not found. Available serials: {available}"
                )
            selected_serial = device_id
        elif id is not None:
            if not isinstance(id, int) or not 0 <= id < len(devices):
                raise ValueError(
                    f"Invalid RealSense device index {id!r}; expected 0 to {len(devices) - 1}. "
                    f"Available serials (sorted by index): {available}"
                )
            selected_serial = serials[id]
        elif len(devices) == 1:
            selected_serial = serials[0]
        else:
            raise ValueError(
                f"Multiple RealSense devices found. Specify device_id with a serial number. "
                f"Available serials: {available}"
            )

        for device in devices:
            print(f"Device: {device.get_info(rs.camera_info.name)}")
            print(f"    Serial number: {device.get_info(rs.camera_info.serial_number)}")
            print(f"    Firmware version: {device.get_info(rs.camera_info.firmware_version)}")

        self.pipeline = rs.pipeline()
        self.config = rs.config()
        self._realsense_config = config
        self._run_as_server = run_as_server
        self._pipeline_started = False
        self._server_started = False
        self.mount_position = mount_position

        try:
            self.config.enable_device(selected_serial)
            self.config.enable_stream(
                rs.stream.color,
                config.color_image_dim[0],
                config.color_image_dim[1],
                rs.format.rgb8,
                config.fps,
            )
            if config.enable_depth:
                self.config.enable_stream(
                    rs.stream.depth,
                    config.depth_image_dim[0],
                    config.depth_image_dim[1],
                    rs.format.z16,
                    config.fps,
                )
            # Also attempt cleanup if an SDK/server start partially succeeds and raises.
            self._pipeline_started = True
            self.pipeline.start(self.config)
            if self._run_as_server:
                self._server_started = True
                self.start_server(port)
        except Exception as e:
            with suppress(Exception):
                self.close()
            raise RuntimeError(f"Failed to start RealSense sensor {selected_serial}: {e}") from e

        print(f"Done initializing RealSense sensor: {selected_serial}")

    def read(self) -> dict[str, Any] | None:
        try:
            frames = self.pipeline.wait_for_frames()
        except Exception as e:
            print(f"ERROR! Failed to wait for frames: {e}")
            return None

        try:
            image_frames = {self.mount_position: frames.get_color_frame()}
            if self._realsense_config.enable_depth:
                image_frames[f"{self.mount_position}_depth"] = frames.get_depth_frame()
            images = {}
            for key, frame in image_frames.items():
                if not frame:
                    print(f"WARNING! No {key} frame")
                    return None
                image = np.asanyarray(frame.get_data())
                if image.size == 0:
                    print(f"WARNING! Empty {key} image")
                    return None
                images[key] = image
        except Exception as e:
            print(f"ERROR! Failed to convert frames to numpy arrays: {e}")
            return None

        current_time = time.time()
        timestamps = {key: current_time for key in images}
        return {"timestamps": timestamps, "images": images}

    def serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        serialized_msg = ImageMessageSchema(timestamps=data["timestamps"], images=data["images"])
        return serialized_msg.serialize()

    def observation_space(self):
        if gym is None:
            return None
        spaces = {
            "color_image": gym.spaces.Box(
                low=0,
                high=255,
                shape=(
                    self._realsense_config.color_image_dim[1],
                    self._realsense_config.color_image_dim[0],
                    3,
                ),
                dtype=np.uint8,
            ),
        }
        if self._realsense_config.enable_depth:
            spaces["depth_image"] = gym.spaces.Box(
                low=0,
                high=np.iinfo(np.uint16).max,
                shape=(
                    self._realsense_config.depth_image_dim[1],
                    self._realsense_config.depth_image_dim[0],
                ),
                dtype=np.uint16,
            )
        return gym.spaces.Dict(spaces)

    def close(self):
        try:
            if self._server_started:
                self._server_started = False
                self.stop_server()
        finally:
            if self._pipeline_started:
                self._pipeline_started = False
                self.pipeline.stop()

    def run_server(self):
        if not self._run_as_server:
            raise ValueError("run_as_server must be True to call run_server()")
        while True:
            read_result = self.read()
            if read_result is None:
                continue
            self.send_message(self.serialize(read_result))

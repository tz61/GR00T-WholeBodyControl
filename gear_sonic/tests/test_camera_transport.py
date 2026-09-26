import cv2
import msgpack
import numpy as np
import pytest

from gear_sonic.camera.sensor_server import ImageMessageSchema
from gear_sonic.scripts.run_camera_viewer import image_to_bgr


def roundtrip(images):
    timestamps = {name: 123.5 for name in images}
    message = ImageMessageSchema(timestamps=timestamps, images=images)
    packed = msgpack.packb(message.serialize(), use_bin_type=True)
    decoded = ImageMessageSchema.deserialize(msgpack.unpackb(packed))
    assert decoded.timestamps == timestamps
    return decoded.images


def test_depth_and_legacy_rgb_roundtrip():
    depth = np.arange(65536, dtype=np.uint16).reshape(256, 256)
    rgb = np.full((16, 24, 3), [240, 80, 20], dtype=np.uint8)

    decoded = roundtrip({"ego_view": rgb, "ego_view_depth": depth})

    assert decoded["ego_view_depth"].dtype == np.uint16
    np.testing.assert_array_equal(decoded["ego_view_depth"], depth)
    assert decoded["ego_view"].shape == rgb.shape
    # JPEG is lossy, but the legacy RGB array channel order must be retained.
    np.testing.assert_allclose(decoded["ego_view"], rgb, atol=3)


@pytest.mark.parametrize("buffer_type", [bytes, bytearray])
def test_raw_png_depth_and_jpeg_color_roundtrip(buffer_type):
    depth = np.array([[1, 65535], [256, 32768]], dtype=np.uint16)
    rgb = np.full((16, 24, 3), [240, 80, 20], dtype=np.uint8)
    _, png = cv2.imencode(".png", depth)
    _, jpeg = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))

    decoded = roundtrip({
        "head_depth": buffer_type(png.tobytes()),
        "head": buffer_type(jpeg.tobytes()),
    })

    assert decoded["head_depth"].dtype == np.uint16
    # The two-dimensional depth image must not have its columns reversed.
    np.testing.assert_array_equal(decoded["head_depth"], depth)
    np.testing.assert_allclose(decoded["head"], rgb, atol=3)


@pytest.mark.parametrize("depth", [
    np.array([[0, 1, 256], [1000, 32768, 65535]], dtype=np.uint16),
    np.zeros((2, 3), dtype=np.uint16),
])
def test_depth_visualization_preserves_input(depth):
    original = depth.copy()

    bgr = image_to_bgr(depth)

    assert bgr.shape == (*depth.shape, 3)
    assert bgr.dtype == np.uint8
    assert not np.shares_memory(bgr, depth)
    np.testing.assert_array_equal(depth, original)
    np.testing.assert_array_equal(bgr[depth == 0], 0)
    if depth.max():
        assert np.any(bgr[depth > 0])
        assert not np.array_equal(bgr[0, 1], bgr[1, 2])


def test_rgb_visualization_converts_channels_without_modifying_input():
    rgb = np.array([[[255, 20, 0], [0, 30, 255]]], dtype=np.uint8)
    original = rgb.copy()

    bgr = image_to_bgr(rgb)

    np.testing.assert_array_equal(bgr, original[..., ::-1])
    np.testing.assert_array_equal(rgb, original)
    assert not np.shares_memory(bgr, rgb)

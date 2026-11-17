"""Unit tests for visual_color_extractor service."""

import io

import numpy as np
import pytest
from PIL import Image

from src.domains.brand.services.visual_color_extractor import (
    _filter_pixels,
    _kmeans,
    _rgb_to_hex,
    extract_dominant_colors,
)


def _make_image(colors: list[tuple[int, int, int]], width: int = 100, height: int = 100) -> bytes:
    """Create a test image with horizontal color bands."""
    img = Image.new("RGB", (width, height))
    band_height = height // len(colors)
    for i, color in enumerate(colors):
        for y in range(i * band_height, (i + 1) * band_height):
            for x in range(width):
                img.putpixel((x, y), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestRgbToHex:
    def test_black(self) -> None:
        assert _rgb_to_hex(0, 0, 0) == "#000000"

    def test_white(self) -> None:
        assert _rgb_to_hex(255, 255, 255) == "#ffffff"

    def test_red(self) -> None:
        assert _rgb_to_hex(255, 0, 0) == "#ff0000"


class TestFilterPixels:
    def test_removes_near_white(self) -> None:
        pixels = np.array([[250, 250, 250], [100, 50, 50]], dtype=np.uint8)
        filtered = _filter_pixels(pixels)
        assert len(filtered) == 1

    def test_removes_near_black(self) -> None:
        pixels = np.array([[5, 5, 5], [100, 50, 50]], dtype=np.uint8)
        filtered = _filter_pixels(pixels)
        assert len(filtered) == 1

    def test_keeps_mid_range(self) -> None:
        pixels = np.array([[128, 128, 128], [100, 50, 50]], dtype=np.uint8)
        filtered = _filter_pixels(pixels)
        assert len(filtered) == 2


class TestKmeans:
    def test_two_clusters(self) -> None:
        red = np.full((50, 3), [200, 50, 50], dtype=np.float64)
        blue = np.full((50, 3), [50, 50, 200], dtype=np.float64)
        pixels = np.vstack([red, blue])
        centers, labels = _kmeans(pixels, 2)
        assert centers.shape == (2, 3)
        assert len(np.unique(labels)) == 2

    def test_single_cluster(self) -> None:
        pixels = np.full((100, 3), [128, 128, 128], dtype=np.float64)
        centers, _labels = _kmeans(pixels, 1)
        assert centers.shape == (1, 3)
        assert np.allclose(centers[0], [128, 128, 128], atol=1)


class TestExtractDominantColors:
    def test_returns_hex_colors(self) -> None:
        img_bytes = _make_image([(200, 50, 50), (50, 50, 200)])
        colors = extract_dominant_colors(img_bytes, n_colors=2)
        assert len(colors) == 2
        for c in colors:
            assert c.startswith("#")
            assert len(c) == 7

    def test_most_dominant_first(self) -> None:
        # 75% red, 25% blue
        img = Image.new("RGB", (100, 100))
        for y in range(75):
            for x in range(100):
                img.putpixel((x, y), (200, 50, 50))
        for y in range(75, 100):
            for x in range(100):
                img.putpixel((x, y), (50, 50, 200))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        colors = extract_dominant_colors(buf.getvalue(), n_colors=2)
        assert len(colors) == 2
        # First color should be reddish (dominant)
        r = int(colors[0][1:3], 16)
        assert r > 150

    def test_empty_image(self) -> None:
        # All white pixels get filtered → fallback to unfiltered
        img_bytes = _make_image([(255, 255, 255)])
        colors = extract_dominant_colors(img_bytes, n_colors=2)
        # Should still return something (unfiltered fallback)
        assert isinstance(colors, list)

    def test_n_colors_parameter(self) -> None:
        img_bytes = _make_image([(200, 50, 50), (50, 200, 50), (50, 50, 200)])
        colors = extract_dominant_colors(img_bytes, n_colors=3)
        assert len(colors) == 3

    @pytest.mark.parametrize("n", [1, 3, 5])
    def test_various_n_colors(self, n: int) -> None:
        img_bytes = _make_image([(100, 50, 50), (50, 100, 50), (50, 50, 100)])
        colors = extract_dominant_colors(img_bytes, n_colors=n)
        assert len(colors) == n

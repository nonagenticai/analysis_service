"""Extract dominant colors from screenshot images using k-means clustering."""

from __future__ import annotations

import io
import logging

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

RESIZE_WIDTH = 200
MIN_LIGHTNESS = 25
MAX_LIGHTNESS = 230
MAX_ITERATIONS = 20
CONVERGENCE_THRESHOLD = 1.0


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _filter_pixels(pixels: np.ndarray) -> np.ndarray:
    """Remove near-white and near-black pixels."""
    lightness = pixels.mean(axis=1)
    mask = (lightness >= MIN_LIGHTNESS) & (lightness <= MAX_LIGHTNESS)
    return pixels[mask]


def _kmeans(pixels: np.ndarray, n_clusters: int) -> tuple[np.ndarray, np.ndarray]:
    """Simple k-means clustering with numpy (no scikit-learn dependency)."""
    rng = np.random.default_rng(42)
    indices = rng.choice(len(pixels), size=n_clusters, replace=False)
    centers = pixels[indices].astype(np.float64)
    labels = np.zeros(len(pixels), dtype=np.int64)

    for _ in range(MAX_ITERATIONS):
        distances = np.linalg.norm(pixels[:, np.newaxis] - centers, axis=2)
        labels = distances.argmin(axis=1)

        new_centers = np.zeros_like(centers)
        for k in range(n_clusters):
            cluster_pixels = pixels[labels == k]
            if len(cluster_pixels) > 0:
                new_centers[k] = cluster_pixels.mean(axis=0)
            else:
                new_centers[k] = centers[k]

        shift = np.linalg.norm(new_centers - centers)
        centers = new_centers
        if shift < CONVERGENCE_THRESHOLD:
            break

    return centers, labels


def extract_dominant_colors(image_bytes: bytes, n_colors: int = 5) -> list[str]:
    """Extract dominant hex colors from a screenshot image.

    Args:
        image_bytes: PNG/JPEG image data.
        n_colors: Number of dominant colors to extract.

    Returns:
        List of hex color strings sorted by frequency (most dominant first).
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    aspect = img.height / img.width
    img = img.resize((RESIZE_WIDTH, int(RESIZE_WIDTH * aspect)), Image.Resampling.LANCZOS)

    pixels = np.array(img).reshape(-1, 3)
    filtered = _filter_pixels(pixels)

    if len(filtered) < n_colors:
        logger.warning("Too few filtered pixels (%d), using unfiltered", len(filtered))
        filtered = pixels

    if len(filtered) < n_colors:
        return []

    centers, labels = _kmeans(filtered, n_colors)

    counts = np.bincount(labels, minlength=n_colors)
    order = counts.argsort()[::-1]

    colors = []
    for idx in order:
        r, g, b = centers[idx].astype(int)
        colors.append(_rgb_to_hex(int(r), int(g), int(b)))

    return colors

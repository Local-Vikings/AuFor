"""Cloud fraction from a sky photo, using tx's PyTorch model, if it is installed here.

The model normally runs on the Raspberry Pi (scripts/pi_cloud_agent.py) and posts its
result to /api/readings. This module lets the server do the same when torch, OpenCV and
the weights are installed on it; otherwise it raises ModelUnavailable and the route
answers 501 with instructions. Nothing at import time needs torch.
"""

from __future__ import annotations

import os
import tempfile

from app.config import CLOUD_ROI_RADIUS_PX


class ModelUnavailable(RuntimeError):
    """The model, its libraries or its weights are not available on this machine."""


def measure_free_percent(image: bytes, radius: int = CLOUD_ROI_RADIUS_PX) -> float:
    """Free-sky percentage (0-100) inside the circle of ``radius`` px, as cloud_predictor.run computes it.

    Raises:
        ModelUnavailable: If torch, OpenCV or the checkpoint is missing.
        ValueError: If the bytes are not a readable image.
    """
    try:
        import cloud_predictor  # noqa: PLC0415 - heavy import, only when a photo arrives
    except ImportError as error:
        raise ModelUnavailable(f"model libraries are not installed on this server ({error})") from error
    handle, path = tempfile.mkstemp(suffix=".img")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(image)
        return float(cloud_predictor.run(path, radius, "server-analyze")[0])
    except FileNotFoundError as error:
        if "checkpoint" in str(error).lower():
            raise ModelUnavailable(str(error)) from error
        raise ValueError("the upload is not a readable image") from error
    finally:
        os.unlink(path)

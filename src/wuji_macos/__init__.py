"""Experimental native SDK reuse, pinned to upstream CLI 2026.8.31.

This package currently exposes an offline header codec, not device control or
the official wuji_sdk Python API. Importing it does not initialize the SDK.
"""

from ._native import NativeHeaderError, backend_info, roundtrip_header

__all__ = ["NativeHeaderError", "backend_info", "roundtrip_header"]
__version__ = "0.1.0"

"""Experimental macOS binding compatible with the WUJI SDK's Hand 2 API.

Native-operation coverage is recorded in the repository README and research
notes. This module never substitutes simulated devices for missing hardware.
"""
from . import _api, _types
from ._types import *
from ._api import *
from ._backend import NativeCapabilityError, StreamLaggedError

__version__ = "2026.8.31+macos.0.1.dev0"
__all__ = sorted(set(_types.__all__ + _api.__all__ + ["NativeCapabilityError", "StreamLaggedError"]))

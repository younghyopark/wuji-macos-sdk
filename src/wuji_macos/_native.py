"""The only foreign calls here cross the narrow C ABI in native/header_bridge.c."""

import ctypes
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import platform

LIB_DIR = Path(__file__).resolve().parent / "_lib"


class NativeHeaderError(ValueError):
    """A header was rejected by the native SDK or its checked C bridge."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def backend_info():
    """Return build provenance without loading native code or opening a device."""
    metadata = LIB_DIR / "backend.json"
    if not metadata.is_file():
        raise RuntimeError("Native backend is not built; run scripts/build_macos.py and install its wheel")
    return json.loads(metadata.read_text())


@lru_cache(maxsize=1)
def _bridge():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("This experimental backend requires native Apple Silicon macOS Python")
    info = backend_info()
    for name, expected in info["native_sha256"].items():
        digest = hashlib.sha256((LIB_DIR / name).read_bytes()).hexdigest()
        if digest != expected:
            raise RuntimeError(f"Native backend checksum mismatch: {name}")
    library = ctypes.CDLL(str(LIB_DIR / "libwuji_header_bridge.dylib"))
    fn = library.wuji_header_roundtrip
    fn.argtypes = [
        ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
    ]
    fn.restype = ctypes.c_int
    return library


def roundtrip_header(data):
    """Decode then encode an inert 18-byte header using the upstream Rust SDK.

    This validates native SDK execution; it never opens a socket, constructs
    SdkManager, or transmits a command. Only this exact upstream ABI is tested.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("Header must be bytes, bytearray, or memoryview")
    data = bytes(data)
    if len(data) != 18:
        raise ValueError("A WUJI wire header must contain exactly 18 bytes")
    source = ctypes.create_string_buffer(data)
    output = ctypes.create_string_buffer(18)
    error = ctypes.create_string_buffer(256)
    code = _bridge().wuji_header_roundtrip(
        str(LIB_DIR / "libwuji_cli_reuse.dylib").encode(), source, len(data),
        output, len(output), error, len(error),
    )
    if code:
        raise NativeHeaderError(code, error.value.decode("utf-8", errors="replace"))
    return output.raw

"""The ctypes backend is Python-ABI independent but contains ARM64 Mach-O code."""
from pathlib import Path
from setuptools import Distribution, setup
from setuptools.command.bdist_wheel import bdist_wheel

native = Path(__file__).resolve().parent / "src/wuji_macos/_lib"
required = ["libwuji_cli_reuse.dylib", "libwuji_header_bridge.dylib",
            "libwuji_manager_bridge.dylib", "libwuji_device_bridge.dylib", "backend.json"]
if not all((native / name).is_file() for name in required):
    raise RuntimeError("Native artifacts are missing. Build with scripts/build_macos.py.")


class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True


class MacOSWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self):
        return "py3", "none", "macosx_11_0_arm64"


setup(cmdclass={"bdist_wheel": MacOSWheel}, distclass=BinaryDistribution)

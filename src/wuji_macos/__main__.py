"""Run an offline codec smoke check: python -m wuji_macos."""
import json
from . import backend_info, roundtrip_header

wire = bytes.fromhex("5710123478563412efcdab90112266558877")
assert roundtrip_header(wire) == wire
print(json.dumps({"native_header_roundtrip": "passed", "backend": backend_info()}, indent=2))

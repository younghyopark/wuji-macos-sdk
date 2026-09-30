// Version-specific adapters for two Rust functions in CLI 2026.8.31 (arm64).
// The native Rust aggregate return pointer is x8, as verified by disassembly.
// These are not general Rust ABI adapters.
.text
.p2align 2
.globl _wuji_decode_bridge
_wuji_decode_bridge:
    mov x9, x0
    mov x0, x1
    mov x1, x2
    mov x8, x3
    br x9

.globl _wuji_encode_bridge
_wuji_encode_bridge:
    mov x9, x0
    mov x0, x1
    mov x1, x2
    mov x2, x3
    mov x8, x4
    br x9

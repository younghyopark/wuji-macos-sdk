// Pinned 2026.8.31 ARM64 Rust aggregate return adapters.
.text
.p2align 2
.globl _wuji_manager_call_sret1
_wuji_manager_call_sret1:
    mov x9, x0
    mov x0, x1
    mov x8, x2
    br x9

.globl _wuji_manager_call_connect
_wuji_manager_call_connect:
    mov x9, x0
    mov x0, x1
    mov x1, x2
    mov x2, x3
    mov x3, x4
    mov x4, x5
    mov x8, x6
    br x9

.globl _wuji_manager_call_sret2
_wuji_manager_call_sret2:
    mov x9, x0
    mov x0, x1
    mov x1, x2
    mov x8, x3
    br x9

.globl _wuji_manager_call_sret3
_wuji_manager_call_sret3:
    mov x9, x0
    mov x0, x1
    mov x1, x2
    mov x2, x3
    mov x8, x4
    br x9

.globl _wuji_manager_call_sret4
_wuji_manager_call_sret4:
    mov x9, x0
    mov x0, x1
    mov x1, x2
    mov x2, x3
    mov x3, x4
    mov x8, x5
    br x9

.globl _wuji_manager_call_sret6
_wuji_manager_call_sret6:
    mov x9, x0
    mov x0, x1
    mov x1, x2
    mov x2, x3
    mov x3, x4
    mov x4, x5
    mov x5, x6
    mov x8, x7
    br x9

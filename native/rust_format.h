#ifndef WUJI_RUST_FORMAT_H
#define WUJI_RUST_FORMAT_H
#include <stddef.h>

/* Pinned 2026.8.31 Rust fmt::Display adapter. needed includes final NUL.
 * Returns 0 when output fits, 1 when truncated, or a negative bridge error.
 * A NULL output with capacity zero is allowed for measuring the required size.
 */
int wuji_format_display(void *library, void *display_fn, const void *value,
                        char *output, size_t capacity, size_t *needed);
#endif

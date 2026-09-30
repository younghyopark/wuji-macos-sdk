/* Formatter ABI recovered from core::fmt::write at image offset 0x1706CC
 * and serde_json::Value::fmt at 0x587018, CLI arm64 2026.8.31.
 * The C writer owns all output; no Rust allocation crosses the interface.
 */
#include "rust_format.h"
#include <dlfcn.h>
#include <stdint.h>
#include <string.h>

typedef uint8_t (*CoreWrite)(void *, const void *, const void *, uintptr_t);
typedef uint8_t (*Display)(const void *, void *);
typedef struct {
    char *output;
    size_t capacity;
    size_t total;
    CoreWrite core_write;
    int overflow;
} Writer;

static uint8_t write_str(void *context, const char *text, size_t length);
static uint8_t write_char(void *context, uint32_t character);
static uint8_t write_fmt(void *context, const void *format, uintptr_t arguments);

/* Rust fmt::Write trait vtable: drop, size, alignment, then three methods. */
static const uintptr_t VTABLE[] = {
    0, sizeof(Writer), _Alignof(Writer),
    (uintptr_t)write_str, (uintptr_t)write_char, (uintptr_t)write_fmt,
};

static uint8_t write_str(void *context, const char *text, size_t length) {
    Writer *writer = context;
    if (SIZE_MAX - writer->total < length) {
        writer->overflow = 1;
        return 1;
    }
    if (writer->output != NULL && writer->capacity > 0 &&
        writer->total < writer->capacity - 1) {
        size_t available = writer->capacity - 1 - writer->total;
        size_t count = length < available ? length : available;
        memcpy(writer->output + writer->total, text, count);
    }
    writer->total += length;
    return 0;
}

static uint8_t write_char(void *context, uint32_t character) {
    char encoded[4];
    size_t length;
    if (character <= 0x7f) {
        encoded[0] = (char)character;
        length = 1;
    } else if (character <= 0x7ff) {
        encoded[0] = (char)(0xc0 | (character >> 6));
        encoded[1] = (char)(0x80 | (character & 0x3f));
        length = 2;
    } else if (character <= 0xffff) {
        encoded[0] = (char)(0xe0 | (character >> 12));
        encoded[1] = (char)(0x80 | ((character >> 6) & 0x3f));
        encoded[2] = (char)(0x80 | (character & 0x3f));
        length = 3;
    } else if (character <= 0x10ffff) {
        encoded[0] = (char)(0xf0 | (character >> 18));
        encoded[1] = (char)(0x80 | ((character >> 12) & 0x3f));
        encoded[2] = (char)(0x80 | ((character >> 6) & 0x3f));
        encoded[3] = (char)(0x80 | (character & 0x3f));
        length = 4;
    } else {
        return 1;
    }
    return write_str(context, encoded, length);
}

static uint8_t write_fmt(void *context, const void *format, uintptr_t arguments) {
    Writer *writer = context;
    return writer->core_write(writer, VTABLE, format, arguments);
}

int wuji_format_display(void *library, void *display_fn, const void *value,
                        char *output, size_t capacity, size_t *needed) {
    if (library == NULL || display_fn == NULL || value == NULL ||
        (output == NULL && capacity != 0)) return -1;
    CoreWrite core_write = (CoreWrite)dlsym(library, "_RNvNtCs8Mbv00yxnRz_4core3fmt5write");
    if (core_write == NULL) return -2;
    Writer writer = {output, capacity, 0, core_write, 0};
    struct Formatter {
        void *data;
        const uintptr_t *vtable;
        uint32_t flags;
        uint16_t width;
        uint16_t precision;
    } formatter = {&writer, VTABLE, 0x60000020u, 0, 0};
    _Static_assert(sizeof(struct Formatter) == 24, "Pinned Formatter size");
    uint8_t result = ((Display)display_fn)(value, &formatter);
    if (output != NULL && capacity > 0) {
        size_t end = writer.total < capacity ? writer.total : capacity - 1;
        output[end] = '\0';
    }
    if (needed != NULL) *needed = writer.total == SIZE_MAX ? SIZE_MAX : writer.total + 1;
    if (result != 0 || writer.overflow) return -3;
    return writer.total < capacity ? 0 : 1;
}

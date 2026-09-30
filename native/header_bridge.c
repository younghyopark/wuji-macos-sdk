/* Offline, version-specific native SDK reuse proof; contains no device API.
 *
 * This C ABI accepts an 18-byte wire header and roundtrips it through the
 * official native Rust WujiHeader::decode and WujiHeader::encode routines.
 * It does not construct an SDK manager, connect to hardware, or send bytes.
 * Only use a trusted derived copy of the pinned 2026.8.31 CLI.
 */
#include <dlfcn.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#if !defined(__APPLE__) || !defined(__aarch64__)
#error This experimental adapter is specifically for Apple Silicon macOS.
#endif

extern void wuji_decode_bridge(void *fn, const void *input, size_t len,
                               void *result);
extern void wuji_encode_bridge(void *fn, const void *header, void *output,
                               size_t len, void *result);

static const char *DECODE_SYMBOL =
    "_RNvMNtCsgTcy8zmbXBL_9wuji_core6headerNtB2_10WujiHeader6decode";
static const char *ENCODE_SYMBOL =
    "_RNvMNtCsgTcy8zmbXBL_9wuji_core6headerNtB2_10WujiHeader6encode";

static void describe(char *error, size_t length, const char *message) {
    if (error != NULL && length > 0) {
        snprintf(error, length, "%s", message != NULL ? message : "unknown error");
    }
}

/* Return 0 on success. Native decode errors: 1 truncated, 2 magic, 3 version.
 * Bridge errors: -1 invalid argument, -2 dlopen, -3 dlsym, -4 native invariant.
 * Exactly 18 bytes are written to output on success; output is unchanged on
 * preflight/decode failure. Optional error text is always NUL terminated when
 * its capacity is nonzero. Input may include payload bytes after the header.
 */
int wuji_header_roundtrip(const char *library_path, const unsigned char *input,
                         size_t input_len, unsigned char *output,
                         size_t output_len, char *error, size_t error_len) {
    describe(error, error_len, "");
    if (library_path == NULL || library_path[0] == '\0' ||
        input == NULL || output == NULL || output_len < 18) {
        describe(error, error_len, "null argument, empty library path, or output capacity below 18 bytes");
        return -1;
    }
    void *library = dlopen(library_path, RTLD_NOW | RTLD_LOCAL);
    if (library == NULL) {
        describe(error, error_len, dlerror());
        return -2;
    }
    dlerror();
    void *decode = dlsym(library, DECODE_SYMBOL);
    const char *decode_error = dlerror();
    if (decode_error != NULL) {
        describe(error, error_len, decode_error);
        dlclose(library);
        return -3;
    }
    void *encode = dlsym(library, ENCODE_SYMBOL);
    const char *encode_error = dlerror();
    if (encode_error != NULL) {
        describe(error, error_len, encode_error);
        dlclose(library);
        return -3;
    }

    /* Disassembly: Result<WujiHeader, _> has tag byte 0 and success payload
     * at byte 4. WujiHeader occupies 20 bytes with 4-byte alignment.
     * Reserve excess zeroed space without exposing this private Rust layout.
     */
    _Alignas(8) unsigned char decoded[64] = {0};
    _Alignas(8) unsigned char encoded[64] = {0};
    unsigned char temporary_output[18] = {0};
    wuji_decode_bridge(decode, input, input_len, decoded);
    if (decoded[0] != 0) {
        int code = decoded[1];
        describe(error, error_len,
                 code == 1 ? "native decoder: truncated header" :
                 code == 2 ? "native decoder: invalid magic" :
                 code == 3 ? "native decoder: unsupported version" :
                             "native decoder: unexpected error");
        dlclose(library);
        return code >= 1 && code <= 3 ? code : -4;
    }
    wuji_encode_bridge(encode, decoded + 4, temporary_output,
                       sizeof(temporary_output), encoded);
    uint64_t written = 0;
    memcpy(&written, encoded + 8, sizeof(written));
    if (encoded[0] != 0 || written != sizeof(temporary_output)) {
        describe(error, error_len, "native encoder returned an unexpected result");
        dlclose(library);
        return -4;
    }
    memcpy(output, temporary_output, sizeof(temporary_output));
    dlclose(library);
    return 0;
}

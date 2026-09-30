/* Build-only, network-free payload oracles for the pinned 2026.8.31 arm64
 * CLI image. These adapters are deliberately not a hardware API. The native
 * memory layouts used by the companion script were recovered from the exact
 * serializer/parser instructions recorded in research/hand2-semantics.md.
 */
#include <dlfcn.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>

extern void wuji_device_sret2(void *, uintptr_t, uintptr_t, void *);
typedef struct { uint64_t capacity; unsigned char *data; uint64_t length; } RustVec;

static uintptr_t image_base(void *library) {
    void *anchor = dlsym(library,
        "_RNvNtNtNtCsgTcy8zmbXBL_9wuji_core7schemas9generated20json_schema_dispatch15get_json_schema");
    Dl_info info;
    if (anchor == NULL || !dladdr(anchor, &info)) return 0;
    return (uintptr_t)info.dli_fbase;
}

/* value borrows all its fields; the native serializer does not take ownership.
 * Only the returned Vec<u8> is owned and is always dropped with its native drop.
 */
int wuji_schema_oracle_serialize(const char *path, uint16_t schema,
                                const void *value, size_t value_size,
                                unsigned char *output, size_t capacity,
                                size_t *written) {
    uintptr_t offset;
    size_t expected;
    if (schema == 261) { offset = 0xc21f64u; expected = 336; }
    else if (schema == 332) { offset = 0xbf506cu; expected = 128; }
    else return 2;
    if (!path || !value || !written || value_size != expected || (!output && capacity)) return -1;
    *written = 0;
    void *library = dlopen(path, RTLD_LOCAL | RTLD_NOW);
    if (!library) return -2;
    uintptr_t base = image_base(library);
    if (!base) { dlclose(library); return -3; }
    RustVec result = {0, (unsigned char *)1, 0};
    uint8_t code = ((uint8_t (*)(const void *, void *))(base + offset))(value, &result);
    int status = 0;
    if (code != 255) status = -4;
    else {
        *written = (size_t)result.length;
        if (result.length > capacity) status = 1;
        else if (result.length) memcpy(output, result.data, (size_t)result.length);
    }
    ((void (*)(void *))(base + 0x1c10c8u))(&result);
    dlclose(library);
    return status;
}

/* HandCommunicationDiagnostics has five fixed, numeric FingerDiagnostics
 * values. Native Result layout: success tag:u8 at0 and 1120 bytes at+4.
 * There are no String/Vec/Arc fields to drop on success or postcard error.
 */
int wuji_schema_oracle_comm_decode(const char *path, const unsigned char *input,
                                  size_t length, void *output, size_t capacity) {
    if (!path || (!input && length) || !output || capacity != 1120) return -1;
    void *library = dlopen(path, RTLD_LOCAL | RTLD_NOW);
    if (!library) return -2;
    uintptr_t base = image_base(library);
    if (!base) { dlclose(library); return -3; }
    _Alignas(8) unsigned char result[1128] = {0};
    wuji_device_sret2((void *)(base + 0xbf0a28u), (uintptr_t)input, length, result);
    int status = result[0] == 0 ? 0 : 3;
    if (status == 0) memcpy(output, result + 4, 1120);
    dlclose(library);
    return status;
}

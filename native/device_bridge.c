/* Actual SDK schema access and byte-codec adapters. All Rust layouts and
 * image offsets in this file are pinned to official arm64 CLI 2026.8.31.
 */
#include "rust_format.h"
#include <dlfcn.h>
#include <math.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

extern void wuji_device_sret1(void *, uintptr_t, void *);
extern void wuji_device_sret2(void *, uintptr_t, uintptr_t, void *);
extern void wuji_device_sret3(void *, uintptr_t, uintptr_t, uintptr_t, void *);
extern void wuji_device_sret4(void *, uintptr_t, uintptr_t, uintptr_t, uintptr_t, void *);
extern void wuji_device_sret5(void *, uintptr_t, uintptr_t, uintptr_t, uintptr_t,
                              uintptr_t, void *);
extern void wuji_device_sret6(void *, uintptr_t, uintptr_t, uintptr_t, uintptr_t,
                              uintptr_t, uintptr_t, void *);

static void error_text(char *error, size_t size, const char *text) {
    if (error != NULL && size > 0) snprintf(error, size, "%s", text ? text : "unknown error");
}

typedef struct { uint64_t capacity; unsigned char *data; uint64_t length; } RustVec;
typedef struct {
    uint16_t id;
    uintptr_t json_parse, postcard_parse, postcard_write, json_write, drop;
} VectorCodec;

/* Every entry is a native Vec of primitives with trivial element destructors.
 * Parse Result uses capacity UINT64_MAX for an error; success is a Rust Vec.
 * The offsets correspond to retained monomorphized exported SDK functions.
 */
static const VectorCodec VECTOR_CODECS[] = {
    {0x118, 0xbd5738, 0xbac5c4, 0xbacbb0, 0xc720d0, 0xc33ac4}, /* MitParams */
    {0x144, 0xbd57e0, 0xbac6ac, 0xbacc58, 0xc721cc, 0xf25444}, /* EffortLimitVec */
    {0x145, 0xbd57c4, 0xbac678, 0xbacc20, 0xc21e58, 0x1c10c8}, /* ControlModeVec */
};

static uintptr_t image_base(void *library) {
    void *anchor = dlsym(library,
        "_RNvNtNtNtCsgTcy8zmbXBL_9wuji_core7schemas9generated20json_schema_dispatch15get_json_schema");
    Dl_info info;
    if (anchor == NULL || !dladdr(anchor, &info)) return 0;
    return (uintptr_t)info.dli_fbase;
}

static void json_error(void *library, uintptr_t base, void *owned_error,
                       char *error, size_t capacity) {
    void *display = (void *)(base + 0x59b5f8u);
    if (wuji_format_display(library, display, owned_error, error, capacity, NULL) < 0)
        error_text(error, capacity, "native JSON error");
    ((void (*)(void *))(base + 0x59c7d4u))(owned_error);
}

/* Serialize a validated native value. Format 0 is Postcard, 1 is JSON.
 * Required bytes are exact for JSON; Postcard reports insufficient capacity
 * as 1 and does not report a required size because the native encoder does not.
 */
static int serialize_value(void *library, uintptr_t base, const void *value,
                           uintptr_t postcard_fn, uintptr_t json_fn, int format,
                           unsigned char *output, size_t capacity, size_t *written,
                           char *error, size_t error_capacity) {
    if (written != NULL) *written = 0;
    if (format == 0) {
        _Alignas(8) unsigned char result[16] = {0};
        unsigned char zero_capacity_storage;
        wuji_device_sret3((void *)(base + postcard_fn), (uintptr_t)value,
            (uintptr_t)(output != NULL ? output : &zero_capacity_storage), capacity, result);
        if (result[0] != 0) {
            error_text(error, error_capacity, "native Postcard encoder: insufficient output capacity");
            return 1;
        }
        uint64_t length;
        memcpy(&length, result + 8, sizeof(length));
        if (length > capacity) return -4;
        if (written != NULL) *written = (size_t)length;
        return 0;
    }
    RustVec vector = {0, (unsigned char *)1, 0};
    RustVec *vector_pointer = &vector;
    void *native_error = ((void *(*)(const void *, void *))(base + json_fn))(value, &vector_pointer);
    int result = 0;
    if (native_error != NULL) {
        json_error(library, base, &native_error, error, error_capacity);
        result = 3;
    } else {
        if (written != NULL) *written = (size_t)vector.length;
        if (vector.length > capacity) result = 1;
        else if (vector.length > 0) memcpy(output, vector.data, (size_t)vector.length);
    }
    ((void (*)(void *))(base + 0x1c10c8u))(&vector);
    return result;
}

/* Convert with actual native typed JSON/Postcard parsers and serializers.
 * Formats 0=Postcard, 1=JSON. 0 success, 1 output short, 2 unsupported schema,
 * 3 native parse error, negative bridge error. Output is bytes without NUL.
 */
int wuji_device_codec(const char *library_path, uint16_t schema_id,
                      int input_format, int output_format,
                      const unsigned char *input, size_t input_length,
                      unsigned char *output, size_t capacity, size_t *written,
                      char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (written != NULL) *written = 0;
    if (library_path == NULL || input == NULL || (output == NULL && capacity > 0) ||
        input_format < 0 || input_format > 1 || output_format < 0 || output_format > 1) return -1;
    const VectorCodec *codec = NULL;
    for (size_t i = 0; i < sizeof(VECTOR_CODECS) / sizeof(VECTOR_CODECS[0]); ++i)
        if (VECTOR_CODECS[i].id == schema_id) codec = &VECTOR_CODECS[i];
    if (codec == NULL) { error_text(error, error_capacity, "native codec not mapped for this schema"); return 2; }
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) { error_text(error, error_capacity, dlerror()); return -2; }
    uintptr_t base = image_base(library);
    if (base == 0) { dlclose(library); return -3; }
    _Alignas(8) unsigned char parsed[32] = {0};
    uintptr_t parser = input_format == 1 ? codec->json_parse : codec->postcard_parse;
    wuji_device_sret2((void *)(base + parser), (uintptr_t)input, input_length, parsed);
    uint64_t tag;
    memcpy(&tag, parsed, sizeof(tag));
    if (tag == UINT64_MAX) {
        if (input_format == 1) json_error(library, base, parsed + 8, error, error_capacity);
        else error_text(error, error_capacity, "native Postcard decoder rejected input");
        dlclose(library); return 3;
    }
    int result = serialize_value(library, base, parsed, codec->postcard_write,
        codec->json_write, output_format, output, capacity, written, error, error_capacity);
    ((void (*)(void *))(base + codec->drop))(parsed);
    dlclose(library);
    return result;
}

/* Command data is a flat array of 20 (position, velocity, effort) triples.
 * Native Rust JointCommandFrame has joints[20] at 0 and seq at 240, as shown
 * independently by its JSON and Postcard serializers. No packets are sent.
 */
int wuji_device_joint_command_encode(const char *library_path, uint32_t sequence,
                                     const float *commands, size_t command_values,
                                     int output_format, unsigned char *output,
                                     size_t capacity, size_t *written,
                                     char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (written != NULL) *written = 0;
    if (library_path == NULL || commands == NULL || command_values != 60 ||
        (output == NULL && capacity > 0) || output_format < 0 || output_format > 1) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) { error_text(error, error_capacity, dlerror()); return -2; }
    uintptr_t base = image_base(library);
    if (base == 0) { dlclose(library); return -3; }
    struct { float joints[60]; uint32_t seq; } native;
    _Static_assert(offsetof(__typeof__(native), seq) == 240, "pinned native joint frame layout");
    memcpy(native.joints, commands, sizeof(native.joints));
    native.seq = sequence;
    int result = serialize_value(library, base, &native, 0xbaccc8, 0xbf46d8,
        output_format, output, capacity, written, error, error_capacity);
    dlclose(library);
    return result;
}

/* Generic native request path. device is the first 56 bytes of the owned
 * WujiDevice record returned by manager_bridge; its lifetime belongs to caller.
 * operation: 0=get_raw, 1=set_raw, 2=execute. Paths are UTF-8, NUL terminated.
 * Success returns 0. Native SDK failure returns 3 with the actual Display text.
 * get_raw copies bytes into C-owned memory; release with wuji_device_buffer_free.
 * Unlike a length-probe API, this performs each request exactly once.
 *
 * Signatures and Result layouts are recovered from get_raw@0xB9F910,
 * set_raw@0xB9F9D4, execute@0xB9F724 and their outlined helpers. Physical-device
 * success paths cannot be hardware-validated by the offline probe.
 */
int wuji_device_request_raw(const char *library_path, void *device, int operation,
                            const char *path, const unsigned char *input,
                            size_t input_length, unsigned char **output,
                            size_t *output_length, char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (output != NULL) *output = NULL;
    if (output_length != NULL) *output_length = 0;
    if (library_path == NULL || device == NULL || path == NULL ||
        operation < 0 || operation > 2 || (input == NULL && input_length > 0) ||
        (operation == 0 && (output == NULL || output_length == NULL))) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) { error_text(error, error_capacity, dlerror()); return -2; }
    uintptr_t base = image_base(library);
    if (base == 0) { dlclose(library); return -3; }
    size_t path_length = strlen(path);
    _Alignas(8) unsigned char result[64] = {0};
    if (operation == 1) {
        unsigned char empty;
        wuji_device_sret5((void *)(base + 0xb9f9d4u), (uintptr_t)device,
            (uintptr_t)path, path_length,
            (uintptr_t)(input != NULL ? input : &empty), input_length, result);
    } else {
        uintptr_t function = operation == 0 ? 0xb9f910u : 0xb9f724u;
        wuji_device_sret3((void *)(base + function), (uintptr_t)device,
                          (uintptr_t)path, path_length, result);
    }
    uint64_t tag;
    memcpy(&tag, result, sizeof(tag));
    if (tag != UINT64_MAX) {
        if (wuji_format_display(library, (void *)(base + 0xc331e8u), result,
                                 error, error_capacity, NULL) < 0)
            error_text(error, error_capacity, "native SDK error formatting failed");
        ((void (*)(void *))(base + 0xb9c1f0u))(result);
        dlclose(library);
        return 3;
    }
    int status = 0;
    if (operation == 0) {
        RustVec vector;
        memcpy(&vector, result + 8, sizeof(vector));
        if (vector.length > SIZE_MAX || vector.length > vector.capacity) {
            status = -4;
            error_text(error, error_capacity, "native byte-vector invariant failed");
        } else if (vector.length > 0) {
            *output = malloc((size_t)vector.length);
            if (*output == NULL) {
                status = -5;
                error_text(error, error_capacity, "allocating response bytes failed");
            } else {
                memcpy(*output, vector.data, (size_t)vector.length);
                *output_length = (size_t)vector.length;
            }
        }
        ((void (*)(void *))(base + 0x1c10c8u))(&vector);
    }
    dlclose(library);
    return status;
}

void wuji_device_buffer_free(void *buffer) { free(buffer); }

/* Resource layout is established by native From<&ResourceEntry>@0xbece0c:
 * String, u16 schema_id, five access booleans, and u8 wire serde format.
 * resources_all/resources return bare Vec<Resource> via x8, not Result.
 */
typedef struct {
    RustVec path;
    uint16_t schema_id;
    uint8_t can_get, can_set, can_sub, can_pub, can_exec, serde_format;
} NativeResource;
_Static_assert(sizeof(NativeResource) == 32, "pinned Resource layout");
typedef struct { char *out; size_t capacity, length; } JsonOutput;
static void json_bytes(JsonOutput *writer, const char *bytes, size_t length) {
    size_t available = writer->length < writer->capacity ? writer->capacity - writer->length : 0;
    size_t copied = length < available ? length : available;
    if (copied != 0) memcpy(writer->out + writer->length, bytes, copied);
    writer->length += length;
}
static void json_literal(JsonOutput *writer, const char *text) { json_bytes(writer, text, strlen(text)); }
static void json_quoted(JsonOutput *writer, const unsigned char *bytes, size_t length) {
    json_literal(writer, "\"");
    for (size_t i = 0; i < length; ++i) {
        if (bytes[i] == '"' || bytes[i] == '\\') {
            char escaped[2] = {'\\', (char)bytes[i]}; json_bytes(writer, escaped, 2);
        } else if (bytes[i] < 0x20) {
            char escaped[7]; snprintf(escaped, sizeof(escaped), "\\u%04x", bytes[i]);
            json_bytes(writer, escaped, 6);
        } else json_bytes(writer, (const char *)bytes + i, 1);
    }
    json_literal(writer, "\"");
}
static void resource_json(JsonOutput *writer, const NativeResource *resource) {
    char suffix[240];
    json_literal(writer, "{\"path\":");
    json_quoted(writer, resource->path.data, (size_t)resource->path.length);
    snprintf(suffix, sizeof(suffix),
        ",\"schema_id\":%u,\"can_get\":%s,\"can_set\":%s,\"can_sub\":%s,\"can_pub\":%s,\"can_exec\":%s,\"serde_format\":%u}",
        resource->schema_id, resource->can_get ? "true" : "false",
        resource->can_set ? "true" : "false", resource->can_sub ? "true" : "false",
        resource->can_pub ? "true" : "false", resource->can_exec ? "true" : "false",
        resource->serde_format);
    json_literal(writer, suffix);
}

int wuji_device_offline_resource_probe(const char *library_path, char *output,
                                      size_t capacity, size_t *needed) {
    if (library_path == NULL || needed == NULL || (output == NULL && capacity != 0)) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) return -2;
    uintptr_t base = image_base(library);
    if (base == 0) { dlclose(library); return -3; }
    /* Only borrowed by native From<&ResourceEntry>; constructor clones path. */
    static unsigned char path[] = "hand2.\"quoted\"\nparam";
    _Alignas(8) unsigned char entry[72] = {0};
    RustVec borrowed = {sizeof(path) - 1, path, sizeof(path) - 1};
    memcpy(entry, &borrowed, 24);
    entry[24] = 0xff; /* absent schema JSON */
    uint16_t schema_id = 0x118;
    memcpy(entry + 60, &schema_id, 2);
    entry[63] = 2; entry[65] = 0x15; /* get/sub/execute, native LE format */
    NativeResource resource = {0};
    wuji_device_sret1((void *)(base + 0xbece0cu), (uintptr_t)entry, &resource);
    JsonOutput writer = {output, capacity > 0 ? capacity - 1 : 0, 0};
    resource_json(&writer, &resource);
    if (capacity) output[writer.length < capacity ? writer.length : capacity - 1] = '\0';
    *needed = writer.length + 1;
    ((void (*)(void *))(base + 0x1c10c8u))(&resource.path);
    dlclose(library);
    return writer.length < capacity ? 0 : 1;
}

int wuji_device_resources_json(const char *library_path, void *device, int include_hidden,
                               char *output, size_t capacity, size_t *needed,
                               char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (needed != NULL) *needed = 0;
    if (library_path == NULL || device == NULL || (output == NULL && capacity != 0)) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) { error_text(error, error_capacity, dlerror()); return -2; }
    uintptr_t base = image_base(library);
    if (base == 0) { dlclose(library); return -3; }
    RustVec resources = {0};
    wuji_device_sret1((void *)(base + (include_hidden ? 0xb9e190u : 0xb9fad4u)),
                       (uintptr_t)device, &resources);
    JsonOutput writer = {output, capacity > 0 ? capacity - 1 : 0, 0};
    json_literal(&writer, "[");
    for (size_t i = 0; i < resources.length; ++i) {
        if (i) json_literal(&writer, ",");
        resource_json(&writer, (NativeResource *)resources.data + i);
    }
    json_literal(&writer, "]");
    if (capacity > 0) output[writer.length < capacity ? writer.length : capacity - 1] = '\0';
    if (needed != NULL) *needed = writer.length + 1;
    /* Vec::drop drops elements; RawVec::drop separately owns the allocation. */
    ((void (*)(void *))(base + 0xbd2640u))(&resources);
    ((void (*)(void *))(base + 0xc33a70u))(&resources);
    dlclose(library);
    return writer.length < capacity ? 0 : 1;
}

int wuji_device_is_connected(const char *library_path, void *device, int *connected,
                             char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (library_path == NULL || device == NULL || connected == NULL) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) { error_text(error, error_capacity, dlerror()); return -2; }
    uintptr_t base = image_base(library);
    if (base == 0) { dlclose(library); return -3; }
    *connected = ((uint8_t (*)(void *))(base + 0x9af308u))((unsigned char *)device + 24) != 0;
    dlclose(library);
    return 0;
}

int wuji_device_info_json(const char *library_path, void *device,
                          char *output, size_t capacity, size_t *needed,
                          char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (needed != NULL) *needed = 0;
    if (library_path == NULL || device == NULL || (output == NULL && capacity)) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) { error_text(error, error_capacity, dlerror()); return -2; }
    /* Native Device::device_id borrows the immutable Option<DeviceInfo> at
     * client Arc+0x320. DeviceInfo::from(DiscoverResp)@0x96775c constructs
     * serial String@0 and firmware version String@24; clone@0x998894 confirms.
     * The manager's live handle owns this Arc throughout this borrowed read.
     */
    unsigned char *client = NULL;
    memcpy(&client, (unsigned char *)device + 24, sizeof(client));
    if (client == NULL) { dlclose(library); return -4; }
    RustVec serial, firmware;
    memcpy(&serial, client + 0x320, sizeof(serial));
    JsonOutput writer = {output, capacity ? capacity - 1 : 0, 0};
    if (serial.capacity == UINT64_MAX) json_literal(&writer, "null");
    else {
        memcpy(&firmware, client + 0x338, sizeof(firmware));
        if (serial.length > serial.capacity || firmware.length > firmware.capacity) {
            dlclose(library); return -4;
        }
        json_literal(&writer, "{\"serial_number\":");
        json_quoted(&writer, serial.data, serial.length);
        json_literal(&writer, ",\"firmware_version\":");
        json_quoted(&writer, firmware.data, firmware.length);
        json_literal(&writer, "}");
    }
    if (capacity) output[writer.length < capacity ? writer.length : capacity - 1] = '\0';
    if (needed != NULL) *needed = writer.length + 1;
    dlclose(library);
    return writer.length < capacity ? 0 : 1;
}

int wuji_device_is_virtual(const char *library_path, void *device, const char *path,
                           int *is_virtual, char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (!library_path || !device || !path || !is_virtual) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (!library) { error_text(error, error_capacity, dlerror()); return -2; }
    uintptr_t base = image_base(library);
    if (!base) { dlclose(library); return -3; }
    /* Respect the SDK's physical-resource/schema precedence, matching typed
     * WujiDevice::subscribe rather than inferring virtual from a path name. */
    *is_virtual = ((uint8_t (*)(void *, const char *, size_t))(base + 0xb9ef88u))
        (device, path, strlen(path)) != 0;
    dlclose(library);
    return 0;
}

int wuji_device_nodes_json(const char *library_path, void *device,
                           char *output, size_t capacity, size_t *needed,
                           char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (needed != NULL) *needed = 0;
    if (!library_path || !device || (!output && capacity)) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (!library) { error_text(error, error_capacity, dlerror()); return -2; }
    uintptr_t base = image_base(library);
    if (!base) { dlclose(library); return -3; }
    /* Device::capabilities returns a read guard in x0,x1, matching native
     * get_resource@0x961ac8; guard Drop@0x96ce08 releases the held read lock.
     * CapabilityCache::list_nodes returns Vec<&CachedNodeInfo>. Native clone
     * @0xaf10f4 establishes CachedNodeInfo32: String0,id u16@24,type u8@26.
     * Keep the guard until all borrowed paths have been copied into JSON.
     */
    typedef struct { void *cache, *lock; } NativeCapabilityGuard;
    NativeCapabilityGuard guard =
        ((NativeCapabilityGuard (*)(void *))(base + 0x9af258u))((unsigned char *)device + 24);
    RustVec nodes = {0};
    wuji_device_sret1((void *)(base + 0x99ff20u), (uintptr_t)guard.cache, &nodes);
    JsonOutput writer = {output, capacity ? capacity - 1 : 0, 0};
    int status = 0;
    json_literal(&writer, "[");
    for (size_t i = 0; i < nodes.length; ++i) {
        const RustVec *path = ((const RustVec **)nodes.data)[i];
        if (path == NULL || path->length > path->capacity) { status = -4; break; }
        if (i) json_literal(&writer, ",");
        json_literal(&writer, "{\"path\":");
        json_quoted(&writer, path->data, path->length);
        json_literal(&writer, "}");
    }
    json_literal(&writer, "]");
    /* This Vec owns references only; allocation deallocator is the exact
     * RawVec<&CachedNodeInfo> routine used by its native into_iter@0xaf1174. */
    uint64_t allocation[2] = {nodes.capacity, (uintptr_t)nodes.data};
    ((void (*)(void *))(base + 0x98c8ccu))(allocation);
    ((void (*)(void *))(base + 0x96ce08u))(&guard);
    if (capacity) output[writer.length < capacity ? writer.length : capacity - 1] = '\0';
    if (needed != NULL) *needed = writer.length + 1;
    dlclose(library);
    return status ? status : writer.length < capacity ? 0 : 1;
}

int wuji_hand2_handedness(const char *library_path, void *device, int *handedness,
                          char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (library_path == NULL || device == NULL || handedness == NULL) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) { error_text(error, error_capacity, dlerror()); return -2; }
    uintptr_t base = image_base(library);
    if (base == 0) { dlclose(library); return -3; }
    /* Native HandHandedness is a borrowed &WujiDevice holder. Its get method
     * validates firmware 1/2 and returns SDK Left=0/Right=1, or SdkError.
     */
    _Alignas(8) unsigned char result[56] = {0};
    wuji_device_sret1((void *)(base + 0xbb8a00u), (uintptr_t)&device, result);
    uint64_t tag;
    memcpy(&tag, result, 8);
    if (tag != UINT64_MAX) {
        wuji_format_display(library, (void *)(base + 0xc331e8u), result,
                            error, error_capacity, NULL);
        ((void (*)(void *))(base + 0xb9c1f0u))(result);
        dlclose(library); return 3;
    }
    *handedness = result[8];
    dlclose(library); return 0;
}

static void native_header_json(JsonOutput *writer, const unsigned char *header) {
    RustVec frame_id;
    uint64_t timestamp;
    uint32_t sequence;
    memcpy(&frame_id, header, sizeof(frame_id));
    memcpy(&timestamp, header + 24, 8);
    memcpy(&sequence, header + 32, 4);
    char numbers[128];
    snprintf(numbers, sizeof(numbers), "{\"seq\":%u,\"timestamp_us\":%llu,\"frame_id\":",
        sequence, (unsigned long long)timestamp);
    json_literal(writer, numbers);
    json_quoted(writer, frame_id.data, frame_id.length);
    json_literal(writer, "}");
}

static void fingertip_info_json(JsonOutput *writer, const unsigned char *value) {
    RustVec model, format;
    uint32_t digest;
    uint16_t device_type;
    float rate;
    memcpy(&model, value + 40, sizeof(model));
    memcpy(&format, value + 64, sizeof(format));
    memcpy(&digest, value + 88, 4);
    memcpy(&rate, value + 92, 4);
    memcpy(&device_type, value + 96, 2);
    char numbers[160], rate_text[32];
    if (isfinite(rate)) snprintf(rate_text, sizeof(rate_text), "%.9g", (double)rate);
    else snprintf(rate_text, sizeof(rate_text), "null");
    json_literal(writer, "{\"header\":");
    native_header_json(writer, value);
    snprintf(numbers, sizeof(numbers), ",\"digest\":%u,\"device_type\":%u,\"rate_hz\":%s,\"model\":",
        digest, device_type, rate_text);
    json_literal(writer, numbers);
    json_quoted(writer, model.data, model.length);
    json_literal(writer, ",\"format\":");
    json_quoted(writer, format.data, format.length);
    json_literal(writer, "}");
}

static void fingertip_info_drop(uintptr_t base, void *value) {
    ((void (*)(void *))(base + 0x1c10c8u))(value);
    ((void (*)(void *))(base + 0x1c10c8u))((unsigned char *)value + 40);
    ((void (*)(void *))(base + 0x1c10c8u))((unsigned char *)value + 64);
}

int wuji_hand2_fingertip_info_json(const char *library_path, void *device, uint8_t finger,
                                  char **output, char *error, size_t error_capacity) {
    /* CLI 2026.9.22 dropped WujiHand2::get_fingertip_info (the SDK moved to
     * per-finger accessors) and links only the generic typed get/set it used.
     * Report it as unavailable rather than guess a replacement. */
    (void)library_path; (void)device; (void)finger;
    if (output != NULL) *output = NULL;
    error_text(error, error_capacity, "fingertip info is not available in this build (SDK 2026.9.22 removed get_fingertip_info)");
    return 2;
}

/* Pure native-deserializer oracle for fixture validation. Reads actual Rust
 * values after native Postcard parsing; no device, manager or network calls.
 */
int wuji_device_decode_fixture_json(const char *library_path, uint16_t schema_id,
                                    const unsigned char *input, size_t length,
                                    char *output, size_t capacity, size_t *needed) {
    if (!library_path || !input || (!output && capacity) || !needed) return -1;
    if (schema_id != 291 && schema_id != 334) return 2;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (!library) return -2;
    uintptr_t base = image_base(library);
    if (!base) { dlclose(library); return -3; }
    _Alignas(8) unsigned char value[104] = {0};
    wuji_device_sret2((void *)(base + (schema_id == 291 ? 0xbf09acu : 0xbf0954u)),
        (uintptr_t)input, length, value);
    uint64_t tag; memcpy(&tag, value, 8);
    if (tag == UINT64_MAX) { dlclose(library); return 3; }
    JsonOutput writer = {output, capacity ? capacity - 1 : 0, 0};
    if (schema_id == 334) {
        fingertip_info_json(&writer, value);
        fingertip_info_drop(base, value);
    } else {
        /* RealtimeStreamFrame::deserialize@0xa54ad4: Header40,Vec@40,num@64;
         * entry parser@0xa54910 yields32bytes: status32, six f32,err16,nid8.
         */
        RustVec joints; memcpy(&joints, value + 40, sizeof(joints));
        json_literal(&writer, "{\"header\":");
        native_header_json(&writer, value);
        char numbers[512];
        snprintf(numbers, sizeof(numbers), ",\"num_joints\":%u,\"joints\":[", value[64]);
        json_literal(&writer, numbers);
        for (size_t i = 0; i < joints.length; ++i) {
            const unsigned char *entry = joints.data + i * 32;
            uint32_t status; uint16_t code; float values[6];
            memcpy(&status, entry, 4); memcpy(values, entry + 4, sizeof(values));
            memcpy(&code, entry + 28, 2);
            if (i) json_literal(&writer, ",");
            snprintf(numbers, sizeof(numbers),
                "{\"nid\":%u,\"status_word\":%u,\"pos_rad_jnt_fb\":%.9g,\"vel_rad_s_jnt_fb\":%.9g,"
                "\"effort\":%.9g,\"current\":%.9g,\"vbus_v_fb\":%.9g,\"mcu_temp_c_fb\":%.9g,\"error_code_current\":%u}",
                entry[30], status, (double)values[0], (double)values[1], (double)values[2],
                (double)values[3], (double)values[4], (double)values[5], code);
            json_literal(&writer, numbers);
        }
        json_literal(&writer, "]}");
        ((void (*)(void *))(base + 0x1c10c8u))(value);
        if (joints.capacity) free(joints.data); /* trivial32byte entries, Rust System */
    }
    if (capacity) output[writer.length < capacity ? writer.length : capacity - 1] = '\0';
    *needed = writer.length + 1;
    dlclose(library);
    return writer.length < capacity ? 0 : 1;
}

typedef struct {
    _Alignas(8) unsigned char native[64];
    void *library;
    uintptr_t base;
    int is_virtual;
    char *serial, *path;
    uint64_t rate_arc;
} NativeSubscription;

/* SDK subscribe_raw returns Result<client::RawSubscription,SdkError>:
 * outer u64 tag 0=Ok,1=Err and payload at 8. Native RawSubscription is64bytes;
 * its Receiver<Arc<Vec<u8>>> starts at32, as independently established by
 * recv_async's poll@0xb3e7f8 and native full drop glue@0x73e1ec.
 * Subscription owns its native client Arc and survives a device-handle drop.
 * Caller must serialize receive/close and must never reuse a closed handle.
 */
int wuji_device_subscribe(const char *library_path, void *device, const char *path,
                          void **subscription, char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (subscription != NULL) *subscription = NULL;
    if (library_path == NULL || device == NULL || path == NULL || subscription == NULL) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) { error_text(error, error_capacity, dlerror()); return -2; }
    uintptr_t base = image_base(library);
    if (base == 0) { dlclose(library); return -3; }
    NativeSubscription *owned = calloc(1, sizeof(*owned));
    if (owned == NULL) { dlclose(library); return -5; }
    _Alignas(8) unsigned char result[72] = {0};
    if (((uint8_t (*)(void *, const char *, size_t))(base + 0xb9ef88u))
            (device, path, strlen(path))) {
        RustVec serial;
        memcpy(&serial, device, sizeof(serial));
        owned->serial = malloc(serial.length + 1);
        owned->path = strdup(path);
        if (owned->serial == NULL || owned->path == NULL) {
            free(owned->serial); free(owned->path); free(owned); dlclose(library); return -5;
        }
        memcpy(owned->serial, serial.data, serial.length);
        owned->serial[serial.length] = '\0';
        /* SDK global hub is initialized by should_subscribe_virtual above.
         * Same six arguments as typed SubscriptionHub::subscribe_virtual:
         * &hub, serial str, path str, &client Device. Native untyped result
         * is -1 success with Receiver<Arc<Vec<u8>>>@8, stream_id@24 (0 for
         * derived streams) and owned rate Arc@32. No typed ABI substitution.
         */
        wuji_device_sret6((void *)(base + 0xc61e90u), base + 0x48141d0u,
            (uintptr_t)serial.data, serial.length, (uintptr_t)path, strlen(path),
            (uintptr_t)device + 24, result);
        uint64_t tag; memcpy(&tag, result, sizeof(tag));
        if (tag != UINT64_MAX) {
            wuji_format_display(library, (void *)(base + 0xc331e8u), result,
                                error, error_capacity, NULL);
            ((void (*)(void *))(base + 0xb9c1f0u))(result);
            free(owned->serial); free(owned->path); free(owned); dlclose(library); return 3;
        }
        memcpy(owned->native + 32, result + 8, 16);
        memcpy(owned->native + 56, result + 24, 2);
        memcpy(&owned->rate_arc, result + 32, 8);
        owned->is_virtual = 1;
        owned->library = library;
        owned->base = base;
        *subscription = owned;
        return 0;
    }
    wuji_device_sret3((void *)(base + 0xb9e19cu), (uintptr_t)device,
        (uintptr_t)path, strlen(path), result);
    uint64_t tag; memcpy(&tag, result, 8);
    if (tag != 0) {
        wuji_format_display(library, (void *)(base + 0xc331e8u), result + 8,
                            error, error_capacity, NULL);
        ((void (*)(void *))(base + 0xb9c1f0u))(result + 8);
        free(owned); dlclose(library); return 3;
    }
    memcpy(owned->native, result + 8, 64);
    owned->library = library;
    owned->base = base;
    *subscription = owned;
    return 0;
}

static int receive_raw(uintptr_t base, void *receiver, unsigned char **output,
                        size_t *length, uint64_t *lagged,
                        char *error, size_t error_capacity) {
    _Alignas(8) uint64_t result[2] = {0};
    wuji_device_sret1((void *)(base + 0x98b528u), (uintptr_t)receiver, result);
    /* Native TryRecvError discriminants: 0 Empty,1 Closed,2 Lagged(u64).
     * Success uses UINT64_MAX plus an owned Arc<Vec<u8>> pointer.
     */
    if (result[0] == 0) return 1;
    if (result[0] == 1) return 2;
    if (result[0] == 2) { if (lagged != NULL) *lagged = result[1]; return 4; }
    if (result[0] != UINT64_MAX || result[1] == 0) {
        error_text(error, error_capacity, "native receive result invariant failed"); return -4;
    }
    RustVec vector;
    memcpy(&vector, (const unsigned char *)(uintptr_t)result[1] + 16, sizeof(vector));
    int status = 0;
    if (vector.length > vector.capacity) status = -4;
    else if (vector.length != 0) {
        *output = malloc((size_t)vector.length);
        if (*output == NULL) status = -5;
        else { memcpy(*output, vector.data, (size_t)vector.length); *length = (size_t)vector.length; }
    }
    ((void (*)(void *))(base + 0xc3e300u))(&result[1]);
    return status;
}

int wuji_subscription_try_recv(void *subscription, unsigned char **output, size_t *length,
                               uint64_t *lagged, char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (output != NULL) *output = NULL;
    if (length != NULL) *length = 0;
    if (lagged != NULL) *lagged = 0;
    if (subscription == NULL || output == NULL || length == NULL) return -1;
    NativeSubscription *owned = subscription;
    return receive_raw(owned->base, owned->native + 32, output, length, lagged, error, error_capacity);
}

int wuji_subscription_stream_id(void *subscription, uint16_t *stream_id) {
    if (subscription == NULL || stream_id == NULL) return -1;
    NativeSubscription *owned = subscription;
    /* Native RawSubscription::drop reads this exact id before canceling it. */
    memcpy(stream_id, owned->native + 56, sizeof(*stream_id));
    return 0;
}

int wuji_subscription_set_rate(void *subscription, uint16_t frequency_hz, uint16_t *actual_hz,
                               char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (subscription == NULL || actual_hz == NULL) return -1;
    NativeSubscription *owned = subscription;
    if (owned->is_virtual) {
        _Alignas(8) unsigned char result[56] = {0};
        wuji_device_sret6((void *)(owned->base + 0xc60d7cu), owned->base + 0x48141d0u,
            (uintptr_t)owned->serial, strlen(owned->serial), (uintptr_t)owned->path,
            strlen(owned->path), frequency_hz, result);
        uint64_t tag; memcpy(&tag, result, 8);
        if (tag != UINT64_MAX) {
            wuji_format_display(owned->library, (void *)(owned->base + 0xc331e8u), result,
                                error, error_capacity, NULL);
            ((void (*)(void *))(owned->base + 0xb9c1f0u))(result);
            return 3;
        }
        memcpy(actual_hz, result + 8, sizeof(*actual_hz));
        return 0;
    }
    RustVec path;
    memcpy(&path, owned->native + 8, sizeof(path));
    _Alignas(8) unsigned char result[48] = {0};
    /* Native client Device::set_stream_rate(self,path,len,u16)->Result<u16,ClientError>.
     * The client Device Arc pointer is the first word of RawSubscription.
     */
    wuji_device_sret4((void *)(owned->base + 0x9af784u), (uintptr_t)owned->native,
        (uintptr_t)path.data, (uintptr_t)path.length, frequency_hz, result);
    uint64_t tag; memcpy(&tag, result, sizeof(tag));
    if (tag != UINT64_MAX) {
        wuji_format_display(owned->library, (void *)(owned->base + 0x97ed6cu), result,
                            error, error_capacity, NULL);
        ((void (*)(void *))(owned->base + 0x9b9798u))(result);
        return 3;
    }
    memcpy(actual_hz, result + 8, sizeof(*actual_hz));
    return 0;
}

void wuji_subscription_close(void *subscription) {
    if (subscription == NULL) return;
    NativeSubscription *owned = subscription;
    if (owned->is_virtual) {
        /* Native HubSubscriptionLease::close does exactly this hub release;
         * full lease Drop@0xb39624 drops its rate Arc through0xb37f3c. */
        ((void (*)(void *, const char *, size_t, const char *, size_t))
            (owned->base + 0xc62f9cu))((void *)(owned->base + 0x48141d0u),
                owned->serial, strlen(owned->serial), owned->path, strlen(owned->path));
        ((void (*)(void *))(owned->base + 0x73be08u))(owned->native + 32);
        ((void (*)(void *))(owned->base + 0xc5c808u))(&owned->rate_arc);
        free(owned->serial); free(owned->path);
    } else ((void (*)(void *))(owned->base + 0x73e1ecu))(owned->native);
    dlclose(owned->library);
    free(owned);
}

/* Offline ABI proof of the exact broadcast receiver used by RawSubscription.
 * Native channel constructor, send, receive and destructors run without a
 * device or Tokio runtime. Allocation matches the pinned Darwin Rust System
 * allocator: Arc counts at0/8 and Vec at16 (native drop@0x9c29b8).
 * This does not validate device subscription negotiation or network delivery.
 */
int wuji_device_offline_stream_probe(const char *library_path, char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (library_path == NULL) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) { error_text(error, error_capacity, dlerror()); return -2; }
    uintptr_t base = image_base(library);
    if (base == 0) { dlclose(library); return -3; }
    _Alignas(8) uint64_t channel[3] = {0}; /* Sender pointer; Receiver pointer/cursor */
    wuji_device_sret1((void *)(base + 0x988350u), 2, channel);
    unsigned char *bytes = NULL;
    size_t length = 0;
    uint64_t lagged = 0;
    int status = -4, sender_live = 1;
    if (receive_raw(base, channel + 1, &bytes, &length, &lagged, error, error_capacity) != 1) goto cleanup;
    for (unsigned char n = 0; n < 3; ++n) {
        struct ArcBytes { uint64_t strong, weak; RustVec vec; } *arc = malloc(sizeof(*arc));
        if (arc == NULL) { status = -5; goto cleanup; }
        arc->vec.data = malloc(3);
        if (arc->vec.data == NULL) { free(arc); status = -5; goto cleanup; }
        arc->strong = 1; arc->weak = 1;
        arc->vec.capacity = 3; arc->vec.length = 3;
        arc->vec.data[0] = n; arc->vec.data[1] = 0; arc->vec.data[2] = 255;
        typedef struct { uint64_t tag, value; } SendResult;
        SendResult result = ((SendResult (*)(void *, void *))(base + 0x9894a4u))(channel, arc);
        if (result.tag != 0) {
            ((void (*)(void *))(base + 0xc3e300u))(&arc);
            goto cleanup;
        }
    }
    if (receive_raw(base, channel + 1, &bytes, &length, &lagged, error, error_capacity) != 4 || lagged != 1) goto cleanup;
    for (unsigned char n = 1; n < 3; ++n) {
        if (receive_raw(base, channel + 1, &bytes, &length, &lagged, error, error_capacity) != 0) goto cleanup;
        int matched = length == 3 && bytes[0] == n && bytes[1] == 0 && bytes[2] == 255;
        free(bytes); bytes = NULL;
        if (!matched) goto cleanup;
    }
    ((void (*)(void *))(base + 0x9b96c4u))(channel);
    sender_live = 0;
    if (receive_raw(base, channel + 1, &bytes, &length, &lagged, error, error_capacity) != 2) goto cleanup;
    status = 0;
cleanup:
    free(bytes);
    if (sender_live) ((void (*)(void *))(base + 0x9b96c4u))(channel);
    ((void (*)(void *))(base + 0x73be08u))(channel + 1);
    if (status != 0) error_text(error, error_capacity, "offline native stream ABI invariant failed");
    dlclose(library);
    return status;
}

/* Generates a receive-codec fixture using the original JointStateFrame
 * Postcard serializer. The in-memory offsets come from that serializer and
 * Header/JointStateEntry serializers, rather than assuming Rust field order.
 */
int wuji_device_joint_state_fixture(const char *library_path, unsigned char *output,
                                    size_t capacity, size_t *written) {
    if (library_path == NULL || written == NULL || (output == NULL && capacity != 0)) return -1;
    *written = 0;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) return -2;
    uintptr_t base = image_base(library);
    if (base == 0) { dlclose(library); return -3; }
    struct JointEntry { float position, velocity, effort; uint8_t nid, pad[3]; } joints[2] = {
        {0.25f, -1.5f, 2.75f, 0, {0}}, {-0.5f, 3.25f, -4.5f, 23, {0}}
    };
    struct Header { RustVec frame_id; uint64_t timestamp_us; uint32_t seq, pad; };
    struct Frame { struct Header header; RustVec joints; uint8_t num_joints, pad[7]; } frame = {
        {{11, (unsigned char *)"hand2\"\nbase", 11}, UINT64_C(1234567890123), 300, 0},
        {2, (unsigned char *)joints, 2}, 2, {0}
    };
    _Static_assert(sizeof(struct JointEntry) == 16, "pinned JointStateEntry layout");
    _Static_assert(offsetof(struct Frame, joints) == 40, "pinned JointStateFrame layout");
    _Static_assert(offsetof(struct Frame, num_joints) == 64, "pinned JointStateFrame layout");
    RustVec serialized = {0, (unsigned char *)1, 0};
    uint8_t status = ((uint8_t (*)(void *, void *))(base + 0xbf3e3cu))(&frame, &serialized);
    int result = 0;
    if (status != 255) result = -4;
    else {
        *written = (size_t)serialized.length;
        if (serialized.length > capacity) result = 1;
        else if (serialized.length != 0) memcpy(output, serialized.data, (size_t)serialized.length);
    }
    ((void (*)(void *))(base + 0x1c10c8u))(&serialized);
    dlclose(library);
    return result;
}

/* Returns 0 success, 1 insufficient output capacity (needed includes NUL),
 * 2 no schema registered for this id, or negative bridge error.
 * Schema data is generated by the embedded SDK, not a local schema copy.
 */
int wuji_device_schema_json(const char *library_path, uint16_t schema_id,
                            char *output, size_t capacity, size_t *needed,
                            char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (needed != NULL) *needed = 0;
    if (library_path == NULL || (output == NULL && capacity != 0)) return -1;
    void *library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (library == NULL) { error_text(error, error_capacity, dlerror()); return -2; }
    void *schema_fn = dlsym(library,
        "_RNvNtNtNtCsgTcy8zmbXBL_9wuji_core7schemas9generated20json_schema_dispatch15get_json_schema");
    void *display_fn = dlsym(library,
        "_RNvXs_NtCsbfgWHvRAF2V_10serde_json5valueNtB4_5ValueNtNtCs8Mbv00yxnRz_4core3fmt7Display3fmt");
    Dl_info info;
    if (schema_fn == NULL || display_fn == NULL || !dladdr(schema_fn, &info)) {
        error_text(error, error_capacity, "pinned SDK schema symbols unavailable");
        dlclose(library); return -3;
    }
    /* Value drop helper verified from Vec<Value>::drop and its direct body.
     * It is not in the export trie; offset is relative to loaded __TEXT base.
     */
    void (*drop_value)(void *) = (void (*)(void *))((uintptr_t)info.dli_fbase + 0x594eecu);
    _Alignas(8) unsigned char value[32] = {0};
    wuji_device_sret1(schema_fn, schema_id, value);
    if (value[0] == 0xff) {
        error_text(error, error_capacity, "no native schema for this id");
        dlclose(library); return 2;
    }
    int result = wuji_format_display(library, display_fn, value, output, capacity, needed);
    drop_value(value);
    if (result < 0) error_text(error, error_capacity, "native schema formatting failed");
    dlclose(library);
    return result;
}

/* Native Hand 2 joint-command publisher. CLI 2026.9.22 links the SDK's own
 * typed publisher, so the bridge calls it instead of rebuilding the protocol:
 *
 *   wuji_client::manager::Device::publish::<JointCommandFrame>   image+0xc3c14c
 *     x0=&Device (WujiDevice+24), x1/x2=path; x8=Result<Publisher, ClientError>.
 *     Ok is the 0x90-byte Publisher inline (word 0: 2 = Zenoh, else UDP stream);
 *     Err is UINT64_MAX at 0 with the ClientError at 8. It rejects any resource
 *     schema other than 315 and negotiates the UDP stream itself.
 *   WujiHand2 JointCommandPublisher::send                        image+0xbb9790
 *     x0=&JointCommandPublisher, x1=&[JointCommand; 20]; x8=Result<(), SdkError>.
 *     It checks all 60 floats are finite, stamps the u32 sequence at +0x90 and
 *     serializes with the resource's own format. Ok is UINT64_MAX at 0; Err is
 *     the SdkError inline.
 *   Publisher<JointCommandFrame>::send_raw                       image+0xbb8364
 *     x0=&Publisher, x1/x2=bytes; x8=Result<(), ClientError>, same Ok encoding.
 *   WujiHand2 JointCommandPublisher::close                       image+0xbb993c
 *     Consumes the publisher: UDP cancellation, then every field is dropped.
 *
 * JointCommandPublisher is the Publisher followed by an AtomicU32 sequence at
 * +0x90 (JointCommandPublisher::send fetch-adds it; close copies 0x90 bytes).
 * The publisher owns its clone of the device client, so it outlives the handle.
 */
typedef struct {
    _Alignas(16) unsigned char native[0xa0];
    void *library;
    uintptr_t base;
} NativePublisher;

static void client_error_text(void *library, uintptr_t base, void *value, char *error, size_t capacity) {
    if (wuji_format_display(library, (void *)(base + 0x97ed6cu), value, error, capacity, NULL) < 0)
        error_text(error, capacity, "native publisher error formatting failed");
    ((void (*)(void *))(base + 0x9b9798u))(value);
}

int wuji_device_publisher_open(const char *library_path, void *device, const char *path,
    void **publisher, char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (publisher) *publisher = NULL;
    if (!library_path || !device || !path || !publisher) return -1;
    NativePublisher *owned = calloc(1, sizeof(*owned));
    if (!owned) return -5;
    owned->library = dlopen(library_path, RTLD_LOCAL | RTLD_NOW);
    if (!owned->library) { error_text(error, error_capacity, dlerror()); free(owned); return -2; }
    owned->base = image_base(owned->library);
    if (!owned->base) { dlclose(owned->library); free(owned); return -3; }
    _Alignas(16) unsigned char result[0xa0] = {0};
    wuji_device_sret3((void *)(owned->base + 0xc3c14cu), (uintptr_t)device + 24,
                      (uintptr_t)path, strlen(path), result);
    uint64_t tag; memcpy(&tag, result, sizeof(tag));
    if (tag == UINT64_MAX) {
        client_error_text(owned->library, owned->base, result + 8, error, error_capacity);
        dlclose(owned->library); free(owned);
        return 3;
    }
    memcpy(owned->native, result, 0x90);  /* sequence at +0x90 starts at zero */
    *publisher = owned;
    return 0;
}

int wuji_publisher_stream_id(void *publisher, uint16_t *stream_id) {
    if (!publisher || !stream_id) return -1;
    NativePublisher *owned = publisher;
    uint64_t variant; memcpy(&variant, owned->native, sizeof(variant));
    /* Zenoh publishes on the resource key and has no negotiated stream. */
    if (variant == 2) *stream_id = 0;
    else memcpy(stream_id, owned->native + 0x80, sizeof(*stream_id));
    return 0;
}

int wuji_publisher_send(void *publisher, const unsigned char *payload, size_t length,
                        char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (!publisher || (!payload && length)) return -1;
    NativePublisher *owned = publisher;
    unsigned char empty = 0;
    _Alignas(16) unsigned char result[64] = {0};
    wuji_device_sret3((void *)(owned->base + 0xbb8364u), (uintptr_t)owned->native,
                      (uintptr_t)(payload ? payload : &empty), length, result);
    uint64_t tag; memcpy(&tag, result, sizeof(tag));
    if (tag == UINT64_MAX) return 0;
    client_error_text(owned->library, owned->base, result, error, error_capacity);
    return 3;
}

int wuji_publisher_send_joint_commands(void *publisher, const float *commands, size_t count,
                                       char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (!publisher || !commands || count != 60) return -1;
    for (size_t i = 0; i < count; ++i) if (!isfinite(commands[i])) {
        error_text(error, error_capacity, "joint commands must contain finite floats"); return -1;
    }
    NativePublisher *owned = publisher;
    _Alignas(16) unsigned char result[64] = {0};
    wuji_device_sret2((void *)(owned->base + 0xbb9790u), (uintptr_t)owned->native,
                      (uintptr_t)commands, result);
    uint64_t tag; memcpy(&tag, result, sizeof(tag));
    if (tag == UINT64_MAX) return 0;
    if (wuji_format_display(owned->library, (void *)(owned->base + 0xc331e8u), result,
                            error, error_capacity, NULL) < 0)
        error_text(error, error_capacity, "native publisher error formatting failed");
    ((void (*)(void *))(owned->base + 0xb9c1f0u))(result);
    return 3;
}

int wuji_publisher_close(void *publisher, char *error, size_t error_capacity) {
    error_text(error, error_capacity, "");
    if (!publisher) return 0;
    NativePublisher *owned = publisher;
    ((void (*)(void *))(owned->base + 0xbb993cu))(owned->native);
    dlclose(owned->library);
    free(owned);
    return 0;
}

/* Version-pinned manager bridge for the converted 2026.8.31 ARM64 CLI.
 * getenv interposition is restricted to the imported pointer in that image.
 * The process environment and other libraries' getenv calls are unchanged.
 */
#include <dlfcn.h>
#include <limits.h>
#include <mach-o/loader.h>
#include <mach-o/nlist.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <stdatomic.h>
#include "manager_bridge.h"
#include "rust_format.h"

#if !defined(__APPLE__) || !defined(__aarch64__)
#error This bridge requires Apple Silicon macOS.
#endif

extern void wuji_manager_call_sret1(void *, void *, void *);
extern void wuji_manager_call_sret2(void *, const void *, size_t, void *);
extern void wuji_manager_call_sret3(void *, void *, const void *, size_t, void *);
extern void wuji_manager_call_sret4(void *, uintptr_t, uintptr_t, uintptr_t, uintptr_t, void *);
extern void wuji_manager_call_sret6(void *, uintptr_t, uintptr_t, uintptr_t, uintptr_t, uintptr_t, uintptr_t, void *);
extern void wuji_manager_call_connect(void *, void *, void *, const char *, size_t, void *, void *);

typedef struct DeviceHandle {
    _Alignas(16) unsigned char native[56];
    struct DeviceHandle *next;
    char *name;
    int released;
    int disconnected;
} DeviceHandle;

typedef struct {
    void *library;
    void *instance;
    uintptr_t image_base;
    char state_root[PATH_MAX];
    char logs_root[PATH_MAX];
    char library_path[PATH_MAX];
    void *scan;
    void *connect;
    void *drop_scan_vec;
    void *error_display;
    void *disconnect;
    void *device_id;
    DeviceHandle *devices;
} Manager;

static Manager singleton;
static pthread_mutex_t singleton_mutex = PTHREAD_MUTEX_INITIALIZER;
static void *filter_library;
static char filter_library_path[PATH_MAX];

static void message(char *out, size_t length, const char *text) {
    if (out != NULL && length != 0) snprintf(out, length, "%s", text ? text : "unknown error");
}

int wuji_set_log_level(const char *library_path, const char *level, char *err, size_t errlen) {
    message(err, errlen, "");
    if (!library_path || !level) return -1;
    size_t log_level;
    if (!strcasecmp(level, "off")) log_level = 0;
    else if (!strcasecmp(level, "error")) log_level = 1;
    else if (!strcasecmp(level, "warn") || !strcasecmp(level, "warning")) log_level = 2;
    else if (!strcasecmp(level, "info")) log_level = 3;
    else if (!strcasecmp(level, "debug")) log_level = 4;
    else if (!strcasecmp(level, "trace")) log_level = 5;
    else { message(err, errlen, "Invalid log level; expected trace/debug/info/warn/warning/error/off"); return -1; }
    char canonical[PATH_MAX];
    if (!realpath(library_path, canonical)) { message(err, errlen, "Native SDK library does not exist"); return -2; }
    pthread_mutex_lock(&singleton_mutex);
    if ((filter_library && strcmp(filter_library_path, canonical)) ||
        (singleton.instance && strcmp(singleton.library_path, canonical))) {
        pthread_mutex_unlock(&singleton_mutex);
        message(err, errlen, "Native SDK logging already configured for another library");
        return -1;
    }
    if (!filter_library) {
        filter_library = dlopen(canonical, RTLD_NOW | RTLD_LOCAL);
        if (filter_library) snprintf(filter_library_path, sizeof(filter_library_path), "%s", canonical);
    }
    if (!filter_library) {
        message(err, errlen, dlerror());
        pthread_mutex_unlock(&singleton_mutex);
        return -2;
    }
    _Atomic size_t *log_filter = dlsym(filter_library, "_RNvCs6LChmAdslVW_3log20MAX_LOG_LEVEL_FILTER");
    _Atomic size_t *tracing_filter = dlsym(filter_library, "_RNvNtCs9mYr3JH1WmH_12tracing_core8metadata9MAX_LEVEL");
    if (!log_filter || !tracing_filter) {
        message(err, errlen, "Pinned native log/tracing filter symbols could not be resolved");
        pthread_mutex_unlock(&singleton_mutex);
        return -3;
    }
    /* log::LevelFilter uses OFF=0..TRACE=5. tracing_core deliberately reverses
     * its usize representation: TRACE=0..ERROR=4, OFF=5. SDK scan checks the
     * exported log atomic; its subscriber/log writer remains CLI-owned. */
    atomic_store_explicit(log_filter, log_level, memory_order_relaxed);
    atomic_exchange_explicit(tracing_filter, 5 - log_level, memory_order_acq_rel);
    pthread_mutex_unlock(&singleton_mutex);
    return 0;
}

static char *sdk_getenv(const char *name) {
    if (strcmp(name, "HOME") == 0) return singleton.state_root;
    if (strcmp(name, "WUJI_LOGS_DIR") == 0) return singleton.logs_root;
    if (strcmp(name, "WUJI_TELEMETRY_DISABLE") == 0) return "1";
    if (strcmp(name, "WUJI_NO_UPDATE_CHECK") == 0) return "1";
    return getenv(name);
}

/* Replace only the SDK image's __DATA lazy getenv import. RTLD_NOW must already
 * have resolved it. No text/code pages or process-global symbols are modified.
 */
static int contain_sdk_paths(const struct mach_header_64 *header, char *err, size_t errlen) {
    const unsigned char *cursor = (const unsigned char *)(header + 1);
    const struct symtab_command *symtab = NULL;
    const struct dysymtab_command *dysymtab = NULL;
    const struct segment_command_64 *linkedit = NULL;
    intptr_t slide = 0;
    for (uint32_t i = 0; i < header->ncmds; ++i) {
        const struct load_command *load = (const struct load_command *)cursor;
        if (load->cmd == LC_SYMTAB) symtab = (const struct symtab_command *)cursor;
        if (load->cmd == LC_DYSYMTAB) dysymtab = (const struct dysymtab_command *)cursor;
        if (load->cmd == LC_SEGMENT_64) {
            const struct segment_command_64 *segment = (const struct segment_command_64 *)cursor;
            if (strcmp(segment->segname, "__TEXT") == 0) slide = (intptr_t)header - segment->vmaddr;
            if (strcmp(segment->segname, "__LINKEDIT") == 0) linkedit = segment;
        }
        cursor += load->cmdsize;
    }
    if (!symtab || !dysymtab || !linkedit) {
        message(err, errlen, "SDK image is missing indirect-symbol metadata");
        return -1;
    }
    uintptr_t linkbase = slide + linkedit->vmaddr - linkedit->fileoff;
    const struct nlist_64 *symbols = (const struct nlist_64 *)(linkbase + symtab->symoff);
    const char *strings = (const char *)(linkbase + symtab->stroff);
    const uint32_t *indirect = (const uint32_t *)(linkbase + dysymtab->indirectsymoff);
    cursor = (const unsigned char *)(header + 1);
    size_t replacements = 0;
    for (uint32_t i = 0; i < header->ncmds; ++i) {
        const struct load_command *load = (const struct load_command *)cursor;
        if (load->cmd == LC_SEGMENT_64) {
            const struct segment_command_64 *segment = (const struct segment_command_64 *)cursor;
            const struct section_64 *sections = (const struct section_64 *)(segment + 1);
            for (uint32_t j = 0; j < segment->nsects; ++j) {
                const struct section_64 *section = &sections[j];
                if (strcmp(section->segname, "__DATA") != 0 ||
                    (section->flags & SECTION_TYPE) != S_LAZY_SYMBOL_POINTERS) continue;
                void **pointers = (void **)(slide + section->addr);
                for (size_t k = 0; k < section->size / sizeof(void *); ++k) {
                    uint32_t index = indirect[section->reserved1 + k];
                    if (index & (INDIRECT_SYMBOL_LOCAL | INDIRECT_SYMBOL_ABS)) continue;
                    if (index >= symtab->nsyms) return -1;
                    const char *name = strings + symbols[index].n_un.n_strx;
                    if (strcmp(name, "_getenv") == 0) {
                        pointers[k] = (void *)sdk_getenv;
                        ++replacements;
                    }
                }
            }
        }
        cursor += load->cmdsize;
    }
    if (replacements != 1) {
        message(err, errlen, "Expected exactly one SDK-local getenv import");
        return -1;
    }
    return 0;
}

/* Returns a process-lifetime singleton. The library stays loaded because SDK
 * global runtimes and worker threads may still own code/data in the image.
 */
int wuji_manager_open(const char *library_path, const char *state_root,
                      void **out, char *err, size_t errlen) {
    message(err, errlen, "");
    if (!library_path || !state_root || !out || state_root[0] != '/') {
        message(err, errlen, "Expected library path, absolute state root, and manager output");
        return -1;
    }
    *out = NULL;
    char canonical_library[PATH_MAX], canonical_state[PATH_MAX];
    if (!realpath(library_path, canonical_library) || !realpath(state_root, canonical_state)) {
        message(err, errlen, "Library and state root must exist");
        return -1;
    }
    pthread_mutex_lock(&singleton_mutex);
    if (filter_library && strcmp(filter_library_path, canonical_library)) {
        pthread_mutex_unlock(&singleton_mutex);
        message(err, errlen, "Native SDK logging already configured for another library");
        return -1;
    }
    if (singleton.instance) {
        if (strcmp(singleton.state_root, canonical_state) || strcmp(singleton.library_path, canonical_library)) {
            pthread_mutex_unlock(&singleton_mutex);
            message(err, errlen, "SDK singleton already initialized with different library or state root");
            return -1;
        }
        *out = &singleton;
        pthread_mutex_unlock(&singleton_mutex);
        return 0;
    }
    snprintf(singleton.state_root, sizeof(singleton.state_root), "%s", canonical_state);
    snprintf(singleton.library_path, sizeof(singleton.library_path), "%s", canonical_library);
    int count = snprintf(singleton.logs_root, sizeof(singleton.logs_root), "%s/.wuji/logs", canonical_state);
    if (count < 0 || (size_t)count >= sizeof(singleton.logs_root)) {
        pthread_mutex_unlock(&singleton_mutex);
        message(err, errlen, "State-root path is too long");
        return -1;
    }
    singleton.library = dlopen(canonical_library, RTLD_NOW | RTLD_LOCAL);
    if (!singleton.library) {
        message(err, errlen, dlerror());
        pthread_mutex_unlock(&singleton_mutex);
        return -2;
    }
    void *(*instance_fn)(void) = dlsym(singleton.library,
        "_RNvMNtCs61nGhHav3cI_8wuji_sdk7managerNtB2_10SdkManager8instance");
    singleton.scan = dlsym(singleton.library,
        "_RNvMNtCs61nGhHav3cI_8wuji_sdk7managerNtB2_10SdkManager4scan");
    singleton.connect = dlsym(singleton.library,
        "_RNvMNtCs61nGhHav3cI_8wuji_sdk7managerNtB2_10SdkManager7connect");
    singleton.drop_scan_vec = dlsym(singleton.library,
        "_RNvXso_NtCshxvaOLs88l5_5alloc3vecINtB5_3VecNtNtCsjQDt8kIRWQo_11wuji_client7manager16DiscoveredDeviceENtNtNtCs8Mbv00yxnRz_4core3ops4drop4Drop4dropBJ_");
    singleton.error_display = dlsym(singleton.library,
        "_RNvXNtCs61nGhHav3cI_8wuji_sdk5errorNtB2_8SdkErrorNtNtCs8Mbv00yxnRz_4core3fmt7Display3fmt");
    singleton.disconnect = dlsym(singleton.library,
        "_RNvMNtCs61nGhHav3cI_8wuji_sdk7managerNtB2_10SdkManager10disconnect");
    singleton.device_id = dlsym(singleton.library,
        "_RNvMNtCs61nGhHav3cI_8wuji_sdk6deviceNtB2_10WujiDevice9device_id");
    Dl_info image;
    if (!instance_fn || !singleton.scan || !singleton.connect || !singleton.drop_scan_vec || !singleton.error_display || !singleton.disconnect || !singleton.device_id ||
        !dladdr((void *)instance_fn, &image)) {
        message(err, errlen, "Pinned SDK manager symbols could not be resolved");
        pthread_mutex_unlock(&singleton_mutex);
        return -3;
    }
    singleton.image_base = (uintptr_t)image.dli_fbase;
    if (contain_sdk_paths(image.dli_fbase, err, errlen)) {
        pthread_mutex_unlock(&singleton_mutex);
        return -4;
    }
    singleton.instance = instance_fn();
    *out = &singleton;
    pthread_mutex_unlock(&singleton_mutex);
    return 0;
}

static int native_error(void *value, char *err, size_t errlen) {
    size_t needed = 0;
    int formatted = wuji_format_display(singleton.library, singleton.error_display,
                                        value, err, errlen, &needed);
    ((void (*)(void *))(singleton.image_base + 0xb9c1f0))(value);
    if (formatted < 0) message(err, errlen, "Native SDK operation failed (error formatting failed)");
    return 1;
}

void wuji_manager_free_string(char *value) { free(value); }

static void json_string(FILE *stream, const unsigned char *data, size_t len) {
    fputc('"', stream);
    for (size_t i = 0; i < len; ++i) {
        unsigned char c = data[i];
        if (c == '"' || c == '\\') fprintf(stream, "\\%c", c);
        else if (c < 32) fprintf(stream, "\\u%04x", c);
        else fputc(c, stream);
    }
    fputc('"', stream);
}

/* DiscoveredDevice is 152 bytes, derived from its upstream From<TransportInfo>
 * constructor and native Vec destructor: sn String@0, address String@48,
 * device type u8@144, transport type u8@145. Strings are cap,ptr,len.
 */
int wuji_manager_scan_json(void *handle, char **json, char *err, size_t errlen) {
    message(err, errlen, "");
    if (handle != &singleton || !singleton.instance || !json) return -1;
    *json = NULL;
    _Alignas(16) unsigned char result[256] = {0};
    wuji_manager_call_sret1(singleton.scan, singleton.instance, result);
    uint64_t *words = (uint64_t *)result;
    if (words[0] != UINT64_MAX) return native_error(result, err, errlen);
    size_t count = words[3];
    if (count > words[1] || count > 1000000) {
        message(err, errlen, "Native discovery Vec layout failed validation");
        return -5;
    }
    size_t output_len = 0;
    FILE *stream = open_memstream(json, &output_len);
    if (!stream) {
        ((void (*)(void *))singleton.drop_scan_vec)(result + 8);
        message(err, errlen, "Could not allocate discovery JSON");
        return -6;
    }
    const unsigned char *records = (const unsigned char *)(uintptr_t)words[2];
    fputc('[', stream);
    for (size_t i = 0; i < count; ++i) {
        const unsigned char *record = records + i * 152;
        const uint64_t *fields = (const uint64_t *)record;
        if (i) fputc(',', stream);
        fputs("{\"sn\":", stream);
        json_string(stream, (const unsigned char *)(uintptr_t)fields[1], fields[2]);
        fputs(",\"address\":", stream);
        json_string(stream, (const unsigned char *)(uintptr_t)fields[7], fields[8]);
        const char *transport = record[145] == 0 ? "Usb" : record[145] == 1 ? "Udp" : record[145] == 2 ? "Zenoh" : record[145] == 3 ? "CppNative" : "Unknown";
        const char *device = record[144] == 1 ? "WujiGlove" : record[144] == 2 ? "WujiHand2" : record[144] == 3 ? "WujiHand" : "Unknown";
        fprintf(stream, ",\"transport_type\":\"%s\",\"device_type\":\"%s\"}", transport, device);
    }
    fputc(']', stream);
    int failed = ferror(stream);
    if (fclose(stream) != 0) failed = 1;
    ((void (*)(void *))singleton.drop_scan_vec)(result + 8);
    if (failed) {
        free(*json);
        *json = NULL;
        message(err, errlen, "Could not render discovery JSON");
        return -6;
    }
    return 0;
}

int wuji_manager_probe_current_user(void *handle, char **user, char *err, size_t errlen) {
    if (handle != &singleton || !user) return -1;
    *user = NULL;
    void *fn = dlsym(singleton.library, "_RNvNtCs61nGhHav3cI_8wuji_sdk4user15current_user_id");
    if (!fn) return -3;
    _Alignas(16) unsigned char result[128] = {0};
    wuji_manager_call_sret1(fn, NULL, result);
    uint64_t *words = (uint64_t *)result;
    if (words[0] != UINT64_MAX) return native_error(result, err, errlen);
    *user = strndup((const char *)(uintptr_t)words[2], words[3]);
    ((void (*)(void *))(singleton.image_base + 0x1c10c8))(result + 8);
    return *user ? 0 : -6;
}

int wuji_manager_current_user_id(void *handle, char **user, char *err, size_t errlen) {
    message(err, errlen, "");
    return wuji_manager_probe_current_user(handle, user, err, errlen);
}

static int user_info_json(void *info, char **json, char *err, size_t errlen) {
    *json = NULL;
    void *serialize = dlsym(singleton.library,
        "_RINvXNvNtCs61nGhHav3cI_8wuji_sdk4user1__NtB5_8UserInfoNtNtCsZDvtF3NvIf_10serde_core3ser9Serialize9serializeNtNtNtCsbfgWHvRAF2V_10serde_json5value3ser10SerializerEB7_");
    void *display = dlsym(singleton.library,
        "_RNvXs_NtCsbfgWHvRAF2V_10serde_json5valueNtB4_5ValueNtNtCs8Mbv00yxnRz_4core3fmt7Display3fmt");
    if (!serialize || !display) return -3;
    _Alignas(16) unsigned char value[64] = {0};
    wuji_manager_call_sret1(serialize, info, value);
    if (value[0] == 255) {
        void *display_error = (void *)(singleton.image_base + 0x59b5f8);
        size_t needed;
        wuji_format_display(singleton.library, display_error, value + 8, err, errlen, &needed);
        ((void (*)(void *))(singleton.image_base + 0x59c7d4))(value + 8);
        return 1;
    }
    if (value[0] > 5) { message(err, errlen, "UserInfo JSON Value layout failed validation"); return -5; }
    size_t needed = 0;
    int status = wuji_format_display(singleton.library, display, value, NULL, 0, &needed);
    if (status >= 0 && needed < 16 * 1024 * 1024) {
        *json = malloc(needed);
        status = *json ? wuji_format_display(singleton.library, display, value, *json, needed, &needed) : -6;
    } else status = -5;
    ((void (*)(void *))(singleton.image_base + 0x594eec))(value);
    if (status) { free(*json); *json = NULL; message(err, errlen, "Could not serialize UserInfo JSON"); }
    return status;
}

static int fetch_user_json(const char *id, char **json, char *err, size_t errlen) {
    void *get_user = dlsym(singleton.library, "_RNvNtCs61nGhHav3cI_8wuji_sdk4user8get_user");
    if (!get_user) return -3;
    _Alignas(16) unsigned char result[256] = {0};
    wuji_manager_call_sret2(get_user, id, strlen(id), result);
    uint64_t tag;
    memcpy(&tag, result, sizeof(tag));
    if (tag == UINT64_MAX) return native_error(result + 8, err, errlen);
    int status = user_info_json(result, json, err, errlen);
    ((void (*)(void *))(singleton.image_base + 0xc1ecec))(result);
    return status;
}

int wuji_manager_current_user_json(void *handle, char **json, char *err, size_t errlen) {
    message(err, errlen, "");
    if (handle != &singleton || !json) return -1;
    *json = NULL;
    char *id = NULL;
    int status = wuji_manager_current_user_id(handle, &id, err, errlen);
    if (status) return status;
    status = fetch_user_json(id, json, err, errlen);
    free(id);
    return status;
}

int wuji_manager_switch_user_json(void *handle, const char *id, char **json, char *err, size_t errlen) {
    message(err, errlen, "");
    if (handle != &singleton || !id || !json) return -1;
    *json = NULL;
    void *fn = dlsym(singleton.library, "_RNvMNtCs61nGhHav3cI_8wuji_sdk7managerNtB2_10SdkManager11switch_user");
    if (!fn) return -3;
    _Alignas(16) unsigned char result[256] = {0};
    wuji_manager_call_sret3(fn, singleton.instance, id, strlen(id), result);
    uint64_t tag;
    memcpy(&tag, result, sizeof(tag));
    if (tag == UINT64_MAX) return native_error(result + 8, err, errlen);
    int status = user_info_json(result, json, err, errlen);
    ((void (*)(void *))(singleton.image_base + 0xc1ecec))(result);
    return status;
}

int wuji_manager_list_users_json(void *handle, char **json, char *err, size_t errlen) {
    message(err, errlen, "");
    if (handle != &singleton || !json) return -1;
    *json = NULL;
    void (*drop_vec)(void *) = dlsym(singleton.library,
        "_RNvXso_NtCshxvaOLs88l5_5alloc3vecINtB5_3VecNtNtCs61nGhHav3cI_8wuji_sdk4user8UserInfoENtNtNtCs8Mbv00yxnRz_4core3ops4drop4Drop4dropBJ_");
    if (!drop_vec) { message(err, errlen, "Native UserInfo destructor is unavailable"); return -3; }
    _Alignas(16) unsigned char result[128] = {0};
    wuji_manager_call_sret1((void *)(singleton.image_base + 0xc2b2c4), NULL, result);
    uint64_t *words = (uint64_t *)result;
    if (words[0] != UINT64_MAX) return native_error(result, err, errlen);
    if (words[6] > words[4] || words[6] > 1000000) {
        message(err, errlen, "Native user-registry Vec layout failed validation");
        return -5;
    }
    size_t length = 0;
    FILE *stream = open_memstream(json, &length);
    int status = stream ? 0 : -6;
    if (stream) {
        fputc('[', stream);
        for (size_t i = 0; i < words[6]; ++i) {
            char *item = NULL;
            status = user_info_json((unsigned char *)(uintptr_t)words[5] + 152 * i, &item, err, errlen);
            if (status) break;
            if (i) fputc(',', stream);
            fputs(item, stream);
            free(item);
        }
        fputc(']', stream);
        if (ferror(stream)) status = -6;
        if (fclose(stream)) status = -6;
    }
    ((void (*)(void *))(singleton.image_base + 0x1c10c8))(result + 8);
    drop_vec(result + 32);
    if (status) { free(*json); *json = NULL; }
    return status;
}

/* The CLI retains native user::create/update bodies without dynamic names.
 * Their scalar Rust ABI and complete persistence/validation paths match the
 * pinned Linux C wrapper's create/update targets (0x23af24/0x23bec0), with the
 * manager receiver removed. Mac bodies are0xad3ab4/0xc29e54 respectively.
 * Both return bare UserInfo152 on success, cap=-1 + SdkError@8 on error.
 */
static int lock_user_operation(unsigned char *guard, char *err, size_t errlen) {
    wuji_manager_call_sret1((void *)(singleton.image_base + 0xc74ffc), singleton.instance, guard);
    uint64_t tag;
    memcpy(&tag, guard, sizeof(tag));
    return tag == UINT64_MAX ? 0 : native_error(guard, err, errlen);
}

static int finish_user_operation(unsigned char *guard, unsigned char *result,
                                  char **json, char *err, size_t errlen) {
    /* The exact native MutexGuard<()> drop called by manager::delete_user. */
    ((void (*)(void *))(singleton.image_base + 0xc6dafc))(guard + 8);
    uint64_t tag;
    memcpy(&tag, result, sizeof(tag));
    if (tag == UINT64_MAX) return native_error(result + 8, err, errlen);
    int status = user_info_json(result, json, err, errlen);
    ((void (*)(void *))(singleton.image_base + 0xc1ecec))(result);
    return status;
}

int wuji_manager_create_user_json(void *handle, const char *name, const char *description,
                                  const char *external_id, char **json, char *err, size_t errlen) {
    message(err, errlen, "");
    if (json) *json = NULL;
    if (handle != &singleton || !singleton.instance || !name || !json) return -1;
    _Alignas(16) unsigned char guard[64] = {0}, result[160] = {0};
    int status = lock_user_operation(guard, err, errlen);
    if (status) return status;
    wuji_manager_call_sret6((void *)(singleton.image_base + 0xc29600),
        (uintptr_t)name, strlen(name), (uintptr_t)external_id, external_id ? strlen(external_id) : 0,
        (uintptr_t)description, description ? strlen(description) : 0, result);
    return finish_user_operation(guard, result, json, err, errlen);
}

int wuji_manager_update_user_json(void *handle, const char *id, const char *name,
                                  const char *description, const char *external_id,
                                  unsigned clear_fields, char **json, char *err, size_t errlen) {
    message(err, errlen, "");
    if (json) *json = NULL;
    if (handle != &singleton || !singleton.instance || !id || !json || clear_fields > 3 ||
        (description && (clear_fields & 1)) || (external_id && (clear_fields & 2))) {
        message(err, errlen, "Invalid user update; a field cannot be set and cleared together");
        return -1;
    }
    /* Option<Option<&str>>: tag0 unchanged; tag1 with null pointer clears. */
    uint64_t description_change[3] = {description || (clear_fields & 1) ? 1 : 0,
        (uintptr_t)description, description ? strlen(description) : 0};
    uint64_t external_change[3] = {external_id || (clear_fields & 2) ? 1 : 0,
        (uintptr_t)external_id, external_id ? strlen(external_id) : 0};
    _Alignas(16) unsigned char guard[64] = {0}, result[160] = {0};
    int status = lock_user_operation(guard, err, errlen);
    if (status) return status;
    wuji_manager_call_sret6((void *)(singleton.image_base + 0xc29e54),
        (uintptr_t)id, strlen(id), (uintptr_t)name, name ? strlen(name) : 0,
        (uintptr_t)external_change, (uintptr_t)description_change, result);
    return finish_user_operation(guard, result, json, err, errlen);
}

int wuji_manager_delete_user(void *handle, const char *id, char *err, size_t errlen) {
    message(err, errlen, "");
    if (handle != &singleton || !singleton.instance || !id) return -1;
    _Alignas(16) unsigned char result[64] = {0};
    wuji_manager_call_sret3((void *)(singleton.image_base + 0xc7400c), singleton.instance, id, strlen(id), result);
    uint64_t tag;
    memcpy(&tag, result, sizeof(tag));
    return tag == UINT64_MAX ? 0 : native_error(result, err, errlen);
}

int wuji_manager_tactile_paths_json(void *handle, const char *kind, const char *id,
                                    const char *serial, char **json, char *err, size_t errlen) {
    message(err, errlen, "");
    if (json) *json = NULL;
    if (handle != &singleton || !singleton.instance || !kind || !id || !serial || !json) return -1;
    uintptr_t offset;
    int models = 0;
    if (!strcmp(kind, "tactile_dir")) offset = 0xc29d7c;
    else if (!strcmp(kind, "tactile_run_dir")) offset = 0xc2a878;
    else if (!strcmp(kind, "tactile_model_paths")) { offset = 0xc2ac9c; models = 1; }
    else { message(err, errlen, "Unknown native tactile path resolver"); return -1; }
    _Alignas(16) unsigned char result[96] = {0};
    wuji_manager_call_sret4((void *)(singleton.image_base + offset),
        (uintptr_t)id, strlen(id), (uintptr_t)serial, strlen(serial), result);
    uint64_t tag;
    memcpy(&tag, result, sizeof(tag));
    if ((models && tag == UINT64_MAX) || (!models && tag != UINT64_MAX))
        return native_error(result + (models ? 8 : 0), err, errlen);
    unsigned char *paths = result + (models ? 0 : 8);
    size_t length = 0;
    FILE *stream = open_memstream(json, &length);
    int status = stream ? 0 : -6;
    if (stream) {
        if (models) fputc('{', stream);
        const char *keys[3] = {"dir", "safetensors", "npz"};
        for (size_t i = 0; i < (models ? 3u : 1u); ++i) {
            const uint64_t *path = (const uint64_t *)(paths + 24 * i);
            if (models) fprintf(stream, "%s\"%s\":", i ? "," : "", keys[i]);
            json_string(stream, (const unsigned char *)(uintptr_t)path[1], path[2]);
        }
        if (models) fputc('}', stream);
        if (ferror(stream)) status = -6;
        if (fclose(stream)) status = -6;
    }
    for (size_t i = 0; i < (models ? 3u : 1u); ++i)
        ((void (*)(void *))(singleton.image_base + 0x1c10c8))(paths + 24 * i);
    if (status) { free(*json); *json = NULL; message(err, errlen, "Could not allocate tactile path JSON"); }
    return status;
}

int wuji_manager_probe_connect(void *handle, unsigned variant, const char *target,
                                const char *name, unsigned char *raw,
                                size_t rawlen, char *err, size_t errlen) {
    if (handle != &singleton || variant > 1 || !target || !name || !raw || rawlen < 64) return -1;
    _Alignas(16) uint64_t native_target[8] = {0};
    _Alignas(16) uint64_t native_options[8] = {2};
    _Alignas(16) unsigned char result[512] = {0};
    native_target[0] = UINT64_C(0x8000000000000000) + variant;
    wuji_manager_call_sret2((void *)(singleton.image_base + 0xc731e8), target, strlen(target), native_target + 1);
    wuji_manager_call_connect(singleton.connect, singleton.instance,
                               native_target, name, strlen(name), native_options, result);
    memcpy(raw, result, 64);
    uint64_t tag;
    memcpy(&tag, result, sizeof(tag));
    if (tag != 0) return native_error(result + 8, err, errlen);
    ((void (*)(void *))(singleton.image_base + 0xbc6438))(result + 8);
    char cleanup_error[1024];
    int cleanup = wuji_manager_disconnect(handle, name, cleanup_error, sizeof(cleanup_error));
    if (cleanup) { message(err, errlen, cleanup_error); return cleanup; }
    message(err, errlen, "Native connect succeeded; diagnostic probe disconnected and released it");
    return 0;
}

/* Diagnostic-only state-write exercise; native result is dropped after copy. */
int wuji_manager_probe_switch_default(void *handle, unsigned char *raw, size_t rawlen) {
    if (handle != &singleton || !raw || rawlen < 128) return -1;
    void *fn = dlsym(singleton.library, "_RNvMNtCs61nGhHav3cI_8wuji_sdk7managerNtB2_10SdkManager11switch_user");
    if (!fn) return -3;
    _Alignas(16) unsigned char result[512] = {0};
    wuji_manager_call_sret3(fn, singleton.instance, "", 0, result);
    memcpy(raw, result, 128);
    uint64_t tag;
    memcpy(&tag, result, sizeof(tag));
    if (tag == UINT64_MAX) {
        ((void (*)(void *))(singleton.image_base + 0xb9c1f0))(result + 8);
        return 1;
    }
    ((void (*)(void *))(singleton.image_base + 0xc1ecec))(result);
    return 0;
}

static void skip_space(const char **p) {
    while (isspace((unsigned char)**p)) ++*p;
}

/* Deliberately narrow parser for the public scalar ConnectOptions fields. A
 * malformed or unknown option is an error; it is never silently ignored.
 */
static int connection_options(const char *json, unsigned char *out, char *err, size_t errlen) {
    memset(out, 0, 64);
    uint64_t *words = (uint64_t *)out;
    if (!json) { words[0] = 2; return 0; }
    uint64_t timeout = 1000, retries = 3, interval = 30000;
    int bridge = 1, has_interval = 1;
    const char *p = json;
    skip_space(&p);
    if (!strncmp(p, "null", 4)) {
        p += 4;
        skip_space(&p);
        if (*p) goto invalid;
        words[0] = 2;
        return 0;
    }
    if (*p++ != '{') goto invalid;
    unsigned seen = 0;
    skip_space(&p);
    while (*p != '}') {
        if (*p++ != '"') goto invalid;
        const char *start = p;
        while (*p && *p != '"' && *p != '\\') ++p;
        if (*p != '"' || p - start >= 64) goto invalid;
        char key[64];
        memcpy(key, start, (size_t)(p - start));
        key[p - start] = 0;
        ++p;
        skip_space(&p);
        if (*p++ != ':') goto invalid;
        skip_space(&p);
        unsigned bit = !strcmp(key, "timeout_ms") ? 1 : !strcmp(key, "retry_count") ? 2 : !strcmp(key, "enable_bridge") ? 4 : !strcmp(key, "auto_time_sync_interval_ms") ? 8 : 0;
        if (!bit || (seen & bit)) goto invalid;
        seen |= bit;
        if (bit == 4) {
            if (!strncmp(p, "true", 4)) { bridge = 1; p += 4; }
            else if (!strncmp(p, "false", 5)) { bridge = 0; p += 5; }
            else goto invalid;
        } else if (bit == 8 && !strncmp(p, "null", 4)) {
            has_interval = 0;
            p += 4;
        } else {
            if (!isdigit((unsigned char)*p)) goto invalid;
            char *end;
            errno = 0;
            unsigned long long value = strtoull(p, &end, 10);
            if (errno || end == p || (*p == '0' && end - p != 1)) goto invalid;
            p = end;
            if (bit == 1) timeout = value;
            else if (bit == 2) retries = value;
            else interval = value;
        }
        skip_space(&p);
        if (*p == '}') break;
        if (*p++ != ',') goto invalid;
        skip_space(&p);
        if (*p == '}') goto invalid;
    }
    if (*p++ != '}') goto invalid;
    skip_space(&p);
    if (*p || timeout > UINT16_MAX || retries > UINT16_MAX) goto invalid;
    words[0] = has_interval ? 1 : 0;
    words[1] = interval;
    words[2] = UINT64_MAX; // Optional SDK profile string is None.
    /* Unexposed fields keep SdkManager::connect's defaults (CLI 2026.9.22):
     * +47 and the new +48 flag are 0 and fold into one three-state mode; the
     * capability query budget is auxiliary_timeout x attempts (+44 x +49). */
    uint16_t timeout_short = (uint16_t)timeout, retries_short = (uint16_t)retries, auxiliary_timeout = 1000;
    memcpy(out + 40, &timeout_short, 2);
    memcpy(out + 42, &retries_short, 2);
    memcpy(out + 44, &auxiliary_timeout, 2);
    out[46] = (unsigned char)bridge;
    out[47] = 0;
    out[48] = 0;
    out[49] = 3;
    return 0;
invalid:
    message(err, errlen, "Invalid ConnectOptions JSON; supported fields are timeout_ms/retry_count (0..65535), enable_bridge (bool), auto_time_sync_interval_ms (nonnegative int or null)");
    return -1;
}

int wuji_manager_connect(void *handle, const char *kind, const char *target,
                         const char *name, const char *options_json, void **device,
                         char **metadata, char *err, size_t errlen) {
    message(err, errlen, "");
    if (handle != &singleton || !singleton.instance || !kind || !target || !name || !device || !metadata) return -1;
    *device = NULL;
    *metadata = NULL;
    unsigned variant;
    if (!strcmp(kind, "sn")) variant = 0;
    else if (!strcmp(kind, "address")) variant = 1;
    else if (!strcmp(kind, "handedness") && (!strcmp(target, "Left") || !strcmp(target, "Right"))) variant = 3;
    else { message(err, errlen, "Expected target_kind sn/address/handedness and valid target"); return -1; }
    _Alignas(16) uint64_t native_target[8] = {0};
    _Alignas(16) unsigned char native_options[64] = {0};
    _Alignas(16) unsigned char result[128] = {0};
    if (connection_options(options_json, native_options, err, errlen)) return -1;
    native_target[0] = UINT64_C(0x8000000000000000) + variant;
    if (variant == 3) ((unsigned char *)native_target)[8] = !strcmp(target, "Right");
    else wuji_manager_call_sret2((void *)(singleton.image_base + 0xc731e8), target, strlen(target), native_target + 1);
    wuji_manager_call_connect(singleton.connect, singleton.instance, native_target,
                               name, strlen(name), native_options, result);
    uint64_t tag;
    memcpy(&tag, result, sizeof(tag));
    if (tag == 1) return native_error(result + 8, err, errlen);
    if (tag != 0) { message(err, errlen, "Native connect Result layout failed validation"); return -5; }
    DeviceHandle *connected = calloc(1, sizeof(*connected));
    if (!connected) {
        char cleanup_error[1024];
        wuji_manager_disconnect(handle, name, cleanup_error, sizeof(cleanup_error));
        ((void (*)(void *))(singleton.image_base + 0xbc6438))(result + 8);
        message(err, errlen, "Could not allocate device handle");
        return -6;
    }
    memcpy(connected->native, result + 8, sizeof(connected->native));
    connected->name = strdup(name);
    unsigned id = ((unsigned char (*)(void *))singleton.device_id)(connected->native);
    const char *type = id == 1 ? "WujiGlove" : id == 2 ? "WujiHand2" : id == 3 ? "WujiHand" : "Unknown";
    size_t size = 0;
    FILE *stream = open_memstream(metadata, &size);
    if (stream && connected->name) {
        fputs("{\"device_name\":", stream);
        json_string(stream, (const unsigned char *)name, strlen(name));
        fprintf(stream, ",\"device_type\":\"%s\",\"sn\":", type);
        /* Client DeviceInner stores serial String at0x2b8, as used by upstream
         * WujiDevice::disconnect. The handshake populates it on connect. */
        uintptr_t client;
        memcpy(&client, connected->native + 24, sizeof(client));
        const uint64_t *serial = (const uint64_t *)(client + 0x2b8);
        json_string(stream, (const unsigned char *)(uintptr_t)serial[1], serial[2]);
        fputs(",\"address\":", stream);
        if (variant == 1) json_string(stream, (const unsigned char *)target, strlen(target));
        else fputs("null", stream);
        fputs("}", stream);
    }
    int failed = !stream || !connected->name || (stream && ferror(stream));
    if (stream && fclose(stream)) failed = 1;
    if (failed) {
        char cleanup_error[1024];
        wuji_manager_disconnect(handle, name, cleanup_error, sizeof(cleanup_error));
        ((void (*)(void *))(singleton.image_base + 0xbc6438))(connected->native);
        free(connected->name);
        free(connected);
        free(*metadata);
        *metadata = NULL;
        message(err, errlen, "Could not allocate connected-device metadata");
        return -6;
    }
    pthread_mutex_lock(&singleton_mutex);
    connected->next = singleton.devices;
    singleton.devices = connected;
    pthread_mutex_unlock(&singleton_mutex);
    *device = connected;
    return 0;
}

int wuji_manager_disconnect(void *handle, const char *name, char *err, size_t errlen) {
    message(err, errlen, "");
    if (handle != &singleton || !name) return -1;
    _Alignas(16) unsigned char result[128] = {0};
    wuji_manager_call_sret3(singleton.disconnect, singleton.instance, name, strlen(name), result);
    uint64_t tag;
    memcpy(&tag, result, sizeof(tag));
    if (tag != UINT64_MAX) return native_error(result, err, errlen);
    pthread_mutex_lock(&singleton_mutex);
    for (DeviceHandle *d = singleton.devices; d; d = d->next)
        if (!strcmp(d->name, name)) d->disconnected = 1;
    pthread_mutex_unlock(&singleton_mutex);
    return 0;
}

int wuji_manager_disconnect_all(void *handle, char *err, size_t errlen) {
    if (handle != &singleton) return -1;
    message(err, errlen, "");
    /* Tracking nodes live until process exit. Snapshot the head so a connection
     * created concurrently belongs to the next disconnect_all invocation. */
    pthread_mutex_lock(&singleton_mutex);
    DeviceHandle *head = singleton.devices;
    pthread_mutex_unlock(&singleton_mutex);
    for (DeviceHandle *d = head; d; d = d->next) {
        pthread_mutex_lock(&singleton_mutex);
        int disconnected = d->disconnected;
        pthread_mutex_unlock(&singleton_mutex);
        if (disconnected) continue;
        int code = wuji_manager_disconnect(handle, d->name, err, errlen);
        if (code) return code;
    }
    return 0;
}

void wuji_manager_release_device(void *pointer) {
    pthread_mutex_lock(&singleton_mutex);
    DeviceHandle **slot = &singleton.devices;
    while (*slot && *slot != pointer) slot = &(*slot)->next;
    if (!*slot) { pthread_mutex_unlock(&singleton_mutex); return; }
    DeviceHandle *device = *slot;
    if (device->released) { pthread_mutex_unlock(&singleton_mutex); return; }
    device->released = 1;
    pthread_mutex_unlock(&singleton_mutex);
    ((void (*)(void *))(singleton.image_base + 0xbc6438))(device->native);
    memset(device->native, 0, sizeof(device->native));
    /* Keep name tracking until process exit: SDK manager still owns a clone,
     * and disconnect_all must release it even after all Python refs are gone. */
}

/* Diagnostic entry for bounded offline characterization. The result is copied
 * before its success Vec is dropped. Errors currently use the matching SDK
 * internal SdkError destructor after copying the raw value for inspection.
 */
int wuji_manager_probe_scan(void *handle, unsigned char *raw, size_t capacity,
                            char *err, size_t errlen) {
    if (handle != &singleton || !singleton.instance || !raw || capacity < 64) return -1;
    _Alignas(16) unsigned char result[256] = {0};
    wuji_manager_call_sret1(singleton.scan, singleton.instance, result);
    memcpy(raw, result, 64);
    uint64_t tag;
    memcpy(&tag, result, sizeof(tag));
    if (tag == UINT64_MAX) {
        ((void (*)(void *))singleton.drop_scan_vec)(result + 8);
        message(err, errlen, "");
        return 0;
    }
    return native_error(result, err, errlen);
}

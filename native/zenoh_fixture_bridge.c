/* Test-only adapter for the loopback Zenoh peer. Never linked into wheels.
 * Public SDK address targets normally need a discovery record to select
 * Zenoh. This fixture names the exact native Zenoh connect method directly,
 * restricting the endpoint to loopback. Its runner denies non-loopback
 * outbound traffic. The production device/publisher code is unchanged. */
#define wuji_manager_connect wuji_manager_connect_production
#define wuji_manager_disconnect wuji_manager_disconnect_production
#define wuji_manager_disconnect_all wuji_manager_disconnect_all_production
#include "manager_bridge.c"
#undef wuji_manager_connect
#undef wuji_manager_disconnect
#undef wuji_manager_disconnect_all

typedef struct { uint64_t words[8]; } FixtureNativeResult;

static int fixture_client_error(void *result, char *error, size_t capacity) {
    wuji_format_display(singleton.library, (void *)(singleton.image_base + 0x97ed6cu),
                         result, error, capacity, NULL);
    ((void (*)(void *))(singleton.image_base + 0x9b9798u))(result);
    return 1;
}

int wuji_manager_disconnect(void *handle, const char *name, char *error, size_t capacity) {
    message(error, capacity, "");
    if (handle != &singleton || !name) return -1;
    uintptr_t low_manager;
    memcpy(&low_manager, singleton.instance, 8);
    _Alignas(16) unsigned char result[64] = {0};
    wuji_manager_call_sret4((void *)(singleton.image_base + 0x9b1b0cu), low_manager,
                             (uintptr_t)name, strlen(name), 0, result);
    uint64_t tag;
    memcpy(&tag, result, 8);
    if (tag != UINT64_MAX) return fixture_client_error(result, error, capacity);
    for (DeviceHandle *item = singleton.devices; item; item = item->next)
        if (!strcmp(item->name, name)) item->disconnected = 1;
    return 0;
}

int wuji_manager_disconnect_all(void *handle, char *error, size_t capacity) {
    if (handle != &singleton) return -1;
    int status = 0;
    for (DeviceHandle *item = singleton.devices; item; item = item->next)
        if (!item->disconnected) {
            int current = wuji_manager_disconnect(handle, item->name, error, capacity);
            if (current) status = current;
        }
    return status;
}

int wuji_manager_connect(void *handle, const char *kind, const char *target,
                         const char *name, const char *options_json, void **device,
                         char **metadata, char *error, size_t capacity) {
    (void)options_json;
    message(error, capacity, "");
    if (handle != &singleton || !singleton.instance || !kind || strcmp(kind, "address") ||
        !target || !name || !device || !metadata ||
        (strncmp(target, "tcp/127.0.0.1:", 14) && strncmp(target, "udp/127.0.0.1:", 14))) {
        message(error, capacity, "Zenoh fixture accepts only an explicit loopback endpoint"); return -1;
    }
    *device = NULL;
    *metadata = NULL;
    const char *serial = "MOCKZENOHAND2ABC";
    /* The pinned SDK adds WUJI_ZENOH_PROTO to the address itself. The test
     * runner sets that variable to match its peer and supplies the explicit
     * scheme here solely for validating loopback scope. */
    const char *native_endpoint = target + 4;
    /* Allocate the C-owned pieces before native connection. Once the SDK
     * returns a client Arc, every remaining path must consume and drop it. */
    DeviceHandle *connected = calloc(1, sizeof(*connected));
    if (!connected) return -6;
    connected->name = strdup(name);
    if (!connected->name) { free(connected); return -6; }
    size_t size = 0;
    FILE *stream = open_memstream(metadata, &size);
    if (!stream) { free(connected->name); free(connected); return -6; }
    uintptr_t low_manager;
    memcpy(&low_manager, singleton.instance, 8);
    /* The SdkManager::connect call at 0xc77960 establishes name, serial and endpoint slice pairs,
     * bridge flag, optional connection config pair, and discovery flag.
     * The aggregate uses x8; the three final arguments are stack values. */
    typedef FixtureNativeResult (*Connect)(uintptr_t, const char *, size_t,
        const char *, size_t, const char *, size_t, uintptr_t, uintptr_t, uintptr_t, uint8_t);
    FixtureNativeResult result = ((Connect)(singleton.image_base + 0x9b1160u))
        (low_manager, name, strlen(name), serial, strlen(serial), native_endpoint,
         strlen(native_endpoint), 0, 0, 0, 1);
    if (result.words[0] != UINT64_MAX) {
        fclose(stream);
        free(*metadata); *metadata = NULL;
        free(connected->name); free(connected);
        return fixture_client_error(&result, error, capacity);
    }
    /* Native constructor consumes the returned client Arc and supplies both
     * real SDK stream/virtual-hub Arcs needed by production device cloning. */
    wuji_manager_call_sret2((void *)(singleton.image_base + 0xb9e5b4u),
                             (void *)result.words[1], 0, connected->native);
    unsigned id = ((unsigned char (*)(void *))singleton.device_id)(connected->native);
    fputs("{\"device_name\":", stream);
    json_string(stream, (const unsigned char *)name, strlen(name));
    fprintf(stream, ",\"device_type\":\"%s\",\"sn\":", id == 2 ? "WujiHand2" : "Unknown");
    json_string(stream, (const unsigned char *)serial, strlen(serial));
    fputs(",\"address\":", stream);
    json_string(stream, (const unsigned char *)target, strlen(target));
    fputs("}", stream);
    if (fclose(stream)) {
        char ignored_error[512];
        wuji_manager_disconnect(handle, name, ignored_error, sizeof(ignored_error));
        ((void (*)(void *))(singleton.image_base + 0xbc6438u))(connected->native);
        free(*metadata); *metadata = NULL;
        free(connected->name); free(connected);
        message(error, capacity, "could not allocate fixture metadata");
        return -6;
    }
    connected->next = singleton.devices;
    singleton.devices = connected;
    *device = connected;
    return 0;
}

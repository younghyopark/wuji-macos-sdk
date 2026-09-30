#ifndef WUJI_MACOS_MANAGER_BRIDGE_H
#define WUJI_MACOS_MANAGER_BRIDGE_H
#include <stddef.h>

/* Stable bridge-owned ABI. Strings and handles never expose Rust containers.
 * Return 0 for success, positive for native SDK errors, negative for bridge
 * argument/load/ABI errors. Error messages are NUL terminated when errlen > 0.
 */
int wuji_manager_open(const char *library_path, const char *state_root,
                      void **manager, char *err, size_t errlen);
/* Sets the native log/tracing maximum filters without initializing a manager.
 * The CLI's output subscriber is not installed by this operation. */
int wuji_set_log_level(const char *library_path, const char *level, char *err, size_t errlen);
int wuji_manager_scan_json(void *manager, char **json, char *err, size_t errlen);
int wuji_manager_connect(void *manager, const char *target_kind,
                         const char *target_value, const char *device_name,
                         const char *options_json, void **device,
                         char **metadata_json, char *err, size_t errlen);
int wuji_manager_disconnect_all(void *manager, char *err, size_t errlen);
int wuji_manager_disconnect(void *manager, const char *device_name, char *err, size_t errlen);
void wuji_manager_release_device(void *device);
int wuji_manager_current_user_id(void *manager, char **user_id, char *err, size_t errlen);
int wuji_manager_current_user_json(void *manager, char **json, char *err, size_t errlen);
int wuji_manager_list_users_json(void *manager, char **json, char *err, size_t errlen);
int wuji_manager_switch_user_json(void *manager, const char *user_id, char **json, char *err, size_t errlen);
int wuji_manager_create_user_json(void *manager, const char *display_name,
    const char *description, const char *external_id, char **json, char *err, size_t errlen);
/* NULL fields are unchanged; clear_fields value1 clears description, value2 clears
 * external_id. Supplying and clearing the same field is an argument error. */
int wuji_manager_update_user_json(void *manager, const char *user_id,
    const char *display_name, const char *description, const char *external_id,
    unsigned clear_fields, char **json, char *err, size_t errlen);
int wuji_manager_delete_user(void *manager, const char *user_id, char *err, size_t errlen);
/* kind: tactile_dir, tactile_run_dir, tactile_model_paths. JSON is a string
 * for directory methods and an object {dir,safetensors,npz} for model paths. */
int wuji_manager_tactile_paths_json(void *manager, const char *kind,
    const char *user_id, const char *device_sn, char **json, char *err, size_t errlen);
void wuji_manager_free_string(char *value);

#endif

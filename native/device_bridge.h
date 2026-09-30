#ifndef WUJI_DEVICE_BRIDGE_H
#define WUJI_DEVICE_BRIDGE_H
#include <stddef.h>
#include <stdint.h>

/* The SDK's own Hand 2 joint-command publisher (schema 315 resources only). */
int wuji_device_publisher_open(const char *library_path, void *device, const char *path,
    void **publisher, char *error, size_t error_capacity);
int wuji_publisher_send(void *publisher, const unsigned char *payload, size_t length,
    char *error, size_t error_capacity);
/* Exactly60 floats: (position,velocity,effort) repeated for20 joints. */
int wuji_publisher_send_joint_commands(void *publisher, const float *commands, size_t count,
    char *error, size_t error_capacity);
int wuji_publisher_stream_id(void *publisher, uint16_t *stream_id);
/* Closes (UDP cancellation) and frees the publisher; always succeeds. */
int wuji_publisher_close(void *publisher, char *error, size_t error_capacity);

int wuji_device_schema_json(const char *library_path, uint16_t schema_id,
    char *output, size_t capacity, size_t *needed, char *error, size_t error_capacity);
int wuji_device_codec(const char *library_path, uint16_t schema_id,
    int input_format, int output_format, const unsigned char *input, size_t input_length,
    unsigned char *output, size_t capacity, size_t *written, char *error, size_t error_capacity);
int wuji_device_joint_command_encode(const char *library_path, uint32_t sequence,
    const float *commands, size_t command_values, int output_format,
    unsigned char *output, size_t capacity, size_t *written, char *error, size_t error_capacity);
int wuji_device_request_raw(const char *library_path, void *device, int operation,
    const char *path, const unsigned char *input, size_t input_length,
    unsigned char **output, size_t *output_length, char *error, size_t error_capacity);
void wuji_device_buffer_free(void *buffer);
int wuji_device_resources_json(const char *library_path, void *device, int include_hidden,
    char *output, size_t capacity, size_t *needed, char *error, size_t error_capacity);
int wuji_device_is_connected(const char *library_path, void *device, int *connected,
    char *error, size_t error_capacity);
int wuji_device_info_json(const char *library_path, void *device,
    char *output, size_t capacity, size_t *needed, char *error, size_t error_capacity);
int wuji_device_nodes_json(const char *library_path, void *device,
    char *output, size_t capacity, size_t *needed, char *error, size_t error_capacity);
int wuji_device_is_virtual(const char *library_path, void *device, const char *path,
    int *is_virtual, char *error, size_t error_capacity);
int wuji_hand2_handedness(const char *library_path, void *device, int *handedness,
    char *error, size_t error_capacity);
/* Single native chunk transaction; free JSON using wuji_device_buffer_free. */
int wuji_hand2_fingertip_info_json(const char *library_path, void *device, uint8_t finger,
    char **output, char *error, size_t error_capacity);
int wuji_device_subscribe(const char *library_path, void *device, const char *path,
    void **subscription, char *error, size_t error_capacity);
/* 0 frame (free output),1 empty,2 closed,4 lagged,negative bridge error. */
int wuji_subscription_try_recv(void *subscription, unsigned char **output, size_t *length,
    uint64_t *lagged, char *error, size_t error_capacity);
void wuji_subscription_close(void *subscription);
int wuji_subscription_stream_id(void *subscription, uint16_t *stream_id);
int wuji_subscription_set_rate(void *subscription, uint16_t frequency_hz, uint16_t *actual_hz,
    char *error, size_t error_capacity);
#endif

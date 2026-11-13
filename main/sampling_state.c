/**
 * 采样状态记录实现（第 10 周）
 *
 * 主任务写、命令处理任务读，使用临界区保护一个很小的状态结构。
 * 所有字符串都是静态字面量，读取时不需要额外分配内存。
 */

#include "sampling_state.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

typedef enum {
    IMU_ERR_NONE = 0,
    IMU_ERR_READ,
    IMU_ERR_TIMEOUT,
    IMU_ERR_CAMERA_BUSY,
    IMU_ERR_OTHER,
} imu_error_t;

static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static sampling_attempt_t s_attempt = SAMPLING_ATTEMPT_UNKNOWN;
static imu_error_t s_error = IMU_ERR_NONE;
static uint32_t s_fail_count = 0;
static char s_boot_id[9] = "00000000";
static bool s_inited = false;

void sampling_state_init(void)
{
    if (s_inited) {
        return;
    }
    /* 8 位十六进制足够区分同一设备的重启；不做持久化，重启即换新值。 */
    snprintf(s_boot_id, sizeof(s_boot_id), "%08lx",
             (unsigned long)esp_random());
    s_attempt = SAMPLING_ATTEMPT_UNKNOWN;
    s_error = IMU_ERR_NONE;
    s_fail_count = 0;
    s_inited = true;
}

const char *sampling_state_boot_id(void)
{
    return s_boot_id;
}

void sampling_state_note_imu_ok(void)
{
    taskENTER_CRITICAL(&s_lock);
    s_attempt = SAMPLING_ATTEMPT_OK;
    s_error = IMU_ERR_NONE;
    taskEXIT_CRITICAL(&s_lock);
}

void sampling_state_note_imu_fail(esp_err_t err)
{
    taskENTER_CRITICAL(&s_lock);
    s_attempt = SAMPLING_ATTEMPT_FAIL;
    if (err == ESP_ERR_TIMEOUT) {
        s_error = IMU_ERR_TIMEOUT;
    } else {
        s_error = IMU_ERR_READ;
    }
    if (s_fail_count < UINT32_MAX) {
        s_fail_count++;
    }
    taskEXIT_CRITICAL(&s_lock);
}

void sampling_state_note_imu_busy(void)
{
    taskENTER_CRITICAL(&s_lock);
    s_attempt = SAMPLING_ATTEMPT_BUSY;
    s_error = IMU_ERR_CAMERA_BUSY;
    taskEXIT_CRITICAL(&s_lock);
}

sampling_attempt_t sampling_state_imu_attempt(void)
{
    sampling_attempt_t attempt;
    taskENTER_CRITICAL(&s_lock);
    attempt = s_attempt;
    taskEXIT_CRITICAL(&s_lock);
    return attempt;
}

const char *sampling_state_imu_attempt_code(void)
{
    switch (sampling_state_imu_attempt()) {
    case SAMPLING_ATTEMPT_OK:
        return "ok";
    case SAMPLING_ATTEMPT_FAIL:
        return "fail";
    case SAMPLING_ATTEMPT_BUSY:
        return "busy";
    default:
        return "unknown";
    }
}

const char *sampling_state_imu_error_code(void)
{
    imu_error_t error;
    taskENTER_CRITICAL(&s_lock);
    error = s_error;
    taskEXIT_CRITICAL(&s_lock);

    switch (error) {
    case IMU_ERR_READ:
        return "read";
    case IMU_ERR_TIMEOUT:
        return "timeout";
    case IMU_ERR_CAMERA_BUSY:
        return "camera_busy";
    case IMU_ERR_NONE:
        return "none";
    default:
        return "other";
    }
}

uint32_t sampling_state_imu_fail_count(void)
{
    uint32_t count;
    taskENTER_CRITICAL(&s_lock);
    count = s_fail_count;
    taskEXIT_CRITICAL(&s_lock);
    return count;
}

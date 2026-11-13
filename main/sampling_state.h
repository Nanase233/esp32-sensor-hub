/**
 * 采样状态记录（第 10 周）
 *
 * 目的：把"最近一次采样尝试"的结果与本次开机失败次数记录下来，
 * 供命令轮询的心跳上报给服务端。它只记录状态，不参与 I2C 读写，
 * 也不缓存原始加速度数据。
 *
 * 时间语义：
 *   - 观测时间由 main.c 在 qma7981_read_accel() 返回后取 esp_timer_get_time()
 *   - 发送时间由 http_uploader 在发请求前取
 *   - 二者都是开机单调时钟，不依赖设备对时
 */

#ifndef SAMPLING_STATE_H
#define SAMPLING_STATE_H

#include <stdint.h>
#include "esp_err.h"

typedef enum {
    SAMPLING_ATTEMPT_UNKNOWN = 0,  /* 尚未采样 */
    SAMPLING_ATTEMPT_OK,           /* 最近一次读取成功 */
    SAMPLING_ATTEMPT_FAIL,         /* 最近一次读取失败 */
    SAMPLING_ATTEMPT_BUSY,         /* 摄像头占用 I2C，主动暂停 */
} sampling_attempt_t;

/** 初始化：生成每次开机唯一的 boot_id（8 位十六进制）。 */
void sampling_state_init(void);

/** 本次开机的 boot_id；未初始化时返回 "00000000"。 */
const char *sampling_state_boot_id(void);

/** 记录一次成功的 IMU 读取。 */
void sampling_state_note_imu_ok(void);

/** 记录一次失败的 IMU 读取；err 用于区分超时/普通读失败。 */
void sampling_state_note_imu_fail(esp_err_t err);

/** 记录因摄像头占用而暂停采样（暂停不算失败）。 */
void sampling_state_note_imu_busy(void);

/** 最近一次尝试结果。 */
sampling_attempt_t sampling_state_imu_attempt(void);

/** 最近一次尝试的短代码：ok / fail / busy / unknown。 */
const char *sampling_state_imu_attempt_code(void);

/** 最近一次失败的短代码：none / read / timeout / camera_busy / other。 */
const char *sampling_state_imu_error_code(void);

/** 本次开机累计 IMU 读取失败次数（成功不清零）。 */
uint32_t sampling_state_imu_fail_count(void);

#endif /* SAMPLING_STATE_H */

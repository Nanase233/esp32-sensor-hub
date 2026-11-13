/**
 * HTTP 数据上传器
 *
 * 功能：将 IMU 数据通过 HTTP POST 上传到服务器。
 *
 * 第 10 周：额外携带观测/发送时间，服务端据此计算数据年龄。
 * timestamp 仍保留原语义（观测时的开机微秒），新增字段全部可选，
 * 旧服务端忽略它们也不会影响接收。
 */

#ifndef HTTP_UPLOADER_H
#define HTTP_UPLOADER_H

#include <stdint.h>
#include "esp_err.h"
#include "qma7981.h"

/* 服务器配置（根据实际情况修改） */
#define SERVER_BASE_URL     "http://10.1.41.179:5000"              /* 服务器基础地址 */
#define SERVER_URL          SERVER_BASE_URL "/api/imu"             /* IMU 数据 API 地址 */
#define HTTP_TIMEOUT_MS     5000   /* HTTP 请求超时时间 */

/**
 * 一次观测的时间元数据。
 *
 * observed_uptime_ms 与 timestamp（微秒）来自同一时刻，二者都可保留；
 * send_uptime_ms 在真正发起 HTTP 前取，用于计算"观测到发送"的滞留时间。
 */
typedef struct {
    uint64_t observed_uptime_ms;  /* 读取完成时的开机毫秒 */
    uint64_t send_uptime_ms;      /* HTTP 发出前的开机毫秒 */
    const char *boot_id;          /* 本次开机的 8 位十六进制 ID */
} imu_sample_meta_t;

/**
 * 上传 IMU 数据（周期上报，无 request_id）
 *
 * @param data IMU 三轴加速度数据
 * @param timestamp 观测时间戳（微秒，开机计时）
 * @return ESP_OK 上传成功，其他表示失败
 */
esp_err_t http_uploader_send_imu(const qma7981_data_t *data, uint64_t timestamp);

/**
 * 上传 IMU 数据 + 时间元数据（周期上报，无 request_id）
 *
 * @param data IMU 三轴加速度数据
 * @param timestamp 观测时间戳（微秒，开机计时）
 * @param meta 观测/发送时间；为 NULL 时退化为旧格式
 * @return ESP_OK 上传成功，其他表示失败
 */
esp_err_t http_uploader_send_imu_ex(const qma7981_data_t *data,
                                    uint64_t timestamp,
                                    const imu_sample_meta_t *meta);

/**
 * 上传 IMU 数据（携带 request_id，用于远程采集任务）
 *
 * @param data IMU 三轴加速度数据
 * @param timestamp 时间戳（微秒）
 * @param request_id 采集任务 ID（关联服务器任务）
 * @return ESP_OK 上传成功，其他表示失败
 */
esp_err_t http_uploader_send_imu_with_request(const qma7981_data_t *data,
                                               uint64_t timestamp,
                                               const char *request_id);

/**
 * 上传 IMU 数据 + 时间元数据（携带 request_id）
 *
 * @param data IMU 三轴加速度数据
 * @param timestamp 观测时间戳（微秒）
 * @param request_id 采集任务 ID
 * @param meta 观测/发送时间；为 NULL 时退化为旧格式
 * @return ESP_OK 上传成功，其他表示失败
 */
esp_err_t http_uploader_send_imu_with_request_ex(const qma7981_data_t *data,
                                                   uint64_t timestamp,
                                                   const char *request_id,
                                                   const imu_sample_meta_t *meta);

#endif /* HTTP_UPLOADER_H */

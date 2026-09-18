/**
 * HTTP 数据上传器
 *
 * 功能：将 IMU 数据通过 HTTP POST 上传到服务器
 */

#ifndef HTTP_UPLOADER_H
#define HTTP_UPLOADER_H

#include "esp_err.h"
#include "qma7981.h"

/* 服务器配置（根据实际情况修改） */
#define SERVER_URL          "http://10.1.41.179:5000/api/imu"  /* 服务器 API 地址 */
#define HTTP_TIMEOUT_MS     5000   /* HTTP 请求超时时间 */

/**
 * 上传 IMU 数据到服务器
 *
 * @param data IMU 三轴加速度数据
 * @param timestamp 时间戳（毫秒）
 * @return ESP_OK 上传成功，其他表示失败
 */
esp_err_t http_uploader_send_imu(const qma7981_data_t *data, uint64_t timestamp);

#endif /* HTTP_UPLOADER_H */

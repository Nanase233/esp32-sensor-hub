/**
 * WiFi 连接管理器
 *
 * 功能：连接指定WiFi热点，自动重连
 */

#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include "esp_err.h"
#include <stdbool.h>

/* WiFi 配置（根据实际情况修改） */
#define WIFI_SSID       "Crystalline"       /* WiFi 名称 */
#define WIFI_PASS       "159357asdf"        /* WiFi 密码 */
#define WIFI_MAX_RETRY  10              /* 最大重试次数 */

/**
 * 初始化 WiFi 并连接
 * 阻塞等待连接成功
 *
 * @return ESP_OK 连接成功，其他表示失败
 */
esp_err_t wifi_manager_init(void);

/**
 * 检查 WiFi 是否已连接
 *
 * @return true 已连接，false 未连接
 */
bool wifi_manager_is_connected(void);

#endif /* WIFI_MANAGER_H */

/**
 * ESP32-S3-EYE IMU 数据采集主程序
 *
 * 功能：
 *   1. 初始化 QMA7981 加速度计
 *   2. 连接 WiFi
 *   3. 持续采集 IMU 数据并上传服务器
 *   4. 串口同步输出（调试用）
 */

#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "qma7981.h"
#include "wifi_manager.h"
#include "http_uploader.h"

static const char *TAG = "MAIN";

/* 采样间隔（ms），对应约 20Hz 采样率 */
#define SAMPLE_INTERVAL_MS  50

/* 每采集 N 个样本上传一次（减少网络请求频率） */
#define UPLOAD_BATCH_SIZE   10

void app_main(void)
{
    ESP_LOGI(TAG, "===== ESP32-S3-EYE IMU 数据采集 + HTTP 上传 =====");

    /* 1. 初始化 QMA7981 */
    esp_err_t ret = qma7981_init(QMA7981_RANGE_2G);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "QMA7981 初始化失败，程序停止");
        ESP_LOGE(TAG, "排错步骤：");
        ESP_LOGE(TAG, "  1. 确认 I2C 接线：SDA=GPIO4, SCL=GPIO5");
        ESP_LOGE(TAG, "  2. 确认 I2C 上拉电阻已连接");
        ESP_LOGE(TAG, "  3. 用万用表测量 SDA/SCL 空闲时是否为高电平");
        ESP_LOGE(TAG, "  4. 尝试降低 I2C 频率（修改 qma7981.h 中 QMA7981_I2C_FREQ_HZ）");
        return;
    }

    /* 2. 连接 WiFi */
    ESP_LOGI(TAG, "正在连接 WiFi...");
    ret = wifi_manager_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "WiFi 连接失败，仅本地串口输出");
        ESP_LOGE(TAG, "请修改 wifi_manager.h 中的 WIFI_SSID 和 WIFI_PASS");
        /* WiFi 失败不退出，继续本地采集 */
    }

    /* 3. 主循环：采集 + 上传 */
    qma7981_data_t accel;
    uint32_t sample_count = 0;
    uint32_t upload_count = 0;

    ESP_LOGI(TAG, "开始数据采集...");

    while (1) {
        ret = qma7981_read_accel(&accel);
        if (ret == ESP_OK) {
            /* 获取微秒级时间戳 */
            uint64_t timestamp_us = esp_timer_get_time();

            /* 串口输出 */
            printf("[%06lu] ax=%.4f, ay=%.4f, az=%.4f m/s²\n",
                   sample_count, accel.x, accel.y, accel.z);

            /* 批量上传（WiFi 已连接时） */
            if (wifi_manager_is_connected()) {
                upload_count++;
                if (upload_count >= UPLOAD_BATCH_SIZE) {
                    ret = http_uploader_send_imu(&accel, timestamp_us);
                    if (ret == ESP_OK) {
                        ESP_LOGI(TAG, "批次上传成功 (样本 #%lu)", sample_count);
                    }
                    upload_count = 0;
                }
            }

            sample_count++;
        } else {
            ESP_LOGW(TAG, "读取失败，跳过本次采样");
        }

        vTaskDelay(pdMS_TO_TICKS(SAMPLE_INTERVAL_MS));
    }
}

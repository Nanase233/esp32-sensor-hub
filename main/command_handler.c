/**
 * 远程采集命令处理器实现
 *
 * 流程：
 *   1. 每 2 秒轮询 GET /api/collect/pending
 *   2. 收到 request_id 后标记 received（POST /api/collect/<id>/received）
 *   3. 执行一次 IMU 采集，携带 request_id 上传
 *   4. 上传失败则上报失败（POST /api/collect/<id>/fail）
 */

#include "command_handler.h"
#include "wifi_manager.h"
#include "http_uploader.h"
#include "qma7981.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include <string.h>

static const char *TAG = "CMD";

/* 服务器基础 URL（不含 /api/imu 路径） */
#define SERVER_BASE_URL  "http://10.1.41.179:5000"

/* HTTP 响应缓冲区大小 */
#define HTTP_RESP_BUF_SIZE  512

/**
 * 执行 HTTP GET 请求并返回响应体
 *
 * @param url 请求 URL
 * @param resp_buf 响应缓冲区
 * @param buf_size 缓冲区大小
 * @return 实际读取字节数，-1 表示失败
 */
static int http_get(const char *url, char *resp_buf, size_t buf_size)
{
    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .timeout_ms = 5000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        ESP_LOGW(TAG, "HTTP GET 初始化失败");
        return -1;
    }

    /* 使用 open + fetch_headers + read 方式，确保能读取响应体 */
    esp_err_t err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "HTTP GET open 失败: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return -1;
    }

    int content_length = esp_http_client_fetch_headers(client);
    ESP_LOGI(TAG, "HTTP GET 响应头获取完成, content_length=%d", content_length);

    int status = esp_http_client_get_status_code(client);
    if (status != 200) {
        ESP_LOGW(TAG, "HTTP GET 状态码: %d", status);
        /* 读取并丢弃响应体 */
        char discard[64];
        while (esp_http_client_read(client, discard, sizeof(discard)) > 0) {}
        esp_http_client_cleanup(client);
        return -1;
    }

    /* 读取响应体 */
    int total_read = 0;
    int read_len;
    while (total_read < (int)buf_size - 1) {
        read_len = esp_http_client_read(client, resp_buf + total_read,
                                         buf_size - total_read - 1);
        if (read_len <= 0) {
            break;
        }
        total_read += read_len;
    }
    resp_buf[total_read] = '\0';
    ESP_LOGI(TAG, "HTTP GET 读取 %d 字节: %.128s", total_read, resp_buf);

    esp_http_client_cleanup(client);
    return total_read;
}

/**
 * 执行 HTTP POST 请求
 *
 * @param url 请求 URL
 * @param json_body JSON 请求体（可为 NULL）
 * @return ESP_OK 成功
 */
static esp_err_t http_post(const char *url, const char *json_body)
{
    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 5000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        ESP_LOGW(TAG, "HTTP POST 初始化失败");
        return ESP_FAIL;
    }

    esp_http_client_set_header(client, "Content-Type", "application/json");

    if (json_body) {
        esp_http_client_set_post_field(client, json_body, strlen(json_body));
    } else {
        esp_http_client_set_post_field(client, "{}", 2);
    }

    esp_err_t err = esp_http_client_open(client, strlen(json_body ? json_body : "{}"));
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "HTTP POST open 失败: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return ESP_FAIL;
    }

    esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);

    /* 读取并丢弃响应体 */
    char discard[64];
    while (esp_http_client_read(client, discard, sizeof(discard)) > 0) {}
    esp_http_client_cleanup(client);

    if (status == 200 || status == 201) {
        ESP_LOGI(TAG, "HTTP POST 成功: %s (状态 %d)", url, status);
        return ESP_OK;
    }

    ESP_LOGW(TAG, "HTTP POST 状态码: %d, URL: %s", status, url);
    return ESP_FAIL;
}

/**
 * 轮询待执行命令
 *
 * @return ESP_OK 且 *request_id 非空表示收到命令
 */
static esp_err_t poll_pending_command(char *request_id, size_t id_size)
{
    char url[128];
    snprintf(url, sizeof(url), "%s/api/collect/pending", SERVER_BASE_URL);

    char resp_buf[HTTP_RESP_BUF_SIZE];
    int read_len = http_get(url, resp_buf, sizeof(resp_buf));

    if (read_len <= 0) {
        request_id[0] = '\0';
        ESP_LOGD(TAG, "轮询无待执行命令 (read_len=%d)", read_len);
        return ESP_OK;  /* 无响应或无待执行命令 */
    }

    cJSON *root = cJSON_Parse(resp_buf);
    if (!root) {
        request_id[0] = '\0';
        return ESP_OK;
    }

    cJSON *rid = cJSON_GetObjectItem(root, "request_id");
    if (rid && cJSON_IsString(rid) && rid->valuestring && rid->valuestring[0] != '\0') {
        strncpy(request_id, rid->valuestring, id_size - 1);
        request_id[id_size - 1] = '\0';
        cJSON_Delete(root);
        return ESP_OK;
    }

    cJSON_Delete(root);
    request_id[0] = '\0';
    return ESP_OK;  /* 无待执行命令 */
}

/**
 * 上报命令已接收
 */
static esp_err_t mark_received(const char *request_id)
{
    char url[128];
    snprintf(url, sizeof(url), "%s/api/collect/%s/received", SERVER_BASE_URL, request_id);

    esp_err_t ret = http_post(url, "{}");
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "命令已确认: %s", request_id);
    }
    return ret;
}

/**
 * 上报采集失败
 */
static esp_err_t mark_failed(const char *request_id, const char *error_msg)
{
    char url[128];
    snprintf(url, sizeof(url), "%s/api/collect/%s/fail", SERVER_BASE_URL, request_id);

    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "error", error_msg);
    char *json_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);

    esp_err_t ret = http_post(url, json_str);
    free(json_str);

    if (ret == ESP_OK) {
        ESP_LOGW(TAG, "失败已上报: %s - %s", request_id, error_msg);
    }
    return ret;
}

/**
 * 执行采集命令
 */
static void execute_collect_command(const char *request_id)
{
    ESP_LOGI(TAG, "执行采集命令: %s", request_id);

    /* 1. 确认已接收 */
    mark_received(request_id);

    /* 2. 读取 IMU 数据 */
    qma7981_data_t accel;
    esp_err_t ret = qma7981_read_accel(&accel);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "IMU 读取失败");
        mark_failed(request_id, "IMU read failed");
        return;
    }

    /* 3. 携带 request_id 上传 */
    uint64_t timestamp_us = esp_timer_get_time();
    ret = http_uploader_send_imu_with_request(&accel, timestamp_us, request_id);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "数据上传失败");
        mark_failed(request_id, "HTTP upload failed");
        return;
    }

    ESP_LOGI(TAG, "采集完成: %s", request_id);
}

void command_handler_task(void *pvParameters)
{
    ESP_LOGI(TAG, "命令处理任务启动");

    int poll_count = 0;
    while (1) {
        /* 仅在 WiFi 连接时轮询 */
        if (wifi_manager_is_connected()) {
            char request_id[16] = {0};

            esp_err_t err = poll_pending_command(request_id, sizeof(request_id));
            if (err == ESP_OK && request_id[0] != '\0') {
                ESP_LOGI(TAG, "收到采集命令: %s", request_id);
                execute_collect_command(request_id);
            }

            /* 每 15 次轮询输出一次心跳日志 */
            poll_count++;
            if (poll_count % 15 == 0) {
                ESP_LOGI(TAG, "命令轮询心跳 (#%d)", poll_count);
            }
        }

        vTaskDelay(pdMS_TO_TICKS(COMMAND_POLL_INTERVAL_MS));
    }
}

esp_err_t command_handler_init(void)
{
    xTaskCreate(
        command_handler_task,
        "cmd_handler",
        8192,
        NULL,
        5,
        NULL
    );
    return ESP_OK;
}

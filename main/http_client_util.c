/**
 * 轻量 HTTP 工具实现
 *
 * 本文件的函数体与 command_handler.c 原先的 static http_get / http_post 完全一致，
 * 仅改了函数名与日志 TAG，便于 command_handler 与 inquiry_handler 共用。
 */

#include "http_client_util.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "HTTP";

int http_util_get_text(const char *url, char *resp_buf, size_t buf_size)
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
    ESP_LOGD(TAG, "HTTP GET 响应头获取完成, content_length=%d", content_length);

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

    esp_http_client_cleanup(client);
    return total_read;
}

esp_err_t http_util_post_json(const char *url, const char *json_body)
{
    const char *body = json_body ? json_body : "{}";

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
    esp_http_client_set_post_field(client, body, strlen(body));

    esp_err_t err = esp_http_client_open(client, strlen(body));
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
        ESP_LOGD(TAG, "HTTP POST 成功: %s (状态 %d)", url, status);
        return ESP_OK;
    }

    ESP_LOGW(TAG, "HTTP POST 状态码: %d, URL: %s", status, url);
    return ESP_FAIL;
}

/**
 * HTTP 数据上传器实现
 */

#include "http_uploader.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "cJSON.h"

static const char *TAG = "HTTP";

esp_err_t http_uploader_send_imu(const qma7981_data_t *data, uint64_t timestamp)
{
    if (data == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 构建 JSON 数据 */
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        ESP_LOGE(TAG, "创建 JSON 失败");
        return ESP_FAIL;
    }

    cJSON_AddNumberToObject(root, "timestamp", (double)timestamp);
    cJSON_AddNumberToObject(root, "ax", data->x);
    cJSON_AddNumberToObject(root, "ay", data->y);
    cJSON_AddNumberToObject(root, "az", data->z);

    char *json_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);

    if (json_str == NULL) {
        ESP_LOGE(TAG, "序列化 JSON 失败");
        return ESP_FAIL;
    }

    /* 配置 HTTP 客户端 */
    esp_http_client_config_t config = {
        .url = SERVER_URL,
        .method = HTTP_METHOD_POST,
        .timeout_ms = HTTP_TIMEOUT_MS,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        ESP_LOGE(TAG, "初始化 HTTP 客户端失败");
        free(json_str);
        return ESP_FAIL;
    }

    /* 设置请求头和内容 */
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, json_str, strlen(json_str));

    /* 发送请求 */
    esp_err_t err = esp_http_client_perform(client);
    if (err == ESP_OK) {
        int status_code = esp_http_client_get_status_code(client);
        if (status_code == 200 || status_code == 201) {
            ESP_LOGI(TAG, "上传成功: %.2f, %.2f, %.2f", data->x, data->y, data->z);
        } else {
            ESP_LOGW(TAG, "服务器返回: %d", status_code);
            err = ESP_FAIL;
        }
    } else {
        ESP_LOGE(TAG, "HTTP 请求失败: %s", esp_err_to_name(err));
    }

    esp_http_client_cleanup(client);
    free(json_str);

    return err;
}

esp_err_t http_uploader_send_imu_with_request(const qma7981_data_t *data,
                                               uint64_t timestamp,
                                               const char *request_id)
{
    if (data == NULL || request_id == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 构建 JSON 数据（携带 request_id） */
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        ESP_LOGE(TAG, "创建 JSON 失败");
        return ESP_FAIL;
    }

    cJSON_AddNumberToObject(root, "timestamp", (double)timestamp);
    cJSON_AddNumberToObject(root, "ax", data->x);
    cJSON_AddNumberToObject(root, "ay", data->y);
    cJSON_AddNumberToObject(root, "az", data->z);
    cJSON_AddStringToObject(root, "request_id", request_id);

    char *json_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);

    if (json_str == NULL) {
        ESP_LOGE(TAG, "序列化 JSON 失败");
        return ESP_FAIL;
    }

    /* 配置 HTTP 客户端 */
    esp_http_client_config_t config = {
        .url = SERVER_URL,
        .method = HTTP_METHOD_POST,
        .timeout_ms = HTTP_TIMEOUT_MS,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        ESP_LOGE(TAG, "初始化 HTTP 客户端失败");
        free(json_str);
        return ESP_FAIL;
    }

    /* 设置请求头和内容 */
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, json_str, strlen(json_str));

    /* 发送请求 */
    esp_err_t err = esp_http_client_perform(client);
    if (err == ESP_OK) {
        int status_code = esp_http_client_get_status_code(client);
        if (status_code == 200 || status_code == 201) {
            ESP_LOGI(TAG, "采集上传成功 [%s]: %.2f, %.2f, %.2f", request_id, data->x, data->y, data->z);
        } else {
            ESP_LOGW(TAG, "服务器返回: %d", status_code);
            err = ESP_FAIL;
        }
    } else {
        ESP_LOGE(TAG, "HTTP 请求失败: %s", esp_err_to_name(err));
    }

    esp_http_client_cleanup(client);
    free(json_str);

    return err;
}

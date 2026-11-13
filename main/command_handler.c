/**
 * 远程采集命令处理器实现
 *
 * 流程：
 *   1. 每 2 秒轮询 GET /api/collect/pending
 *   2. 收到 request_id 后标记 received（POST /api/collect/<id>/received）
 *   3. 执行一次 IMU 采集，携带 request_id 上传
 *   4. 上传失败则上报失败（POST /api/collect/<id>/fail）
 *
 * 任务分派（按响应里的 type 字段）：
 *   type=image     第 7 周：按需图像采集  → image_capture_run()
 *   type=inquiry   第 9 周：主动询问      → inquiry_handler_on_inquiry()
 *   type=imu（默认）第 2 周：IMU 采集     → execute_collect_command()
 *
 * 第 9 周说明：询问复用这条既有轮询通道，因此设备端 HTTP 请求次数不变。
 * 服务端按 image → inquiry → imu 的顺序返回任务，询问不会挤占图像任务。
 *
 * HTTP 读写已提取到 http_client_util.c（与 inquiry_handler 共用），行为不变。
 */

#include "command_handler.h"
#include "wifi_manager.h"
#include "http_uploader.h"
#include "http_client_util.h"
#include "qma7981.h"
#include "image_capture.h"
#include "inquiry_handler.h"
#include "sampling_state.h"
#include "esp_log.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include <string.h>

static const char *TAG = "CMD";

/* HTTP 响应缓冲区大小（第 9 周放大：询问响应含中文问句与多个字段） */
#define HTTP_RESP_BUF_SIZE  768

/**
 * 一条待执行命令（轮询结果）
 */
typedef struct {
    char request_id[24];                    /* 任务/询问 ID */
    char type[12];                          /* imu / image / inquiry */
    char question[INQUIRY_QUESTION_MAX];    /* 仅 inquiry 使用 */
    char reason_label[48];                  /* 仅 inquiry 使用 */
    int  attempt_no;                        /* 仅 inquiry 使用：第几轮询问 */
    int  expires_in_s;                      /* 仅 inquiry 使用，<=0 表示未给出 */
} pending_cmd_t;

/**
 * 轮询待执行命令
 *
 * @param cmd 输出：命令内容（调用前无需初始化，函数内部会清零）
 * @return ESP_OK（cmd->request_id 为空表示当前没有待执行命令）
 */
static esp_err_t poll_pending_command(pending_cmd_t *cmd)
{
    memset(cmd, 0, sizeof(*cmd));
    /* 默认按 IMU 任务处理，保持与原接口兼容 */
    strncpy(cmd->type, "imu", sizeof(cmd->type) - 1);

    /* 第 10 周：复用心跳上报 IMU 尝试状态，不新增 HTTP 请求。
     * 参数只使用短代码，服务端按白名单解析。 */
    char url[192];
    snprintf(url, sizeof(url),
             "%s/api/collect/pending?device_id=esp32-s3-eye&imu=%s&err=%s&fails=%lu",
             SERVER_BASE_URL,
             sampling_state_imu_attempt_code(),
             sampling_state_imu_error_code(),
             (unsigned long)sampling_state_imu_fail_count());

    char resp_buf[HTTP_RESP_BUF_SIZE];
    int read_len = http_util_get_text(url, resp_buf, sizeof(resp_buf));
    if (read_len <= 0) {
        ESP_LOGD(TAG, "轮询无待执行命令 (read_len=%d)", read_len);
        return ESP_OK;
    }

    cJSON *root = cJSON_Parse(resp_buf);
    if (!root) {
        ESP_LOGW(TAG, "轮询响应不是合法 JSON: %.64s", resp_buf);
        return ESP_OK;
    }

    cJSON *rid = cJSON_GetObjectItem(root, "request_id");
    if (rid && cJSON_IsString(rid) && rid->valuestring && rid->valuestring[0] != '\0') {
        strncpy(cmd->request_id, rid->valuestring, sizeof(cmd->request_id) - 1);

        cJSON *type = cJSON_GetObjectItem(root, "type");
        if (type && cJSON_IsString(type) && type->valuestring) {
            strncpy(cmd->type, type->valuestring, sizeof(cmd->type) - 1);
        }

        /* 第 9 周：询问任务携带问句、原因标签与剩余有效期 */
        cJSON *question = cJSON_GetObjectItem(root, "question");
        if (question && cJSON_IsString(question) && question->valuestring) {
            strncpy(cmd->question, question->valuestring, sizeof(cmd->question) - 1);
        }

        cJSON *reason = cJSON_GetObjectItem(root, "reason_label");
        if (reason && cJSON_IsString(reason) && reason->valuestring) {
            strncpy(cmd->reason_label, reason->valuestring, sizeof(cmd->reason_label) - 1);
        }

        cJSON *expires = cJSON_GetObjectItem(root, "expires_in_s");
        if (expires && cJSON_IsNumber(expires)) {
            cmd->expires_in_s = expires->valueint;
        }

        cJSON *attempt = cJSON_GetObjectItem(root, "attempt_no");
        if (attempt && cJSON_IsNumber(attempt)) {
            cmd->attempt_no = attempt->valueint;
        }
    }

    cJSON_Delete(root);
    return ESP_OK;
}

/**
 * 上报命令已接收
 */
static esp_err_t mark_received(const char *request_id)
{
    char url[128];
    snprintf(url, sizeof(url), "%s/api/collect/%s/received", SERVER_BASE_URL, request_id);

    esp_err_t ret = http_util_post_json(url, "{}");
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

    esp_err_t ret = http_util_post_json(url, json_str);
    free(json_str);

    if (ret == ESP_OK) {
        ESP_LOGW(TAG, "失败已上报: %s - %s", request_id, error_msg);
    }
    return ret;
}

/**
 * 执行 IMU 采集命令
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
        sampling_state_note_imu_fail(ret);
        ESP_LOGE(TAG, "IMU 读取失败");
        mark_failed(request_id, "IMU read failed");
        return;
    }
    sampling_state_note_imu_ok();

    /* 3. 携带 request_id 与观测/发送时间上传 */
    uint64_t timestamp_us = esp_timer_get_time();
    imu_sample_meta_t meta = {
        .observed_uptime_ms = timestamp_us / 1000,
        .send_uptime_ms = (uint64_t)(esp_timer_get_time() / 1000),
        .boot_id = sampling_state_boot_id(),
    };
    ret = http_uploader_send_imu_with_request_ex(&accel, timestamp_us,
                                                 request_id, &meta);
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
            pending_cmd_t cmd;
            esp_err_t err = poll_pending_command(&cmd);

            if (err == ESP_OK && cmd.request_id[0] != '\0') {
                if (strcmp(cmd.type, "inquiry") == 0) {
                    /* 第 9 周：主动询问（画面不可用 → 是否重拍） */
                    ESP_LOGI(TAG, "收到询问命令: %s", cmd.request_id);
                    inquiry_handler_on_inquiry(cmd.request_id, cmd.question,
                                               cmd.reason_label, cmd.attempt_no,
                                               cmd.expires_in_s);
                } else if (strcmp(cmd.type, "image") == 0) {
                    /* 第 7 周：按需图像采集任务 */
                    ESP_LOGI(TAG, "收到图像采集命令: %s", cmd.request_id);
                    image_capture_run(cmd.request_id);
                } else {
                    /* 第 2 周：IMU 采集任务 */
                    ESP_LOGI(TAG, "收到采集命令: %s", cmd.request_id);
                    execute_collect_command(cmd.request_id);
                }
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

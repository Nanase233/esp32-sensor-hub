/**
 * 按键触发与物理反馈处理器实现
 *
 * 流程：
 *   1. 每 50ms 轮询 GPIO 按键状态
 *   2. 检测到按键按下 → 本地反馈（LED 闪烁）
 *   3. 生成 request_id，上传求助消息到服务端
 *   4. 每 2 秒轮询服务端获取回应/取消状态
 *   5. 收到回应 → LED 长亮确认；收到取消 → LED 熄灭
 *
 * 断网容错：
 *   - 按键触发后立即本地反馈（不依赖网络）
 *   - 网络失败时记录日志，不阻塞主流程
 */

#include "button_handler.h"
#include "wifi_manager.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_http_client.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include <string.h>
#include <stdlib.h>

static const char *TAG = "BTN";

/* ==================== 硬件配置 ==================== */

/* 按键 GPIO（使用 GPIO34，ESP32-S3-EYE 板载按键） */
#define BUTTON_GPIO         GPIO_NUM_34

/* LED GPIO（使用 GPIO2，板载 LED） */
#define LED_GPIO            GPIO_NUM_2

/* 按键轮询间隔（ms） */
#define BUTTON_POLL_INTERVAL_MS  50

/* 服务端回应轮询间隔（ms） */
#define RESPONSE_POLL_INTERVAL_MS  2000

/* 服务端基础 URL */
#define SERVER_BASE_URL     "http://10.1.41.179:5000"

/* HTTP 响应缓冲区大小 */
#define HTTP_RESP_BUF_SIZE  512

/* ==================== 状态管理 ==================== */

/* 求助请求状态 */
typedef enum {
    HELP_IDLE = 0,          /* 无求助请求 */
    HELP_PENDING,           /* 已上传，等待回应 */
    HELP_RESPONDED,         /* 已收到回应 */
    HELP_CANCELLED          /* 已取消 */
} help_state_t;

/* 全局状态 */
static help_state_t s_help_state = HELP_IDLE;
static char s_request_id[16] = {0};  /* 当前求助请求 ID */
static int s_button_press_count = 0; /* 按键按下计数（用于调试） */

/* ==================== 硬件控制 ==================== */

/**
 * 初始化 GPIO
 */
static void gpio_init(void)
{
    /* 按键 GPIO 配置为输入（上拉） */
    gpio_config_t btn_config = {
        .pin_bit_mask = (1ULL << BUTTON_GPIO),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&btn_config);

    /* LED GPIO 配置为输出 */
    gpio_config_t led_config = {
        .pin_bit_mask = (1ULL << LED_GPIO),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&led_config);

    /* 初始状态：LED 熄灭 */
    gpio_set_level(LED_GPIO, 0);

    ESP_LOGI(TAG, "GPIO 初始化完成 (按键=GPIO%d, LED=GPIO%d)", BUTTON_GPIO, LED_GPIO);
}

/**
 * LED 闪烁（本地反馈）
 *
 * @param times 闪烁次数
 * @param interval_ms 闪烁间隔（ms）
 */
static void led_blink(int times, int interval_ms)
{
    for (int i = 0; i < times; i++) {
        gpio_set_level(LED_GPIO, 1);  /* LED 亮 */
        vTaskDelay(pdMS_TO_TICKS(interval_ms));
        gpio_set_level(LED_GPIO, 0);  /* LED 灭 */
        if (i < times - 1) {
            vTaskDelay(pdMS_TO_TICKS(interval_ms));
        }
    }
}

/**
 * LED 长亮（收到回应确认）
 */
static void led_on(void)
{
    gpio_set_level(LED_GPIO, 1);
}

/**
 * LED 熄灭（取消或空闲）
 */
static void led_off(void)
{
    gpio_set_level(LED_GPIO, 0);
}

/* ==================== HTTP 通信 ==================== */

/**
 * 生成随机 request_id（8 位十六进制）
 */
static void generate_request_id(char *buf, size_t size)
{
    uint64_t timestamp = esp_timer_get_time();
    /* 使用低 32 位时间戳 + 随机数生成 ID */
    uint32_t rand_val = (uint32_t)(timestamp ^ (timestamp >> 16));
    snprintf(buf, size, "%08lx", (unsigned long)rand_val);
}

/**
 * 上传求助消息到服务端
 *
 * @param request_id 请求 ID
 * @return ESP_OK 成功
 */
static esp_err_t upload_help_request(const char *request_id)
{
    char url[128];
    snprintf(url, sizeof(url), "%s/api/help", SERVER_BASE_URL);

    /* 构建 JSON 请求体 */
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "request_id", request_id);
    cJSON_AddStringToObject(root, "device_id", "esp32-s3-eye");
    cJSON_AddStringToObject(root, "message", "教学求助：需要帮助");
    cJSON_AddNumberToObject(root, "timestamp", (double)(esp_timer_get_time() / 1000000.0));

    char *json_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);

    if (!json_str) {
        ESP_LOGE(TAG, "序列化 JSON 失败");
        return ESP_FAIL;
    }

    /* 配置 HTTP 客户端 */
    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 5000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        ESP_LOGE(TAG, "HTTP 客户端初始化失败");
        free(json_str);
        return ESP_FAIL;
    }

    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, json_str, strlen(json_str));

    esp_err_t err = esp_http_client_perform(client);
    if (err == ESP_OK) {
        int status = esp_http_client_get_status_code(client);
        if (status == 200 || status == 201) {
            ESP_LOGI(TAG, "求助消息上传成功: %s", request_id);
            err = ESP_OK;
        } else {
            ESP_LOGW(TAG, "服务器返回状态: %d", status);
            err = ESP_FAIL;
        }
    } else {
        ESP_LOGE(TAG, "HTTP 请求失败: %s", esp_err_to_name(err));
    }

    esp_http_client_cleanup(client);
    free(json_str);

    return err;
}

/**
 * 轮询服务端获取回应状态
 *
 * @return ESP_OK 且 *status 非空表示收到回应
 */
static esp_err_t poll_help_response(char *status, size_t status_size)
{
    if (s_request_id[0] == '\0') {
        status[0] = '\0';
        return ESP_OK;
    }

    char url[128];
    snprintf(url, sizeof(url), "%s/api/help/%s", SERVER_BASE_URL, s_request_id);

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .timeout_ms = 5000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        status[0] = '\0';
        return ESP_OK;
    }

    esp_err_t err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        esp_http_client_cleanup(client);
        status[0] = '\0';
        return ESP_OK;
    }

    esp_http_client_fetch_headers(client);
    int http_status = esp_http_client_get_status_code(client);

    if (http_status != 200) {
        /* 读取并丢弃响应体 */
        char discard[64];
        while (esp_http_client_read(client, discard, sizeof(discard)) > 0) {}
        esp_http_client_cleanup(client);
        status[0] = '\0';
        return ESP_OK;
    }

    /* 读取响应体 */
    char resp_buf[HTTP_RESP_BUF_SIZE];
    int total_read = 0;
    int read_len;
    while (total_read < (int)sizeof(resp_buf) - 1) {
        read_len = esp_http_client_read(client, resp_buf + total_read,
                                         sizeof(resp_buf) - total_read - 1);
        if (read_len <= 0) {
            break;
        }
        total_read += read_len;
    }
    resp_buf[total_read] = '\0';
    esp_http_client_cleanup(client);

    /* 解析 JSON 响应 */
    cJSON *root = cJSON_Parse(resp_buf);
    if (!root) {
        status[0] = '\0';
        return ESP_OK;
    }

    cJSON *status_obj = cJSON_GetObjectItem(root, "status");
    if (status_obj && cJSON_IsString(status_obj) && status_obj->valuestring) {
        strncpy(status, status_obj->valuestring, status_size - 1);
        status[status_size - 1] = '\0';
    } else {
        status[0] = '\0';
    }

    cJSON_Delete(root);
    return ESP_OK;
}

/* ==================== 任务处理 ==================== */

/**
 * 处理按键按下事件
 */
static void handle_button_press(void)
{
    s_button_press_count++;
    ESP_LOGI(TAG, "按键按下 (#%d)", s_button_press_count);

    /* 1. 立即本地反馈（不依赖网络） */
    led_blink(3, 200);  /* 闪烁 3 次，每次 200ms */
    ESP_LOGI(TAG, "本地反馈完成：LED 闪烁 3 次");

    /* 2. 如果已有待处理求助，忽略本次按键 */
    if (s_help_state == HELP_PENDING) {
        ESP_LOGW(TAG, "已有待处理求助，忽略本次按键");
        return;
    }

    /* 3. 生成 request_id */
    generate_request_id(s_request_id, sizeof(s_request_id));
    ESP_LOGI(TAG, "生成请求 ID: %s", s_request_id);

    /* 4. 上传求助消息（如果 WiFi 已连接） */
    if (wifi_manager_is_connected()) {
        esp_err_t ret = upload_help_request(s_request_id);
        if (ret == ESP_OK) {
            s_help_state = HELP_PENDING;
            ESP_LOGI(TAG, "求助消息已上传，等待回应...");
        } else {
            ESP_LOGW(TAG, "求助消息上传失败，仅本地反馈");
            /* 网络失败时保持本地反馈，不阻塞 */
            s_help_state = HELP_IDLE;
            s_request_id[0] = '\0';
        }
    } else {
        ESP_LOGW(TAG, "WiFi 未连接，仅本地反馈");
        s_help_state = HELP_IDLE;
        s_request_id[0] = '\0';
    }
}

/**
 * 处理服务端回应
 *
 * @param status 回应状态（responded/cancelled）
 */
static void handle_help_response(const char *status)
{
    if (strcmp(status, "responded") == 0) {
        ESP_LOGI(TAG, "收到回应：LED 长亮确认");
        led_on();
        s_help_state = HELP_RESPONDED;
    } else if (strcmp(status, "cancelled") == 0) {
        ESP_LOGI(TAG, "求助已取消：LED 熄灭");
        led_off();
        s_help_state = HELP_CANCELLED;
        s_request_id[0] = '\0';
    }
}

/**
 * 按键处理任务（FreeRTOS）
 *
 * 职责：
 *   1. 轮询按键状态
 *   2. 检测按键按下（下降沿）
 *   3. 触发本地反馈和求助上传
 *   4. 轮询服务端回应状态
 */
static void button_handler_task(void *pvParameters)
{
    ESP_LOGI(TAG, "按键处理任务启动");

    int last_button_level = 1;  /* 初始状态：按键未按下（高电平） */
    int response_poll_count = 0;

    while (1) {
        /* 1. 读取按键状态 */
        int button_level = gpio_get_level(BUTTON_GPIO);

        /* 2. 检测下降沿（按键按下） */
        if (last_button_level == 1 && button_level == 0) {
            handle_button_press();
        }
        last_button_level = button_level;

        /* 3. 轮询服务端回应（仅在有待处理求助时） */
        if (s_help_state == HELP_PENDING && wifi_manager_is_connected()) {
            response_poll_count++;
            if (response_poll_count % (RESPONSE_POLL_INTERVAL_MS / BUTTON_POLL_INTERVAL_MS) == 0) {
                char status[32] = {0};
                esp_err_t ret = poll_help_response(status, sizeof(status));
                if (ret == ESP_OK && status[0] != '\0') {
                    handle_help_response(status);
                    response_poll_count = 0;  /* 重置计数器 */
                }
            }
        }

        vTaskDelay(pdMS_TO_TICKS(BUTTON_POLL_INTERVAL_MS));
    }
}

/* ==================== 公共接口 ==================== */

esp_err_t button_handler_init(void)
{
    /* 1. 初始化 GPIO */
    gpio_init();

    /* 2. 创建 FreeRTOS 任务 */
    xTaskCreate(
        button_handler_task,
        "btn_handler",
        4096,
        NULL,
        5,
        NULL
    );

    ESP_LOGI(TAG, "按键处理器初始化完成");
    return ESP_OK;
}

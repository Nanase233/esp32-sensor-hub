/**
 * 第 9 周询问模块实现（设备侧）
 *
 * 状态机只有两个状态：空闲 / 待答复。
 *   - 待答复：收到询问后进入，记录截止时间；
 *   - 空闲：答复成功、答复失败或超时后回到空闲。
 *
 * 说明：设备端不做"是否值得问"的判断，也不做冷却计时 —— 那是服务端询问模块
 * （server/inquiry.py）的唯一权威。设备端只保证"问一次、答一次、不重复提示"。
 */

#include "inquiry_handler.h"
#include "http_client_util.h"
#include "http_uploader.h"   /* 复用 SERVER_BASE_URL，避免多个模块各自写死地址 */
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

static const char *TAG = "INQUIRY";

/* 兜底有效期：服务端未给出有效秒数时使用 */
#define DEFAULT_EXPIRES_S   30

/* 答复失败后的重试次数与间隔 */
#define ANSWER_RETRY_COUNT  1
#define ANSWER_RETRY_DELAY_MS  300

/* ==================== 本地状态 ==================== */

static bool s_asking = false;
static char s_inquiry_id[32] = {0};
static char s_question[INQUIRY_QUESTION_MAX] = {0};
static char s_reason_label[48] = {0};
static int s_attempt_no = 0;       /* 当前询问的轮次（用于识别"重拍后的再次询问"） */
static int64_t s_deadline_us = 0;
static int s_answered_total = 0;   /* 本次开机已答复次数（调试用） */

/* ==================== 内部辅助 ==================== */

/**
 * 清除本地询问状态
 */
static void reset_state(void)
{
    s_asking = false;
    s_inquiry_id[0] = '\0';
    s_question[0] = '\0';
    s_reason_label[0] = '\0';
    s_attempt_no = 0;
    s_deadline_us = 0;
}

/**
 * 上报一次答复
 *
 * 失败只影响本次上报（服务端 30s 后按超时处理），不重试到阻塞主流程。
 */
static esp_err_t post_answer(const char *inquiry_id, const char *decision)
{
    char url[160];
    snprintf(url, sizeof(url), "%s/api/inquiry/%s/answer", SERVER_BASE_URL, inquiry_id);

    char body[96];
    snprintf(body, sizeof(body),
             "{\"decision\":\"%s\",\"channel\":\"device_key\"}", decision);

    esp_err_t ret = ESP_FAIL;
    for (int attempt = 0; attempt <= ANSWER_RETRY_COUNT; attempt++) {
        ret = http_util_post_json(url, body);
        if (ret == ESP_OK) {
            break;
        }
        ESP_LOGW(TAG, "答复上报失败（第 %d 次），稍后重试", attempt + 1);
        vTaskDelay(pdMS_TO_TICKS(ANSWER_RETRY_DELAY_MS));
    }
    return ret;
}

/* ==================== 公共接口 ==================== */

esp_err_t inquiry_handler_init(void)
{
    reset_state();
    ESP_LOGI(TAG, "第 9 周询问模块就绪（复用 /api/collect/pending 通道，无独立轮询任务）");
    return ESP_OK;
}

esp_err_t inquiry_handler_on_inquiry(const char *inquiry_id, const char *question,
                                     const char *reason_label, int attempt_no,
                                     int expires_in_s)
{
    if (!inquiry_id || inquiry_id[0] == '\0') {
        ESP_LOGW(TAG, "收到空询问 ID，忽略");
        return ESP_ERR_INVALID_ARG;
    }

    if (attempt_no < 0) {
        attempt_no = 0;
    }

    /* 同一条询问、同一轮次的重复下发：忽略，避免同一条提示重复打扰 */
    if (s_asking && strcmp(s_inquiry_id, inquiry_id) == 0 && s_attempt_no == attempt_no) {
        ESP_LOGW(TAG, "同一条询问重复下发，忽略: %s（第 %d 轮）", inquiry_id, attempt_no);
        return ESP_ERR_INVALID_STATE;
    }

    /* 本地仍有未决询问，但来的是新的一条或新的一轮：
       服务端同一时刻只保留一条未决询问，因此上一轮必然已被其他通道（网页/语音）答复
       或已超时。这里直接接管，保证"重拍后仍不可用 → 再问一次"不会因为设备状态过期
       而丢失。*/
    if (s_asking) {
        ESP_LOGW(TAG, "上一询问 %s 已由其他通道处理，接管新询问 %s（第 %d 轮）",
                 s_inquiry_id, inquiry_id, attempt_no);
    }

    strncpy(s_inquiry_id, inquiry_id, sizeof(s_inquiry_id) - 1);
    s_inquiry_id[sizeof(s_inquiry_id) - 1] = '\0';

    strncpy(s_question, question ? question : "", sizeof(s_question) - 1);
    s_question[sizeof(s_question) - 1] = '\0';

    strncpy(s_reason_label, reason_label ? reason_label : "", sizeof(s_reason_label) - 1);
    s_reason_label[sizeof(s_reason_label) - 1] = '\0';

    int seconds = (expires_in_s > 0) ? expires_in_s : DEFAULT_EXPIRES_S;
    s_attempt_no = attempt_no;
    s_deadline_us = esp_timer_get_time() + (int64_t)seconds * 1000000;
    s_asking = true;

    ESP_LOGI(TAG, "收到主动询问: id=%s 原因=%s 第 %d 轮 有效期=%ds",
             s_inquiry_id, s_reason_label, s_attempt_no, seconds);
    ESP_LOGI(TAG, "询问内容: %s", s_question);
    ESP_LOGI(TAG, "实体答复: 短按=重拍, 长按(%dms)=不用了", INQUIRY_LONG_PRESS_MS);
    return ESP_OK;
}

bool inquiry_handler_is_asking(void)
{
    return s_asking;
}

const char *inquiry_handler_current_id(void)
{
    return s_inquiry_id;
}

esp_err_t inquiry_handler_answer(bool short_press)
{
    if (!s_asking) {
        return ESP_ERR_INVALID_STATE;
    }

    const char *decision = short_press ? "retake" : "decline";
    char inquiry_id[sizeof(s_inquiry_id)];
    strncpy(inquiry_id, s_inquiry_id, sizeof(inquiry_id) - 1);
    inquiry_id[sizeof(inquiry_id) - 1] = '\0';

    ESP_LOGI(TAG, "实体按键答复: %s → %s", short_press ? "短按(重拍)" : "长按(不用了)", inquiry_id);

    esp_err_t ret = post_answer(inquiry_id, decision);
    if (ret == ESP_OK) {
        s_answered_total++;
        ESP_LOGI(TAG, "答复已上报（本次开机第 %d 次）", s_answered_total);
    } else {
        ESP_LOGW(TAG, "答复上报失败，服务端将按超时处理（不再提示）");
    }

    /* 无论上报成功与否都清除状态：同一询问不重复打扰 */
    reset_state();
    return ret;
}

void inquiry_handler_tick(void)
{
    if (!s_asking) {
        return;
    }
    if (esp_timer_get_time() >= s_deadline_us) {
        ESP_LOGW(TAG, "询问 %s 已过期（未答复），清除本地状态", s_inquiry_id);
        reset_state();
    }
}

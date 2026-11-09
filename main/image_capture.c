/**
 * 按需环境图像采集模块实现
 *
 * 核心逻辑：
 *   1. 收到图像任务后先确认接收（状态置为 received，前端可显示"采集中"）
 *   2. 释放 IMU 的 I2C 驱动，让摄像头 SCCB 独占 GPIO4/5
 *   3. 首次使用时初始化摄像头（懒加载）
 *   4. 抓一帧 RGB565 计算平均亮度，仅作为排错提示
 *   5. 切换为 JPEG 再抓一帧，携带 request_id 上传服务器
 *   6. 无论成功失败，释放摄像头并恢复 IMU 的 I2C 总线
 *
 * 可用性判定边界（第 8 周）：
 *   暗光、模糊、遮挡等失败画面同样需要作为"输入"上传，
 *   由服务端 check_usability() 给出结构化拒识原因。
 *   设备端不再因为画面暗或体积小就提前丢弃图像。
 *
 * 停止条件：一次任务只抓一帧，任务结束立即停止，无后台取流。
 */

#include "image_capture.h"
#include "qma7981.h"
#include "http_uploader.h"

#include "esp_camera.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <stdio.h>
#include <string.h>

static const char *TAG = "IMAGE";

/* ---- 摄像头引脚（ESP32-S3-EYE 板载 OV2640） ---- */
#define CAM_PIN_PWDN    (-1)
#define CAM_PIN_RESET   (-1)
#define CAM_PIN_XCLK    15
#define CAM_PIN_SIOD    4    /* 与 IMU 共用，采集期间分时复用 */
#define CAM_PIN_SIOC    5    /* 与 IMU 共用，采集期间分时复用 */
#define CAM_PIN_D7      16
#define CAM_PIN_D6      17
#define CAM_PIN_D5      18
#define CAM_PIN_D4      12
#define CAM_PIN_D3      10
#define CAM_PIN_D2      8
#define CAM_PIN_D1      9
#define CAM_PIN_D0      11
#define CAM_PIN_VSYNC   6
#define CAM_PIN_HREF    7
#define CAM_PIN_PCLK    13

/* ---- 采集参数 ---- */
#define IMAGE_JPEG_QUALITY      12      /* JPEG 质量（数值越小画质越高、体积越大） */
#define IMAGE_FRAMESIZE         FRAMESIZE_QVGA   /* 320x240，单帧约 10~20KB */
#define IMAGE_MIN_JPEG_BYTES    128     /* 仅拦截明显不是 JPEG 的残帧 */
#define IMAGE_DARK_HINT         12      /* 平均亮度低于此值仅记录提示，不作为失败条件 */

/* 摄像头是否已初始化（懒加载标记） */
static bool s_camera_ready = false;
/* 采集进行中标记，供主循环暂停 IMU 采样 */
static volatile bool s_busy = false;

/* ---- 内部辅助函数 ---- */

/**
 * POST 一段 JSON 并检查状态码
 *
 * @param url 目标地址
 * @param json_body JSON 请求体
 * @return ESP_OK 服务器返回 200/201
 */
static esp_err_t post_json(const char *url, const char *json_body)
{
    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 5000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        return ESP_FAIL;
    }

    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, json_body, strlen(json_body));

    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "POST 失败: %s (%s)", url, esp_err_to_name(err));
        return err;
    }
    if (status != 200 && status != 201) {
        ESP_LOGW(TAG, "POST 返回状态 %d: %s", status, url);
        return ESP_FAIL;
    }
    return ESP_OK;
}

/**
 * 上报任务已接收（前端显示"采集中"）
 */
static void report_received(const char *request_id)
{
    char url[160];
    snprintf(url, sizeof(url), "%s/api/image/%s/received", SERVER_BASE_URL, request_id);
    if (post_json(url, "{}") == ESP_OK) {
        ESP_LOGI(TAG, "任务已确认接收: %s", request_id);
    }
}

/**
 * 上报采集失败
 *
 * @param reason 失败类型：blocked（镜头遮挡/画面不可用）或 error（其他异常）
 * @param detail 具体原因描述
 */
static void report_failure(const char *request_id, const char *reason, const char *detail)
{
    char url[160];
    char body[192];
    snprintf(url, sizeof(url), "%s/api/image/%s/fail", SERVER_BASE_URL, request_id);
    snprintf(body, sizeof(body), "{\"reason\":\"%s\",\"error\":\"%s\"}", reason, detail);

    if (post_json(url, body) == ESP_OK) {
        ESP_LOGW(TAG, "失败已上报: %s -> %s (%s)", request_id, reason, detail);
    }
}

/**
 * 上传 JPEG 图像
 *
 * 请求体为原始 JPEG 字节流，X-Capture-Ts 头携带设备侧采集时间（微秒）。
 *
 * @param request_id 图像任务 ID
 * @param buf JPEG 数据
 * @param len 数据长度
 * @param timestamp_us 设备采集时间戳（微秒）
 * @return ESP_OK 上传成功
 */
static esp_err_t upload_jpeg(const char *request_id, const uint8_t *buf,
                             size_t len, uint64_t timestamp_us)
{
    char url[160];
    char ts_header[24];
    snprintf(url, sizeof(url), "%s/api/image/%s/data", SERVER_BASE_URL, request_id);
    snprintf(ts_header, sizeof(ts_header), "%llu", (unsigned long long)timestamp_us);

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 10000,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        return ESP_FAIL;
    }

    esp_http_client_set_header(client, "Content-Type", "image/jpeg");
    esp_http_client_set_header(client, "X-Capture-Ts", ts_header);

    esp_err_t err = esp_http_client_open(client, len);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "图像上传 open 失败: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return err;
    }

    /* 分块写入，避免一次性占用过多栈空间 */
    size_t written = 0;
    while (written < len) {
        int chunk = (len - written) > 4096 ? 4096 : (int)(len - written);
        int ret = esp_http_client_write(client, (const char *)buf + written, chunk);
        if (ret <= 0) {
            ESP_LOGW(TAG, "图像写入失败，已写 %u/%u 字节", (unsigned)written, (unsigned)len);
            esp_http_client_cleanup(client);
            return ESP_FAIL;
        }
        written += ret;
    }

    esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);

    /* 读取并丢弃响应体 */
    char discard[64];
    while (esp_http_client_read(client, discard, sizeof(discard)) > 0) {
    }
    esp_http_client_cleanup(client);

    if (status != 200 && status != 201) {
        ESP_LOGW(TAG, "图像上传返回状态 %d", status);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "图像上传成功: %u 字节", (unsigned)len);
    return ESP_OK;
}

/**
 * 以指定像素格式启动摄像头
 *
 * 每次抓图分两步：先用 RGB565 测亮度，再以 JPEG 重新初始化取最终帧。
 * 通过"反初始化 + 重新初始化"切换格式，避免运行中切换 pixformat
 * 导致帧缓冲重新配置失败。
 *
 * @param format 像素格式（PIXFORMAT_RGB565 / PIXFORMAT_JPEG）
 * @return ESP_OK 成功
 */
static esp_err_t camera_start(pixformat_t format)
{
    /* 若已有实例，先释放，保证同一时刻只有一个摄像头实例 */
    if (s_camera_ready) {
        esp_camera_deinit();
        s_camera_ready = false;
    }

    camera_config_t config = {
        .pin_pwdn = CAM_PIN_PWDN,
        .pin_reset = CAM_PIN_RESET,
        .pin_xclk = CAM_PIN_XCLK,
        .pin_sccb_sda = CAM_PIN_SIOD,
        .pin_sccb_scl = CAM_PIN_SIOC,
        .pin_d7 = CAM_PIN_D7,
        .pin_d6 = CAM_PIN_D6,
        .pin_d5 = CAM_PIN_D5,
        .pin_d4 = CAM_PIN_D4,
        .pin_d3 = CAM_PIN_D3,
        .pin_d2 = CAM_PIN_D2,
        .pin_d1 = CAM_PIN_D1,
        .pin_d0 = CAM_PIN_D0,
        .pin_vsync = CAM_PIN_VSYNC,
        .pin_href = CAM_PIN_HREF,
        .pin_pclk = CAM_PIN_PCLK,
        .xclk_freq_hz = 10000000,   /* 10MHz：S3 + OV2640 下 20MHz 易出现 VSYNC 溢出 */
        .ledc_timer = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,
        .frame_size = IMAGE_FRAMESIZE,
        .jpeg_quality = IMAGE_JPEG_QUALITY,
        .fb_count = 2,
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_LATEST,
        .pixel_format = format,
    };

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "摄像头初始化失败(%s): %s",
                 format == PIXFORMAT_JPEG ? "JPEG" : "RGB565", esp_err_to_name(err));
        return err;
    }

    s_camera_ready = true;
    ESP_LOGI(TAG, "摄像头已启动（%dx%d, %s）", 320, 240,
             format == PIXFORMAT_JPEG ? "JPEG" : "RGB565");
    return ESP_OK;
}

/**
 * 计算 RGB565 图像的平均亮度
 *
 * 采用近似亮度公式 Y ≈ 0.30R + 0.59G + 0.11B，
 * 为避免浮点开销，使用整数移位近似。
 *
 * @return 平均亮度（0~255）
 */
static int average_brightness(const uint8_t *buf, size_t pixel_count)
{
    const uint16_t *pixels = (const uint16_t *)buf;
    uint32_t sum = 0;

    /* 隔行采样，降低计算量（每 4 个像素取 1 个） */
    for (size_t i = 0; i < pixel_count; i += 4) {
        uint16_t v = pixels[i];
        uint8_t r = (v >> 11) & 0x1F;
        uint8_t g = (v >> 5) & 0x3F;
        uint8_t b = v & 0x1F;
        /* 扩展到位宽 8 后加权：R*77 + G*150 + B*29 ≈ 256*Y */
        sum += ((uint32_t)r * 8 * 77 + (uint32_t)g * 4 * 150 + (uint32_t)b * 8 * 29) >> 8;
    }

    size_t sampled = (pixel_count + 3) / 4;
    return (int)(sum / (sampled > 0 ? sampled : 1));
}

/**
 * 检查一帧数据是否具备最基本的 JPEG 结构
 *
 * 只检查 SOI 和最小长度，不根据体积或亮度判断画面是否可用。
 * 暗光、均匀、疑似遮挡的合法 JPEG 必须继续上传，
 * 由服务端完成可用性判定和结构化反馈；解码失败也由服务端记录为错误样例。
 *
 * @param buf 帧数据
 * @param len 数据长度
 * @return true 结构有效
 */
static bool jpeg_frame_valid(const uint8_t *buf, size_t len)
{
    if (buf == NULL || len < IMAGE_MIN_JPEG_BYTES) {
        return false;
    }
    return buf[0] == 0xFF && buf[1] == 0xD8;
}

/* ---- 对外接口 ---- */

bool image_capture_is_busy(void)
{
    return s_busy;
}

/**
 * 释放摄像头并恢复 IMU 的 I2C 总线
 *
 * 摄像头 SCCB 与 IMU 共用 GPIO4/5，两者不能同时持有总线：
 * 采集结束后必须反初始化摄像头（释放 SCCB 驱动），再重新初始化 IMU。
 */
static void restore_imu_bus(void)
{
    if (s_camera_ready) {
        esp_camera_deinit();
        s_camera_ready = false;
        ESP_LOGI(TAG, "摄像头已释放");
    }

    if (qma7981_init(QMA7981_RANGE_2G) != ESP_OK) {
        ESP_LOGE(TAG, "IMU I2C 总线恢复失败，IMU 数据将中断");
    }
}

esp_err_t image_capture_run(const char *request_id)
{
    if (request_id == NULL || request_id[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }

    s_busy = true;
    ESP_LOGI(TAG, "开始采集图像: %s", request_id);

    /* 1. 确认接收，前端进入"采集中"状态 */
    report_received(request_id);

    /* 2. 释放 IMU 的 I2C 总线，交由摄像头 SCCB 使用 */
    qma7981_bus_deinit();

    /* 3. 以 RGB565 启动摄像头，用于亮度检测 */
    esp_err_t err = camera_start(PIXFORMAT_RGB565);
    if (err != ESP_OK) {
        report_failure(request_id, "error", "camera init failed");
        restore_imu_bus();
        s_busy = false;
        return err;
    }

    /* 4. 丢弃一帧，等待自动曝光稳定 */
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb != NULL) {
        esp_camera_fb_return(fb);
    }
    vTaskDelay(pdMS_TO_TICKS(100));

    /* 5. 抓一帧 RGB565 估算亮度，仅作为排错提示 */
    fb = esp_camera_fb_get();
    if (fb == NULL) {
        ESP_LOGE(TAG, "抓帧失败");
        report_failure(request_id, "error", "frame grab failed");
        restore_imu_bus();
        s_busy = false;
        return ESP_FAIL;
    }

    int brightness = average_brightness(fb->buf, fb->len / 2);
    esp_camera_fb_return(fb);
    ESP_LOGI(TAG, "画面平均亮度提示: %d", brightness);
    if (brightness < IMAGE_DARK_HINT) {
        ESP_LOGW(TAG, "画面偏暗，仍上传原图交由服务端判定可用性");
    }

    /* 6. 以 JPEG 重新启动摄像头，抓取最终上传帧 */
    err = camera_start(PIXFORMAT_JPEG);
    if (err != ESP_OK) {
        report_failure(request_id, "error", "camera jpeg init failed");
        restore_imu_bus();
        s_busy = false;
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(150));

    fb = esp_camera_fb_get();
    if (fb == NULL) {
        /* 重试一次，规避首帧未就绪 */
        vTaskDelay(pdMS_TO_TICKS(200));
        fb = esp_camera_fb_get();
    }
    if (fb == NULL) {
        ESP_LOGE(TAG, "JPEG 抓帧失败");
        report_failure(request_id, "error", "jpeg grab failed");
        restore_imu_bus();
        s_busy = false;
        return ESP_FAIL;
    }

    if (!jpeg_frame_valid(fb->buf, fb->len)) {
        ESP_LOGW(TAG, "JPEG 结构异常（%u 字节），无法作为有效输入", (unsigned)fb->len);
        esp_camera_fb_return(fb);
        report_failure(request_id, "error", "invalid jpeg frame");
        restore_imu_bus();
        s_busy = false;
        return ESP_FAIL;
    }

    /* 7. 上传，携带采集时间戳；暗光/遮挡帧同样上传，由服务端给出原因 */
    uint64_t captured_us = esp_timer_get_time();
    err = upload_jpeg(request_id, fb->buf, fb->len, captured_us);
    esp_camera_fb_return(fb);

    if (err != ESP_OK) {
        report_failure(request_id, "error", "upload failed");
    } else {
        ESP_LOGI(TAG, "采集完成: %s", request_id);
    }

    /* 8. 释放摄像头并恢复 IMU 的 I2C 总线，结束本次任务 */
    restore_imu_bus();

    s_busy = false;
    return err;
}

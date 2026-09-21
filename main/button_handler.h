/**
 * 按键触发与物理反馈处理器
 *
 * 功能：
 *   1. GPIO 按键检测（轮询方式）
 *   2. 本地反馈：LED 闪烁 + 蜂鸣器提示
 *   3. 触发求助消息上传（携带 request_id）
 *   4. 轮询服务端获取回应/取消状态
 *   5. 断网容错：本地反馈不依赖网络
 */

#ifndef BUTTON_HANDLER_H
#define BUTTON_HANDLER_H

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 初始化按键处理器
 *
 * 启动 FreeRTOS 任务进行按键轮询和状态管理
 *
 * @return ESP_OK 成功
 */
esp_err_t button_handler_init(void);

#ifdef __cplusplus
}
#endif

#endif /* BUTTON_HANDLER_H */

/**
 * 远程采集命令处理器
 *
 * 功能：
 *   1. 定期轮询服务器获取待执行采集命令
 *   2. 执行 IMU 采集并携带 request_id 上传
 *   3. 上报命令接收确认和失败状态
 */

#ifndef COMMAND_HANDLER_H
#define COMMAND_HANDLER_H

#include "esp_err.h"

/* 命令轮询间隔（ms） */
#define COMMAND_POLL_INTERVAL_MS  2000

/**
 * 初始化命令处理器（创建 FreeRTOS 任务）
 *
 * @return ESP_OK 成功
 */
esp_err_t command_handler_init(void);

/**
 * 命令处理任务入口（由 FreeRTOS 调用）
 */
void command_handler_task(void *pvParameters);

#endif /* COMMAND_HANDLER_H */

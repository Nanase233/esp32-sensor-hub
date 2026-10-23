/**
 * 按需环境图像采集模块
 *
 * 第 7 周任务：按需获取一张环境图像
 *
 * 设计约束：
 *   1. 只在收到图像采集命令时取一张图，无后台连续取流（停止条件）
 *   2. 摄像头 SCCB 与 IMU 共用 GPIO4/5，采用"抓图期间独占 I2C 总线"的
 *      分时复用方式：抓图前释放 IMU 的 I2C 驱动，抓图后恢复
 *   3. 摄像头驱动首次使用时才初始化（懒加载），不占用常驻内存
 *
 * 状态反馈：
 *   采集中   -> 上报 received
 *   采集成功 -> 上传 JPEG，服务端标记 captured（带采集时间）
 *   镜头遮挡/画面不可用 -> 上报 blocked
 *   采集失败 -> 上报 error
 */

#ifndef IMAGE_CAPTURE_H
#define IMAGE_CAPTURE_H

#include "esp_err.h"
#include <stdbool.h>

/**
 * 执行一次按需图像采集
 *
 * 完整流程：确认接收 -> 初始化摄像头 -> 抓帧 -> 质量检查 -> 上传 -> 释放
 *
 * @param request_id 服务端分配的图像任务 ID，用于关联请求与图像记录
 * @return ESP_OK 采集并上传成功，其他表示失败（失败原因已上报服务端）
 */
esp_err_t image_capture_run(const char *request_id);

/**
 * 查询是否正在执行图像采集
 *
 * 采集期间 I2C 总线被摄像头独占，主循环应暂停 IMU 采样。
 *
 * @return true 正在采集
 */
bool image_capture_is_busy(void);

#endif /* IMAGE_CAPTURE_H */

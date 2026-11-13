/**
 * 轻量 HTTP 工具
 *
 * 来源说明（为什么要有这个文件）：
 *   这两个函数原本是 command_handler.c 内部的 static 实现。第 9 周的询问模块
 *   也需要发一个 POST（答复「重拍 / 不用了」），为了避免把同一段 HTTP 逻辑
 *   复制一份，这里把原实现**原样**提取为公共函数，逻辑未做任何改写：
 *     - 打开连接 → 取响应头 → 校验状态码 → 读尽响应体 → 清理
 *     - POST 把 200/201 视为成功，其余状态码只记录日志并返回失败
 *
 * 约束：
 *   - 只做一次性短请求（超时 5s），不承载长连接/流式上传
 *   - 调用方负责提供足够大的缓冲区；本模块不动态分配响应内存
 */

#ifndef HTTP_CLIENT_UTIL_H
#define HTTP_CLIENT_UTIL_H

#include <stddef.h>
#include "esp_err.h"

/**
 * 执行 HTTP GET 并把响应体读入缓冲区（自动补 '\0'）
 *
 * @param url       请求地址
 * @param resp_buf  输出缓冲区
 * @param buf_size  缓冲区大小
 * @return 实际读取字节数；-1 表示失败（连接失败或状态码非 200）
 */
int http_util_get_text(const char *url, char *resp_buf, size_t buf_size);

/**
 * 执行 HTTP POST（application/json）
 *
 * @param url        请求地址
 * @param json_body  JSON 请求体（NULL 时发送 "{}"）
 * @return ESP_OK 表示服务端返回 200/201
 */
esp_err_t http_util_post_json(const char *url, const char *json_body);

#endif /* HTTP_CLIENT_UTIL_H */

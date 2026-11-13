/**
 * 第 9 周：画面不可用 → 主动询问「是否重拍」的设备侧
 *
 * 本周问题：画面无法使用时，系统能否主动询问「是否重拍」，并尊重用户选择？
 *
 * 设备端只负责"提示 + 答复"这一层，不参与任何判定：
 *   1. 询问由服务端产生，经**既有的** /api/collect/pending 通道下发（type=inquiry），
 *      因此不新增轮询任务、不增加 HTTP 请求次数；
 *   2. 收到询问后置本地状态，由按键处理器点亮 LED 双闪（实体操作通道）；
 *   3. 实体按键答复：短按 = 重拍，长按（>=800ms）= 不用了；
 *   4. 答复回传 POST /api/inquiry/<id>/answer，失败重试一次后放弃（不阻塞主流程）；
 *   5. 询问到期或已答复后清除本地状态，不再重复提示。
 *
 * 与既有模块的边界：
 *   - 不读写 IMU、不碰摄像头、不初始化 GPIO（LED/按键由 button_handler 拥有）；
 *   - 不改变第 3 周求助链路的任何行为；求助未决时按键与 LED 优先给求助。
 */

#ifndef INQUIRY_HANDLER_H
#define INQUIRY_HANDLER_H

#include "esp_err.h"
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 长按判定阈值（ms）：达到该时长视为「不用了」 */
#define INQUIRY_LONG_PRESS_MS   800

/** 单条询问文案的最大长度（UTF-8 字节数，含结尾 '\0'） */
#define INQUIRY_QUESTION_MAX    128

/**
 * 初始化询问模块（无独立任务，只重置本地状态）
 *
 * @return ESP_OK
 */
esp_err_t inquiry_handler_init(void);

/**
 * 收到一条询问（由命令处理器在 type=inquiry 时调用）
 *
 * @param inquiry_id    询问 ID（服务端分配，答复时原样带回）
 * @param question      询问文案（服务端已按原因生成，直接展示/播报）
 * @param reason_label  原因短标签，如"画面过暗"
 * @param attempt_no    第几次询问（0=首次；重拍后仍不可用会用同 ID、更高轮次再问）
 * @param expires_in_s  剩余有效秒数（<=0 时按 30s 兜底）
 * @return ESP_OK 已接管；ESP_ERR_INVALID_STATE 表示同一条询问的同一轮次重复下发（忽略）
 */
esp_err_t inquiry_handler_on_inquiry(const char *inquiry_id, const char *question,
                                     const char *reason_label, int attempt_no,
                                     int expires_in_s);

/**
 * 是否有待答复的询问（供 LED 提示与按键仲裁使用）
 */
bool inquiry_handler_is_asking(void);

/**
 * 当前询问 ID（无未决询问时返回空字符串，永不为 NULL）
 */
const char *inquiry_handler_current_id(void);

/**
 * 用实体按键答复当前询问
 *
 * @param short_press true=短按（重拍）；false=长按（不用了）
 * @return ESP_OK 服务端已接受；其他表示上报失败（本地状态仍会清除）
 */
esp_err_t inquiry_handler_answer(bool short_press);

/**
 * 周期调用（建议 50ms）：清理到期询问
 *
 * 只做状态清理，不碰 LED —— LED 由 button_handler 统一驱动，避免两个模块抢同一个 GPIO。
 */
void inquiry_handler_tick(void);

#ifdef __cplusplus
}
#endif

#endif /* INQUIRY_HANDLER_H */

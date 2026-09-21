/**
 * QMA7981 加速度计驱动
 *
 * 硬件连接（ESP32-S3-EYE）：
 *   I2C SDA = GPIO4
 *   I2C SCL = GPIO5
 *   I2C 地址 = 0x12
 */

#ifndef QMA7981_H
#define QMA7981_H

#include "esp_err.h"
#include <stdint.h>

/* ---- 硬件参数 ---- */
#define QMA7981_I2C_ADDR        0x12    /* I2C 从机地址 */
#define QMA7981_I2C_SDA_GPIO    4       /* SDA 引脚 */
#define QMA7981_I2C_SCL_GPIO    5       /* SCL 引脚 */
#define QMA7981_I2C_FREQ_HZ    400000  /* I2C 时钟频率 400kHz */

/* ---- 寄存器地址 ---- */
#define QMA7981_REG_CHIP_ID     0x00    /* 芯片 ID（只读） */
#define QMA7981_REG_ACC_X       0x01    /* X 轴加速度（2 字节） */
#define QMA7981_REG_ACC_Y       0x03    /* Y 轴加速度（2 字节） */
#define QMA7981_REG_ACC_Z       0x05    /* Z 轴加速度（2 字节） */
#define QMA7981_REG_RANGE       0x0F    /* 量程选择 */
#define QMA7981_REG_BW          0x10    /* 带宽/采样率 */
#define QMA7981_REG_POWER_CTL   0x11    /* 电源控制 */

/* ---- 电源控制命令 ---- */
#define QMA7981_CMD_ACTIVE      0xC0    /* 进入测量模式 */

/* ---- 芯片 ID 期望值（0xE7 或 0x90 均可能） ---- */
#define QMA7981_CHIP_ID_VALUE   0xE7

/* ---- 量程枚举 ---- */
typedef enum {
    QMA7981_RANGE_2G  = 0x00,  /* ±2g  */
    QMA7981_RANGE_4G  = 0x01,  /* ±4g  */
    QMA7981_RANGE_8G  = 0x02,  /* ±8g  */
    QMA7981_RANGE_16G = 0x03,  /* ±16g */
} qma7981_range_t;

/* ---- 三轴加速度数据 ---- */
typedef struct {
    float x;  /* X 轴加速度，单位 m/s² */
    float y;  /* Y 轴加速度，单位 m/s² */
    float z;  /* Z 轴加速度，单位 m/s² */
} qma7981_data_t;

/**
 * 初始化 I2C 总线和 QMA7981
 * 完成：I2C 外设初始化 → 读取并校验芯片 ID → 配置量程和采样率 → 进入测量模式
 *
 * @param range 量程设置
 * @return ESP_OK 成功，其他表示失败
 */
esp_err_t qma7981_init(qma7981_range_t range);

/**
 * 读取三轴加速度数据
 *
 * @param data 输出参数，存放三轴加速度（m/s²）
 * @return ESP_OK 成功
 */
esp_err_t qma7981_read_accel(qma7981_data_t *data);

/**
 * 读取芯片 ID（用于排错诊断）
 *
 * @param chip_id 输出参数
 * @return ESP_OK 成功
 */
esp_err_t qma7981_read_chip_id(uint8_t *chip_id);

#endif /* QMA7981_H */

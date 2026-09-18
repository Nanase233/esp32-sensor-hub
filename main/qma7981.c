/**
 * QMA7981 加速度计驱动实现
 *
 * 排错思路（I2C 不通时按此顺序排查）：
 *   1. 检查接线：SDA=GPIO4, SCL=GPIO5，确认无虚焊
 *   2. 检查上拉电阻：I2C 总线需要 4.7kΩ 上拉至 3.3V
 *   3. 扫描 I2C 地址：用 i2c_master_scan() 确认设备是否应答
 *   4. 读取芯片 ID：期望返回 0xE6，若返回 0xFF 或 0x00 说明通信异常
 *   5. 降低 I2C 频率至 100kHz 再试，排除信号完整性问题
 */

#include "qma7981.h"
#include "driver/i2c.h"
#include "esp_log.h"
#include <math.h>

static const char *TAG = "QMA7981";

/* I2C 外设编号 */
#define I2C_PORT_NUM  I2C_NUM_0

/* 当前量程对应的灵敏度（m/s² per LSB）
 * QMA7981 输出为 14 位有符号数，左对齐在 16 位中（低 2 位为 0）
 * 右移 2 后得到 14 位原始值，范围 [-8192, 8191]
 * ±2g:  8192 LSB = 2g → 灵敏度 = 9.80665 / 4096 ≈ 0.002394 m/s²/LSB
 * ±4g:  灵敏度翻倍，以此类推
 */
static float s_sensitivity = 0.0f;

/* ---- 内部辅助函数 ---- */

/**
 * 写单个寄存器
 */
static esp_err_t qma7981_write_reg(uint8_t reg, uint8_t value)
{
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (QMA7981_I2C_ADDR << 1) | I2C_MASTER_WRITE, true);
    i2c_master_write_byte(cmd, reg, true);
    i2c_master_write_byte(cmd, value, true);
    i2c_master_stop(cmd);

    esp_err_t ret = i2c_master_cmd_begin(I2C_PORT_NUM, cmd, pdMS_TO_TICKS(100));
    i2c_cmd_link_delete(cmd);
    return ret;
}

/**
 * 读单个寄存器
 */
static esp_err_t qma7981_read_reg(uint8_t reg, uint8_t *value)
{
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();

    /* 写阶段：发送寄存器地址 */
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (QMA7981_I2C_ADDR << 1) | I2C_MASTER_WRITE, true);
    i2c_master_write_byte(cmd, reg, true);

    /* 读阶段：读取数据 */
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (QMA7981_I2C_ADDR << 1) | I2C_MASTER_READ, true);
    i2c_master_read_byte(cmd, value, I2C_MASTER_LAST_NACK);
    i2c_master_stop(cmd);

    esp_err_t ret = i2c_master_cmd_begin(I2C_PORT_NUM, cmd, pdMS_TO_TICKS(100));
    i2c_cmd_link_delete(cmd);
    return ret;
}

/**
 * 连续读取多个寄存器
 */
static esp_err_t qma7981_read_regs(uint8_t start_reg, uint8_t *buf, uint8_t len)
{
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();

    /* 写阶段：发送起始寄存器地址 */
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (QMA7981_I2C_ADDR << 1) | I2C_MASTER_WRITE, true);
    i2c_master_write_byte(cmd, start_reg, true);

    /* 读阶段：连续读取 */
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (QMA7981_I2C_ADDR << 1) | I2C_MASTER_READ, true);
    if (len > 1) {
        i2c_master_read(cmd, buf, len - 1, I2C_MASTER_ACK);
    }
    i2c_master_read_byte(cmd, &buf[len - 1], I2C_MASTER_LAST_NACK);
    i2c_master_stop(cmd);

    esp_err_t ret = i2c_master_cmd_begin(I2C_PORT_NUM, cmd, pdMS_TO_TICKS(100));
    i2c_cmd_link_delete(cmd);
    return ret;
}

/* ---- 公开接口 ---- */

esp_err_t qma7981_init(qma7981_range_t range)
{
    esp_err_t ret;

    /* 1. 初始化 I2C 外设 */
    i2c_config_t i2c_cfg = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = QMA7981_I2C_SDA_GPIO,
        .scl_io_num = QMA7981_I2C_SCL_GPIO,
        .sda_pullup_en = true,   /* 启用内部上拉（外部有上拉时可关闭） */
        .scl_pullup_en = true,
        .master.clk_speed = QMA7981_I2C_FREQ_HZ,
        .clk_flags = 0,
    };
    ret = i2c_param_config(I2C_PORT_NUM, &i2c_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C 参数配置失败: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = i2c_driver_install(I2C_PORT_NUM, I2C_MODE_MASTER, 0, 0, 0);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C 驱动安装失败: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGI(TAG, "I2C 初始化完成 (SDA=GPIO%d, SCL=GPIO%d, freq=%dHz)",
             QMA7981_I2C_SDA_GPIO, QMA7981_I2C_SCL_GPIO, QMA7981_I2C_FREQ_HZ);

    /* 2. 读取并校验芯片 ID */
    uint8_t chip_id = 0;
    ret = qma7981_read_reg(QMA7981_REG_CHIP_ID, &chip_id);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "读取芯片 ID 失败: %s", esp_err_to_name(ret));
        ESP_LOGE(TAG, "排错：检查 I2C 接线和上拉电阻");
        return ret;
    }

    if (chip_id != QMA7981_CHIP_ID_VALUE) {
        ESP_LOGE(TAG, "芯片 ID 不匹配！期望 0x%02X，实际 0x%02X",
                 QMA7981_CHIP_ID_VALUE, chip_id);
        ESP_LOGE(TAG, "排错：确认传感器型号，或降低 I2C 频率至 100kHz 重试");
        return ESP_ERR_INVALID_RESPONSE;
    }
    ESP_LOGI(TAG, "芯片 ID 校验通过: 0x%02X", chip_id);

    /* 3. 设置量程 */
    ret = qma7981_write_reg(QMA7981_REG_RANGE, (uint8_t)range);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "设置量程失败: %s", esp_err_to_name(ret));
        return ret;
    }

    /* 根据量程计算灵敏度 */
    switch (range) {
        case QMA7981_RANGE_2G:  s_sensitivity = 0.002394f; break;
        case QMA7981_RANGE_4G:  s_sensitivity = 0.004788f; break;
        case QMA7981_RANGE_8G:  s_sensitivity = 0.009575f; break;
        case QMA7981_RANGE_16G: s_sensitivity = 0.019150f; break;
        default:                s_sensitivity = 0.002394f; break;
    }
    ESP_LOGI(TAG, "量程设置为 ±%dg，灵敏度 = %.6f m/s²/LSB",
             (1 << (int)range) * 2, s_sensitivity);

    /* 4. 设置带宽（采样率）—— 0x00 = 128Hz（典型值） */
    ret = qma7981_write_reg(QMA7981_REG_BW, 0x00);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "设置带宽失败: %s", esp_err_to_name(ret));
        return ret;
    }

    /* 5. 进入测量模式（电源控制寄存器置为 active） */
    ret = qma7981_write_reg(QMA7981_REG_POWER_CTL, 0x01);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "进入测量模式失败: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGI(TAG, "QMA7981 初始化完成，进入测量模式");
    return ESP_OK;
}

esp_err_t qma7981_read_accel(qma7981_data_t *data)
{
    if (data == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 连续读取 6 个字节（X/Y/Z 各 2 字节） */
    uint8_t buf[6] = {0};
    esp_err_t ret = qma7981_read_regs(QMA7981_REG_ACC_X_LSB, buf, 6);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "读取加速度数据失败: %s", esp_err_to_name(ret));
        return ret;
    }

    /* 拼接 16 位原始值，右移 2 得到 14 位有符号数 */
    int16_t raw_x = (int16_t)((buf[1] << 8) | buf[0]) >> 2;
    int16_t raw_y = (int16_t)((buf[3] << 8) | buf[2]) >> 2;
    int16_t raw_z = (int16_t)((buf[5] << 8) | buf[4]) >> 2;

    /* 转换为 m/s² */
    data->x = raw_x * s_sensitivity;
    data->y = raw_y * s_sensitivity;
    data->z = raw_z * s_sensitivity;

    return ESP_OK;
}

esp_err_t qma7981_read_chip_id(uint8_t *chip_id)
{
    if (chip_id == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    return qma7981_read_reg(QMA7981_REG_CHIP_ID, chip_id);
}

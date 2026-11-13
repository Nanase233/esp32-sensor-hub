"""
IMU 数据接收服务器

功能：
  1. 接收 ESP32 上传的 IMU 数据
  2. 存储到 SQLite 数据库
  3. 提供 REST API 查询数据
  4. 提供 Web 页面实时展示
  5. 远程采集指令与执行结果反馈
  6. 按键触发与物理反馈闭环（第 3 周任务）
  7. 语音任务澄清与停止（第 6 周任务）
  8. 视觉推理结果反馈与人工纠错（第 8 周任务）

启动：python app.py
访问：http://localhost:5000
"""

from flask import Flask, request, jsonify, render_template, send_file
from flask_cors import CORS
from datetime import datetime
import sqlite3
import os
import uuid
import time
import re
import json
import base64
import urllib.request
import urllib.error
import logging

# 视觉推理模块（第 8 周任务）：统一接口，HTTP/ONNX 后端可替换
from vision import (run_inference, HIGH_SCORE_THRESHOLD,
                    LOW_SCORE_THRESHOLD, AMBIGUOUS_MARGIN,
                    SCORE_NOTE, COARSE_NOTE)

# 第 10 周：多来源状态/数据年龄（纯读聚合，不改原始观测）
import source_state as source_state_mod

app = Flask(__name__)
CORS(app)  # 允许跨域请求

# ==================== 日志配置（第 6 周任务） ====================
# 交互日志文件，记录每个请求的生命周期
# 注意：request_id 通过 logger.info(..., extra={"request_id": ...}) 逐条传入。
# 不要用 addFilter(RequestIdFilter(...)) —— 过滤器会累积且互相覆盖，
# 并发时会把 A 请求的日志错标成 B 的 request_id。
# 允许测试/多实例通过 INTERACTION_LOG 指定日志文件，避免与正在运行的服务争用。
LOG_FILE = os.environ.get("INTERACTION_LOG") or os.path.join(os.path.dirname(__file__), "interaction.log")

# 使用独立 logger，不修改 root logger，避免影响 Flask/Werkzeug 日志
logger = logging.getLogger("interaction")
logger.setLevel(logging.INFO)
_log_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(request_id)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_log_handler)
logger.propagate = False  # 不传播到 root logger


# ==================== 任务取消状态存储 ====================
# 存储被取消的请求 ID，用于过滤迟到结果
cancelled_requests = set()

# ==================== LLM 配置 ====================
# 授权语言服务配置（密钥留服务端，不暴露到 ESP32）
LLM_API_URL = os.environ.get("LLM_API_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-3.5-turbo")

# LLM 超时（秒）
LLM_TIMEOUT_S = 10

# ==================== 语音服务配置（第 5 周任务） ====================
# STT（语音转文字）配置
STT_API_URL = os.environ.get("STT_API_URL", "")
STT_API_KEY = os.environ.get("STT_API_KEY", "")
STT_MODEL = os.environ.get("STT_MODEL", "whisper-1")
STT_TIMEOUT_S = 15

# TTS（文字转语音）配置
TTS_API_URL = os.environ.get("TTS_API_URL", "")
TTS_API_KEY = os.environ.get("TTS_API_KEY", "")
TTS_MODEL = os.environ.get("TTS_MODEL", "tts-1")
TTS_VOICE = os.environ.get("TTS_VOICE", "alloy")
TTS_TIMEOUT_S = 15


def call_llm(user_text: str) -> dict:
    """
    调用授权语言服务进行意图识别。
    返回 JSON 结构：
    {
        "intent": "query_last" | "collect_new" | "ambiguous" | "out_of_scope",
        "device_id": "esp32-s3-eye" | null,
        "sensor": "imu" | null,
        "reasoning": "判断依据"
    }
    服务不可用时返回 None，由调用方回退到规则匹配。
    """
    if not LLM_API_URL or not LLM_API_KEY:
        return None

    system_prompt = (
        "你是一个传感器数据采集系统的意图识别助手。"
        "用户的输入会被映射为以下五种意图之一：\n"
        "1. query_last - 查看上次/最近/历史数据（如：'查看上次数据'、'最近一次采集结果'、'上次加速度是多少'）\n"
        "2. collect_new - 请求重新采集一次新数据（如：'重新采集一次'、'再采一次'、'现在采集'）\n"
        "3. capture_image - 请求拍摄/查看环境图像（如：'看看周围环境'、'拍一张照片'、'现在画面是什么'）\n"
        "4. ambiguous - 输入模糊，无法确定是查看、采集还是拍照（如：'看看数据'、'帮我查一下'）\n"
        "5. out_of_scope - 与数据采集无关的请求（如：'今天天气如何'、'讲个笑话'）\n\n"
        "请严格按以下 JSON 格式返回，不要添加任何其他内容：\n"
        '{"intent": "...", "device_id": "esp32-s3-eye", "sensor": "imu", "reasoning": "..."}\n'
        "device_id 和 sensor 仅在明确提及时填写，否则为 null。"
    )

    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        "temperature": 0.1,
        "max_tokens": 200
    }).encode("utf-8")

    req = urllib.request.Request(
        LLM_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            # 提取 JSON（兼容 markdown 代码块包裹）
            match = re.search(r'\{.*?\}', content, re.DOTALL)
            if match:
                return json.loads(match.group())
            return None
    except Exception as e:
        print(f"[LLM] 调用失败: {e}")
        return None


def classify_intent_by_rules(user_text: str) -> dict:
    """
    备用路径：基于关键词规则进行意图分类。
    当 LLM 服务不可用时使用（服务异常时使用结构化样例定位解析问题）。
    """
    text = user_text.lower().strip()

    # 采集意图关键词
    collect_keywords = ["重新采集", "再采", "新采集", "采集一次", "现在采集", "立即采集",
                        "重新采", "再采一次", "采一次", "采集新的", "新数据"]
    # 查询意图关键词
    query_keywords = ["上次", "最近", "历史", "上一次", "之前的", "刚才", "刚才的",
                      "查看数据", "看看数据", "查一下", "上次数据", "最近一次",
                      "加速度", "数据是多少", "结果"]
    # 图像采集意图关键词（第 7 周新增）
    image_keywords = ["拍一张", "拍个照", "拍照", "照相", "看看周围", "周围环境",
                      "环境图像", "画面", "图像", "看一下环境", "看看环境"]

    is_collect = any(kw in text for kw in collect_keywords)
    is_query = any(kw in text for kw in query_keywords)
    is_image = any(kw in text for kw in image_keywords)

    if is_image and not is_collect:
        return {
            "intent": "capture_image",
            "device_id": "esp32-s3-eye",
            "sensor": None,
            "reasoning": "规则匹配：检测到图像采集关键词"
        }
    elif is_collect and not is_query:
        return {
            "intent": "collect_new",
            "device_id": "esp32-s3-eye",
            "sensor": "imu",
            "reasoning": "规则匹配：检测到采集关键词"
        }
    elif is_query and not is_collect:
        return {
            "intent": "query_last",
            "device_id": "esp32-s3-eye",
            "sensor": "imu",
            "reasoning": "规则匹配：检测到查询关键词"
        }
    elif is_collect and is_query:
        return {
            "intent": "ambiguous",
            "device_id": None,
            "sensor": None,
            "reasoning": "规则匹配：同时检测到采集和查询关键词，意图模糊"
        }
    else:
        # 无匹配关键词 → out_of_scope
        return {
            "intent": "out_of_scope",
            "device_id": None,
            "sensor": None,
            "reasoning": "规则匹配：未检测到相关关键词"
        }


def classify_intent(user_text: str) -> dict:
    """
    意图识别入口：优先调用 LLM，失败时回退到规则匹配。
    """
    result = call_llm(user_text)
    if result and result.get("intent") in ("query_last", "collect_new", "capture_image",
                                           "ambiguous", "out_of_scope"):
        result["source"] = "llm"
        return result
    # 回退到规则匹配
    result = classify_intent_by_rules(user_text)
    result["source"] = "rules"
    return result

# 数据库文件
DB_FILE = "imu_data.db"

# Yaw 估算状态（陀螺仪积分）
_yaw_estimate = 0.0
_last_yaw_ts = 0.0
# 上一帧加速度（用于估算陀螺仪）
_prev_ax, _prev_ay, _prev_az = 0.0, 0.0, 0.0
_prev_ts = 0.0

# 任务超时时间（秒）
TASK_TIMEOUT_S = 30

# ==================== 图像采集配置（第 7 周任务） ====================
# 图像文件保存目录
IMAGE_DIR = os.path.join(os.path.dirname(__file__), "images")
# 最少保留：保留最近 N 张图像文件；已有人工纠错的图片永不清理
# （记录本身始终保留，便于审计和“输入—结果—反馈”对应）
IMAGE_KEEP_COUNT = 30
# 图像任务超时时间（秒）
IMAGE_TIMEOUT_S = 30
# 允许的设备与图像来源
IMAGE_SOURCES = {"device", "replay"}   # device=真机采集，replay=授权历史图像回放

# 样例对照口径（第 8 周）：四类基础桶 + 误判叠加视图
BUCKET_NOTES = {
    "normal": "模型给出结论且未被人工否定；不代表真实正确率",
    "low_score": "模型完成推理但分数低或候选接近，系统拒识",
    "unknown": "画面不可用或模型不可用，系统无法判断；含人工补充标签的拒识样本",
    "error": "采集失败、超时或图像无法解码，没有可用输入",
    "misjudged": "模型给出结论但被人工纠正；高分误判只表示模型分数高，不代表整体准确率",
}

os.makedirs(IMAGE_DIR, exist_ok=True)


def _ensure_column(c, table: str, column: str, decl: str):
    """幂等补列：旧库升级时不重建表、不丢历史数据。

    表名/列名/类型都来自本文件内的常量，不接受外部输入。
    """
    existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _update_device_status(c, device_id, now, *, last_seen_at=None,
                          boot_id=None, uptime_ms=None, imu_attempt=None,
                          imu_error=None, clear_imu_error=False,
                          imu_fail_count=None):
    """更新设备心跳/采样状态（每设备一行）。

    只记录状态，不写原始观测；失败不会覆盖 imu_data 的历史行。
    imu_error 只在本次明确提供时更新，避免心跳把已有错误清空。
    """
    c.execute("""
        INSERT OR IGNORE INTO device_status (device_id, last_seen_at, updated_at)
        VALUES (?, ?, ?)
    """, (device_id, now, now))
    c.execute("""
        UPDATE device_status
        SET last_seen_at = COALESCE(?, last_seen_at),
            boot_id = COALESCE(?, boot_id),
            uptime_ms = COALESCE(?, uptime_ms),
            imu_attempt = COALESCE(?, imu_attempt),
            imu_fail_count = COALESCE(?, imu_fail_count),
            updated_at = ?
        WHERE device_id = ?
    """, (last_seen_at, boot_id, uptime_ms, imu_attempt,
          imu_fail_count, now, device_id))
    if clear_imu_error:
        c.execute("UPDATE device_status SET imu_error=NULL WHERE device_id=?", (device_id,))
    elif imu_error is not None:
        c.execute("UPDATE device_status SET imu_error=? WHERE device_id=?", (imu_error, device_id))


def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # IMU 数据表（新增 request_id 字段）
    c.execute("""
        CREATE TABLE IF NOT EXISTS imu_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            ax REAL NOT NULL,
            ay REAL NOT NULL,
            az REAL NOT NULL,
            request_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_timestamp ON imu_data(timestamp)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_request_id ON imu_data(request_id)
    """)

    # 第 10 周：观测时间、接收时间、传输时延与来源标记。
    # observed_at 由服务器按设备单调时钟换算；旧行保持 NULL，不猜测 timestamp 单位。
    for column, decl in [
        ("observed_at", "REAL"),
        ("received_at", "REAL"),
        ("observed_uptime_ms", "INTEGER"),
        ("send_uptime_ms", "INTEGER"),
        ("boot_id", "TEXT"),
        ("age_basis", "TEXT"),
        ("provenance", "TEXT NOT NULL DEFAULT 'device'"),
    ]:
        _ensure_column(c, "imu_data", column, decl)

    # 旧行的接收时间可以从 created_at（UTC 文本）回填；观测时间仍保持未知。
    c.execute("""
        UPDATE imu_data
        SET received_at = CAST(strftime('%s', created_at) AS REAL)
        WHERE received_at IS NULL AND created_at IS NOT NULL
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_observed_at ON imu_data(observed_at)
    """)

    # 设备链路/采样状态（第 10 周）：复用 2s 轮询作心跳，不新增请求。
    c.execute("""
        CREATE TABLE IF NOT EXISTS device_status (
            device_id TEXT PRIMARY KEY,
            last_seen_at REAL,
            boot_id TEXT,
            uptime_ms INTEGER,
            imu_attempt TEXT,
            imu_error TEXT,
            imu_fail_count INTEGER DEFAULT 0,
            updated_at REAL
        )
    """)

    # 采集任务表
    c.execute("""
        CREATE TABLE IF NOT EXISTS collection_tasks (
            request_id TEXT PRIMARY KEY,
            device_id TEXT,
            sensor TEXT NOT NULL DEFAULT 'imu',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            completed_at REAL,
            data_id INTEGER,
            error_msg TEXT,
            FOREIGN KEY (data_id) REFERENCES imu_data(id)
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_status ON collection_tasks(status)
    """)

    # 求助请求表（第 3 周任务）
    c.execute("""
        CREATE TABLE IF NOT EXISTS help_requests (
            request_id TEXT PRIMARY KEY,
            device_id TEXT,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            responded_at REAL,
            response_msg TEXT
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_help_status ON help_requests(status)
    """)

    # 图像采集记录表（第 7 周任务）
    # 一条记录 = 一次按需取图请求，关联请求 ID、图像文件与采集时间
    c.execute("""
        CREATE TABLE IF NOT EXISTS image_records (
            request_id TEXT PRIMARY KEY,
            device_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            source TEXT NOT NULL DEFAULT 'device',
            file_path TEXT,
            size_bytes INTEGER,
            device_ts_us INTEGER,
            captured_at REAL,
            error_msg TEXT,
            created_at REAL NOT NULL
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_image_status ON image_records(status)
    """)

    # 视觉推理结果表（第 8 周任务）
    # 一条图像采集记录对应一条最新推理结果；模型版本随结果落库，保证可追溯
    c.execute("""
        CREATE TABLE IF NOT EXISTS inference_results (
            request_id TEXT PRIMARY KEY,
            label TEXT,
            score REAL,
            coarse TEXT,
            model_name TEXT,
            model_version TEXT,
            usability TEXT,
            usability_metrics TEXT,
            status TEXT NOT NULL DEFAULT 'ok',
            is_rejected INTEGER NOT NULL DEFAULT 0,
            rejected_reason TEXT,
            uncertain_note TEXT,
            topk TEXT,
            inferred_at REAL NOT NULL,
            FOREIGN KEY (request_id) REFERENCES image_records(request_id)
        )
    """)

    # 人工纠错记录表（第 8 周任务）
    # 模型原结果以快照一并留存，用于比较高分误判与正常样例；推理结果永不被覆盖
    c.execute("""
        CREATE TABLE IF NOT EXISTS human_feedbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            original_label TEXT,
            original_score REAL,
            corrected_label TEXT NOT NULL,
            note TEXT,
            created_at REAL NOT NULL
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_feedback_request ON human_feedbacks(request_id)
    """)

    conn.commit()
    conn.close()
    print("[DB] 数据库初始化完成")


@app.route("/")
def index():
    """Web 前端页面"""
    return render_template("index.html")


def _optional_uint(data: dict, key: str):
    """读取可选的非负整数字段；类型不对返回 None，不因扩展字段拒绝旧客户端。"""
    value = data.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _optional_boot_id(data: dict):
    """boot_id 只保留短的十六进制串，避免脏数据进入状态页。"""
    value = data.get("boot_id")
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if not value or len(value) > 32:
        return None
    if any(ch not in "0123456789abcdef" for ch in value):
        return None
    return value


@app.route("/api/imu", methods=["POST"])
def receive_imu():
    """接收 IMU 数据（支持 request_id 关联采集任务）。

    第 10 周扩展（向后兼容）：
      - 设备可上报 observed_uptime_ms / send_uptime_ms / boot_id；
      - 服务器用"接收时间 - 观测到发送的滞留"换算观测时刻；
      - 缺少新字段时 observed_at 保持 NULL，状态页显示"时效未知"，
        绝不把旧固件的 timestamp 猜成 Unix 时间。
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "无效的 JSON 数据"}), 400

    required_fields = ["timestamp", "ax", "ay", "az"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"缺少字段：{field}"}), 400

    request_id = data.get("request_id")
    device_id = str(data.get("device_id") or "esp32-s3-eye")[:32]
    boot_id = _optional_boot_id(data)
    observed_uptime_ms = _optional_uint(data, "observed_uptime_ms")
    send_uptime_ms = _optional_uint(data, "send_uptime_ms")

    now = time.time()
    observed_at = None
    age_basis = "unknown"
    transit_ms = None
    if observed_uptime_ms is not None and send_uptime_ms is not None:
        transit_ms = send_uptime_ms - observed_uptime_ms
        if 0 <= transit_ms <= source_state_mod.MAX_TRANSIT_MS:
            # 用设备自己的单调时钟测滞留，不要求设备与服务端对时。
            observed_at = now - transit_ms / 1000.0
            age_basis = "device_transit"

    # 存储到数据库（原始 timestamp 保持原样，不重新解释）
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO imu_data (
            timestamp, ax, ay, az, request_id, observed_at, received_at,
            observed_uptime_ms, send_uptime_ms, boot_id, age_basis, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'device')
    """, (data["timestamp"], data["ax"], data["ay"], data["az"], request_id,
          observed_at, now, observed_uptime_ms, send_uptime_ms, boot_id, age_basis))
    data_id = c.lastrowid

    # 成功上传同时证明链路可达；清空上一次读取错误。
    _update_device_status(
        c, device_id, now,
        last_seen_at=now,
        boot_id=boot_id,
        uptime_ms=send_uptime_ms,
        imu_attempt="ok",
        clear_imu_error=True,
    )

    # 如果有关联的采集任务，更新任务状态为已完成
    if request_id:
        c.execute(
            "UPDATE collection_tasks SET status='completed', completed_at=?, data_id=? WHERE request_id=? AND status IN ('sent','received')",
            (now, data_id, request_id)
        )

    conn.commit()
    conn.close()

    return jsonify({
        "status": "success",
        "message": "数据接收成功",
        "timestamp": data["timestamp"],
        "data_id": data_id,
        "observed_at": observed_at,
        "received_at": now,
        "age_basis": age_basis,
    }), 201


@app.route("/api/imu/latest", methods=["GET"])
def get_latest_imu():
    """获取最新的 IMU 数据（含模拟陀螺仪和姿态角）。

    第 10 周：
      - 按 observed_at（而不是插入 id）取最新，补传旧数据不会顶掉新数据；
      - 返回观测时间/接收时间/年龄，前端不再把开机微秒当 Unix 时间；
      - 陀螺仪/姿态仍保留兼容字段，但明确标记为"估算"。
    """
    import math
    import random
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT *
        FROM imu_data
        ORDER BY COALESCE(observed_at, received_at, 0) DESC, id DESC
        LIMIT 1
    """)
    row = c.fetchone()
    conn.close()

    if row:
        ax, ay, az = row["ax"], row["ay"], row["az"]
        # 基于加速度变化量估算陀螺仪（静止时趋近于零）
        global _prev_ax, _prev_ay, _prev_az, _prev_ts
        now = time.time()
        dt = (now - _prev_ts) if _prev_ts > 0 else 0.05
        _prev_ts = now
        # 角速度 ≈ 加速度变化率（简化模型）
        gx = (ay - _prev_ay) / dt * 0.1 + random.uniform(-0.005, 0.005)
        gy = (ax - _prev_ax) / dt * 0.1 + random.uniform(-0.005, 0.005)
        gz = random.uniform(-0.005, 0.005)  # Z 轴无加速度参考，仅保留微小噪声
        _prev_ax, _prev_ay, _prev_az = ax, ay, az
        # 从加速度计算姿态角（pitch/roll）
        pitch = math.degrees(math.atan2(ax, math.sqrt(ay*ay + az*az)))
        roll = math.degrees(math.atan2(ay, math.sqrt(ax*ax + az*az)))
        # yaw 通过陀螺仪 Z 轴积分估算（无磁力计，存在漂移）
        global _yaw_estimate, _last_yaw_ts
        _last_yaw_ts = now
        _yaw_estimate += gz * dt * 10  # 缩放系数使变化更明显
        _yaw_estimate = ((_yaw_estimate + 180) % 360) - 180  # 归一化到 [-180, 180]
        yaw = _yaw_estimate

        observed_at = row["observed_at"]
        received_at = row["received_at"]
        if received_at is None:
            received_at = source_state_mod._created_at_epoch(row["created_at"])
        age_ms = None
        if observed_at is not None:
            age_ms = max(0.0, (now - observed_at) * 1000.0)
        if age_ms is None:
            freshness = "unknown_age"
        elif age_ms > source_state_mod.ttl_config()["imu_ms"]:
            freshness = "stale"
        else:
            freshness = "ok"

        return jsonify({
            "timestamp": row["timestamp"],
            "ax": ax, "ay": ay, "az": az,
            "gx": gx, "gy": gy, "gz": gz,
            "pitch": pitch, "roll": roll, "yaw": yaw,
            "created_at": row["created_at"],
            "observed_at": observed_at,
            "received_at": received_at,
            "age_ms": None if age_ms is None else round(age_ms),
            "age_basis": row["age_basis"] or ("device_transit" if observed_at else "unknown"),
            "freshness": freshness,
            "is_stale": None if age_ms is None else bool(age_ms > source_state_mod.ttl_config()["imu_ms"]),
            "derived": {"gyro": True, "attitude": True, "yaw": True},
            "derived_note": "gx/gy/gz、pitch/roll/yaw 由加速度变化估算，仅用于展示，不是传感器观测；yaw 无磁力计会漂移。",
        })
    else:
        return jsonify({"error": "暂无数据"}), 404


@app.route("/api/imu/history", methods=["GET"])
def get_imu_history():
    """获取历史 IMU 数据"""
    limit = request.args.get("limit", 100, type=int)
    limit = min(limit, 1000)  # 最多返回 1000 条

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT *
        FROM imu_data
        ORDER BY COALESCE(observed_at, received_at, 0) DESC, id DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()

    now = time.time()
    data = []
    for row in rows:
        observed_at = row["observed_at"]
        age_ms = None
        if observed_at is not None:
            age_ms = max(0.0, (now - observed_at) * 1000.0)
        data.append({
            "timestamp": row["timestamp"],
            "ax": row["ax"],
            "ay": row["ay"],
            "az": row["az"],
            "created_at": row["created_at"],
            "observed_at": observed_at,
            "received_at": row["received_at"],
            "age_ms": None if age_ms is None else round(age_ms),
            "age_basis": row["age_basis"] or ("device_transit" if observed_at else "unknown"),
            "provenance": row["provenance"],
        })

    return jsonify({"data": data, "count": len(data)})


@app.route("/api/stats", methods=["GET"])
def get_stats():
    """获取数据统计"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # 总记录数
    c.execute("SELECT COUNT(*) FROM imu_data")
    total = c.fetchone()[0]

    # 最新记录时间
    c.execute("SELECT MAX(created_at) FROM imu_data")
    latest = c.fetchone()[0]

    conn.close()

    return jsonify({
        "total_records": total,
        "latest_record": latest or "无"
    })


@app.route("/api/sources", methods=["GET"])
def get_sources():
    """第 10 周：各来源独立状态、数据年龄与对照案例。

    纯读接口：不修改原始观测，不清理过期数据。
    """
    return jsonify(source_state_mod.build_report(
        DB_FILE, now=source_state_mod.now_epoch()
    ))


# ==================== 远程采集指令 API ====================

@app.route("/api/collect", methods=["POST"])
def create_collect_task():
    """创建采集任务，返回 request_id"""
    data = request.get_json() or {}
    sensor = data.get("sensor", "imu")
    device_id = data.get("device_id", "esp32-s3-eye")

    request_id = str(uuid.uuid4())[:8]
    now = time.time()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO collection_tasks (request_id, device_id, sensor, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (request_id, device_id, sensor, now)
    )
    conn.commit()
    conn.close()

    return jsonify({
        "request_id": request_id,
        "status": "pending",
        "created_at": now
    }), 201


@app.route("/api/collect/<request_id>", methods=["GET"])
def get_task_status(request_id):
    """查询采集任务状态和结果"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # 检查超时
    c.execute("SELECT status, created_at FROM collection_tasks WHERE request_id=?", (request_id,))
    row = c.fetchone()
    if row:
        status, created_at = row
        if status == "pending" and (time.time() - created_at) > TASK_TIMEOUT_S:
            c.execute("UPDATE collection_tasks SET status='timeout' WHERE request_id=?", (request_id,))
            conn.commit()
            status = "timeout"

    # 获取任务详情
    c.execute("""
        SELECT t.request_id, t.device_id, t.sensor, t.status,
               t.created_at, t.completed_at, t.error_msg,
               d.ax, d.ay, d.az, d.timestamp as data_timestamp
        FROM collection_tasks t
        LEFT JOIN imu_data d ON t.data_id = d.id
        WHERE t.request_id=?
    """, (request_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "任务不存在"}), 404

    result = {
        "request_id": row[0],
        "device_id": row[1],
        "sensor": row[2],
        "status": row[3],
        "created_at": row[4],
        "completed_at": row[5],
    }
    if row[6]:
        result["error_msg"] = row[6]
    if row[7] is not None:
        result["data"] = {
            "ax": row[7],
            "ay": row[8],
            "az": row[9],
            "timestamp": row[10]
        }

    return jsonify(result)


@app.route("/api/collect/pending", methods=["GET"])
def get_pending_task():
    """ESP32 轮询待执行命令（返回最新的 pending 任务并标记为 sent）

    第 7 周扩展：图像采集任务优先返回，响应中带 type 字段区分任务类型：
      type=image -> 按需图像采集（sensor=camera）
      type=imu   -> IMU 数据采集（原有逻辑）
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # 第 10 周：复用 2s 轮询作设备心跳，不新增 HTTP 请求。
    # 只接受状态码白名单，URL 参数不会直接拼进 SQL。
    now = time.time()
    device_id = (request.args.get("device_id") or "esp32-s3-eye")[:32]
    imu_state = (request.args.get("imu") or "").lower()
    imu_error = (request.args.get("err") or "").lower()
    if imu_state in ("ok", "fail", "busy"):
        fails = request.args.get("fails", type=int)
        if fails is not None:
            fails = max(0, min(int(fails), 10 ** 9))
        _update_device_status(
            c, device_id, now,
            last_seen_at=now,
            imu_attempt=imu_state,
            imu_error=imu_error if imu_error in
            ("none", "read", "timeout", "init", "camera_busy", "other") else None,
            imu_fail_count=fails,
        )
    else:
        _update_device_status(c, device_id, now, last_seen_at=now)
    conn.commit()

    # 1. 先检查待执行的图像采集任务（仅 device 来源由设备执行，replay 仅用于回放演示）
    c.execute("""
        SELECT request_id, device_id, created_at
        FROM image_records
        WHERE status='pending' AND source='device'
        ORDER BY created_at ASC
        LIMIT 1
    """)
    img_row = c.fetchone()

    if img_row:
        c.execute("UPDATE image_records SET status='sent' WHERE request_id=?", (img_row[0],))
        conn.commit()
        conn.close()
        return jsonify({
            "request_id": img_row[0],
            "device_id": img_row[1],
            "sensor": "camera",
            "type": "image",
            "created_at": img_row[2]
        })

    # 2. 再检查第 9 周的主动询问（未投递过的 asking 询问，投递一次即标记）
    #    顺序：image → inquiry → imu。询问不影响图像任务状态，仅复用同一条轮询通道。
    inq = inquiry_mod.pending_for_device()
    if inq:
        conn.close()
        return jsonify({
            "request_id": inq["inquiry_id"],
            "device_id": inq["device_id"],
            "sensor": "camera",
            "type": "inquiry",
            "reason": inq["reason"],
            "reason_label": inq["reason_label"],
            "question": inq["question"],
            "attempt_no": inq["attempt_no"],
            "max_retake": inq["max_retake"],
            "expires_in_s": inq["remaining_s"],
            "created_at": inq["created_at"]
        })

    # 3. 再检查 IMU 采集任务
    c.execute("""
        SELECT request_id, device_id, sensor, created_at
        FROM collection_tasks
        WHERE status='pending'
        ORDER BY created_at ASC
        LIMIT 1
    """)
    row = c.fetchone()

    if row:
        request_id = row[0]
        c.execute("UPDATE collection_tasks SET status='sent' WHERE request_id=?", (request_id,))
        conn.commit()
        conn.close()
        return jsonify({
            "request_id": row[0],
            "device_id": row[1],
            "sensor": row[2],
            "type": "imu",
            "created_at": row[3]
        })
    else:
        conn.close()
        return jsonify({"request_id": None}), 200


@app.route("/api/collect/<request_id>/received", methods=["POST"])
def mark_task_received(request_id):
    """ESP32 确认已接收命令"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE collection_tasks SET status='received' WHERE request_id=? AND status='sent'", (request_id,))
    conn.commit()
    conn.close()

    if c.rowcount == 0:
        return jsonify({"error": "任务状态不正确或不存在"}), 404
    return jsonify({"status": "received"})


@app.route("/api/collect/<request_id>/fail", methods=["POST"])
def mark_task_failed(request_id):
    """ESP32 上报采集失败"""
    data = request.get_json() or {}
    error_msg = data.get("error", "未知错误")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "UPDATE collection_tasks SET status='failed', error_msg=?, completed_at=? WHERE request_id=?",
        (error_msg, time.time(), request_id)
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "failed"})


@app.route("/api/tasks", methods=["GET"])
def get_all_tasks():
    """获取所有采集任务列表"""
    limit = request.args.get("limit", 20, type=int)
    limit = min(limit, 100)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT request_id, device_id, sensor, status,
               created_at, completed_at, error_msg
        FROM collection_tasks
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()

    tasks = []
    for row in rows:
        tasks.append({
            "request_id": row[0],
            "device_id": row[1],
            "sensor": row[2],
            "status": row[3],
            "created_at": row[4],
            "completed_at": row[5],
            "error_msg": row[6]
        })

    return jsonify({"tasks": tasks, "count": len(tasks)})


# ==================== 求助请求 API（第 3 周任务） ====================

@app.route("/api/help", methods=["POST"])
def create_help_request():
    """创建求助请求（ESP32 按键触发）"""
    data = request.get_json() or {}

    request_id = data.get("request_id")
    if not request_id:
        request_id = str(uuid.uuid4())[:8]

    device_id = data.get("device_id", "esp32-s3-eye")
    message = data.get("message", "教学求助：需要帮助")
    now = time.time()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO help_requests (request_id, device_id, message, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (request_id, device_id, message, now)
    )
    conn.commit()
    conn.close()

    return jsonify({
        "request_id": request_id,
        "status": "pending",
        "created_at": now
    }), 201


@app.route("/api/help/<request_id>", methods=["GET"])
def get_help_status(request_id):
    """查询求助请求状态（ESP32 轮询回应）"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT request_id, device_id, message, status,
               created_at, responded_at, response_msg
        FROM help_requests
        WHERE request_id=?
    """, (request_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "求助请求不存在"}), 404

    result = {
        "request_id": row[0],
        "device_id": row[1],
        "message": row[2],
        "status": row[3],
        "created_at": row[4],
    }
    if row[5]:
        result["responded_at"] = row[5]
    if row[6]:
        result["response_msg"] = row[6]

    return jsonify(result)


@app.route("/api/help/<request_id>/respond", methods=["POST"])
def respond_help_request(request_id):
    """回应求助请求（Web 前端操作）"""
    data = request.get_json() or {}
    response_msg = data.get("message", "已收到求助，正在处理")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "UPDATE help_requests SET status='responded', responded_at=?, response_msg=? WHERE request_id=? AND status='pending'",
        (time.time(), response_msg, request_id)
    )
    conn.commit()
    conn.close()

    if c.rowcount == 0:
        return jsonify({"error": "求助请求状态不正确或不存在"}), 404

    return jsonify({"status": "responded"})


@app.route("/api/help/<request_id>/cancel", methods=["POST"])
def cancel_help_request(request_id):
    """取消求助请求（Web 前端操作）"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "UPDATE help_requests SET status='cancelled', responded_at=? WHERE request_id=? AND status='pending'",
        (time.time(), request_id)
    )
    conn.commit()
    conn.close()

    if c.rowcount == 0:
        return jsonify({"error": "求助请求状态不正确或不存在"}), 404

    return jsonify({"status": "cancelled"})


@app.route("/api/help", methods=["GET"])
def get_all_help_requests():
    """获取所有求助请求列表"""
    limit = request.args.get("limit", 20, type=int)
    limit = min(limit, 100)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT request_id, device_id, message, status,
               created_at, responded_at, response_msg
        FROM help_requests
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()

    help_list = []
    for row in rows:
        help_list.append({
            "request_id": row[0],
            "device_id": row[1],
            "message": row[2],
            "status": row[3],
            "created_at": row[4],
            "responded_at": row[5],
            "response_msg": row[6]
        })

    return jsonify({"help_requests": help_list, "count": len(help_list)})


# ==================== 按需图像采集 API（第 7 周任务） ====================

def _purge_old_images(keep=IMAGE_KEEP_COUNT):
    """
    最少保留策略：保留最近 keep 张图像文件。

    已有人工纠错的图片永不清理，保证“输入—结果—反馈”证据链不丢。
    记录本身始终保留（失败记录与审计需要），仅清理文件并置空 file_path。
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT request_id FROM human_feedbacks")
    pinned = {row[0] for row in c.fetchall()}

    c.execute("""
        SELECT request_id, file_path
        FROM image_records
        WHERE file_path IS NOT NULL
        ORDER BY created_at DESC
    """)
    rows = c.fetchall()

    purged = 0
    for request_id, file_path in rows[keep:]:
        if request_id in pinned:
            continue
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                purged += 1
        except OSError:
            pass
        c.execute("""
            UPDATE image_records
            SET file_path=NULL,
                error_msg=COALESCE(error_msg || ' ', '') || 'file purged by retention'
            WHERE request_id=?
        """, (request_id,))

    conn.commit()
    conn.close()
    if purged:
        print(f"[IMAGE] 保留策略清理了 {purged} 个旧文件（保留最近 {keep} 张，纠错样本不清理）")


def _image_mark_timeout(c, request_id):
    """将超时未完成的图像任务标记为 timeout"""
    c.execute("SELECT status, created_at FROM image_records WHERE request_id=?", (request_id,))
    row = c.fetchone()
    if row and row[0] in ("pending", "sent", "received") and (time.time() - row[1]) > IMAGE_TIMEOUT_S:
        c.execute("UPDATE image_records SET status='timeout' WHERE request_id=?", (request_id,))
        return "timeout"
    return row[0] if row else None


def _image_dict(row):
    """把图像记录行转成响应结构"""
    return {
        "request_id": row[0],
        "device_id": row[1],
        "status": row[2],
        "source": row[3],
        "size_bytes": row[5],
        "device_ts_us": row[6],
        "captured_at": row[7],
        "captured_at_text": (datetime.fromtimestamp(row[7]).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                            if row[7] else None),
        "error_msg": row[8],
        "created_at": row[9],
        "file_url": f"/api/image/{row[0]}/file" if row[4] else None,
    }


def create_image_task(device_id="esp32-s3-eye", source="device"):
    """
    创建一个按需图像采集任务。

    这是唯一的取图入口：只有本函数被调用（用户请求/教学事件）才会产生采集任务，
    设备端不做任何主动拍照。
    """
    request_id = str(uuid.uuid4())[:8]
    now = time.time()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO image_records (request_id, device_id, status, source, created_at)
        VALUES (?, ?, 'pending', ?, ?)
    """, (request_id, device_id, source, now))
    conn.commit()
    conn.close()

    logger.info(f"创建图像采集任务: {request_id}", extra={"request_id": request_id})
    return request_id, now


# ==================== 第 9 周：画面不可用的主动询问 ====================
# 询问模块独立成表（image_inquiries），不改动上面四张已有表的结构；
# 重拍复用上面的 create_image_task（唯一的取图入口），因此重拍走的是
# 第 7 周原有的 pending→sent→received→captured 图像任务流，不新增设备任务类型。
import inquiry as inquiry_mod  # noqa: E402  （放在函数定义之后，避免与 create_image_task 顺序耦合）

inquiry_mod.configure(
    DB_FILE,
    create_image_task,
    log=lambda msg, rid=None: logger.info(msg, extra={"request_id": rid or "-"}),
)
app.register_blueprint(inquiry_mod.bp)
inquiry_mod.init_db()


@app.route("/api/image", methods=["POST"])
def create_image():
    """
    创建按需图像采集任务。

    请求体：{"device_id": "esp32-s3-eye", "source": "device"|"replay"}
    返回：{"request_id", "status": "pending", "created_at"}
    """
    data = request.get_json() or {}
    device_id = data.get("device_id", "esp32-s3-eye")
    source = data.get("source", "device")

    if device_id not in VALID_DEVICES:
        return jsonify({"error": f"设备 '{device_id}' 不在可用范围内"}), 400
    if source not in IMAGE_SOURCES:
        return jsonify({"error": f"图像来源 '{source}' 不合法"}), 400

    request_id, now = create_image_task(device_id, source)
    return jsonify({
        "request_id": request_id,
        "status": "pending",
        "source": source,
        "created_at": now
    }), 201


@app.route("/api/image/<request_id>/received", methods=["POST"])
def image_received(request_id):
    """确认已接收图像任务（状态 sent -> received，前端显示'采集中'）

    source=replay 的任务（授权历史图像回放）不走设备轮询，
    允许由回放脚本从 pending 直接置为 received。
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        UPDATE image_records SET status='received'
        WHERE request_id=?
          AND (status='sent' OR (status='pending' AND source='replay'))
    """, (request_id,))
    conn.commit()
    rowcount = c.rowcount
    conn.close()

    if rowcount == 0:
        return jsonify({"error": "任务状态不正确或不存在"}), 404
    logger.info("设备已接收图像任务", extra={"request_id": request_id})
    return jsonify({"status": "received"})


@app.route("/api/image/<request_id>/data", methods=["POST"])
def image_upload(request_id):
    """
    接收设备上传的 JPEG 图像。

    安全约束（当堂验证重点）：
      只有本任务处于 sent/received 状态时才接受数据，
      绝不复用或引用上一张图像，保证"上次成功图像不会被标为本次采集成功"。
    """
    image_bytes = request.get_data()
    if not image_bytes:
        return jsonify({"error": "空图像数据"}), 400
    if len(image_bytes) < 128 or not image_bytes.startswith(b"\xff\xd8"):
        return jsonify({"error": "不是有效的 JPEG 数据"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT status FROM image_records WHERE request_id=?", (request_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "任务不存在"}), 404

    status = _image_mark_timeout(c, request_id)
    if status not in ("sent", "received"):
        conn.commit()
        conn.close()
        return jsonify({"error": f"任务状态为 {status}，不接受图像"}), 409

    # 保存文件：文件名与 request_id 一一对应
    file_path = os.path.join(IMAGE_DIR, f"{request_id}.jpg")
    with open(file_path, "wb") as f:
        f.write(image_bytes)

    # 设备侧采集时间（微秒，开机计时，仅作参考）
    device_ts_us = request.headers.get("X-Capture-Ts")
    try:
        device_ts_us = int(device_ts_us) if device_ts_us else None
    except ValueError:
        device_ts_us = None

    captured_at = time.time()
    c.execute("""
        UPDATE image_records
        SET status='captured', file_path=?, size_bytes=?, device_ts_us=?,
            captured_at=?, error_msg=NULL
        WHERE request_id=?
    """, (file_path, len(image_bytes), device_ts_us, captured_at, request_id))
    conn.commit()
    conn.close()

    _purge_old_images()

    # 第 8 周：采集成功后自动跑一次视觉推理。
    # 推理失败（模型不可用/解码失败等）只影响推理记录，不影响采集结果本身。
    try:
        _run_image_inference(request_id, file_path)
    except Exception as e:
        logger.warning(f"自动推理异常（不影响采集结果）: {e}",
                       extra={"request_id": request_id})

    logger.info(f"图像入库: {len(image_bytes)} 字节", extra={"request_id": request_id})
    return jsonify({
        "status": "captured",
        "request_id": request_id,
        "size_bytes": len(image_bytes),
        "captured_at": captured_at
    }), 201


@app.route("/api/image/<request_id>/fail", methods=["POST"])
def image_fail(request_id):
    """
    设备上报采集失败。

    请求体：{"reason": "blocked"|"error", "error": "描述"}
    reason=blocked 表示镜头遮挡/画面不可用，其余归为 failed。
    """
    data = request.get_json() or {}
    reason = data.get("reason", "error")
    error_msg = data.get("error", "未知错误")

    status = "blocked" if reason == "blocked" else "failed"

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        UPDATE image_records
        SET status=?, error_msg=?
        WHERE request_id=? AND status IN ('sent','received')
    """, (status, error_msg, request_id))
    conn.commit()
    rowcount = c.rowcount
    conn.close()

    if rowcount == 0:
        return jsonify({"error": "任务状态不正确或不存在"}), 404

    logger.warning(f"图像采集失败: {status} - {error_msg}", extra={"request_id": request_id})

    # 第 9 周：采集失败/遮挡也是一次"画面无法使用"事件，交给询问模块做门控。
    # 失败只影响这条事件，不改变本周的失败上报语义（仍返回 blocked/failed）。
    try:
        inquiry_mod.on_unusable(
            request_id,
            "blocked" if status == "blocked" else "capture_failed",
            trigger="device",
        )
    except Exception as e:
        logger.warning(f"询问事件处理异常（不影响失败上报）: {e}",
                       extra={"request_id": request_id})

    return jsonify({"status": status, "request_id": request_id})


@app.route("/api/image/<request_id>", methods=["GET"])
def get_image_status(request_id):
    """
    查询单次图像采集任务的状态与结果。

    注意力：只返回本任务自己的图像，任务未成功时 file_url 为 null，
    不会回退到上一张成功图像。
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    _image_mark_timeout(c, request_id)
    conn.commit()

    c.execute("""
        SELECT request_id, device_id, status, source, file_path,
               size_bytes, device_ts_us, captured_at, error_msg, created_at
        FROM image_records WHERE request_id=?
    """, (request_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "任务不存在"}), 404

    return jsonify(_image_dict(row))


@app.route("/api/image/<request_id>/file", methods=["GET"])
def get_image_file(request_id):
    """返回图像文件；任务未成功或无文件时返回 404"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT file_path, status FROM image_records WHERE request_id=?", (request_id,))
    row = c.fetchone()
    conn.close()

    if not row or not row[0] or row[1] != "captured":
        return jsonify({"error": "该任务没有可用图像"}), 404
    if not os.path.exists(row[0]):
        return jsonify({"error": "图像文件已被清理"}), 404

    return send_file(row[0], mimetype="image/jpeg")


@app.route("/api/images", methods=["GET"])
def get_images():
    """获取最近的图像采集记录列表"""
    limit = request.args.get("limit", IMAGE_KEEP_COUNT, type=int)
    limit = min(limit, 100)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT request_id, device_id, status, source, file_path,
               size_bytes, device_ts_us, captured_at, error_msg, created_at
        FROM image_records
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()

    images = [_image_dict(r) for r in rows]
    return jsonify({"images": images, "count": len(images), "keep_count": IMAGE_KEEP_COUNT})


# ==================== 视觉推理与反馈 API（第 8 周任务） ====================
#
# 设计要点（对应本周问题：模型返回一个标签和分数，系统如何表达
# "看到了什么"和"还有什么不能确定"）：
#   1. 输入（原图/request_id）— 模型结果（label/score/model_version）— 人工反馈
#      三者通过同一个 request_id 严格对应；
#   2. 低分 / 画面不可用 / 类别模糊 -> 拒识，明确输出"还不能确定"及原因，
#      绝不给硬结论；场景类别不转换为精确位置或人身安全判断；
#   3. 人工纠错只新增记录并快照模型原结果，推理结果永不被覆盖，
#      用于比较高分误判与正常样例；
#   4. 分数仅表述为"模型输出分数（未经校准）"，不等于现实正确率。


def _topk_to_dicts(topk):
    """把 (label, score) 元组/旧版数组统一成前端可读的对象数组。"""
    result = []
    for item in topk or []:
        if isinstance(item, dict):
            label = item.get("label")
            score = item.get("score")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            label, score = item[0], item[1]
        else:
            continue
        if label is None or score is None:
            continue
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        result.append({"label": str(label), "score": score})
    return result


def _model_source(model_name, model_version):
    """把模型内部名称映射为前端可理解的来源。"""
    name = (model_name or "").lower()
    version = (model_version or "").lower()
    if name == "unavailable" or not name:
        return "unavailable"
    if "student" in name:
        return "student"
    if name.endswith("_http") or "teacher-http" in version:
        return "teacher_http"
    if name.endswith("_onnx") or "onnx" in version:
        return "teacher_local_onnx"
    return "teacher_baseline"


def _decision_dict(topk, usability, usability_metrics, rejected_reason):
    """生成“为什么给出/不给出结论”的可解释判定依据。

    分数只作为模型输出分数展示，不解释为正确率。
    """
    top1 = topk[0] if len(topk) > 0 else None
    top2 = topk[1] if len(topk) > 1 else None
    margin = None
    if top1 is not None and top2 is not None:
        margin = round(float(top1["score"]) - float(top2["score"]), 6)

    return {
        "top1": top1,
        "top2": top2,
        "margin": margin,
        "low_score_threshold": LOW_SCORE_THRESHOLD,
        "ambiguous_margin": AMBIGUOUS_MARGIN,
        "usability": usability,
        "usability_metrics": {
            k: round(float(v), 2) for k, v in (usability_metrics or {}).items()
        },
        "rejected_reason": rejected_reason,
    }


def _sample_bucket(img_status, inference, has_feedback):
    """统一样例分类口径。

    normal     模型接受且无纠错
    low_score  模型拒识原因为 low_score/ambiguous，且无纠错
    unknown    画面/模型不可用，或拒识后被人工补充答案
    error      采集失败、超时、JPEG 无法解码
    misjudged  模型接受但被人工纠错（叠加视图）
    """
    if img_status in ("failed", "blocked", "timeout"):
        return "error"
    if inference and inference.get("status") == "decode_failed":
        return "error"

    accepted = bool(
        inference
        and inference.get("status") == "ok"
        and not inference.get("is_rejected")
    )

    if has_feedback:
        return "misjudged" if accepted else "unknown"
    if accepted:
        return "normal"
    if inference and inference.get("rejected_reason") in ("low_score", "ambiguous"):
        return "low_score"
    return "unknown"


def _store_inference(request_id, result):
    """把一次推理结果写入 inference_results（同一 request_id 覆盖为最新一次）。"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO inference_results
        (request_id, label, score, coarse, model_name, model_version, usability,
         usability_metrics, status, is_rejected, rejected_reason, uncertain_note,
         topk, inferred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        request_id, result.label, result.score, result.coarse, result.model_name,
        result.model_version, result.usability,
        json.dumps(result.usability_metrics), result.status,
        1 if result.is_rejected else 0, result.rejected_reason,
        result.uncertain_note, json.dumps(_topk_to_dicts(result.topk)), time.time(),
    ))
    conn.commit()
    conn.close()

    # 第 9 周：把推理结果接入询问状态。
    #   画面不可用（dark/blur/blocked/模型不可用/解码失败）→ 可能是询问的触发源；
    #   模型正常接受 → 若这张图是一次重拍，则关闭对应询问为 resolved。
    # 低分 / 候选接近不在这里询问：那是"模型不确定"，不是"画面无法使用"。
    try:
        # 1) 画面客观可用（usability=clear）→ 若这张是一次重拍，关闭询问为 resolved。
        #    低分/候选接近也属于"画面可用"，只是模型不确定，不应把重拍判成失败；
        #    但 decode_failed / unavailable 的 usability 字段没有真实测量值（默认 clear），
        #    因此必须同时限定 status，否则会把"解码失败"误判成"重拍成功"。
        if result.status in ("ok", "rejected") and result.usability == "clear":
            inquiry_mod.on_usable(request_id)

        # 2) 画面不可用 / 模型不可用 → 触发新询问或推进已有询问的重拍计数
        if result.is_rejected and result.rejected_reason in inquiry_mod.REASONS:
            inquiry_mod.on_unusable(
                request_id,
                result.rejected_reason,
                trigger="model" if result.status == "unavailable" else "server",
                snapshot={
                    "usability": result.usability,
                    "usability_metrics": result.usability_metrics,
                    "rejected_reason": result.rejected_reason,
                    "model_version": result.model_version,
                    "score": result.score,
                },
            )
    except Exception as e:
        logger.warning(f"询问事件处理异常（不影响推理结果）: {e}",
                       extra={"request_id": request_id})


def _run_image_inference(request_id, file_path):
    """读取已采集图像并推理落库，返回推理结果（不可用时返回 unavailable 结果）"""
    if not file_path or not os.path.exists(file_path):
        return None
    with open(file_path, "rb") as f:
        image_bytes = f.read()

    result = run_inference(image_bytes)
    _store_inference(request_id, result)
    logger.info(
        f"视觉推理: status={result.status} label={result.label} "
        f"score={result.score} model={result.model_version}",
        extra={"request_id": request_id},
    )
    return result


def _inference_dict(request_id):
    """查询某任务的推理结果（无则 None）。"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT request_id, label, score, coarse, model_name, model_version,
               usability, usability_metrics, status, is_rejected,
               rejected_reason, uncertain_note, topk, inferred_at
        FROM inference_results WHERE request_id=?
    """, (request_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None

    try:
        raw_topk = json.loads(row[12]) if row[12] else []
    except (TypeError, ValueError):
        raw_topk = []
    topk = _topk_to_dicts(raw_topk)

    try:
        usability_metrics = json.loads(row[7]) if row[7] else {}
    except (TypeError, ValueError):
        usability_metrics = {}

    return {
        "request_id": row[0],
        "label": row[1],
        "score": row[2],
        "coarse": row[3],
        "model_name": row[4],
        "model_version": row[5],
        "model_source": _model_source(row[4], row[5]),
        "usability": row[6],
        "usability_metrics": usability_metrics,
        "status": row[8],
        "is_rejected": bool(row[9]),
        "rejected_reason": row[10],
        "uncertain_note": row[11],
        "topk": topk,
        "decision": _decision_dict(topk, row[6], usability_metrics, row[10]),
        "inferred_at": row[13],
        "inferred_at_text": (datetime.fromtimestamp(row[13]).strftime("%Y-%m-%d %H:%M:%S")
                             if row[13] else None),
        "score_note": SCORE_NOTE,
        "coarse_note": COARSE_NOTE if row[3] else None,
    }


def _feedback_dict(request_id):
    """查询某任务的人工纠错记录（最新一条，无则 None）"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT corrected_label, note, original_label, original_score, created_at
        FROM human_feedbacks WHERE request_id=? ORDER BY id DESC LIMIT 1
    """, (request_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "corrected_label": row[0],
        "note": row[1],
        "original_label": row[2],
        "original_score": row[3],
        "created_at": row[4],
        "created_at_text": (datetime.fromtimestamp(row[4]).strftime("%Y-%m-%d %H:%M:%S")
                            if row[4] else None),
    }


@app.route("/api/image/<request_id>/infer", methods=["POST"])
def run_image_inference_api(request_id):
    """对已采集的图像执行（重新）推理。

    仅对 status=captured 且文件仍存在的任务有效；
    已有人工纠错的任务锁定推理结果，避免覆盖“结果—反馈”对应；
    模型不可用时返回 503 并说明原因（不伪造结果）。
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT status, file_path FROM image_records WHERE request_id=?",
              (request_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "任务不存在"}), 404
    if row[0] != "captured" or not row[1]:
        conn.close()
        return jsonify({"error": f"任务状态为 {row[0]}，没有可推理的图像"}), 409
    if not os.path.exists(row[1]):
        conn.close()
        return jsonify({"error": "图像文件已被保留策略清理，无法推理"}), 409

    c.execute("SELECT 1 FROM human_feedbacks WHERE request_id=? LIMIT 1", (request_id,))
    if c.fetchone():
        conn.close()
        return jsonify({
            "error": "已有人工纠错，推理结果已锁定；如需新模型结果请重新采集"
        }), 409
    conn.close()

    result = _run_image_inference(request_id, row[1])
    if result is not None and result.status == "unavailable":
        return jsonify({"error": result.uncertain_note or "视觉模型不可用"}), 503
    return jsonify({
        "status": "ok",
        "inference": _inference_dict(request_id),
        "score_note": SCORE_NOTE,
    })


@app.route("/api/image/<request_id>/result", methods=["GET"])
def get_image_result(request_id):
    """结构化推理结果：输入（图像记录）+ 模型结果 + 人工反馈，三者同 request_id 对应。

    响应固定带 score_note：分数是未经校准的模型输出，不等于现实正确率。
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT request_id, device_id, status, source, file_path,
               size_bytes, device_ts_us, captured_at, error_msg, created_at
        FROM image_records WHERE request_id=?
    """, (request_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "任务不存在"}), 404

    inference = _inference_dict(request_id)
    feedback = _feedback_dict(request_id)
    bucket = _sample_bucket(row[2], inference, feedback is not None)

    return jsonify({
        "image": _image_dict(row),
        "inference": inference,
        "feedback": feedback,
        "bucket": bucket,
        "bucket_note": BUCKET_NOTES.get(bucket, ""),
        "score_note": SCORE_NOTE,
    })


@app.route("/api/image/<request_id>/feedback", methods=["POST"])
def submit_image_feedback(request_id):
    """人工纠错入口：登记"模型判错了，正确应该是什么"。

    模型原结果以快照一并留存（original_label/original_score），
    用于后续比较高分误判与正常样例；不修改、不覆盖推理结果。
    """
    data = request.get_json() or {}
    corrected_label = (data.get("corrected_label") or "").strip()
    note = (data.get("note") or "").strip()

    if not corrected_label:
        return jsonify({"error": "corrected_label 不能为空"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT status FROM image_records WHERE request_id=?", (request_id,))
    img = c.fetchone()
    if not img:
        conn.close()
        return jsonify({"error": "任务不存在"}), 404

    # 快照当前模型结果（可能为 None：未推理或已拒识）
    inference = _inference_dict(request_id)
    c.execute("""
        INSERT INTO human_feedbacks
        (request_id, original_label, original_score, corrected_label, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        request_id,
        inference["label"] if inference else None,
        inference["score"] if inference else None,
        corrected_label, note, time.time(),
    ))
    conn.commit()
    conn.close()

    logger.info(
        f"人工纠错: 模型原判={inference['label'] if inference else None} -> {corrected_label}",
        extra={"request_id": request_id},
    )
    updated_feedback = _feedback_dict(request_id)
    bucket = _sample_bucket(img[0], inference, True)
    return jsonify({
        "status": "ok",
        "feedback": updated_feedback,
        "inference": inference,
        "bucket": bucket,
        "bucket_note": BUCKET_NOTES.get(bucket, ""),
        "score_note": SCORE_NOTE,
    })


@app.route("/api/samples/compare", methods=["GET"])
def get_samples_compare():
    """样例对照：正常 / 低分 / 未知 / 错误 + 误判叠加视图。

    分类口径（按优先级）：
      error      采集失败（failed/blocked/timeout）或图像无法解码
      misjudged  模型接受但被人工纠错；分数 >= HIGH_SCORE_THRESHOLD 标记高分误判
      unknown    画面不可用 / 模型不可用；拒识后被人工补充答案也归入此桶
      low_score  模型拒识原因为 low_score / ambiguous，且无纠错
      normal     模型接受且无纠错（不代表真实正确率）
    """
    limit_per_bucket = request.args.get("limit_per_bucket", 8, type=int)
    limit_per_bucket = max(1, min(limit_per_bucket, 20))

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    buckets = {key: [] for key in ("normal", "low_score", "unknown", "error", "misjudged")}

    # 取最近 200 条采集记录，保证稀有类别也有机会出现
    c.execute("""
        SELECT request_id, device_id, status, source, file_path,
               size_bytes, device_ts_us, captured_at, error_msg, created_at
        FROM image_records ORDER BY created_at DESC LIMIT 200
    """)
    img_rows = c.fetchall()

    for img in img_rows:
        rid = img[0]
        c.execute("""
            SELECT request_id, label, score, coarse, model_name, model_version,
                   usability, usability_metrics, status, is_rejected,
                   rejected_reason, uncertain_note, topk, inferred_at
            FROM inference_results WHERE request_id=?
        """, (rid,))
        inf_row = c.fetchone()
        c.execute("""
            SELECT corrected_label, note, original_label, original_score, created_at
            FROM human_feedbacks WHERE request_id=? ORDER BY id DESC LIMIT 1
        """, (rid,))
        fb = c.fetchone()

        # 未完成的任务没有输入/结果，不进入样例对照
        if img[2] in ("pending", "sent", "received") and inf_row is None:
            continue

        inference = None
        if inf_row:
            inference = {
                "status": inf_row[8],
                "is_rejected": bool(inf_row[9]),
                "rejected_reason": inf_row[10],
                "label": inf_row[1],
                "score": inf_row[2],
            }
        bucket = _sample_bucket(img[2], inference, fb is not None)
        if len(buckets[bucket]) >= limit_per_bucket:
            continue

        # 统一样例结构：输入 / 模型结果 / 人工反馈三段同屏可对照
        item = {
            "request_id": rid,
            "bucket": bucket,
            "status": img[2],
            "source": img[3],
            "file_url": f"/api/image/{rid}/file" if img[4] else None,
            "captured_at_text": (datetime.fromtimestamp(img[7]).strftime("%Y-%m-%d %H:%M:%S")
                                 if img[7] else None),
            "error_msg": img[8],
            "label": inf_row[1] if inf_row else None,
            "score": inf_row[2] if inf_row else None,
            "coarse": inf_row[3] if inf_row else None,
            "model_name": inf_row[4] if inf_row else None,
            "model_version": inf_row[5] if inf_row else None,
            "model_source": _model_source(inf_row[4], inf_row[5]) if inf_row else "unavailable",
            "usability": inf_row[6] if inf_row else None,
            "is_rejected": bool(inf_row[9]) if inf_row else None,
            "rejected_reason": inf_row[10] if inf_row else None,
            "uncertain_note": inf_row[11] if inf_row else None,
            "corrected_label": fb[0] if fb else None,
            "feedback_note": fb[1] if fb else None,
            "has_feedback": fb is not None,
        }
        item["is_high_score"] = bool(
            bucket == "misjudged"
            and item["score"] is not None
            and item["score"] >= HIGH_SCORE_THRESHOLD
        )
        buckets[bucket].append(item)

    conn.close()
    return jsonify({
        "buckets": buckets,
        "counts": {key: len(items) for key, items in buckets.items()},
        "bucket_notes": BUCKET_NOTES,
        "high_score_threshold": HIGH_SCORE_THRESHOLD,
        "score_note": SCORE_NOTE,
        "coarse_note": COARSE_NOTE,
    })


# ==================== 自然语言查询 API（第 4 周任务） ====================

# 合法设备列表（校验设备范围）
VALID_DEVICES = {"esp32-s3-eye"}
# 合法传感器列表
VALID_SENSORS = {"imu"}


def query_last_data(device_id: str, sensor: str) -> dict:
    """
    工具：查询最新一条记录。
    返回来源、时间与状态，无数据时说明不足。
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, ax, ay, az, created_at, request_id
        FROM imu_data
        ORDER BY id DESC
        LIMIT 1
    """)
    row = c.fetchone()
    conn.close()

    if not row:
        return {
            "success": False,
            "message": "数据库中暂无数据，设备可能尚未完成任何采集。",
            "source": "database",
        }

    return {
        "success": True,
        "message": "已查询到最近一条记录。",
        "source": "database",
        "data": {
            "ax": row[1],
            "ay": row[2],
            "az": row[3],
            "timestamp": row[0],
            "created_at": row[4],
            "request_id": row[5],
        },
        "note": "以上为数据库中已有记录的时间，非实时采集。",
    }


def collect_new_data(device_id: str, sensor: str) -> dict:
    """
    工具：请求设备重新采集一次。
    创建采集任务，轮询等待 ESP32 完成并上传，基于真实结果回复。
    无设备完成证据时不回复"已采集成功"。
    """
    # 校验设备
    if device_id and device_id not in VALID_DEVICES:
        return {
            "success": False,
            "message": f"设备 '{device_id}' 不在可用范围内。可用设备：{', '.join(VALID_DEVICES)}",
        }
    # 校验传感器
    if sensor and sensor not in VALID_SENSORS:
        return {
            "success": False,
            "message": f"传感器 '{sensor}' 不支持。可用传感器：{', '.join(VALID_SENSORS)}",
        }

    device_id = device_id or "esp32-s3-eye"
    sensor = sensor or "imu"

    # 创建采集任务（复用已有 /api/collect 逻辑）
    request_id = str(uuid.uuid4())[:8]
    now = time.time()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO collection_tasks (request_id, device_id, sensor, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (request_id, device_id, sensor, now)
    )
    conn.commit()
    conn.close()

    # 轮询等待 ESP32 完成（最多 30 秒）
    max_wait = TASK_TIMEOUT_S
    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(1)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            SELECT t.status, d.ax, d.ay, d.az, d.timestamp as data_ts
            FROM collection_tasks t
            LEFT JOIN imu_data d ON t.data_id = d.id
            WHERE t.request_id=?
        """, (request_id,))
        row = c.fetchone()
        conn.close()

        if not row:
            continue

        status = row[0]
        if status == "completed" and row[1] is not None:
            return {
                "success": True,
                "message": "设备已完成新采集并上传数据。",
                "source": "device",
                "data": {
                    "ax": row[1],
                    "ay": row[2],
                    "az": row[3],
                    "timestamp": row[4],
                },
                # 注意：键名不能叫 request_id，否则会在 nl_query 响应中
                # 覆盖用户会话的 request_id，导致前端误判为迟到结果而丢弃
                "collect_task_id": request_id,
            }
        elif status in ("failed", "timeout"):
            return {
                "success": False,
                "message": f"采集任务{status}，设备未能完成数据采集。",
                "source": "device",
                "collect_task_id": request_id,
            }

    # 超时
    return {
        "success": False,
        "message": "等待设备响应超时（30秒），设备可能离线或无响应。",
        "source": "timeout",
        "collect_task_id": request_id,
    }


def capture_image_and_wait(device_id=None, wait_s=None) -> dict:
    """
    工具：请求设备按需拍摄一张环境图像，并等待采集结果。

    回复中必须明确「是否成功」与「采集时刻」，不臆测成功。
    """
    device_id = device_id or "esp32-s3-eye"
    if device_id not in VALID_DEVICES:
        return {
            "success": False,
            "message": f"设备 '{device_id}' 不在可用范围内。",
            "source": "device",
        }

    request_id, _ = create_image_task(device_id, "device")
    deadline = time.time() + (wait_s or IMAGE_TIMEOUT_S)

    while time.time() < deadline:
        time.sleep(0.5)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        _image_mark_timeout(c, request_id)
        conn.commit()
        c.execute("SELECT status, captured_at, size_bytes, error_msg FROM image_records WHERE request_id=?",
                  (request_id,))
        row = c.fetchone()
        conn.close()

        if not row:
            continue

        status, captured_at, size_bytes, error_msg = row
        if status == "captured":
            return {
                "success": True,
                "message": f"已拍摄环境图像，采集时间 {datetime.fromtimestamp(captured_at).strftime('%H:%M:%S')}，"
                           f"图像大小 {size_bytes} 字节。",
                "source": "device",
                "request_id": request_id,
                "captured_at": captured_at,
                "image_url": f"/api/image/{request_id}/file",
            }
        if status in ("blocked", "failed", "timeout"):
            reason_map = {
                "blocked": "镜头遮挡或画面不可用",
                "failed": "采集失败",
                "timeout": "设备响应超时，未完成取图",
            }
            return {
                "success": False,
                "message": f"{reason_map[status]}（{error_msg or status}），本次没有取得可用的新图像。",
                "source": "device",
                "request_id": request_id,
            }

    return {
        "success": False,
        "message": "等待设备取图超时，本次没有取得新图像。",
        "source": "timeout",
        "request_id": request_id,
    }


@app.route("/api/nl_query", methods=["POST"])
def nl_query():
    """
    自然语言查询入口（第 4 周任务）。
    接收用户自然语言输入，通过 LLM 意图识别后路由到对应工具。

    请求体：{"text": "用户输入", "request_id": "可选的请求 ID"}
    返回：结构化结果（包含来源、时间、状态、request_id、处理时间）
    """
    start_time = time.time()

    data = request.get_json() or {}
    user_text = data.get("text", "").strip()
    request_id = data.get("request_id") or str(uuid.uuid4())[:8]

    if not user_text:
        return jsonify({"error": "请输入查询内容"}), 400

    # 检查是否已被取消
    if request_id in cancelled_requests:
        cancelled_requests.discard(request_id)
        logger.info(f"请求已取消：{user_text}", extra={"request_id": request_id})
        return jsonify({
            "request_id": request_id,
            "status": "cancelled",
            "message": "任务已取消"
        }), 200

    logger.info(f"开始处理：{user_text}", extra={"request_id": request_id})

    # 第 9 周：有待答复的询问时，"重拍 / 不用了 / 其实可用"先当成本次询问的答复，
    # 不进入意图分类（避免新增意图枚举，改动第 6 周的意图契约）。
    answered = inquiry_mod.try_answer_from_text(user_text, channel="ui")
    if answered:
        return jsonify({
            "request_id": request_id,
            "status": "answered",
            "intent": "answer_inquiry",
            "success": True,
            "message": answered.get("message", "已答复"),
            "inquiry": answered,
            "run_location": "server",
            "processing_time_ms": round((time.time() - start_time) * 1000, 2),
        }), 200

    # 1. 意图识别（LLM 优先，规则回退）
    intent_result = classify_intent(user_text)
    intent = intent_result.get("intent", "out_of_scope")

    # 2. 根据意图路由到工具
    if intent == "query_last":
        tool_result = query_last_data(
            intent_result.get("device_id"),
            intent_result.get("sensor")
        )
    elif intent == "collect_new":
        tool_result = collect_new_data(
            intent_result.get("device_id"),
            intent_result.get("sensor")
        )
    elif intent == "capture_image":
        tool_result = capture_image_and_wait(intent_result.get("device_id"))
    elif intent == "ambiguous":
        tool_result = {
            "success": False,
            "message": "您的请求不够明确。请说明是'查看上次数据'还是'重新采集一次'？",
            "source": "intent_classifier",
        }
    else:  # out_of_scope
        tool_result = {
            "success": False,
            "message": "抱歉，我只能处理传感器数据查询和采集相关的请求。",
            "source": "intent_classifier",
        }

    processing_time_ms = (time.time() - start_time) * 1000
    logger.info(f"完成处理：intent={intent}, time={processing_time_ms:.0f}ms",
                extra={"request_id": request_id})

    return jsonify({
        "request_id": request_id,
        "input": user_text,
        "intent": intent,
        "intent_source": intent_result.get("source", "unknown"),
        "reasoning": intent_result.get("reasoning", ""),
        "processing_time_ms": round(processing_time_ms, 2),
        **tool_result,
    })


@app.route("/api/cancel", methods=["POST"])
def cancel_task():
    """
    取消正在处理的任务（第 6 周任务）。
    将请求 ID 加入取消列表，后续返回时前端会过滤迟到结果。

    请求体：{"request_id": "请求 ID"}
    返回：{"status": "cancelled"}
    """
    data = request.get_json() or {}
    request_id = data.get("request_id")

    if not request_id:
        return jsonify({"error": "缺少 request_id"}), 400

    cancelled_requests.add(request_id)
    logger.info(f"任务已取消：{request_id}", extra={"request_id": request_id})

    return jsonify({"status": "cancelled", "request_id": request_id})


# ==================== 语音输入输出 API（第 5 周任务） ====================

@app.route("/api/stt", methods=["POST"])
def stt():
    """
    语音转文字（STT）。
    接收浏览器上传的音频文件（WAV/WebM/MP3），调用授权语音识别服务。
    返回识别文本 + 置信度。
    服务不可用时返回结构化错误。
    """
    if "file" not in request.files:
        return jsonify({"error": "请上传音频文件", "field": "file"}), 400

    audio_file = request.files["file"]
    if not audio_file.filename:
        return jsonify({"error": "未选择音频文件"}), 400

    # 检查 STT 服务是否可用
    if not STT_API_URL or not STT_API_KEY:
        return jsonify({
            "error": "语音识别服务未配置",
            "fallback": "请使用文字输入",
            "source": "stt_config"
        }), 503

    # 构建 multipart 请求调用 STT 服务
    boundary = "----FormBoundary" + str(uuid.uuid4()).replace("-", "")
    content_type = f"multipart/form-data; boundary={boundary}"

    body = b""
    # 文件部分
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{audio_file.filename}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += audio_file.read()
    body += b"\r\n"
    # 模型部分
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="model"\r\n\r\n'
    body += STT_MODEL.encode()
    body += b"\r\n"
    # 语言部分
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="language"\r\n\r\n'
    body += b"zh"
    body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        STT_API_URL,
        data=body,
        headers={
            "Content-Type": content_type,
            "Authorization": f"Bearer {STT_API_KEY}"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=STT_TIMEOUT_S) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            text = result.get("text", "").strip()
            if not text:
                return jsonify({
                    "error": "未检测到语音内容",
                    "source": "stt_silence"
                }), 400
            return jsonify({
                "text": text,
                "source": "stt",
                "model": STT_MODEL
            })
    except urllib.error.HTTPError as e:
        return jsonify({
            "error": f"语音识别服务返回错误: {e.code}",
            "fallback": "请使用文字输入",
            "source": "stt_error"
        }), 502
    except Exception as e:
        return jsonify({
            "error": f"语音识别服务超时或不可用: {str(e)}",
            "fallback": "请使用文字输入",
            "source": "stt_timeout"
        }), 504


@app.route("/api/tts", methods=["POST"])
def tts():
    """
    文字转语音（TTS）。
    接收文本，调用语音合成服务，返回音频数据（base64）。
    服务不可用时返回结构化错误。
    """
    data = request.get_json() or {}
    text = data.get("text", "").strip()

    if not text:
        return jsonify({"error": "请输入要转换的文本"}), 400

    # 检查 TTS 服务是否可用
    if not TTS_API_URL or not TTS_API_KEY:
        return jsonify({
            "error": "语音合成服务未配置",
            "fallback": "请查看文字结果",
            "source": "tts_config"
        }), 503

    # 构建请求调用 TTS 服务
    payload = json.dumps({
        "model": TTS_MODEL,
        "voice": TTS_VOICE,
        "input": text,
        "response_format": "mp3"
    }).encode("utf-8")

    req = urllib.request.Request(
        TTS_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TTS_API_KEY}"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=TTS_TIMEOUT_S) as resp:
            audio_data = resp.read()
            audio_b64 = base64.b64encode(audio_data).decode("utf-8")
            return jsonify({
                "audio": audio_b64,
                "format": "mp3",
                "source": "tts",
                "model": TTS_MODEL
            })
    except urllib.error.HTTPError as e:
        return jsonify({
            "error": f"语音合成服务返回错误: {e.code}",
            "fallback": "请查看文字结果",
            "source": "tts_error"
        }), 502
    except Exception as e:
        return jsonify({
            "error": f"语音合成服务超时或不可用: {str(e)}",
            "fallback": "请查看文字结果",
            "source": "tts_timeout"
        }), 504


@app.route("/api/voice_query", methods=["POST"])
def voice_query():
    """
    语音查询统一入口（第 5 周任务）。
    流程：接收音频 → STT 转文本 → 复用 nl_query 逻辑 → TTS 合成 → 返回音频 + 文本结果。
    记录音频来源（浏览器麦克风）和运行位置（服务端）。
    """
    if "file" not in request.files:
        return jsonify({"error": "请上传音频文件"}), 400

    audio_file = request.files["file"]

    # 步骤 1: STT 转文本
    if not STT_API_URL or not STT_API_KEY:
        return jsonify({
            "error": "语音识别服务未配置，请使用文字输入",
            "source": "stt_config",
            "stage": "stt"
        }), 503

    # 内部调用 STT
    boundary = "----FormBoundary" + str(uuid.uuid4()).replace("-", "")
    content_type = f"multipart/form-data; boundary={boundary}"

    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{audio_file.filename}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += audio_file.read()
    body += b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="model"\r\n\r\n'
    body += STT_MODEL.encode()
    body += b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="language"\r\n\r\n'
    body += b"zh"
    body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        STT_API_URL,
        data=body,
        headers={
            "Content-Type": content_type,
            "Authorization": f"Bearer {STT_API_KEY}"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=STT_TIMEOUT_S) as resp:
            stt_result = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return jsonify({
            "error": f"语音识别失败: {str(e)}",
            "source": "stt_error",
            "stage": "stt"
        }), 502

    text = stt_result.get("text", "").strip()
    if not text:
        return jsonify({
            "error": "未检测到语音内容",
            "source": "stt_silence",
            "stage": "stt"
        }), 400

    # 第 9 周：语音答复协同——"重拍 / 不用了 / 其实可用"直接答复当前询问
    answered = inquiry_mod.try_answer_from_text(text, channel="voice")
    if answered:
        return jsonify({
            "input": text,
            "status": "answered",
            "intent": "answer_inquiry",
            "success": True,
            "message": answered.get("message", "已答复"),
            "inquiry": answered,
            "audio": None,
            "tts_source": "not_needed",
            "run_location": "server",
        }), 200

    # 步骤 2: 复用 nl_query 逻辑
    intent_result = classify_intent(text)
    intent = intent_result.get("intent", "out_of_scope")

    if intent == "query_last":
        tool_result = query_last_data(intent_result.get("device_id"), intent_result.get("sensor"))
    elif intent == "collect_new":
        tool_result = collect_new_data(intent_result.get("device_id"), intent_result.get("sensor"))
    elif intent == "capture_image":
        tool_result = capture_image_and_wait(intent_result.get("device_id"))
    elif intent == "ambiguous":
        tool_result = {
            "success": False,
            "message": "您的请求不够明确。请说明是'查看上次数据'、'重新采集一次'还是'拍一张环境图像'？",
            "source": "intent_classifier",
        }
    else:
        tool_result = {
            "success": False,
            "message": "抱歉，我只能处理传感器数据查询、采集和环境取图相关的请求。",
            "source": "intent_classifier",
        }

    nl_result = {
        "input": text,
        "intent": intent,
        "intent_source": intent_result.get("source", "unknown"),
        "reasoning": intent_result.get("reasoning", ""),
        **tool_result,
    }

    # 步骤 3: TTS 合成回复文本
    response_text = nl_result.get("message", "未知结果")
    if nl_result.get("data"):
        d = nl_result["data"]
        response_text += f"，加速度 X={d.get('ax', 0):.2f}，Y={d.get('ay', 0):.2f}，Z={d.get('az', 0):.2f}"

    audio_b64 = None
    tts_source = None
    if TTS_API_URL and TTS_API_KEY:
        try:
            tts_payload = json.dumps({
                "model": TTS_MODEL,
                "voice": TTS_VOICE,
                "input": response_text,
                "response_format": "mp3"
            }).encode("utf-8")
            tts_req = urllib.request.Request(
                TTS_API_URL,
                data=tts_payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {TTS_API_KEY}"
                },
                method="POST"
            )
            with urllib.request.urlopen(tts_req, timeout=TTS_TIMEOUT_S) as tts_resp:
                audio_b64 = base64.b64encode(tts_resp.read()).decode("utf-8")
                tts_source = "tts"
        except Exception:
            tts_source = "tts_unavailable"

    nl_result["audio"] = audio_b64
    nl_result["tts_source"] = tts_source
    nl_result["audio_source"] = "browser_mic"
    nl_result["run_location"] = "server"

    return jsonify(nl_result)


if __name__ == "__main__":
    print("=" * 50)
    print("IMU 数据接收服务器")
    print("=" * 50)

    # 初始化数据库
    init_db()

    # 启动服务器
    print("\n启动服务器...")
    print("访问地址：http://localhost:5000")
    print("API 文档：http://localhost:5000/api/imu (POST)")
    print("\n按 Ctrl+C 停止服务器\n")

    app.run(host="0.0.0.0", port=5000, debug=False)

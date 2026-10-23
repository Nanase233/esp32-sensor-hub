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

app = Flask(__name__)
CORS(app)  # 允许跨域请求

# ==================== 日志配置（第 6 周任务） ====================
# 交互日志文件，记录每个请求的生命周期
# 注意：request_id 通过 logger.info(..., extra={"request_id": ...}) 逐条传入。
# 不要用 addFilter(RequestIdFilter(...)) —— 过滤器会累积且互相覆盖，
# 并发时会把 A 请求的日志错标成 B 的 request_id。
LOG_FILE = os.path.join(os.path.dirname(__file__), "interaction.log")

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
# 最少保留：只保留最近 N 张图像文件，更早的文件在入库后清理（记录仍保留用于审计）
IMAGE_KEEP_COUNT = 10
# 图像任务超时时间（秒）
IMAGE_TIMEOUT_S = 30
# 允许的设备与图像来源
IMAGE_SOURCES = {"device", "replay"}   # device=真机采集，replay=授权历史图像回放

os.makedirs(IMAGE_DIR, exist_ok=True)


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

    conn.commit()
    conn.close()
    print("[DB] 数据库初始化完成")


@app.route("/")
def index():
    """Web 前端页面"""
    return render_template("index.html")


@app.route("/api/imu", methods=["POST"])
def receive_imu():
    """接收 IMU 数据（支持 request_id 关联采集任务）"""
    data = request.get_json()

    if not data:
        return jsonify({"error": "无效的 JSON 数据"}), 400

    required_fields = ["timestamp", "ax", "ay", "az"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"缺少字段：{field}"}), 400

    request_id = data.get("request_id")

    # 存储到数据库
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO imu_data (timestamp, ax, ay, az, request_id) VALUES (?, ?, ?, ?, ?)",
        (data["timestamp"], data["ax"], data["ay"], data["az"], request_id)
    )
    data_id = c.lastrowid

    # 如果有关联的采集任务，更新任务状态为已完成
    if request_id:
        c.execute(
            "UPDATE collection_tasks SET status='completed', completed_at=?, data_id=? WHERE request_id=? AND status IN ('sent','received')",
            (time.time(), data_id, request_id)
        )

    conn.commit()
    conn.close()

    return jsonify({
        "status": "success",
        "message": "数据接收成功",
        "timestamp": data["timestamp"],
        "data_id": data_id
    }), 201


@app.route("/api/imu/latest", methods=["GET"])
def get_latest_imu():
    """获取最新的 IMU 数据（含模拟陀螺仪和姿态角）"""
    import math
    import random
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, ax, ay, az, created_at
        FROM imu_data
        ORDER BY id DESC
        LIMIT 1
    """)
    row = c.fetchone()
    conn.close()

    if row:
        ax, ay, az = row[1], row[2], row[3]
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
        return jsonify({
            "timestamp": row[0],
            "ax": ax, "ay": ay, "az": az,
            "gx": gx, "gy": gy, "gz": gz,
            "pitch": pitch, "roll": roll, "yaw": yaw,
            "created_at": row[4]
        })
    else:
        return jsonify({"error": "暂无数据"}), 404


@app.route("/api/imu/history", methods=["GET"])
def get_imu_history():
    """获取历史 IMU 数据"""
    limit = request.args.get("limit", 100, type=int)
    limit = min(limit, 1000)  # 最多返回 1000 条

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, ax, ay, az, created_at
        FROM imu_data
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()

    data = []
    for row in rows:
        data.append({
            "timestamp": row[0],
            "ax": row[1],
            "ay": row[2],
            "az": row[3],
            "created_at": row[4]
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

    # 2. 再检查 IMU 采集任务
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
    最少保留策略：只保留最近 keep 张图像文件。

    记录本身不删除（失败记录与审计需要），仅清理文件并置空 file_path。
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT request_id, file_path
        FROM image_records
        WHERE file_path IS NOT NULL
        ORDER BY created_at DESC
    """)
    rows = c.fetchall()

    purged = 0
    for request_id, file_path in rows[keep:]:
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
        print(f"[IMAGE] 保留策略清理了 {purged} 个旧文件（仅保留最近 {keep} 张）")


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

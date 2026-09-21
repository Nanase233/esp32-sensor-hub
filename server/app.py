"""
IMU 数据接收服务器

功能：
  1. 接收 ESP32 上传的 IMU 数据
  2. 存储到 SQLite 数据库
  3. 提供 REST API 查询数据
  4. 提供 Web 页面实时展示
  5. 远程采集指令与执行结果反馈
  6. 按键触发与物理反馈闭环（第 3 周任务）

启动：python app.py
访问：http://localhost:5000
"""

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from datetime import datetime
import sqlite3
import os
import uuid
import time
import re
import json
import urllib.request
import urllib.error

app = Flask(__name__)
CORS(app)  # 允许跨域请求

# ==================== LLM 配置 ====================
# 授权语言服务配置（密钥留服务端，不暴露到 ESP32）
LLM_API_URL = os.environ.get("LLM_API_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-3.5-turbo")

# LLM 超时（秒）
LLM_TIMEOUT_S = 10


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
        "用户的输入会被映射为以下四种意图之一：\n"
        "1. query_last - 查看上次/最近/历史数据（如：'查看上次数据'、'最近一次采集结果'、'上次加速度是多少'）\n"
        "2. collect_new - 请求重新采集一次新数据（如：'重新采集一次'、'再采一次'、'现在采集'）\n"
        "3. ambiguous - 输入模糊，无法确定是查看还是采集（如：'看看数据'、'帮我查一下'）\n"
        "4. out_of_scope - 与数据采集无关的请求（如：'今天天气如何'、'讲个笑话'）\n\n"
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

    is_collect = any(kw in text for kw in collect_keywords)
    is_query = any(kw in text for kw in query_keywords)

    if is_collect and not is_query:
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
    if result and result.get("intent") in ("query_last", "collect_new", "ambiguous", "out_of_scope"):
        result["source"] = "llm"
        return result
    # 回退到规则匹配
    result = classify_intent_by_rules(user_text)
    result["source"] = "rules"
    return result

# 数据库文件
DB_FILE = "imu_data.db"

# 任务超时时间（秒）
TASK_TIMEOUT_S = 30


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
    """获取最新的 IMU 数据"""
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
        return jsonify({
            "timestamp": row[0],
            "ax": row[1],
            "ay": row[2],
            "az": row[3],
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
    """ESP32 轮询待执行命令（返回最新的 pending 任务并标记为 sent）"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
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
                "request_id": request_id,
            }
        elif status in ("failed", "timeout"):
            return {
                "success": False,
                "message": f"采集任务{status}，设备未能完成数据采集。",
                "source": "device",
                "request_id": request_id,
            }

    # 超时
    return {
        "success": False,
        "message": "等待设备响应超时（30秒），设备可能离线或无响应。",
        "source": "timeout",
        "request_id": request_id,
    }


@app.route("/api/nl_query", methods=["POST"])
def nl_query():
    """
    自然语言查询入口（第 4 周任务）。
    接收用户自然语言输入，通过 LLM 意图识别后路由到对应工具。

    请求体：{"text": "用户输入"}
    返回：结构化结果（包含来源、时间、状态）
    """
    data = request.get_json() or {}
    user_text = data.get("text", "").strip()

    if not user_text:
        return jsonify({"error": "请输入查询内容"}), 400

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

    return jsonify({
        "input": user_text,
        "intent": intent,
        "intent_source": intent_result.get("source", "unknown"),
        "reasoning": intent_result.get("reasoning", ""),
        **tool_result,
    })


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

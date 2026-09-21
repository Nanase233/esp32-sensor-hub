"""
IMU 数据接收服务器

功能：
  1. 接收 ESP32 上传的 IMU 数据
  2. 存储到 SQLite 数据库
  3. 提供 REST API 查询数据
  4. 提供 Web 页面实时展示
  5. 远程采集指令与执行结果反馈

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

app = Flask(__name__)
CORS(app)  # 允许跨域请求

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
            return jsonify({"error": f"缺少字段: {field}"}), 400

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


if __name__ == "__main__":
    print("=" * 50)
    print("IMU 数据接收服务器")
    print("=" * 50)

    # 初始化数据库
    init_db()

    # 启动服务器
    print("\n启动服务器...")
    print("访问地址: http://localhost:5000")
    print("API 文档: http://localhost:5000/api/imu (POST)")
    print("\n按 Ctrl+C 停止服务器\n")

    app.run(host="0.0.0.0", port=5000, debug=False)

"""
IMU 数据接收服务器

功能：
  1. 接收 ESP32 上传的 IMU 数据
  2. 存储到 SQLite 数据库
  3. 提供 REST API 查询数据
  4. 提供 Web 页面实时展示

启动：python app.py
访问：http://localhost:5000
"""

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from datetime import datetime
import sqlite3
import os

app = Flask(__name__)
CORS(app)  # 允许跨域请求

# 数据库文件
DB_FILE = "imu_data.db"


def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS imu_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            ax REAL NOT NULL,
            ay REAL NOT NULL,
            az REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_timestamp ON imu_data(timestamp)
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
    """接收 IMU 数据"""
    data = request.get_json()

    if not data:
        return jsonify({"error": "无效的 JSON 数据"}), 400

    required_fields = ["timestamp", "ax", "ay", "az"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"缺少字段: {field}"}), 400

    # 存储到数据库
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO imu_data (timestamp, ax, ay, az) VALUES (?, ?, ?, ?)",
        (data["timestamp"], data["ax"], data["ay"], data["az"])
    )
    conn.commit()
    conn.close()

    return jsonify({
        "status": "success",
        "message": "数据接收成功",
        "timestamp": data["timestamp"]
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

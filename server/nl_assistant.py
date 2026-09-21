"""
自然语言助手（第 4 周任务）

功能：
  1. 解析用户自然语言输入，识别意图（查询/采集）
  2. 调用对应工具函数执行操作
  3. 基于真实结果回复，附带来源、时间与状态
  4. 预留 LLM 接口，当前使用规则解析（备用路径）

工具：
  - query_latest: 查看最近一次采集的 IMU 数据
  - collect_once: 请求设备重新采集一次 IMU 数据
"""

import sqlite3
import time
import uuid
import re
import os
from datetime import datetime

DB_FILE = os.path.join(os.path.dirname(__file__), "imu_data.db")
TASK_TIMEOUT_S = 30

# 支持的传感器列表
SUPPORTED_SENSORS = ["imu"]

# 支持的设备列表
SUPPORTED_DEVICES = ["esp32-s3-eye"]


# ==================== 意图关键词匹配 ====================

# 查询类意图关键词
QUERY_KEYWORDS = [
    "查看", "查询", "显示", "看看", "最近", "上次", "最新",
    "上一次", "最后", "当前", "现在", "数据", "读数",
    "多少", "数值", "加速度", "值", "get", "show", "view",
    "last", "latest", "current", "read"
]

# 采集类意图关键词
COLLECT_KEYWORDS = [
    "采集", "重新", "刷新", "获取", "取一次", "测一次",
    "采一次", "收集", "更新", "新数据", "新的",
    "collect", "refresh", "fetch", "new", "update"
]


def parse_intent(text):
    """
    规则解析用户意图
    
    返回：
      - "query": 查询已有数据
      - "collect": 触发新采集
      - "unknown": 无法识别
    """
    text_lower = text.lower().strip()
    
    # 检查采集类关键词（优先级高，因为"重新采集"包含"采集"）
    for keyword in COLLECT_KEYWORDS:
        if keyword in text_lower:
            return "collect"
    
    # 检查查询类关键词
    for keyword in QUERY_KEYWORDS:
        if keyword in text_lower:
            return "query"
    
    return "unknown"


def extract_sensor(text):
    """从文本中提取传感器类型，默认 imu"""
    text_lower = text.lower()
    for sensor in SUPPORTED_SENSORS:
        if sensor in text_lower:
            return sensor
    return "imu"


def extract_device(text):
    """从文本中提取设备 ID，默认 esp32-s3-eye"""
    text_lower = text.lower()
    for device in SUPPORTED_DEVICES:
        if device in text_lower:
            return device
    return "esp32-s3-eye"


# ==================== 工具函数 ====================

def tool_query_latest(params=None):
    """
    工具：查看最近一次采集的 IMU 数据
    
    不触发新采集，直接返回数据库中最新记录。
    回复中包含数据来源、采集时间。
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
            "reply": "数据库中暂无任何 IMU 数据记录。请先让设备完成一次采集。",
            "source": "database",
            "data": None
        }
    
    timestamp, ax, ay, az, created_at, request_id = row
    
    # 格式化时间
    try:
        dt = datetime.fromtimestamp(timestamp)
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        time_str = str(timestamp)
    
    return {
        "success": True,
        "reply": (
            f"最近一次 IMU 数据（来源：数据库记录）：\n"
            f"  采集时间：{time_str}\n"
            f"  X轴加速度：{ax:.4f} m/s²\n"
            f"  Y轴加速度：{ay:.4f} m/s²\n"
            f"  Z轴加速度：{az:.4f} m/s²\n"
            f"  记录ID：{request_id or '自动上传'}"
        ),
        "source": "database",
        "data": {
            "timestamp": timestamp,
            "time_str": time_str,
            "ax": ax, "ay": ay, "az": az,
            "request_id": request_id
        }
    }


def tool_collect_once(params=None):
    """
    工具：请求设备重新采集一次 IMU 数据
    
    创建采集任务，轮询等待 ESP32 完成采集并上传。
    只有在收到实际数据后才回复"已采集成功"。
    超时或失败时如实报告。
    """
    sensor = "imu"
    device_id = "esp32-s3-eye"
    
    if params:
        sensor = params.get("sensor", sensor)
        device_id = params.get("device_id", device_id)
    
    # 校验传感器参数
    if sensor not in SUPPORTED_SENSORS:
        return {
            "success": False,
            "reply": f"不支持的传感器类型：{sensor}。仅支持：{', '.join(SUPPORTED_SENSORS)}",
            "source": "validation",
            "data": None
        }
    
    # 校验设备参数
    if device_id not in SUPPORTED_DEVICES:
        return {
            "success": False,
            "reply": f"不支持的设备：{device_id}。仅支持：{', '.join(SUPPORTED_DEVICES)}",
            "source": "validation",
            "data": None
        }
    
    # 创建采集任务
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
    
    # 轮询等待任务完成（最多等待 TASK_TIMEOUT_S 秒）
    max_wait = TASK_TIMEOUT_S
    start_time = time.time()
    poll_interval = 1  # 每秒轮询一次
    
    while (time.time() - start_time) < max_wait:
        time.sleep(poll_interval)
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            SELECT t.status, d.ax, d.ay, d.az, d.timestamp as data_timestamp, t.error_msg
            FROM collection_tasks t
            LEFT JOIN imu_data d ON t.data_id = d.id
            WHERE t.request_id=?
        """, (request_id,))
        row = c.fetchone()
        conn.close()
        
        if not row:
            continue
        
        status, ax, ay, az, data_timestamp, error_msg = row
        
        if status == "completed":
            # 有实际数据才回复成功
            if ax is not None:
                try:
                    dt = datetime.fromtimestamp(data_timestamp)
                    time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                except:
                    time_str = str(data_timestamp)
                
                return {
                    "success": True,
                    "reply": (
                        f"新采集完成（来源：设备实时上传）：\n"
                        f"  采集时间：{time_str}\n"
                        f"  X轴加速度：{ax:.4f} m/s²\n"
                        f"  Y轴加速度：{ay:.4f} m/s²\n"
                        f"  Z轴加速度：{az:.4f} m/s²\n"
                        f"  任务ID：{request_id}"
                    ),
                    "source": "device",
                    "data": {
                        "timestamp": data_timestamp,
                        "time_str": time_str,
                        "ax": ax, "ay": ay, "az": az,
                        "request_id": request_id
                    }
                }
            else:
                return {
                    "success": False,
                    "reply": f"任务 {request_id} 标记为完成，但未收到传感器数据。",
                    "source": "device",
                    "data": None
                }
        
        elif status == "failed":
            return {
                "success": False,
                "reply": f"采集失败（任务 {request_id}）：{error_msg or '未知错误'}",
                "source": "device",
                "data": None
            }
        
        elif status == "timeout":
            return {
                "success": False,
                "reply": f"采集超时（任务 {request_id}）：设备未在 {TASK_TIMEOUT_S} 秒内响应。请检查设备是否在线。",
                "source": "device",
                "data": None
            }
    
    # 超时未收到结果
    return {
        "success": False,
        "reply": f"等待采集结果超时（{max_wait}秒）。设备可能未连接或未响应。任务ID：{request_id}",
        "source": "timeout",
        "data": None
    }


# ==================== 主入口 ====================

def process_nl_input(text, params=None):
    """
    处理自然语言输入
    
    流程：
      1. 解析意图（规则匹配 / LLM）
      2. 提取参数（传感器、设备）
      3. 调用对应工具
      4. 返回结果
    
    参数：
      text: 用户输入的自然语言文本
      params: 额外参数（可选）
    
    返回：
      dict: {success, reply, source, data, intent}
    """
    intent = parse_intent(text)
    sensor = extract_sensor(text)
    device = extract_device(text)
    
    tool_params = {
        "sensor": sensor,
        "device_id": device
    }
    if params:
        tool_params.update(params)
    
    if intent == "query":
        result = tool_query_latest(tool_params)
    elif intent == "collect":
        result = tool_collect_once(tool_params)
    else:
        result = {
            "success": False,
            "reply": "无法识别您的意图。请尝试：\n"
                     "  - 查询类：'查看上次数据'、'显示最新读数'\n"
                     "  - 采集类：'重新采集一次'、'刷新数据'",
            "source": "intent_parser",
            "data": None
        }
    
    result["intent"] = intent
    return result


# ==================== LLM 预留接口 ====================

LLM_TOOLS_SCHEMA = [
    {
        "name": "query_latest",
        "description": "查看最近一次采集的IMU数据，不触发新采集。返回数据库中最新记录，包含加速度三轴数值和采集时间。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "collect_once",
        "description": "请求设备重新采集一次IMU数据，等待结果返回。会创建采集任务并轮询等待设备完成。",
        "parameters": {
            "type": "object",
            "properties": {
                "sensor": {
                    "type": "string",
                    "enum": ["imu"],
                    "description": "传感器类型，仅支持 imu"
                },
                "device_id": {
                    "type": "string",
                    "enum": ["esp32-s3-eye"],
                    "description": "设备ID"
                }
            },
            "required": []
        }
    }
]


def process_with_llm(text, llm_client=None):
    """
    使用 LLM 解析用户意图（预留接口）
    
    当 LLM 服务可用时，使用 Function Calling 解析意图。
    当前未配置 LLM 时，回退到规则解析。
    
    参数：
      text: 用户输入文本
      llm_client: LLM 客户端实例（可选）
    
    返回：
      dict: 同 process_nl_input
    """
    if llm_client is None:
        # 回退到规则解析
        return process_nl_input(text)
    
    # TODO: 实现 LLM Function Calling
    # 1. 发送 text + LLM_TOOLS_SCHEMA 给 LLM
    # 2. 解析 LLM 返回的工具调用
    # 3. 执行对应工具函数
    # 4. 返回结果
    return process_nl_input(text)

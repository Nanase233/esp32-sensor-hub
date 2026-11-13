"""
第 9 周：画面不可用 → 主动询问「是否重拍」

本周问题：画面无法使用时，系统能否主动询问「是否重拍」，并尊重用户选择？

本模块是询问的**唯一权威**，负责三件事：

  1. 事件归一：把三处已有的「画面不可用」判定收成一条事件
       - 端侧：image_capture 上报 reason=blocked / error            （trigger=device）
       - 服务端：vision.check_usability 判 dark / blur / blocked     （trigger=server）
       - 模型：status=unavailable / decode_failed                    （trigger=model）

  2. 门控 G1–G5：决定「这一次是否值得打扰用户」
       G1 原因在已知集合内（低分 / 候选接近不算"画面无法使用"，不询问）
       G2 来源是设备真机采集（replay 只在显式开启时参与，避免把回放当真实证据）
       G3 同一时刻只允许一条未决询问（单飞行 one-in-flight）
       G4 不在全局间隔 / 同原因冷却窗口内
       G5 同一 request_id 未询问过；重拍产生的新 request_id 归属原询问，不新开一条
       通道可用性不单独探测，由 30s 超时（expired）兜底，避免依赖虚假在线信号。

  3. 状态机 + 冷却：把「确认 / 忽略 / 误判纠正 / 超时」变成确定性行为

    asking ──重拍──→ confirmed ──重拍后可用──→ resolved
      │                  │
      │                  ├──重拍后仍不可用且 attempt<MAX_RETAKE──→ asking（再问一次）
      │                  └──重拍后仍不可用且 attempt=MAX_RETAKE──→ exhausted
      ├──不用了──→ declined（登记冷却，同原因不再追问）
      ├──其实可用──→ dismissed（只记录误判纠正，不重拍、不写第 8 周纠错表）
      └──30s 未答──→ expired（按忽略处理，不重投、不重弹）

设计约束（与既有代码的分工）：

  * 不新增设备任务类型：重拍直接复用 app.create_image_task()，走第 7 周原图像任务流；
  * 不改第 7 周 image_records 状态机、不改第 8 周 inference_results / human_feedbacks；
  * 询问投递复用设备既有的 GET /api/collect/pending（type=inquiry），不新增轮询任务，
    设备端 HTTP 请求次数不变；
  * 冷却参数全部可用环境变量覆盖，便于自动化测试用秒级参数验证「拒绝后不反复打扰」。
"""

import os
import sqlite3
import time
import uuid
import logging

from flask import Blueprint, jsonify, request

# ==================== 可调参数（环境变量可覆盖，便于测试缩短到秒级） ====================
# 未答复超时：到点置 expired，按忽略处理，不重投、不重弹
INQUIRY_TIMEOUT_S = float(os.environ.get("INQUIRY_TIMEOUT_S", "30"))
# 全局最小间隔：任意两条询问之间至少间隔这么久（防止询问风暴）
INQUIRY_GLOBAL_GAP_S = float(os.environ.get("INQUIRY_GLOBAL_GAP_S", "60"))
# 用户选择「不用了」后的同原因冷却：本周「拒绝后不反复打扰」的硬指标
DECLINE_COOLDOWN_S = float(os.environ.get("DECLINE_COOLDOWN_S", "600"))
# 超时未答复后的同原因冷却（比明确拒绝短一些）
EXPIRE_COOLDOWN_S = float(os.environ.get("EXPIRE_COOLDOWN_S", "300"))
# 用户判定「其实可用」（系统误判）后的同原因冷却
DISMISS_COOLDOWN_S = float(os.environ.get("DISMISS_COOLDOWN_S", "1800"))
# 单张图最多重拍次数；达到上限转 exhausted 并建议走第 3 周求助链路
MAX_RETAKE = int(os.environ.get("MAX_RETAKE", "2"))
# 是否允许 replay 来源参与询问（默认关闭：回放只用于自动化回归，不计入真机证据）
ALLOW_REPLAY = os.environ.get("INQUIRY_ALLOW_REPLAY", "0") == "1"
# 前端勾选「本次不再询问」后的静音时长（会话级静音的落库表示）
MUTE_COOLDOWN_S = float(os.environ.get("MUTE_COOLDOWN_S", "86400"))

# 询问原因 → (短标签, 询问文案)
# 文案都是「陈述事实 + 一个问句」，不夸大严重性，也不承诺系统能判断现实情况
REASONS = {
    "blocked": ("画面被遮挡", "镜头可能被挡住了，这一张看不出周围情况。要重拍一张吗？"),
    "dark": ("画面过暗", "环境太暗，这一张看不清内容。要重拍一张吗？"),
    "blur": ("画面模糊", "画面比较糊，这一张没法判断。要重拍一张吗？"),
    "capture_failed": ("采集失败", "这一次没有取到画面。要重拍一张吗？"),
    "decode_failed": ("图像无法解码", "这一张图像数据不完整。要重拍一张吗？"),
    # 模型不可用重拍大概率也没用，因此文案明确写出限制，避免误导用户
    "model_unavailable": ("视觉服务不可用",
                          "识别服务当前不可用，重拍也可能判不了。要再试一张吗？"),
}

# 不触发询问的拒识原因：低分 / 候选接近属于"模型不确定"，不是"画面无法使用"
NON_INQUIRY_REASONS = {"low_score", "ambiguous"}

# 允许的答复动作 → 状态
DECISIONS = {"retake", "decline", "usable"}
CHANNELS = {"ui", "device_key", "voice"}

# 语音/文字答复的关键词（规则匹配，先判"可用"再判"拒绝"，避免"不用重拍"被误判为重拍）
USABLE_WORDS = ("其实可以", "其实可用", "看得清", "这张可以", "这张就行", "不用重拍", "没问题")
DECLINE_WORDS = ("不用了", "不用", "算了", "不拍了", "不要了", "不需要")
RETAKE_WORDS = ("重拍", "再拍", "重新拍", "再来一张", "再取一张", "换一张")

bp = Blueprint("inquiry", __name__)

_logger = logging.getLogger("inquiry")
_logger.setLevel(logging.INFO)
_logger.propagate = False
if not _logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[INQUIRY] %(message)s"))
    _logger.addHandler(_h)

# 由 app.py 注入的依赖（避免循环 import）
_db_file = "imu_data.db"
_create_image_task = None
_log = None


def configure(db_file, create_image_task, log=None):
    """注入数据库路径与"创建图像任务"工厂。

    :param db_file: SQLite 文件路径（与 app.py 同一个库）
    :param create_image_task: app.create_image_task(device_id, source) -> (request_id, ts)
    :param log: 可选日志回调 log(message, request_id=None)，用于写 interaction.log
    """
    global _db_file, _create_image_task, _log
    _db_file = db_file
    _create_image_task = create_image_task
    _log = log


def _emit(msg, request_id=None):
    """统一日志：写模块 logger；若 app 注入了回调，同时写入交互日志。"""
    _logger.info(msg)
    if _log:
        try:
            _log(msg, request_id)
        except Exception:  # 日志失败绝不影响主流程
            pass


def _now():
    return time.time()


def _connect():
    return sqlite3.connect(_db_file)


# ==================== 建表 ====================

def init_db():
    """创建询问表（幂等）。独立成表，不改动已有的四张表结构。"""
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS image_inquiries (
            inquiry_id TEXT PRIMARY KEY,
            request_id TEXT,
            retake_request_id TEXT,
            device_id TEXT,
            source TEXT,
            trigger TEXT NOT NULL,
            reason TEXT NOT NULL,
            question TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'asking',
            attempt_no INTEGER NOT NULL DEFAULT 0,
            decision TEXT,
            decision_channel TEXT,
            created_at REAL NOT NULL,
            answered_at REAL,
            expires_at REAL NOT NULL,
            delivered_at REAL,
            cooldown_until REAL,
            usability_snapshot TEXT,
            note TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_inquiry_status ON image_inquiries(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inquiry_request ON image_inquiries(request_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inquiry_retake ON image_inquiries(retake_request_id)")
    conn.commit()
    conn.close()


# ==================== 读取辅助 ====================

_COLUMNS = ("inquiry_id, request_id, retake_request_id, device_id, source, trigger, reason, "
            "question, status, attempt_no, decision, decision_channel, created_at, answered_at, "
            "expires_at, delivered_at, cooldown_until, usability_snapshot, note")


def _row_to_dict(row):
    if not row:
        return None
    (inquiry_id, request_id, retake_request_id, device_id, source, trigger, reason,
     question, status, attempt_no, decision, decision_channel, created_at, answered_at,
     expires_at, delivered_at, cooldown_until, snapshot, note) = row
    label, _ = REASONS.get(reason, (reason, ""))
    remaining = max(0, int(round(expires_at - _now()))) if expires_at else 0
    return {
        "inquiry_id": inquiry_id,
        "request_id": request_id,
        "retake_request_id": retake_request_id,
        "device_id": device_id,
        "source": source,
        "trigger": trigger,
        "reason": reason,
        "reason_label": label,
        "question": question,
        "status": status,
        "attempt_no": attempt_no,
        "attempt_text": f"第 {attempt_no} 次重拍" if attempt_no else "首次询问",
        "max_retake": MAX_RETAKE,
        "decision": decision,
        "decision_channel": decision_channel,
        "created_at": created_at,
        "created_at_text": _fmt(created_at),
        "answered_at": answered_at,
        "expires_at": expires_at,
        "remaining_s": remaining,
        "cooldown_until": cooldown_until,
        "cooldown_remaining_s": max(0, int(round((cooldown_until or 0) - _now()))),
        "usability_snapshot": snapshot,
        "note": note,
    }


def _fmt(ts):
    if not ts:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _get(inquiry_id):
    conn = _connect()
    c = conn.cursor()
    c.execute(f"SELECT {_COLUMNS} FROM image_inquiries WHERE inquiry_id=?", (inquiry_id,))
    row = c.fetchone()
    conn.close()
    return _row_to_dict(row)


def _find_by_retake(request_id):
    conn = _connect()
    c = conn.cursor()
    c.execute(f"SELECT {_COLUMNS} FROM image_inquiries "
              "WHERE retake_request_id=? ORDER BY created_at DESC LIMIT 1", (request_id,))
    row = c.fetchone()
    conn.close()
    return _row_to_dict(row)


def _find_by_request(request_id):
    conn = _connect()
    c = conn.cursor()
    c.execute(f"SELECT {_COLUMNS} FROM image_inquiries "
              "WHERE request_id=? ORDER BY created_at DESC LIMIT 1", (request_id,))
    row = c.fetchone()
    conn.close()
    return _row_to_dict(row)


def _update(inquiry_id, **fields):
    """按字段更新一条询问（字段名由内部常量给出，不接受外部拼接）。"""
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    conn = _connect()
    c = conn.cursor()
    c.execute(f"UPDATE image_inquiries SET {sets} WHERE inquiry_id=?",
              (*fields.values(), inquiry_id))
    conn.commit()
    conn.close()


def _image_source(request_id):
    """读第 7 周图像记录，判断这张图来自真机还是回放。"""
    try:
        conn = _connect()
        c = conn.cursor()
        c.execute("SELECT source, device_id FROM image_records WHERE request_id=?", (request_id,))
        row = c.fetchone()
        conn.close()
        return (row[0], row[1]) if row else (None, None)
    except sqlite3.Error:
        return (None, None)


# ==================== 超时与冷却 ====================

def sweep_expired():
    """把超时未答复的询问置为 expired 并登记冷却。

    惰性调用（每次读写前），不额外起定时器。
    """
    now = _now()
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT inquiry_id FROM image_inquiries "
              "WHERE status='asking' AND expires_at IS NOT NULL AND expires_at < ?", (now,))
    ids = [r[0] for r in c.fetchall()]
    for inquiry_id in ids:
        c.execute("""
            UPDATE image_inquiries
            SET status='expired', decision='ignore', decision_channel='timeout',
                answered_at=?, cooldown_until=?, note=COALESCE(note,'超时未答复，按忽略处理')
            WHERE inquiry_id=? AND status='asking'
        """, (now, now + EXPIRE_COOLDOWN_S, inquiry_id))
    conn.commit()
    conn.close()
    for inquiry_id in ids:
        _emit(f"询问 {inquiry_id} 超时未答复 → expired（同类冷却 {EXPIRE_COOLDOWN_S:.0f}s）",
              inquiry_id)
    return len(ids)


def _last_created_at():
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT MAX(created_at) FROM image_inquiries")
    row = c.fetchone()
    conn.close()
    return row[0] or 0.0


def _reason_cooldown(reason):
    """该原因当前剩余的冷却截止时间（取所有同类记录的最大值）。"""
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT MAX(cooldown_until) FROM image_inquiries "
              "WHERE reason=? AND cooldown_until IS NOT NULL", (reason,))
    row = c.fetchone()
    conn.close()
    return row[0] or 0.0


def _gate(request_id, reason, source):
    """门控 G1–G5。返回 None 表示通过，否则返回未通过原因的代码。"""
    now = _now()

    # G1 原因必须是"画面无法使用"类
    if reason in NON_INQUIRY_REASONS or reason not in REASONS:
        return "reason_not_inquirable"

    # G2 只对真机采集询问；replay 仅在显式开启时参与
    if source != "device" and not ALLOW_REPLAY:
        return "source_not_device"

    # G3 单飞行：已有未决询问时不再新建（排队/合并都免了，避免连环弹窗）
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(1) FROM image_inquiries WHERE status='asking'")
    asking = c.fetchone()[0]
    conn.close()
    if asking:
        return "inquiry_in_flight"

    # G4 全局间隔 + 同原因冷却
    if now - _last_created_at() < INQUIRY_GLOBAL_GAP_S:
        return "global_gap"
    if now < _reason_cooldown(reason):
        return "reason_cooldown"

    # G5 同一 request_id 已经问过（重拍结果由 retake_request_id 归属原询问）
    if _find_by_request(request_id):
        return "already_asked"

    return None


# ==================== 询问创建 ====================

def on_unusable(request_id, reason, trigger="server", snapshot=None):
    """收到一条「画面不可用」事件。

    三种去向：
      1. 该 request_id 是一次重拍的结果 → 回到原询问，推进 attempt_no；
      2. 该 request_id 已问过 → 忽略（幂等）；
      3. 门控通过 → 新建一条询问。

    :return: 新建/更新的询问 dict，或被门控拦下时 {"suppressed": 原因}
    """
    sweep_expired()

    # 1. 重拍结果回填：新图的失败归到原询问，不新开一条
    retake = _find_by_retake(request_id)
    if retake and retake["status"] == "confirmed":
        return _handle_retake_result(retake, usable=False, reason=reason)
    # 已终结的重拍（resolved/exhausted）不再因为迟到的失败事件重开一条询问
    if retake and retake["status"] in ("resolved", "exhausted"):
        return retake

    # 2. 幂等：同一张图只问一次
    existing = _find_by_request(request_id)
    if existing:
        return existing

    source, device_id = _image_source(request_id)
    blocked_by = _gate(request_id, reason, source)
    if blocked_by:
        _emit(f"事件未触发询问（{blocked_by}）: request={request_id} reason={reason}",
              request_id)
        return {"suppressed": blocked_by, "request_id": request_id, "reason": reason}

    label, question = REASONS[reason]
    inquiry_id = "inq-" + uuid.uuid4().hex[:10]
    now = _now()
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO image_inquiries
        (inquiry_id, request_id, device_id, source, trigger, reason, question, status,
         attempt_no, created_at, expires_at, usability_snapshot)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'asking', 0, ?, ?, ?)
    """, (inquiry_id, request_id, device_id, source, trigger, reason, question,
          now, now + INQUIRY_TIMEOUT_S, _snapshot_text(snapshot)))
    conn.commit()
    conn.close()

    _emit(f"主动询问已创建: {inquiry_id} reason={reason} trigger={trigger} "
          f"（{INQUIRY_TIMEOUT_S:.0f}s 未答复将过期）", request_id)
    return _get(inquiry_id)


def _snapshot_text(snapshot):
    """判定依据快照：保留当时的画面指标/模型版本，供"其实可用"误判纠正追溯。"""
    if not snapshot:
        return None
    import json
    try:
        return json.dumps(snapshot, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _handle_retake_result(inquiry, usable, reason=None):
    """重拍结果回填：可用→resolved；仍不可用→再问一次或 exhausted。"""
    inquiry_id = inquiry["inquiry_id"]
    if usable:
        _update(inquiry_id, status="resolved", note="重拍后画面可用")
        _emit(f"重拍结果可用 → resolved: {inquiry_id}", inquiry["request_id"])
        return _get(inquiry_id)

    # 仍不可用：先看是否达到重拍上限
    if inquiry["attempt_no"] >= MAX_RETAKE:
        _update(inquiry_id, status="exhausted",
                note=f"已重拍 {inquiry['attempt_no']} 次仍不可用，建议改用求助")
        _emit(f"重拍 {inquiry['attempt_no']} 次仍不可用 → exhausted: {inquiry_id}",
              inquiry["request_id"])
        return _get(inquiry_id)

    # 未达上限：重新发问（同一条记录，attempt_no 不变，重置投递与超时）
    now = _now()
    new_reason = reason if reason in REASONS else inquiry["reason"]
    label, question = REASONS[new_reason]
    _update(inquiry_id, status="asking", reason=new_reason, question=question,
            delivered_at=None, expires_at=now + INQUIRY_TIMEOUT_S)
    _emit(f"重拍仍不可用 → 再次询问: {inquiry_id}（{label}）", inquiry["request_id"])
    return _get(inquiry_id)


def on_usable(request_id):
    """收到一条「画面可用」事件：若它是一次重拍的结果，则关闭询问为 resolved。"""
    retake = _find_by_retake(request_id)
    if retake and retake["status"] == "confirmed":
        return _handle_retake_result(retake, usable=True)
    return None


# ==================== 答复 ====================

def answer(inquiry_id, decision, channel="ui", mute=False):
    """处理一次答复。

    :param decision: retake / decline / usable
    :param channel: ui / device_key / voice（只用于留证，不改变语义）
    :param mute: 拒绝时是否"本次不再询问"（同原因长冷却）
    :return: (payload, http_status)
    """
    sweep_expired()

    if decision not in DECISIONS:
        return {"error": f"未知答复动作 '{decision}'"}, 400

    inquiry = _get(inquiry_id)
    if not inquiry:
        return {"error": "询问不存在"}, 404

    # 尊重"第一个有效答复"：已答复过的询问不再重复处理，也不重复提示
    if inquiry["status"] != "asking":
        return {
            "error": f"询问已是 {inquiry['status']} 状态，本次答复不再生效",
            "inquiry": inquiry,
            "duplicate": True,
        }, 409

    now = _now()

    if decision == "retake":
        if _create_image_task is None:
            return {"error": "图像任务工厂未注入，无法重拍"}, 500
        request_id, created_at = _create_image_task(inquiry["device_id"] or "esp32-s3-eye",
                                                    "device")
        _update(inquiry_id, status="confirmed", decision="retake", decision_channel=channel,
                answered_at=now, retake_request_id=request_id,
                attempt_no=inquiry["attempt_no"] + 1, note="用户选择重拍")
        _emit(f"用户确认重拍 → 新建图像任务 {request_id}"
              f"（第 {inquiry['attempt_no'] + 1}/{MAX_RETAKE} 次）", inquiry["request_id"])
        payload = _get(inquiry_id)
        payload["message"] = f"已为你重拍（第 {payload['attempt_no']} 次）"
        payload["new_image_task"] = request_id
        payload["new_image_created_at"] = created_at
        return payload, 200

    if decision == "decline":
        cooldown = now + (MUTE_COOLDOWN_S if mute else DECLINE_COOLDOWN_S)
        _update(inquiry_id, status="declined", decision="decline", decision_channel=channel,
                answered_at=now, cooldown_until=cooldown,
                note="本次不再询问" if mute else "用户选择不用了")
        _emit(f"用户拒绝重拍 → declined，同原因冷却至 {_fmt(cooldown)}"
              f"{'（本次不再询问）' if mute else ''}", inquiry["request_id"])
        payload = _get(inquiry_id)
        payload["message"] = "好的，不再重拍；同类情况暂时不会再打扰你"
        return payload, 200

    # decision == "usable"：系统误判纠正。只改询问记录，不写第 8 周人工纠错表。
    cooldown = now + DISMISS_COOLDOWN_S
    _update(inquiry_id, status="dismissed", decision="usable", decision_channel=channel,
            answered_at=now, cooldown_until=cooldown,
            note="用户判定画面其实可用（系统误判纠正）")
    _emit(f"用户判定画面可用 → dismissed（误判纠正），同原因冷却至 {_fmt(cooldown)}",
          inquiry["request_id"])
    payload = _get(inquiry_id)
    payload["message"] = "已记录：这张图可用，本次不重拍"
    payload["misjudged"] = True
    return payload, 200


# ==================== 通道投递 ====================

def pending_for_device():
    """设备通道：取一条未投递的询问并标记已投递。

    只在设备轮询 GET /api/collect/pending 时调用；投递一次即标记，
    不重复下发（避免同一条询问每 2 秒灌一次）。
    """
    sweep_expired()
    conn = _connect()
    c = conn.cursor()
    c.execute(f"SELECT {_COLUMNS} FROM image_inquiries "
              "WHERE status='asking' AND delivered_at IS NULL "
              "ORDER BY created_at ASC LIMIT 1")
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    inquiry_id = row[0]
    c.execute("UPDATE image_inquiries SET delivered_at=? WHERE inquiry_id=? AND delivered_at IS NULL",
              (_now(), inquiry_id))
    conn.commit()
    conn.close()
    payload = _row_to_dict(row)
    _emit(f"询问已投递设备: {payload['inquiry_id']}（{payload['reason_label']}）",
          payload["request_id"])
    return payload


def pending_for_ui():
    """网页通道：返回当前未决询问（网页常在线，不消费投递标记）。"""
    sweep_expired()
    conn = _connect()
    c = conn.cursor()
    c.execute(f"SELECT {_COLUMNS} FROM image_inquiries WHERE status='asking' "
              "ORDER BY created_at ASC LIMIT 1")
    row = c.fetchone()
    conn.close()
    return _row_to_dict(row)


def list_recent(limit=20, status=None):
    """最近询问列表（前端历史与验收证据用）。"""
    sweep_expired()
    conn = _connect()
    c = conn.cursor()
    if status:
        c.execute(f"SELECT {_COLUMNS} FROM image_inquiries WHERE status=? "
                  "ORDER BY created_at DESC LIMIT ?", (status, limit))
    else:
        c.execute(f"SELECT {_COLUMNS} FROM image_inquiries "
                  "ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def describe(inquiry_id):
    sweep_expired()
    return _get(inquiry_id)


# ==================== 文字/语音答复匹配 ====================

def match_answer_text(text):
    """把一句话映射成询问答复动作；匹配不到返回 None。

    只做规则匹配，不新增意图枚举：语音与文字助手在调用 LLM 之前先问一次本函数，
    命中就直接答复询问，避免为了本周新意图改动第 6 周的意图分类契约。
    """
    if not text:
        return None
    lowered = text.strip()
    if not pending_for_ui():
        return None

    # 顺序重要："不用重拍"既含"不用"也含"重拍"，必须先判"其实可用"
    if any(w in lowered for w in USABLE_WORDS):
        return "usable"
    if any(w in lowered for w in DECLINE_WORDS):
        return "decline"
    if any(w in lowered for w in RETAKE_WORDS):
        return "retake"
    return None


def try_answer_from_text(text, channel="voice"):
    """若文本是一次询问答复，则直接答复并返回结果 dict；否则返回 None。"""
    decision = match_answer_text(text)
    if not decision:
        return None
    inquiry = pending_for_ui()
    if not inquiry:
        return None
    payload, status_code = answer(inquiry["inquiry_id"], decision, channel)
    payload["matched_decision"] = decision
    payload["ok"] = status_code == 200
    return payload


def config_snapshot():
    """冷却与门控参数的只读快照（前端展示与测试断言用）。"""
    return {
        "timeout_s": INQUIRY_TIMEOUT_S,
        "global_gap_s": INQUIRY_GLOBAL_GAP_S,
        "decline_cooldown_s": DECLINE_COOLDOWN_S,
        "expire_cooldown_s": EXPIRE_COOLDOWN_S,
        "dismiss_cooldown_s": DISMISS_COOLDOWN_S,
        "max_retake": MAX_RETAKE,
        "allow_replay": ALLOW_REPLAY,
        "askable_reasons": sorted(REASONS.keys()),
        "non_inquiry_reasons": sorted(NON_INQUIRY_REASONS),
    }


# ==================== HTTP 端点 ====================

@bp.route("/api/inquiry/pending", methods=["GET"])
def api_pending():
    """网页轮询：当前是否有待答复的询问。"""
    return jsonify({"inquiry": pending_for_ui(), "config": config_snapshot()})


@bp.route("/api/inquiry/<inquiry_id>/answer", methods=["POST"])
def api_answer(inquiry_id):
    """网页/语音答复入口。

    请求体：{"decision": "retake"|"decline"|"usable",
             "channel": "ui"|"voice", "mute": false}
    """
    data = request.get_json(silent=True) or {}
    decision = data.get("decision", "")
    channel = data.get("channel", "ui")
    if channel not in CHANNELS:
        channel = "ui"
    payload, status_code = answer(inquiry_id, decision, channel,
                                  mute=bool(data.get("mute", False)))
    return jsonify(payload), status_code


@bp.route("/api/inquiry/<inquiry_id>", methods=["GET"])
def api_get(inquiry_id):
    inquiry = describe(inquiry_id)
    if not inquiry:
        return jsonify({"error": "询问不存在"}), 404
    return jsonify(inquiry)


@bp.route("/api/inquiries", methods=["GET"])
def api_list():
    """最近询问列表，供前端历史展示与第 9 周验收取证。"""
    limit = request.args.get("limit", default=20, type=int)
    status = request.args.get("status")
    items = list_recent(limit=max(1, min(limit, 200)), status=status)
    return jsonify({
        "inquiries": items,
        "count": len(items),
        "config": config_snapshot(),
    })

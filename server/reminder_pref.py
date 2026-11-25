# -*- coding: utf-8 -*-
"""第 12 周：让用户纠错影响后续提醒（显式偏好 + 配置版本回退）

本周问题：用户说"这次不用提醒"，系统怎样记住当前选择，又能恢复默认？

本模块是「提醒偏好」的唯一权威，只做四件事：

  1. 有限反馈规则 F1–F5：把用户对提醒的反应收成可解释的显式偏好
       F1 confirm 确认   —— 采纳提醒（重拍），撤销同一条的"稍后"，不新增偏好
       F2 correct 纠正   —— "其实可用"（系统误判）→ 该类原因静音偏好
       F3 ignore  忽略   —— "这次不用提醒" → 本轮静音偏好（默认只影响本轮）
       F4 snooze  稍后   —— 现在别问 → 该事件到期前抑制，到期后同类事件仍可提醒
       F5 restore_default 恢复默认 —— 撤销本轮全部偏好并把参数退回默认快照

  2. 偏好作用域：本轮(session) / 该类原因(event_reason) / 这条事件(event_key) /
     这张图(request_id)。作用域越小越贴合"这次不用提醒"的字面意思，
     默认不静默升级成永久关闭——保存反馈不等于在线学习成功。

  3. 配置版本与回退：每次参数或生效偏好集合变化都追加一个版本（append-only），
     回退 = 用目标版本的快照重新生效并生成新版本；历史永不删除，可前后对照。

  4. 事件计数：按 event_key 统计"观测次数 / 实际提醒次数 / 抑制次数"，
     这是"修改前后重复提醒次数"这组对比的唯一数据来源。

与既有代码的分工（严格增量，不重写底层）：

  * 第 9 周的记录级冷却（G4：decline/expire/dismiss 冷却）保持原样，
    它就是"默认策略"；本周的偏好是叠加在默认策略之上的显式策略，
    撤销偏好 = 回到默认策略，而不是"完全没有冷却"。
  * 第 9 周的状态机、通道投递、重拍链路一律不改，只在门控里加一道 G6。
  * 第 11 周上下文引擎只读地引用本模块的偏好状态（R10），不改 R1–R9。
  * 不写 imu_data / image_records / inference_results / human_feedbacks。

"本轮"的定义：服务端进程一次启动为一轮，SESSION_ID 进程级唯一，
TTL 默认 15 分钟（PREF_SESSION_TTL_S 可覆盖，测试可缩到秒级）。
本轮到期后 session 级偏好自动失效并记为 session_expired —— 自动恢复默认。

运行位置：服务端（与 app.py 同进程）。
测试：server/test_week12.py（固定 now + 临时库）。
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid

from flask import Blueprint, jsonify, request

# ==================== 可调参数（环境变量给默认值，运行期以配置版本为准） ====================
# 本轮时长：进程启动后多久算"本轮结束"，本轮偏好随之失效
DEFAULT_SESSION_TTL_S = float(os.environ.get("PREF_SESSION_TTL_S", "900"))
# "稍后再说"的默认抑制时长
DEFAULT_SNOOZE_S = float(os.environ.get("PREF_SNOOZE_S", "300"))
# "这类原因都别问了"的静音时长
DEFAULT_REASON_MUTE_S = float(os.environ.get("PREF_REASON_MUTE_S", "86400"))
# "其实可用"（误判纠正）后该类原因的静音时长，与第 9 周 DISMISS 冷却同口径
DEFAULT_CORRECT_MUTE_S = float(os.environ.get("PREF_CORRECT_MUTE_S", "1800"))

# 偏好作用域与动作（有限集合，不接受外部自由取值）
SCOPES = {"session", "event_reason", "event_key", "request_id"}
ACTIONS = {"mute", "snooze"}
FEEDBACKS = {"confirm", "correct", "ignore", "snooze", "restore_default",
             "revoke", "rollback", "params", "session_expired"}
CHANNELS = {"ui", "device_key", "voice", "system"}

# 作用域短标签（前端与证据文件共用口径）
SCOPE_LABELS = {
    "session": "本轮",
    "event_reason": "该类原因",
    "event_key": "这条事件",
    "request_id": "这张图",
}
FEEDBACK_LABELS = {
    "default": "默认配置快照",
    "confirm": "确认（采纳提醒）",
    "correct": "纠正（其实可用）",
    "ignore": "忽略（这次不用提醒）",
    "snooze": "稍后（待会儿再问）",
    "restore_default": "恢复默认",
    "revoke": "清除偏好",
    "rollback": "配置回退",
    "params": "参数变更",
    "session_expired": "本轮到期",
}
# 原因短标签：event_reason 作用域的 scope_key 就是询问的 reason 代码，
# 文案与 inquiry.REASONS 的短标签逐项对齐（本模块不 import inquiry，避免反向依赖）。
REASON_LABELS = {
    "blocked": "画面被遮挡",
    "dark": "画面过暗",
    "blur": "画面模糊",
    "capture_failed": "采集失败",
    "decode_failed": "图像无法解码",
    "model_unavailable": "视觉服务不可用",
}

bp = Blueprint("reminder_pref", __name__)

_logger = logging.getLogger("reminder_pref")
_logger.setLevel(logging.INFO)
_logger.propagate = False
if not _logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[PREF] %(message)s"))
    _logger.addHandler(_h)

_db_file = "imu_data.db"
_log = None

# ==================== 本轮（进程一轮）====================
# 进程启动即确定，重启服务等于开启新一轮；本轮偏好只在这一轮内有效。
SESSION_ID = "sess-" + uuid.uuid4().hex[:8]
SESSION_STARTED_AT = time.time()


def configure(db_file, log=None):
    """注入数据库路径与日志回调（与 inquiry.configure 同一风格，避免循环 import）。"""
    global _db_file, _log
    _db_file = db_file
    _log = log


def _emit(msg, request_id=None):
    _logger.info(msg)
    if _log:
        try:
            _log(msg, request_id)
        except Exception:  # 日志失败绝不影响主流程
            pass


def _now():
    return time.time()


def _connect(db_path=None):
    conn = sqlite3.connect(db_path or _db_file)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt(ts):
    if not ts:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def event_key_for(request_id, reason):
    """事件签名：同一张图的同一类不可用判定算同一个事件。

    只由 request_id + reason 决定，因此"同一事件重放"（重复上报、回放二次导入）
    一定落到同一个 key，可稳定统计重复提醒次数。
    """
    raw = f"{request_id or ''}|{reason or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ==================== 建表 ====================

def init_db(db_path=None):
    """建表并写入默认配置快照 v1（幂等，只新增表，不改任何已有表结构）。"""
    conn = _connect(db_path)
    c = conn.cursor()

    # 显式偏好：一条偏好一行，撤销只标记 revoked_at，历史保留
    c.execute("""
        CREATE TABLE IF NOT EXISTS reminder_preferences (
            pref_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT 'mute',
            reason TEXT,
            request_id TEXT,
            event_key TEXT,
            session_id TEXT,
            inquiry_id TEXT,
            feedback TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'ui',
            created_at REAL NOT NULL,
            expires_at REAL,
            revoked_at REAL,
            revoked_by TEXT,
            config_version INTEGER NOT NULL,
            note TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_pref_scope "
              "ON reminder_preferences(scope, scope_key)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pref_active "
              "ON reminder_preferences(revoked_at, expires_at)")

    # 事件计数：重复提醒次数前后对照的数据来源
    c.execute("""
        CREATE TABLE IF NOT EXISTS reminder_events (
            event_key TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            request_id TEXT,
            session_id TEXT,
            inquiry_id TEXT,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 0,
            trigger_count INTEGER NOT NULL DEFAULT 0,
            suppress_count INTEGER NOT NULL DEFAULT 0,
            last_suppress_code TEXT,
            last_suppress_at REAL,
            last_suppress_pref TEXT,
            config_version INTEGER
        )
    """)

    # 反馈流水：每次用户纠错/清除/回退都留一条记录（配置变化记录）
    c.execute("""
        CREATE TABLE IF NOT EXISTS reminder_feedbacks (
            feedback_id TEXT PRIMARY KEY,
            inquiry_id TEXT,
            event_key TEXT,
            request_id TEXT,
            reason TEXT,
            feedback TEXT NOT NULL,
            decision TEXT,
            scope TEXT,
            channel TEXT NOT NULL DEFAULT 'ui',
            pref_id TEXT,
            before_version INTEGER,
            after_version INTEGER,
            created_at REAL NOT NULL,
            note TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_feedback_time "
              "ON reminder_feedbacks(created_at)")

    # 配置版本：append-only，回退也是新增一个版本
    c.execute("""
        CREATE TABLE IF NOT EXISTS config_versions (
            version INTEGER PRIMARY KEY,
            created_at REAL NOT NULL,
            source TEXT NOT NULL,
            from_version INTEGER,
            params_json TEXT NOT NULL,
            prefs_json TEXT,
            note TEXT
        )
    """)

    row = c.execute("SELECT MAX(version) FROM config_versions").fetchone()
    if not row[0]:
        _append_version(c, "default", default_params(), [], None,
                        "默认配置快照（本周起点）", _now())
    conn.commit()
    conn.close()


def default_params():
    """默认参数（由环境变量给初值）；本周四个可调旋钮都进配置版本，可回退。"""
    return {
        "session_ttl_s": DEFAULT_SESSION_TTL_S,
        "snooze_s": DEFAULT_SNOOZE_S,
        "reason_mute_s": DEFAULT_REASON_MUTE_S,
        "correct_mute_s": DEFAULT_CORRECT_MUTE_S,
    }


def params_snapshot_text():
    p = params()
    return (f"本轮 {p['session_ttl_s']:.0f}s · 稍后 {p['snooze_s']:.0f}s · "
            f"原因静音 {p['reason_mute_s']:.0f}s · 纠正静音 {p['correct_mute_s']:.0f}s")


# ==================== 配置版本 ====================

def _append_version(c, source, params_dict, prefs_summary, from_version, note, now,
                    db_path=None):
    """追加一个配置版本，返回版本号。只 INSERT，永不 UPDATE/DELETE 历史。"""
    version = (c.execute("SELECT MAX(version) FROM config_versions").fetchone()[0] or 0) + 1
    c.execute("""
        INSERT INTO config_versions (version, created_at, source, from_version,
                                     params_json, prefs_json, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (version, now, source, from_version,
          json.dumps(params_dict, ensure_ascii=False),
          json.dumps(prefs_summary, ensure_ascii=False), note))
    return version


def params(db_path=None, now=None):
    """当前生效参数：取最新配置版本的参数快照；读不到时回落到默认值。"""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT params_json FROM config_versions "
                           "ORDER BY version DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row:
        return default_params()
    try:
        stored = json.loads(row["params_json"])
    except (TypeError, ValueError):
        return default_params()
    merged = default_params()
    merged.update({k: float(v) for k, v in stored.items() if k in merged})
    return merged


def current_version(db_path=None):
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT MAX(version) FROM config_versions").fetchone()
    finally:
        conn.close()
    return int(row[0] or 0)


def list_versions(limit=20, db_path=None):
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM config_versions ORDER BY version DESC LIMIT ?",
                            (max(1, min(int(limit), 100)),)).fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        items.append({
            "version": r["version"],
            "created_at": r["created_at"],
            "created_at_text": _fmt(r["created_at"]),
            "source": r["source"],
            "source_label": FEEDBACK_LABELS.get(r["source"], r["source"]),
            "from_version": r["from_version"],
            "params": _loads(r["params_json"], {}),
            "preferences": _loads(r["prefs_json"], []),
            "note": r["note"],
        })
    return items


def _loads(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def update_params(new_params, note="", channel="system", db_path=None):
    """参数变更：生成新配置版本（配置变化记录）。只接受本周四个旋钮。"""
    now = _now()
    conn = _connect(db_path)
    try:
        c = conn.cursor()
        before = current_version(db_path)
        merged = params(db_path, now)
        changed = {}
        for key in ("session_ttl_s", "snooze_s", "reason_mute_s", "correct_mute_s"):
            if key in new_params:
                try:
                    value = float(new_params[key])
                except (TypeError, ValueError):
                    continue
                if 1.0 <= value <= 7 * 86400:
                    changed[key] = value
        if not changed:
            return {"error": "没有可更新的参数（可改：session_ttl_s / snooze_s / "
                             "reason_mute_s / correct_mute_s）"}, 400
        merged.update(changed)
        version = _append_version(c, "params", merged, _active_rows(conn, now),
                                  before, note or f"参数变更：{changed}", now)
        conn.commit()
    finally:
        conn.close()
    _emit(f"配置参数变更 → v{version}（{params_snapshot_text()}）")
    return {"version": version, "from_version": before, "params": merged,
            "changed": changed, "channel": channel}, 200


def rollback(to_version, note="", db_path=None):
    """回退到指定配置版本：用目标版本的参数与偏好集合重新生效，并追加新版本。

    append-only：历史版本不删除，回退本身也是一条"配置变化记录"。
    """
    now = _now()
    conn = _connect(db_path)
    try:
        c = conn.cursor()
        target = c.execute("SELECT * FROM config_versions WHERE version=?",
                           (int(to_version),)).fetchone()
        if not target:
            return {"error": f"配置版本 {to_version} 不存在"}, 404
        before = current_version(db_path)

        # 1) 撤销当前所有生效偏好
        revoked = _revoke_all(c, now, "rollback", None)

        # 2) 按目标版本的偏好快照重新写入（去掉已过期的）
        restored = 0
        for item in _loads(target["prefs_json"], []):
            if item.get("expires_at") and item["expires_at"] <= now:
                continue
            pref_id = "pref-" + uuid.uuid4().hex[:10]
            c.execute("""
                INSERT INTO reminder_preferences
                (pref_id, scope, scope_key, action, reason, request_id, event_key,
                 session_id, inquiry_id, feedback, channel, created_at, expires_at,
                 config_version, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'rollback', 'system', ?, ?, ?, ?)
            """, (pref_id, item["scope"], item["scope_key"], item.get("action", "mute"),
                  item.get("reason"), item.get("request_id"), item.get("event_key"),
                  item.get("session_id"), item.get("inquiry_id"), now,
                  item.get("expires_at"), 0, f"由 v{target['version']} 回退恢复"))
            restored += 1

        version = _append_version(c, "rollback", _loads(target["params_json"], default_params()),
                                  _active_rows(conn, now), before,
                                  note or f"回退到 v{target['version']}", now)
        c.execute("""
            INSERT INTO reminder_feedbacks (feedback_id, feedback, channel, before_version,
                                            after_version, created_at, note)
            VALUES (?, 'rollback', 'system', ?, ?, ?, ?)
        """, ("fb-" + uuid.uuid4().hex[:10], before, version, now,
              f"回退到 v{target['version']}：撤销 {revoked} 条、恢复 {restored} 条"))
        conn.commit()
    finally:
        conn.close()
    _emit(f"配置回退 v{before} → v{version}（目标 v{to_version}）：撤销 {revoked} 条，恢复 {restored} 条")
    return {"version": version, "from_version": before, "rolled_back_to": target["version"],
            "revoked": revoked, "restored": restored}, 200


def _revoke_all(c, now, revoked_by, session_id):
    """撤销生效偏好，返回条数。session 级可只撤当前轮。"""
    if session_id:
        rows = c.execute("SELECT pref_id FROM reminder_preferences "
                         "WHERE revoked_at IS NULL AND scope='session' AND session_id=?",
                         (session_id,)).fetchall()
    else:
        rows = c.execute("SELECT pref_id FROM reminder_preferences "
                         "WHERE revoked_at IS NULL").fetchall()
    ids = [r["pref_id"] for r in rows]
    for pref_id in ids:
        c.execute("UPDATE reminder_preferences SET revoked_at=?, revoked_by=? "
                  "WHERE pref_id=?", (now, revoked_by, pref_id))
    return len(ids)


def _active_rows(conn, now):
    """当前生效偏好的摘要（写进配置版本快照，供回退重建）。"""
    return [{"pref_id": r["pref_id"], "scope": r["scope"], "scope_key": r["scope_key"],
             "action": r["action"], "reason": r["reason"], "request_id": r["request_id"],
             "event_key": r["event_key"], "session_id": r["session_id"],
             "inquiry_id": r["inquiry_id"], "feedback": r["feedback"],
             "channel": r["channel"], "expires_at": r["expires_at"]}
            for r in active_preferences(db_path=None, now=now, conn=conn)]


# ==================== 本轮与到期清理 ====================

def session_ttl(db_path=None):
    return float(params(db_path)["session_ttl_s"])


def session_remaining_s(db_path=None, now=None):
    """本轮剩余秒数；<=0 表示本轮已结束（下次读写会触发失效）。"""
    now = _now() if now is None else float(now)
    return max(0.0, SESSION_STARTED_AT + session_ttl(db_path) - now)


def sweep_session(db_path=None, now=None):
    """本轮到期：session 级偏好自动失效（自动恢复默认）。返回失效条数。"""
    now = _now() if now is None else float(now)
    conn = _connect(db_path)
    try:
        c = conn.cursor()
        expired = (now - SESSION_STARTED_AT) > session_ttl(db_path)
        if not expired:
            return 0
        rows = c.execute("SELECT pref_id FROM reminder_preferences "
                         "WHERE revoked_at IS NULL AND scope='session' AND session_id=?",
                         (SESSION_ID,)).fetchall()
        for r in rows:
            c.execute("UPDATE reminder_preferences SET revoked_at=?, revoked_by=? "
                      "WHERE pref_id=?", (now, "session_expired", r["pref_id"]))
        if rows:
            c.execute("""
                INSERT INTO reminder_feedbacks (feedback_id, feedback, channel,
                                                before_version, after_version, created_at, note)
                VALUES (?, 'session_expired', 'system', ?, ?, ?, ?)
            """, ("fb-" + uuid.uuid4().hex[:10], current_version(db_path),
                  current_version(db_path), now, f"本轮到期（{SESSION_ID}），自动恢复默认"))
        conn.commit()
    finally:
        conn.close()
    if rows:
        _emit(f"本轮到期 → 失效 {len(rows)} 条本轮偏好，自动恢复默认")
    return len(rows)


def reset_session(db_path=None, now=None):
    """测试用：开启新一轮（不改代码即可验证"本轮结束后自动恢复默认"）。"""
    global SESSION_ID, SESSION_STARTED_AT
    SESSION_ID = "sess-" + uuid.uuid4().hex[:8]
    SESSION_STARTED_AT = _now() if now is None else float(now)
    return SESSION_ID


# ==================== 偏好读写 ====================

def active_preferences(db_path=None, now=None, conn=None):
    """当前生效的偏好（未撤销、未过期，且本轮偏好属于当前轮）。"""
    now = _now() if now is None else float(now)
    own = conn is None
    conn = conn or _connect(db_path)
    try:
        rows = conn.execute("""
            SELECT * FROM reminder_preferences
            WHERE revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC
        """, (now,)).fetchall()
    finally:
        if own:
            conn.close()
    out = []
    for r in rows:
        # 本轮偏好只在当前这一轮内有效
        if r["scope"] == "session" and r["session_id"] != SESSION_ID:
            continue
        out.append(r)
    return out


def _pref_to_dict(r, now):
    remaining = max(0, int(round((r["expires_at"] or 0) - now))) if r["expires_at"] else None
    return {
        "pref_id": r["pref_id"],
        "scope": r["scope"],
        "scope_label": SCOPE_LABELS.get(r["scope"], r["scope"]),
        "scope_key": r["scope_key"],
        "action": r["action"],
        "reason": r["reason"],
        "request_id": r["request_id"],
        "event_key": r["event_key"],
        "session_id": r["session_id"],
        "inquiry_id": r["inquiry_id"],
        "feedback": r["feedback"],
        "feedback_label": FEEDBACK_LABELS.get(r["feedback"], r["feedback"]),
        "channel": r["channel"],
        "created_at": r["created_at"],
        "created_at_text": _fmt(r["created_at"]),
        "expires_at": r["expires_at"],
        "expires_at_text": _fmt(r["expires_at"]),
        "remaining_s": remaining,
        "config_version": r["config_version"],
        "note": r["note"],
    }


def set_preference(scope, scope_key, action="mute", *, reason=None, request_id=None,
                   event_key=None, inquiry_id=None, feedback="ignore", channel="ui",
                   duration_s=None, until=None, note=None, db_path=None):
    """写入一条显式偏好，并追加一个配置版本（配置变化记录）。

    :param duration_s: 相对时长；与 until 二选一，都不给则长期有效（本轮由 session 控制）
    :param until: 绝对到期时间
    """
    if scope not in SCOPES:
        return {"error": f"未知作用域 '{scope}'"}, 400
    if action not in ACTIONS:
        return {"error": f"未知动作 '{action}'"}, 400
    if feedback not in FEEDBACKS:
        feedback = "manual" if feedback is None else "ignore"

    now = _now()
    sweep_session(db_path, now)
    p = params(db_path, now)
    if duration_s is None and until is None:
        duration_s = {"event_key": p["snooze_s"], "event_reason": p["reason_mute_s"]}.get(scope)
    expires_at = float(until) if until is not None else (
        now + float(duration_s) if duration_s else None)

    pref_id = "pref-" + uuid.uuid4().hex[:10]
    conn = _connect(db_path)
    try:
        c = conn.cursor()
        before = current_version(db_path)
        # 同一作用域+键只保留一条生效偏好：先撤销旧的，避免重复叠加
        c.execute("""
            UPDATE reminder_preferences SET revoked_at=?, revoked_by='replace'
            WHERE revoked_at IS NULL AND scope=? AND scope_key=?
        """, (now, scope, scope_key))
        c.execute("""
            INSERT INTO reminder_preferences
            (pref_id, scope, scope_key, action, reason, request_id, event_key,
             session_id, inquiry_id, feedback, channel, created_at, expires_at,
             config_version, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pref_id, scope, scope_key, action, reason, request_id, event_key,
              SESSION_ID if scope == "session" else None, inquiry_id, feedback,
              channel, now, expires_at, 0, note))
        version = _append_version(c, feedback if feedback in FEEDBACKS else "ignore",
                                  p, _active_rows(conn, now), before,
                                  f"{SCOPE_LABELS.get(scope, scope)}"
                                  f"{'静音' if action == 'mute' else '稍后'}：{scope_key}", now)
        c.execute("UPDATE reminder_preferences SET config_version=? WHERE pref_id=?",
                  (version, pref_id))
        c.execute("""
            INSERT INTO reminder_feedbacks (feedback_id, inquiry_id, event_key, request_id,
                                            reason, feedback, scope, channel, pref_id,
                                            before_version, after_version, created_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("fb-" + uuid.uuid4().hex[:10], inquiry_id, event_key, request_id, reason,
              feedback, scope, channel, pref_id, before, version, now, note))
        conn.commit()
    finally:
        conn.close()

    _emit(f"记录提醒偏好：{SCOPE_LABELS.get(scope, scope)} {scope_key} "
          f"{'静音' if action == 'mute' else '稍后'}至 {_fmt(expires_at) or '本轮结束'}"
          f"（v{before} → v{version}）", request_id)
    return {"pref_id": pref_id, "version": version, "from_version": before,
            "expires_at": expires_at, "expires_at_text": _fmt(expires_at)}, 200


def revoke_preference(pref_id, reason="revoke", db_path=None):
    """清除单条偏好（当堂验证：能查看并清除本轮偏好）。"""
    now = _now()
    conn = _connect(db_path)
    try:
        c = conn.cursor()
        row = c.execute("SELECT * FROM reminder_preferences WHERE pref_id=?",
                        (pref_id,)).fetchone()
        if not row:
            return {"error": "偏好不存在"}, 404
        if row["revoked_at"] is not None:
            return {"error": "该偏好已清除", "pref_id": pref_id}, 409
        before = current_version(db_path)
        c.execute("UPDATE reminder_preferences SET revoked_at=?, revoked_by=? WHERE pref_id=?",
                  (now, reason, pref_id))
        p = params(db_path, now)
        version = _append_version(c, "revoke", p, _active_rows(conn, now), before,
                                  f"清除偏好 {pref_id}（{SCOPE_LABELS.get(row['scope'], row['scope'])} "
                                  f"{row['scope_key']}）", now)
        c.execute("""
            INSERT INTO reminder_feedbacks (feedback_id, inquiry_id, event_key, request_id,
                                            reason, feedback, scope, channel, pref_id,
                                            before_version, after_version, created_at, note)
            VALUES (?, ?, ?, ?, ?, 'revoke', ?, ?, ?, ?, ?, ?, ?)
        """, ("fb-" + uuid.uuid4().hex[:10], row["inquiry_id"], row["event_key"],
              row["request_id"], row["reason"], row["scope"], row["channel"], pref_id,
              before, version, now, "用户清除该偏好，后续提醒回到默认策略"))
        conn.commit()
    finally:
        conn.close()
    _emit(f"清除提醒偏好 {pref_id} → v{before} → v{version}（回到默认策略）")
    return {"pref_id": pref_id, "revoked": True, "version": version,
            "from_version": before}, 200


def revoke_by_key(scope, scope_key, feedback="revoke", channel="system", note="",
                  db_path=None):
    """撤销"某一条具体目标"上的生效偏好（F1 确认采纳提醒时用来清掉同事件的"稍后"）。

    与 revoke_preference 的区别：调用方只有 scope+key（事件签名），没有 pref_id。
    没有匹配到生效偏好时不产生新版本，避免"确认重拍"这种正常操作刷屏配置历史。
    """
    now = _now()
    conn = _connect(db_path)
    try:
        c = conn.cursor()
        rows = c.execute("""
            SELECT pref_id FROM reminder_preferences
            WHERE revoked_at IS NULL AND scope=? AND scope_key=?
        """, (scope, scope_key)).fetchall()
        if not rows:
            return {"revoked": 0, "version": current_version(db_path)}, 200
        before = current_version(db_path)
        ids = [r["pref_id"] for r in rows]
        for pref_id in ids:
            c.execute("UPDATE reminder_preferences SET revoked_at=?, revoked_by=? "
                      "WHERE pref_id=?", (now, feedback, pref_id))
        p = params(db_path, now)
        version = _append_version(c, feedback, p, _active_rows(conn, now), before,
                                  note or f"撤销 {SCOPE_LABELS.get(scope, scope)} 上的偏好"
                                          f"（{scope_key}）", now)
        c.execute("""
            INSERT INTO reminder_feedbacks (feedback_id, event_key, request_id, feedback,
                                            scope, channel, before_version, after_version,
                                            created_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("fb-" + uuid.uuid4().hex[:10],
              scope_key if scope == "event_key" else None,
              scope_key if scope == "request_id" else None,
              feedback, scope, channel, before, version, now,
              note or f"撤销 {len(ids)} 条偏好"))
        conn.commit()
    finally:
        conn.close()
    _emit(f"撤销 {SCOPE_LABELS.get(scope, scope)} 上的 {len(ids)} 条偏好 "
          f"→ v{before} → v{version}")
    return {"revoked": len(ids), "pref_ids": ids, "version": version,
            "from_version": before}, 200


def session_deadline(db_path=None, now=None):
    """本轮的绝对结束时间（写 session 级偏好的 expires_at 时用，保证到期口径一致）。"""
    now = _now() if now is None else float(now)
    return SESSION_STARTED_AT + session_ttl(db_path)


def clear_session(db_path=None):
    """清除本轮全部偏好（不影响原因/事件级偏好；要全部回到默认用 restore_default）。"""
    now = _now()
    conn = _connect(db_path)
    try:
        c = conn.cursor()
        before = current_version(db_path)
        revoked = _revoke_all(c, now, "manual", SESSION_ID)
        p = params(db_path, now)
        version = _append_version(c, "revoke", p, _active_rows(conn, now), before,
                                  f"清除本轮偏好（{SESSION_ID}）", now)
        c.execute("""
            INSERT INTO reminder_feedbacks (feedback_id, feedback, channel, before_version,
                                            after_version, created_at, note)
            VALUES (?, 'revoke', 'ui', ?, ?, ?, ?)
        """, ("fb-" + uuid.uuid4().hex[:10], before, version, now,
              f"清除本轮 {revoked} 条偏好"))
        conn.commit()
    finally:
        conn.close()
    _emit(f"清除本轮偏好 {revoked} 条 → v{before} → v{version}")
    return {"revoked": revoked, "session_id": SESSION_ID, "version": version,
            "from_version": before}, 200


def restore_default(db_path=None):
    """恢复默认：撤销全部偏好 + 参数回到默认快照（v1）。

    仍然是 append-only：生成新版本，历史完整保留，可再回退。
    """
    now = _now()
    conn = _connect(db_path)
    try:
        c = conn.cursor()
        before = current_version(db_path)
        base = c.execute("SELECT params_json FROM config_versions "
                        "ORDER BY version ASC LIMIT 1").fetchone()
        base_params = _loads(base["params_json"], default_params()) if base else default_params()
        revoked = _revoke_all(c, now, "restore_default", None)
        version = _append_version(c, "restore_default", base_params, [], before,
                                  "恢复默认：清空全部提醒偏好，参数回到默认快照", now)
        c.execute("""
            INSERT INTO reminder_feedbacks (feedback_id, feedback, channel, before_version,
                                            after_version, created_at, note)
            VALUES (?, 'restore_default', 'ui', ?, ?, ?, ?)
        """, ("fb-" + uuid.uuid4().hex[:10], before, version, now,
              f"恢复默认：撤销 {revoked} 条偏好"))
        conn.commit()
    finally:
        conn.close()
    _emit(f"恢复默认提醒 → v{before} → v{version}（撤销 {revoked} 条偏好）")
    return {"revoked": revoked, "version": version, "from_version": before,
            "params": base_params}, 200


# ==================== G6 偏好门 ====================

def check_gate(event_key, reason, request_id, db_path=None, now=None):
    """G6：这条提醒是否被用户的显式偏好压掉了。

    :return: None 表示不拦；否则返回 {"code", "pref_id", "until", "remaining_s", "hint"}
    """
    now = _now() if now is None else float(now)
    sweep_session(db_path, now)
    conn = _connect(db_path)
    try:
        rows = active_preferences(db_path, now, conn)
    finally:
        conn.close()

    for r in rows:
        if r["action"] != "mute":
            continue
        if r["scope"] == "session":
            code, detail = "session_muted", "本轮不再提醒"
        elif r["scope"] == "event_reason" and r["scope_key"] == reason:
            code, detail = "reason_muted", f"该类原因（{reason}）已被静音"
        elif r["scope"] == "event_key" and r["scope_key"] == event_key:
            code, detail = "event_muted", "这条事件已被静音"
        elif r["scope"] == "request_id" and r["scope_key"] == str(request_id):
            code, detail = "request_muted", "这张图已被静音"
        else:
            continue
        return {"code": code, "pref_id": r["pref_id"], "detail": detail,
                "until": r["expires_at"], "remaining_s": _remaining(r["expires_at"], now),
                "scope": r["scope"], "feedback": r["feedback"]}

    for r in rows:
        if r["action"] == "snooze" and r["scope"] == "event_key" and r["scope_key"] == event_key:
            return {"code": "event_snoozed", "pref_id": r["pref_id"],
                    "detail": "用户选择稍后再说，还没到时间", "until": r["expires_at"],
                    "remaining_s": _remaining(r["expires_at"], now),
                    "scope": r["scope"], "feedback": r["feedback"]}
    return None


def _remaining(expires_at, now):
    if not expires_at:
        return None
    return max(0, int(round(float(expires_at) - now)))


# ==================== 事件计数（前后对照的数据来源） ====================

def record_event(event_key, reason, request_id, db_path=None, now=None,
                 outcome="triggered", code=None, pref_id=None, inquiry_id=None):
    """按事件累计观测/提醒/抑制计数。

    outcome: triggered（真的发出了提醒） / suppressed（被门控压掉）
    """
    now = _now() if now is None else float(now)
    conn = _connect(db_path)
    try:
        c = conn.cursor()
        c.execute("""
            INSERT OR IGNORE INTO reminder_events
            (event_key, reason, request_id, session_id, inquiry_id,
             first_seen_at, last_seen_at, seen_count, trigger_count, suppress_count,
             config_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?)
        """, (event_key, reason, request_id, SESSION_ID, inquiry_id, now, now,
              current_version(db_path)))
        if outcome == "triggered":
            c.execute("""
                UPDATE reminder_events
                SET seen_count=seen_count+1, trigger_count=trigger_count+1,
                    last_seen_at=?, inquiry_id=COALESCE(?, inquiry_id), session_id=?
                WHERE event_key=?
            """, (now, inquiry_id, SESSION_ID, event_key))
        else:
            c.execute("""
                UPDATE reminder_events
                SET seen_count=seen_count+1, suppress_count=suppress_count+1,
                    last_seen_at=?, last_suppress_code=?, last_suppress_at=?,
                    last_suppress_pref=?, session_id=?
                WHERE event_key=?
            """, (now, code, now, pref_id, SESSION_ID, event_key))
        conn.commit()
    finally:
        conn.close()


def list_events(limit=50, db_path=None):
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM reminder_events ORDER BY last_seen_at DESC LIMIT ?",
                            (max(1, min(int(limit), 200)),)).fetchall()
    finally:
        conn.close()
    return [{
        "event_key": r["event_key"], "reason": r["reason"], "request_id": r["request_id"],
        "session_id": r["session_id"], "inquiry_id": r["inquiry_id"],
        "first_seen_at": r["first_seen_at"], "last_seen_at": r["last_seen_at"],
        "first_seen_at_text": _fmt(r["first_seen_at"]),
        "seen_count": r["seen_count"], "trigger_count": r["trigger_count"],
        "suppress_count": r["suppress_count"],
        "last_suppress_code": r["last_suppress_code"],
        "last_suppress_at_text": _fmt(r["last_suppress_at"]),
        "last_suppress_pref": r["last_suppress_pref"],
        "config_version": r["config_version"],
    } for r in rows]


def stats(db_path=None, now=None):
    """重复提醒次数口径：seen（观测到的事件次数） vs trigger（真正打扰的次数）。"""
    now = _now() if now is None else float(now)
    conn = _connect(db_path)
    try:
        totals = conn.execute("""
            SELECT COALESCE(SUM(seen_count),0), COALESCE(SUM(trigger_count),0),
                   COALESCE(SUM(suppress_count),0), COUNT(1)
            FROM reminder_events
        """).fetchone()
        by_code = conn.execute("""
            SELECT last_suppress_code, COUNT(1), COALESCE(SUM(suppress_count),0)
            FROM reminder_events WHERE last_suppress_code IS NOT NULL
            GROUP BY last_suppress_code
        """).fetchall()
        feedback_rows = conn.execute("""
            SELECT feedback, COUNT(1) FROM reminder_feedbacks GROUP BY feedback
        """).fetchall()
    finally:
        conn.close()
    return {
        "event_total": int(totals[3]),
        "seen_count": int(totals[0]),
        "trigger_count": int(totals[1]),
        "suppress_count": int(totals[2]),
        "repeat_count": max(0, int(totals[0]) - int(totals[1])),
        "suppress_by_code": {r[0]: {"events": int(r[1]), "count": int(r[2])} for r in by_code},
        "feedback_by_type": {r[0]: int(r[1]) for r in feedback_rows},
    }


def list_feedbacks(limit=50, db_path=None):
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM reminder_feedbacks ORDER BY created_at DESC LIMIT ?",
                            (max(1, min(int(limit), 200)),)).fetchall()
    finally:
        conn.close()
    return [{
        "feedback_id": r["feedback_id"], "inquiry_id": r["inquiry_id"],
        "event_key": r["event_key"], "request_id": r["request_id"], "reason": r["reason"],
        "feedback": r["feedback"],
        "feedback_label": FEEDBACK_LABELS.get(r["feedback"], r["feedback"]),
        "decision": r["decision"], "scope": r["scope"],
        "scope_label": SCOPE_LABELS.get(r["scope"], r["scope"]) if r["scope"] else None,
        "channel": r["channel"], "pref_id": r["pref_id"],
        "before_version": r["before_version"], "after_version": r["after_version"],
        "created_at": r["created_at"], "created_at_text": _fmt(r["created_at"]),
        "note": r["note"],
    } for r in rows]


# ==================== 汇总视图（前端与第 11 周上下文共用） ====================

def overview(db_path=None, now=None):
    """当前偏好状态的一等公民视图：生效偏好、本轮剩余、配置版本、计数。"""
    now = _now() if now is None else float(now)
    sweep_session(db_path, now)
    conn = _connect(db_path)
    try:
        rows = active_preferences(db_path, now, conn)
    finally:
        conn.close()
    prefs = [_pref_to_dict(r, now) for r in rows]
    p = params(db_path, now)
    return {
        "session_id": SESSION_ID,
        "session_started_at": SESSION_STARTED_AT,
        "session_started_at_text": _fmt(SESSION_STARTED_AT),
        "session_ttl_s": p["session_ttl_s"],
        "session_remaining_s": int(round(session_remaining_s(db_path, now))),
        "session_muted": any(r["scope"] == "session" and r["action"] == "mute" for r in rows),
        "muted_reasons": sorted({r["scope_key"] for r in rows
                                 if r["scope"] == "event_reason" and r["action"] == "mute"}),
        "snoozed_events": [{"event_key": r["scope_key"], "remaining_s": _remaining(r["expires_at"], now)}
                           for r in rows if r["action"] == "snooze"],
        "preferences": prefs,
        "count": len(prefs),
        "config_version": current_version(db_path),
        "params": p,
        "params_text": params_snapshot_text(),
    }


def blocks_capture_prompt(db_path=None, now=None):
    """第 12 周 R10：用户是否已经显式压掉了"要不要现在拍一张"这类追问。

    消费方是 context_engine（只读）：命中就不生成取图追问，
    并把 scope/到期时间/配置版本如实写进回答，用户随时可以恢复默认。
    """
    view = overview(db_path, now)
    return {
        "blocked": bool(view["session_muted"] or view["muted_reasons"]),
        "session_muted": view["session_muted"],
        "muted_reasons": view["muted_reasons"],
        "muted_reason_labels": [REASON_LABELS.get(r, r) for r in view["muted_reasons"]],
        "config_version": view["config_version"],
        "session_remaining_s": view["session_remaining_s"],
    }


# ==================== 文字/语音里的偏好指令 ====================

# 只匹配"回到默认"这类配置动作，与第 9 周的询问答复词表互不重叠，避免抢答复。
RESTORE_WORDS = ("恢复默认", "默认提醒", "重新提醒", "取消静音", "别静音", "解除静音",
                 "都要提醒")
# "这次不用提醒 / 本轮别问了"：作用域词（命中询问答复时用于决定偏好作用域）
SESSION_WORDS = ("这次不用提醒", "这次不用了", "本轮不用", "本轮别", "这一轮不用",
                 "这一轮别", "今天别提醒", "先别提醒", "都别提醒")
REASON_WORDS = ("这类都别", "这种都别", "以后都别", "这种别问了", "这类别问")


def match_scope_text(text):
    """从一句话里判断用户想要的作用域；返回 'session' / 'event_reason' / None。"""
    if not text:
        return None
    lowered = text.strip()
    if any(w in lowered for w in REASON_WORDS):
        return "event_reason"
    if any(w in lowered for w in SESSION_WORDS):
        return "session"
    return None


def is_restore_text(text):
    lowered = (text or "").strip()
    return any(w in lowered for w in RESTORE_WORDS)


def try_config_from_text(text, channel="voice", db_path=None):
    """若这句话是"恢复默认提醒"这类配置指令，直接执行并返回结果；否则返回 None。

    与第 9 周一样只做规则匹配，不新增意图枚举，不动第 6 周的意图契约。
    """
    if not is_restore_text(text):
        return None
    result, status = restore_default(db_path)
    result["ok"] = status == 200
    result["matched_feedback"] = "restore_default"
    result["message"] = ("已恢复默认提醒：本轮之前的静音偏好全部清除，"
                         f"配置版本升到 v{result['version']}")
    return result


# ==================== HTTP 端点 ====================

@bp.route("/api/preferences", methods=["GET"])
def api_preferences():
    """查看当前生效偏好、本轮剩余与配置版本（当堂验证：能查看本轮偏好）。"""
    now = _now()
    view = overview(now=now)
    view["feedbacks"] = list_feedbacks(limit=20)
    view["versions"] = list_versions(limit=10)
    view["stats"] = stats(now=now)
    return jsonify(view)


@bp.route("/api/preferences", methods=["POST"])
def api_set_preference():
    """显式设置一条偏好。

    请求体：{"scope": "session|event_reason|event_key|request_id",
             "key": "blocked", "action": "mute|snooze",
             "reason": "blocked", "duration_s": 900, "channel": "ui"}
    """
    data = request.get_json(silent=True) or {}
    scope = data.get("scope", "")
    key = data.get("key") or data.get("scope_key") or ""
    if not key:
        return jsonify({"error": "缺少 key（作用域对应的键）"}), 400
    channel = data.get("channel", "ui")
    if channel not in CHANNELS:
        channel = "ui"
    payload, status = set_preference(
        scope, str(key)[:64], data.get("action", "mute"),
        reason=data.get("reason"), request_id=data.get("request_id"),
        event_key=data.get("event_key") if scope == "event_key" else None,
        inquiry_id=data.get("inquiry_id"), feedback=data.get("feedback", "ignore"),
        channel=channel, duration_s=data.get("duration_s"),
        note=data.get("note"))
    return jsonify(payload), status


@bp.route("/api/preferences/<pref_id>/revoke", methods=["POST"])
def api_revoke(pref_id):
    data = request.get_json(silent=True) or {}
    payload, status = revoke_preference(pref_id, data.get("reason", "revoke"))
    return jsonify(payload), status


@bp.route("/api/preferences/session/clear", methods=["POST"])
def api_clear_session():
    payload, status = clear_session()
    payload["message"] = f"已清除本轮 {payload['revoked']} 条提醒偏好"
    return jsonify(payload), status


@bp.route("/api/preferences/restore_default", methods=["POST"])
def api_restore_default():
    payload, status = restore_default()
    payload["message"] = (f"已恢复默认提醒：撤销 {payload['revoked']} 条偏好，"
                          f"配置版本 v{payload['from_version']} → v{payload['version']}")
    return jsonify(payload), status


@bp.route("/api/config/versions", methods=["GET"])
def api_versions():
    limit = request.args.get("limit", default=20, type=int)
    return jsonify({
        "versions": list_versions(limit=limit),
        "current": current_version(),
        "params": params(),
    })


@bp.route("/api/config/rollback", methods=["POST"])
def api_rollback():
    data = request.get_json(silent=True) or {}
    to_version = data.get("to_version")
    if to_version is None:
        return jsonify({"error": "缺少 to_version"}), 400
    payload, status = rollback(to_version, data.get("note", ""))
    if status == 200:
        payload["message"] = (f"已回退到 v{payload['rolled_back_to']}，"
                              f"生成新版本 v{payload['version']}（历史保留）")
    return jsonify(payload), status


@bp.route("/api/config/params", methods=["POST"])
def api_params():
    data = request.get_json(silent=True) or {}
    payload, status = update_params(data.get("params") or {}, data.get("note", ""))
    return jsonify(payload), status


@bp.route("/api/reminders/stats", methods=["GET"])
def api_stats():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({
        "stats": stats(),
        "events": list_events(limit=limit),
        "overview": overview(),
    })

# -*- coding: utf-8 -*-
"""第 12 周验收测试：让用户纠错影响后续提醒（显式偏好 + 配置版本回退）。

本周要回答的问题：用户说"这次不用提醒"，系统怎样记住当前选择，又能恢复默认？

覆盖（对应本周要求）：
  1. 有限反馈规则 F1–F5：确认/纠正/忽略/稍后/恢复默认，每条都有确定去向；
  2. 偏好作用域：本轮 / 该类原因 / 这条事件 / 这张图；"这次不用提醒"只压本轮，
     不静默升级成永久关闭（保存反馈 ≠ 在线学习成功）；
  3. 重复提醒次数前后对照：默认策略下同类事件每次都打扰 vs 记录显式偏好后
     同一批事件被 G6 压掉（配置变化记录 + 前后数字，本文件的核心取证）；
  4. 同一事件重放不重复触发：event_key 稳定，seen 计数增加、trigger 不增加；
  5. 可查看 / 可单条清除：清除后回到第 9 周默认策略，而不是"完全没有冷却"；
  6. 自动恢复默认：本轮结束（新一轮进程 / 本轮 TTL 到期）session 级偏好失效；
  7. 配置版本 append-only：参数变更、回退都生成新版本，历史不删除；
  8. 第 11 周 R10：显式偏好压掉"拍一张"追问并如实说明理由，恢复默认后追问回来；
  9. 只读边界：偏好与配置操作不写 imu_data / image_records / inference_results /
     human_feedbacks（纠错不污染第 8 周人工纠错表）。

运行（不依赖已启动的服务器，进程内 Flask test_client）：
    cd server
    venv\\Scripts\\python.exe test_week12.py

隔离：数据库/图片/交互日志重定向到临时目录；第 9 周冷却参数压到 0，
这样"默认策略会不会重复打扰"完全由本周偏好决定，前后对照才干净。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

# ---- 第 9 周参数：压到 0，隔离出"本周偏好"这一唯一变量（必须在 import app 之前）----
os.environ["INQUIRY_TIMEOUT_S"] = "120"    # 测试期间不因超时过期
os.environ["INQUIRY_GLOBAL_GAP_S"] = "0"   # 不等全局间隔
os.environ["DECLINE_COOLDOWN_S"] = "0"     # 默认策略：拒绝后同类仍可提醒（第 9 周原值 600）
os.environ["EXPIRE_COOLDOWN_S"] = "0"
os.environ["DISMISS_COOLDOWN_S"] = "0"
os.environ["MUTE_COOLDOWN_S"] = "0"
os.environ["MAX_RETAKE"] = "2"
# ---- 第 12 周参数：本轮 15 分钟（进程一轮），稍后 120s，纠正静音 300s ----
os.environ["PREF_SESSION_TTL_S"] = "900"
os.environ["PREF_SNOOZE_S"] = "120"
os.environ["PREF_REASON_MUTE_S"] = "86400"
os.environ["PREF_CORRECT_MUTE_S"] = "300"

HERE = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = tempfile.mkdtemp(prefix="week12-")
os.environ["INTERACTION_LOG"] = os.path.join(TMP_DIR, "interaction.log")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.path.insert(0, HERE)

import app as server_app                  # noqa: E402
import context_engine as ctx              # noqa: E402
import inquiry as inquiry_mod             # noqa: E402
import reminder_pref as pref              # noqa: E402

FIXED = time.time()
TTL = {"imu_ms": 5000, "camera_ms": 600000, "link_ms": 10000}
EVIDENCE_PATH = os.environ.get("WEEK12_EVIDENCE", "week12_evidence.json").strip()
EVIDENCE = []
BEFORE_AFTER = {}
PASS_COUNT = 0
FAIL_COUNT = 0


def report(name, ok, detail="", attribution="interaction", **extra):
    """记录用例并写入原始证据（验收时可直接出示）。"""
    global PASS_COUNT, FAIL_COUNT
    if ok:
        PASS_COUNT += 1
        print(f"[PASS] {name}" + (f"  -- {detail}" if detail else ""))
    else:
        FAIL_COUNT += 1
        print(f"[FAIL] {name}" + (f"  -- {detail}" if detail else ""))
    EVIDENCE.append({
        "case": name,
        "pass": bool(ok),
        "detail": detail,
        "attribution": attribution,
        "recorded_at": time.time(),
        **extra,
    })


# ==================== 测试环境 ====================

server_app.DB_FILE = os.path.join(TMP_DIR, "test.db")
server_app.IMAGE_DIR = os.path.join(TMP_DIR, "images")
os.makedirs(server_app.IMAGE_DIR, exist_ok=True)
server_app.init_db()
pref.configure(server_app.DB_FILE)
pref.init_db()
inquiry_mod.configure(server_app.DB_FILE, server_app.create_image_task)
inquiry_mod.init_db()
client = server_app.app.test_client()


def db():
    conn = sqlite3.connect(server_app.DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def table_count(table):
    conn = db()
    n = conn.execute(f"SELECT COUNT(1) FROM {table}").fetchone()[0]
    conn.close()
    return n


def counts_of_original_sources():
    """本周不允许写的四张表（原始观测 + 第 8 周人工纠错）。"""
    return {t: table_count(t) for t in ("imu_data", "image_records",
                                         "inference_results", "human_feedbacks")}


_SEQ = {"n": 0}


def insert_event_image(request_id, source="device"):
    """落一条"画面不可用"的图像记录（询问模块按 source 判定真机/回放）。"""
    conn = db()
    conn.execute("""
        INSERT OR REPLACE INTO image_records (
            request_id, device_id, status, source, file_path, size_bytes,
            device_ts_us, captured_at, error_msg, created_at
        ) VALUES (?, 'esp32-s3-eye', 'blocked', ?, NULL, NULL, 1000, NULL,
                  'lens covered', ?)
    """, (request_id, source, time.time()))
    conn.commit()
    conn.close()


def settle_asking():
    """结掉所有未决询问（第 9 周口径的 decline，不带 feedback/scope → 不写偏好）。

    询问模块 G3 是单飞行：同时只允许一条 asking。测试里每轮新建事件前
    先清空未决询问，才能保证"这次到底弹没弹提醒"只由本周偏好决定。
    """
    for it in inquiry_mod.list_recent(limit=50, status="asking"):
        inquiry_mod.answer(it["inquiry_id"], "decline", channel="ui")


def fire(reason="blocked", source="device"):
    """产生一次画面不可用事件，返回 (request_id, 询问模块的处置结果)。"""
    settle_asking()
    _SEQ["n"] += 1
    request_id = f"w12-{reason}-{_SEQ['n']}"
    insert_event_image(request_id, source)
    return request_id, inquiry_mod.on_unusable(request_id, reason, trigger="server")


def answer(inquiry_id, decision, **fields):
    """走 HTTP 答复入口（和前端/语音走的是同一条路）。"""
    body = {"decision": decision, "channel": fields.pop("channel", "ui")}
    body.update(fields)
    resp = client.post(f"/api/inquiry/{inquiry_id}/answer", json=body)
    return resp.status_code, resp.get_json()


def active_prefs():
    return pref.overview()["preferences"]


def stats_delta(before, after):
    return {k: after[k] - before[k] for k in ("seen_count", "trigger_count",
                                              "suppress_count", "event_total")}


# ==================== T1 建表、默认配置版本与本轮口径 ====================

view = pref.overview()
versions = pref.list_versions()
tables = {r[0] for r in db().execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
report("T1 四张新表 + 默认配置 v1 + 本轮（进程一轮）口径",
       {"reminder_preferences", "reminder_events", "reminder_feedbacks",
        "config_versions"} <= tables
       and versions[-1]["version"] == 1
       and versions[-1]["source"] == "default"
       and view["params"] == pref.default_params()
       and view["count"] == 0
       and view["session_id"] == pref.SESSION_ID
       and view["session_remaining_s"] > 0,
       f"v1={versions[-1]['params']} session={view['session_id']} "
       f"剩余={view['session_remaining_s']}s",
       attribution="server", overview=view, version_v1=versions[-1])

report("T2 事件签名稳定（同一张图同一原因 = 同一个事件）",
       pref.event_key_for("img-a", "blocked") == pref.event_key_for("img-a", "blocked")
       and pref.event_key_for("img-a", "blocked") != pref.event_key_for("img-a", "dark")
       and pref.event_key_for("img-b", "blocked") != pref.event_key_for("img-a", "blocked"),
       f"key={pref.event_key_for('img-a', 'blocked')}", attribution="server")

# ==================== T3 修改前基线：默认策略每次都打扰 ====================

before_stats = pref.stats()
baseline_ids = []
for _ in range(4):
    rid, res = fire("blocked")
    baseline_ids.append(rid)
    code, body = answer(res["inquiry_id"], "decline")
    assert code == 200, body
after_stats = pref.stats()
BEFORE_AFTER["before"] = {
    "policy": "默认策略（第 9 周记录级冷却，无显式偏好）",
    "events": 4,
    **stats_delta(before_stats, after_stats),
    "requests": baseline_ids,
}
report("T3 修改前基线：4 次同类事件 → 4 次都真的打扰了用户",
       stats_delta(before_stats, after_stats) == {
           "seen_count": 4, "trigger_count": 4, "suppress_count": 0, "event_total": 4}
       and active_prefs() == [],
       f"trigger=+4 suppress=+0（默认策略会重复提醒）",
       attribution="interaction", before_after=BEFORE_AFTER["before"])

# ==================== T4 同一事件重放不重复触发 ====================

replay_rid = baseline_ids[0]
replayed = inquiry_mod.on_unusable(replay_rid, "blocked")
events = {e["event_key"]: e for e in pref.list_events(limit=200)}
replay_key = pref.event_key_for(replay_rid, "blocked")
row = events[replay_key]
report("T4 同一事件重放：只增加观测计数，不重复弹提醒",
       isinstance(replayed, dict) and replayed.get("status") != "asking"
       and row["seen_count"] == 2 and row["trigger_count"] == 1
       and row["last_suppress_code"] == "already_asked",
       f"seen={row['seen_count']} trigger={row['trigger_count']} "
       f"suppress_code={row['last_suppress_code']}",
       attribution="server", event=row)

# ==================== T5 F3 忽略："这次不用提醒" → 本轮静音 ====================

rid, res = fire("blocked")
code, body = answer(res["inquiry_id"], "decline", feedback="ignore", scope="session")
prefs = active_prefs()
report("T5 F3 忽略（这次不用提醒）→ 只静音本轮，到期时间=本轮结束",
       code == 200 and body.get("preference", {}).get("scope") == "session"
       and abs(body["preference"]["expires_at"] - pref.session_deadline()) < 1.5
       and len(prefs) == 1 and prefs[0]["scope"] == "session"
       and prefs[0]["action"] == "mute" and prefs[0]["scope_key"] == pref.SESSION_ID
       and pref.overview()["session_muted"] is True
       and body["preference"]["version"] > body["preference"]["from_version"],
       f"scope=session expires={prefs[0]['expires_at_text'] if prefs else None} "
       f"v{body['preference']['from_version']}→v{body['preference']['version']}",
       attribution="interaction", inquiry_id=res["inquiry_id"],
       preference=body.get("preference"), preferences=prefs)

# ==================== T6 修改后对照：同一批事件被显式偏好压掉 ====================

before_stats = pref.stats()
suppressed = []
for _ in range(4):
    _, res = fire("blocked")
    suppressed.append(res.get("suppressed"))
after_stats = pref.stats()
delta = stats_delta(before_stats, after_stats)
BEFORE_AFTER["after"] = {
    "policy": "记录显式偏好后（F3 本轮静音，其余参数与基线完全相同）",
    "events": 4,
    **delta,
    "suppress_codes": suppressed,
}
BEFORE_AFTER["comparison"] = {
    "same_number_of_events": 4,
    "disturbed_before": BEFORE_AFTER["before"]["trigger_count"],
    "disturbed_after": delta["trigger_count"],
    "suppressed_after": delta["suppress_count"],
    "conclusion": "同样的 4 次同类事件：修改前打扰 4 次，修改后打扰 0 次、压掉 4 次",
}
report("T6 修改前后对照：同类事件打扰次数 4 → 0（全部被 G6 压掉）",
       delta == {"seen_count": 4, "trigger_count": 0, "suppress_count": 4,
                 "event_total": 4}
       and set(suppressed) == {"session_muted"}
       and after_stats["suppress_by_code"].get("session_muted", {}).get("count") >= 4,
       f"before trigger=+{BEFORE_AFTER['before']['trigger_count']} "
       f"after trigger=+{delta['trigger_count']} suppress=+{delta['suppress_count']}",
       attribution="interaction", before_after=BEFORE_AFTER)

# ==================== T7 可查看 + 可单条清除 → 回到默认策略 ====================

prefs = active_prefs()
code = client.post(f"/api/preferences/{prefs[0]['pref_id']}/revoke",
                   json={"reason": "week12 test"}).status_code
listed = client.get("/api/preferences").get_json()
rid, res = fire("blocked")
back_to_default = "inquiry_id" in res
report("T7 查看并清除本轮偏好：清除后回到默认策略（仍会提醒，但不是永久关闭）",
       code == 200 and listed["count"] == 0 and listed["session_muted"] is False
       and back_to_default and res.get("session_id") == pref.SESSION_ID,
       f"revoke HTTP={code} 生效偏好={listed['count']} 条 → 下一条事件又触发询问",
       attribution="interaction", preferences_after_clear=listed["preferences"],
       inquiry=res.get("inquiry_id"))

# ==================== T8 词表作用域：文字/语音说"这类都别问" → 该类原因静音 ====================

matched = inquiry_mod.match_feedback_text("这类都别问了")
answered = inquiry_mod.try_answer_from_text("这类都别问了", channel="voice")
_, res_after_reason_mute = fire("blocked")
report("T8 说“这类都别问” → 该类原因静音，同类原因下一条事件被压掉",
       matched == {"decision": "decline", "feedback": "ignore", "scope": "event_reason"}
       and answered.get("ok") is True
       and answered["preference"]["scope"] == "event_reason"
       and res_after_reason_mute.get("suppressed") == "reason_muted",
       f"scope=event_reason suppressed={res_after_reason_mute.get('suppressed')}",
       attribution="interaction", text="这类都别问了",
       preference=answered.get("preference"))

report("T9 作用域不静默升级：'这次不用提醒'只写本轮，不写永久",
       pref.match_scope_text("这次不用提醒了") == "session"
       and pref.match_scope_text("以后这种都别问我") == "event_reason"
       and pref.match_scope_text("重拍一张") is None
       and all(p["action"] == "mute" for p in active_prefs())
       and all(p["expires_at"] for p in active_prefs()),
       "词表 → 作用域映射确定；所有静音都有到期时间", attribution="rules")

# ==================== T10 F5 恢复默认（HTTP + 一句话） ====================

resp = client.post("/api/preferences/restore_default", json={})
code, body = resp.status_code, resp.get_json()
after_restore = pref.overview()
_, res_new = fire("blocked")
said = client.post("/api/nl_query", json={"text": "恢复默认提醒"}).get_json()
report("T10 F5 恢复默认：撤销全部偏好 + 参数回到默认快照，提醒立刻恢复",
       code == 200 and after_restore["count"] == 0
       and after_restore["params"] == pref.default_params()
       and "inquiry_id" in res_new
       and said.get("intent") == "reminder_preference" and said.get("success") is True,
       f"撤销 {body['revoked']} 条偏好，配置 v{body['from_version']}→v{body['version']}；"
       f"再说“恢复默认提醒”仍可用",
       attribution="interaction", restore=body, nl_query=said)

# ==================== T11 F2 纠正："其实可用" → 该类原因静音，不写第 8 周纠错表 ====================

before_fb = table_count("human_feedbacks")
rid, res = fire("blur")
code, body = answer(res["inquiry_id"], "usable", feedback="correct")
prefs = active_prefs()
_, res_next = fire("blur")
report("T11 F2 纠正（其实可用）→ 该类原因静音；纠错不写人工纠错表",
       code == 200 and body.get("misjudged") is True
       and prefs and prefs[0]["scope"] == "event_reason"
       and prefs[0]["scope_key"] == "blur"
       and prefs[0]["remaining_s"] <= 300 and prefs[0]["feedback"] == "correct"
       and res_next.get("suppressed") == "reason_muted"
       and table_count("human_feedbacks") == before_fb == 0,
       f"静音 blur {prefs[0]['remaining_s']}s；human_feedbacks 仍为 "
       f"{table_count('human_feedbacks')} 条（保存反馈≠在线学习）",
       attribution="interaction", preference=prefs[0] if prefs else None)

# ==================== T12 F4 稍后：压住这条事件，到期后同类事件仍可提醒 ====================

pref.restore_default()
rid, res = fire("dark")           # 默认策略下：这条事件会弹提醒
key = pref.event_key_for(rid, "dark")
pref.set_preference("event_key", key, "snooze", reason="dark",
                    request_id=rid, duration_s=60, feedback="snooze")
hit = pref.check_gate(key, "dark", rid)
conn = db()
conn.execute("UPDATE reminder_preferences SET expires_at=? "
             "WHERE scope='event_key' AND scope_key=?", (time.time() - 1, key))
conn.commit()
conn.close()
hit_after = pref.check_gate(key, "dark", rid)
_, res_expired = fire("dark")     # 同类新事件不受这条"稍后"影响
report("T12 F4 稍后：到期前压住该事件，到期后提醒恢复",
       "inquiry_id" in res and hit is not None and hit["code"] == "event_snoozed"
       and hit["remaining_s"] > 0 and hit_after is None
       and "inquiry_id" in res_expired,
       f"稍后命中 {hit['remaining_s']}s → 到期后 check_gate=None",
       attribution="interaction", gate=hit, gate_after=hit_after)

# ==================== T13 F1 确认重拍 → 该事件的"稍后"自动失效 ====================

rid, res = fire("capture_failed")
key = pref.event_key_for(rid, "capture_failed")
before_v = pref.current_version()
pref.set_preference("event_key", key, "snooze", reason="capture_failed",
                    request_id=rid, duration_s=60, feedback="snooze")
code, body = answer(res["inquiry_id"], "retake")
report("T13 F1 确认重拍：自动撤销该事件的“稍后再说”（没有则不写配置历史）",
       code == 200 and body.get("snooze_revoked") == 1
       and all(p["scope_key"] != key for p in active_prefs())
       and pref.current_version() > before_v
       and body.get("retake_request_id"),
       f"retake={body.get('retake_request_id')} 撤销 {body.get('snooze_revoked')} 条稍后",
       attribution="interaction", inquiry_id=res["inquiry_id"])

# ==================== T14 本轮结束自动恢复默认（新一轮进程 / TTL 到期）====================

pref.restore_default()
saved_id, saved_start = pref.SESSION_ID, pref.SESSION_STARTED_AT
pref.set_preference("session", pref.SESSION_ID, "mute",
                    until=pref.session_deadline(), feedback="ignore")
muted_before = pref.overview()["session_muted"]
pref.reset_session()                     # 等价于服务端进程重启一次
new_round = pref.overview()
pref.SESSION_ID = saved_id               # 回到本轮，只改时间戳验证 TTL
pref.SESSION_STARTED_AT = time.time() - (pref.params()["session_ttl_s"] + 5)
expired_n = pref.sweep_session()
swept = pref.overview()
pref.SESSION_STARTED_AT = saved_start
report("T14 本轮结束自动恢复默认：新一轮进程不继承上轮静音，TTL 到期自动失效",
       muted_before is True and new_round["session_muted"] is False
       and new_round["session_id"] != saved_id and new_round["count"] == 0
       and expired_n >= 1 and swept["session_muted"] is False
       and "session_expired" in pref.stats()["feedback_by_type"],
       f"新一轮={new_round['session_id']} → 失效；TTL 到期清除 {expired_n} 条",
       attribution="rules", new_round=new_round, expired=expired_n)

# ==================== T15 配置变化记录：参数变更 + append-only 回退 ====================

history = pref.list_versions(limit=100)
history_ids = {v["version"] for v in history}
resp = client.post("/api/config/params",
                   json={"params": {"snooze_s": 60}, "note": "week12 缩短稍后"})
p_now = pref.params()
key = pref.event_key_for("img-param", "blocked")
pref.set_preference("event_key", key, "snooze", reason="blocked",
                    request_id="img-param", duration_s=None, feedback="snooze")
made = [p for p in active_prefs() if p["scope_key"] == key][0]
pref.restore_default()
_, res_back = fire("blocked")
rollback = client.post("/api/config/rollback",
                       json={"to_version": 1, "note": "回到本周起点"}).get_json()
versions_now = pref.list_versions(limit=100)
now_ids = {v["version"] for v in versions_now}
report("T15 配置版本：参数变更即新版本、生效值可查、回退是 append-only 的新版本",
       resp.status_code == 200 and made["remaining_s"] <= 60 and p_now["snooze_s"] == 60
       and rollback["rolled_back_to"] == 1 and rollback["version"] > rollback["from_version"]
       and max(now_ids) > max(history_ids) and len(versions_now) > len(history)
       and now_ids >= history_ids
       and [v for v in versions_now if v["version"] == rollback["version"]][0]["source"]
       == "rollback"
       and "inquiry_id" in res_back,
       f"参数 snooze_s→60 → 偏好 {made['remaining_s']}s；回退 v{rollback['from_version']}→"
       f"v{rollback['version']}（历史 {len(history)}→{len(versions_now)} 条，只增不减）",
       attribution="server", rollback=rollback, params=p_now)

report("T16 配置历史可取证：每条都有来源/参数快照/说明",
       all(v["params"] and v["created_at_text"] and v["source_label"] for v in versions_now)
       and {"params", "rollback", "restore_default", "ignore", "correct", "snooze",
            "confirm", "revoke"} <= {v["source"] for v in versions_now},
       f"来源集合={sorted({v['source'] for v in versions_now})}",
       attribution="server", versions=versions_now[:12])

# ==================== T17 API 参数校验 ====================

bad_scope = client.post("/api/preferences", json={"scope": "forever", "key": "x"})
no_key = client.post("/api/preferences", json={"scope": "session"})
bad_param = client.post("/api/config/params", json={"params": {"unknown_k": 1}})
miss_version = client.post("/api/config/rollback", json={"to_version": 999999})
no_target = client.post("/api/config/rollback", json={})
report("T17 偏好/配置接口参数校验",
       bad_scope.status_code == 400 and no_key.status_code == 400
       and bad_param.status_code == 400 and miss_version.status_code == 404
       and no_target.status_code == 400,
       f"scope={bad_scope.status_code} key={no_key.status_code} "
       f"param={bad_param.status_code} rollback={miss_version.status_code}/"
       f"{no_target.status_code}", attribution="server")

# ==================== T18 只读边界：偏好与配置操作不碰原始观测 ====================

before_counts = counts_of_original_sources()
pref.set_preference("session", pref.SESSION_ID, "mute",
                    until=pref.session_deadline(), feedback="ignore")
client.post("/api/preferences", json={"scope": "event_reason", "key": "dark",
                                      "action": "mute", "channel": "ui"})
client.get("/api/preferences")
client.get("/api/reminders/stats")
client.post("/api/config/params", json={"params": {"reason_mute_s": 3600}})
client.post("/api/preferences/session/clear", json={})
client.post("/api/preferences/restore_default", json={})
after_counts = counts_of_original_sources()
report("T18 偏好/配置读写不改原始观测与人工纠错表",
       before_counts == after_counts,
       f"counts={after_counts}", attribution="server")

# ==================== T19 第 11 周 R10：显式偏好压掉"拍一张"追问 ====================

def clear_original_rows():
    conn = db()
    for table in ("imu_data", "device_status", "image_records",
                  "inference_results", "context_traces"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()


def insert_imu(age_s):
    conn = db()
    conn.execute("""
        INSERT INTO imu_data (timestamp, ax, ay, az, observed_at, received_at,
            observed_uptime_ms, send_uptime_ms, boot_id, age_basis, provenance)
        VALUES (?, 0.1, 0.1, 9.8, ?, ?, 1000, 1050, 'a1b2c3d4', 'device_transit', 'device')
    """, (1_000_000.0 + age_s, FIXED - age_s, FIXED - age_s))
    conn.commit()
    conn.close()


def insert_blocked_image(request_id, age_s):
    conn = db()
    conn.execute("""
        INSERT OR REPLACE INTO image_records (request_id, device_id, status, source,
            file_path, size_bytes, device_ts_us, captured_at, error_msg, created_at)
        VALUES (?, 'esp32-s3-eye', 'blocked', 'device', NULL, NULL, 1000, NULL,
                'lens covered', ?)
    """, (request_id, FIXED - age_s))
    conn.commit()
    conn.close()


def ask(question):
    return ctx.build_context(server_app.DB_FILE, question, now=FIXED, ttl=TTL)


clear_original_rows()
insert_imu(0.5)
insert_blocked_image("r10-blocked", 2)

pref.restore_default()
default_ctx = ask("周围环境安全吗？")
pref.set_preference("session", pref.SESSION_ID, "mute",
                    until=pref.session_deadline(), feedback="ignore")
muted_ctx = ask("周围环境安全吗？")
muted_fact = [f for f in muted_ctx["facts"] if f["kind"] == "preference"]
pref.restore_default()
restored_ctx = ask("周围环境安全吗？")
report("T19 R10 偏好一致：本轮静音压掉“拍一张”追问并如实说明，恢复默认后追问回来",
       default_ctx["followup"] is not None
       and default_ctx["followup"]["action"] == "capture"
       and muted_ctx["followup"] is None
       and muted_ctx["preference"]["blocked"] is True
       and muted_ctx["layers"]["interaction"]["followup"] is None
       and muted_fact and muted_fact[0]["details"]["scope"] == "session"
       and "不再提示重拍" in muted_ctx["answer"]
       and "恢复默认提醒" in muted_ctx["answer"]
       and restored_ctx["followup"] is not None
       and restored_ctx["followup"]["action"] == "capture",
       f"默认={default_ctx['followup']['action']} → 静音={muted_ctx['followup']} → "
       f"恢复={restored_ctx['followup']['action']}（配置 v{muted_fact[0]['details']['config_version'] if muted_fact else '?'}）",
       attribution="interaction",
       default_followup=default_ctx["followup"], muted_followup=muted_ctx["followup"],
       preference=muted_ctx["preference"], answer=muted_ctx["answer"])

clear_original_rows()
insert_imu(30)
baseline_motion = ask("我在动吗？")
pref.set_preference("session", pref.SESSION_ID, "mute",
                    until=pref.session_deadline(), feedback="ignore")
muted_motion = ask("我在动吗？")
muted_location = ask("设备现在在哪里？")
pref.restore_default()
report("T20 R10 只压取图追问，采样/来源/澄清类追问不受偏好影响",
       baseline_motion["followup"]["action"] == "collect"
       and muted_motion["followup"]["action"] == "collect"
       and muted_motion["preference"]["blocked"] is False
       and muted_location["followup"] is not None
       and muted_location["followup"]["action"] != "capture",
       f"本轮静音生效时：motion 追问仍为 {muted_motion['followup']['action']}，"
       f"location 追问仍为 {muted_location['followup']['action']}",
       attribution="rules", muted_preference=muted_motion["preference"],
       motion_followup=muted_motion["followup"],
       location_followup=muted_location["followup"])

# ==================== 汇总与证据 ====================

summary = {
    "week": 12,
    "title": "让用户纠错影响后续提醒：显式偏好、作用域与配置版本回退",
    "pass": PASS_COUNT,
    "fail": FAIL_COUNT,
    "total": PASS_COUNT + FAIL_COUNT,
    "feedback_rules": {k: v for k, v in pref.FEEDBACK_LABELS.items()},
    "scopes": pref.SCOPE_LABELS,
    "repeat_reminder_comparison": BEFORE_AFTER,
    "config": {
        "session": {"session_id": pref.SESSION_ID, "started_at": pref.SESSION_STARTED_AT,
                    "definition": "服务端进程一次启动 = 一轮；重启或 TTL 到期即结束"},
        "params": pref.default_params(),
        "config_version": pref.current_version(),
        "versions_total": len(pref.list_versions(limit=100)),
        "rule_version": ctx.RULE_VERSION,
    },
    "stats": pref.stats(),
    "note": "第 9 周冷却参数压到 0，因此“打扰次数”的变化只能来自本周的显式偏好；"
            "测试用临时库，不写生产 imu_data.db。",
}

print("-" * 72)
print(f"通过 {PASS_COUNT} / 失败 {FAIL_COUNT} / 共 {PASS_COUNT + FAIL_COUNT}")
print(f"修改前打扰 {BEFORE_AFTER['before']['trigger_count']} 次 → "
      f"修改后打扰 {BEFORE_AFTER['after']['trigger_count']} 次（同类事件各 4 次）")
print("-" * 72)

if EVIDENCE_PATH:
    with open(EVIDENCE_PATH, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "before_after": BEFORE_AFTER, "cases": EVIDENCE},
                  f, ensure_ascii=False, indent=2)
    print(f"原始记录已写入：{EVIDENCE_PATH}")

shutil.rmtree(TMP_DIR, ignore_errors=True)
sys.exit(1 if FAIL_COUNT else 0)

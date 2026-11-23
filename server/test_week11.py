# -*- coding: utf-8 -*-
"""第 11 周验收测试：根据多源证据组织交互上下文。

覆盖：
  1. 规则版本、阈值快照与固定规则集；
  2. 空库：无法确认 + 引导查看来源，不编造移动/位置；
  3. 运动派生：静止 / 有活动 / 样本不足 / 窗口外陈旧；
  4. 时间窗：快照陈旧、本次遮挡 + 上次快照、运动窗口可代表性；
  5. 模型输出：标为推断、带版本/分数、拒识不写成事实；
  6. 位置：无 GNSS 时拒绝给坐标；
  7. 安全/危险：遮挡不解释为危险，hazard 恒为空；
  8. 冲突：链路在线 + IMU 陈旧、遮挡 + 旧图、旧图 + 模型标签；
  9. 追问：至多一条、优先级正确；
 10. trace：规则版本/事实/冲突落库，only_conflict 可取证；
 11. 只读：调用不修改 imu_data / image_records / inference_results；
 12. 回放来源：provenance=replay 不混入真机证据；
 13. 助手白名单：不劫持第 4 周查询与第 7 周取图命令。

运行（不依赖已启动的服务器，进程内 Flask test_client）：
    cd server
    venv\\Scripts\\python.exe test_week11.py

隔离：数据库/图片/交互日志都重定向到临时目录，不影响正在运行的服务。
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = tempfile.mkdtemp(prefix="week11-")
os.environ["INTERACTION_LOG"] = os.path.join(TMP_DIR, "interaction.log")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.path.insert(0, HERE)

import app as server_app                  # noqa: E402
import context_engine as ctx              # noqa: E402

FIXED = time.time()
TTL = {"imu_ms": 5000, "camera_ms": 600000, "link_ms": 10000}
EVIDENCE_PATH = os.environ.get("WEEK11_EVIDENCE", "week11_evidence.json").strip()
EVIDENCE = []
PASS_COUNT = 0
FAIL_COUNT = 0


def report(name, ok, detail="", attribution="server", **extra):
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
client = server_app.app.test_client()


def db():
    conn = sqlite3.connect(server_app.DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def clear_all():
    conn = db()
    for table in ("imu_data", "device_status", "image_records",
                  "inference_results", "human_feedbacks", "context_traces"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()


def insert_imu(age_s, ax=0.1, ay=0.1, az=9.8, provenance="device", observed=True):
    """按“距 FIXED 的秒数”插入一条观测。"""
    t = FIXED - float(age_s)
    conn = db()
    conn.execute("""
        INSERT INTO imu_data (
            timestamp, ax, ay, az, observed_at, received_at,
            observed_uptime_ms, send_uptime_ms, boot_id, age_basis, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (1_000_000.0 + float(age_s), ax, ay, az,
          t if observed else None, t, 1000, 1050, "a1b2c3d4",
          "device_transit" if observed else "unknown", provenance))
    conn.commit()
    conn.close()


def set_device(age_s=0.2, attempt="ok", error=None, fails=0):
    last = FIXED - float(age_s)
    conn = db()
    conn.execute("""
        INSERT INTO device_status (
            device_id, last_seen_at, boot_id, uptime_ms,
            imu_attempt, imu_error, imu_fail_count, updated_at
        ) VALUES ('esp32-s3-eye', ?, 'a1b2c3d4', 1000, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            imu_attempt=excluded.imu_attempt,
            imu_error=excluded.imu_error,
            imu_fail_count=excluded.imu_fail_count,
            updated_at=excluded.updated_at
    """, (last, attempt, error, fails, last))
    conn.commit()
    conn.close()


def insert_image(request_id, status="captured", age_s=10.0, source="device", error=None):
    """插入一条取图记录；captured 时创建真实存在的 JPEG 占位文件。"""
    created = FIXED - float(age_s)
    captured = created if status == "captured" else None
    file_path = None
    if status == "captured":
        file_path = os.path.join(server_app.IMAGE_DIR, f"{request_id}.jpg")
        with open(file_path, "wb") as f:
            f.write(b"\xff\xd8" + request_id.encode("ascii") + b"\xff\xd9")

    conn = db()
    conn.execute("""
        INSERT OR REPLACE INTO image_records (
            request_id, device_id, status, source, file_path, size_bytes,
            device_ts_us, captured_at, error_msg, created_at
        ) VALUES (?, 'esp32-s3-eye', ?, ?, ?, ?, 1000, ?, ?, ?)
    """, (request_id, status, source, file_path,
          100 if file_path else None, captured, error, created))
    conn.commit()
    conn.close()
    return file_path


def insert_inference(request_id, label="computer keyboard", score=0.41,
                     rejected=0, reason=None, version="mobilenetv2-12-onnx",
                     inferred_age_s=10.0):
    conn = db()
    conn.execute("""
        INSERT OR REPLACE INTO inference_results (
            request_id, label, score, coarse, model_name, model_version,
            usability, usability_metrics, status, is_rejected, rejected_reason,
            uncertain_note, topk, inferred_at
        ) VALUES (?, ?, ?, 'indoor', 'teacher_onnx', ?, 'clear', '{}', ?, ?, ?, ?, ?, ?)
    """, (request_id, label, score, version,
          "ok" if not rejected else "rejected", rejected, reason,
          "模型分数低" if rejected else "", json.dumps([{"label": label, "score": score}]),
          FIXED - float(inferred_age_s)))
    conn.commit()
    conn.close()


def ask(question):
    """进程内调用上下文引擎，固定 now，不写原始观测。"""
    return ctx.build_context(server_app.DB_FILE, question, now=FIXED, ttl=TTL)


def facts_of(result, kind):
    return [fact for fact in result["facts"] if fact["kind"] == kind]


def has_coordinate_keys(obj):
    """递归检查是否出现 lat/lon/position 等定位字段。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in ("lat", "lon", "lng", "latitude",
                                    "longitude", "position", "coordinates"):
                return True
            if has_coordinate_keys(value):
                return True
    elif isinstance(obj, list):
        return any(has_coordinate_keys(item) for item in obj)
    return False


def motion_rows(count, ax_seq=None, ay_seq=None):
    """插入 count 条覆盖最近 4 秒的样本（最新一条 0.5s 前）。"""
    for i in range(count):
        age = 0.5 + (count - 1 - i) * 0.5
        ax = ax_seq[i] if ax_seq else 0.1
        ay = ay_seq[i] if ay_seq else 0.1
        insert_imu(age, ax=ax, ay=ay, az=9.8)


# ==================== T1 规则与契约 ====================

clear_all()
res = ask("你现在知道什么？")
report("T1 规则版本、9 条规则与阈值快照",
       res["rule_version"] == ctx.RULE_VERSION
       and [rule["id"] for rule in res["rules"]] == [f"R{i}" for i in range(1, 10)]
       and res["thresholds"]["motion_window_s"] == 5.0
       and res["thresholds"]["scene_recheck_s"] == 120,
       f"version={res['rule_version']} rules={len(res['rules'])}", attribution="rules")

# ==================== T2 空库 ====================

empty = ask("你现在知道什么？")
report("T2 空库：无法确认 + 引导查看来源 + 无坐标",
       empty["decision"]["id"] == "D5"
       and empty["followup"] is not None
       and empty["followup"]["action"] == "sources"
       and "没有收到任何来源数据" in empty["answer"]
       and empty["location"]["available"] is False
       and not has_coordinate_keys(empty),
       f"decision={empty['decision']['id']} followup={empty['followup']['action']}",
       attribution="server")

# ==================== T3 静止 ====================

clear_all()
motion_rows(8)
set_device(0.2)
still = ask("我在动吗？")
still_derived = facts_of(still, "derived")
report("T3 静止样本 → 静止，证据充分度 high",
       still["motion"]["state"] == "still"
       and still["motion"]["sample_count"] == 8
       and still["motion"]["evidence"] == "high"
       and still_derived and still_derived[0]["can_represent_current"] is True
       and "静止" in still["answer"],
       f"state={still['motion']['state']} n={still['motion']['sample_count']} "
       f"age={still['motion']['latest_age_ms']}", attribution="device")

# ==================== T3b 恒定偏差不判移动 ====================

clear_all()
motion_rows(8, ax_seq=[10.3] * 8, ay_seq=[-14.3] * 8)
set_device(0.2)
offset = ask("我在动吗？")
report("T3b 恒定的安装/标定偏差不算移动",
       offset["motion"]["state"] == "still"
       and "标定" in offset["answer"]
       and offset["motion"]["std_max"] <= ctx.MOTION_STD_STILL,
       f"state={offset['motion']['state']} std={offset['motion']['std_max']} "
       f"norm_delta={offset['motion']['norm_delta']}", attribution="device")

# ==================== T4 有活动 ====================
clear_all()
motion_rows(8, ax_seq=[2.0, -2.0, 2.0, -2.0, 2.0, -2.0, 2.0, -2.0],
            ay_seq=[1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0])
set_device(0.2)
moving = ask("我现在在移动吗？")
report("T4 大幅波动 → 有活动",
       moving["motion"]["state"] == "moving"
       and moving["motion"]["std_max"] >= ctx.MOTION_STD_MOVING
       and "有活动" in moving["answer"],
       f"state={moving['motion']['state']} std={moving['motion']['std_max']}",
       attribution="device")

# ==================== T5 样本不足 ====================

clear_all()
motion_rows(3)
set_device(0.2)
few = ask("我在动吗？")
report("T5 样本不足 → 无法判断 + 追问采样",
       few["motion"]["state"] == "unknown"
       and few["motion"]["reason"] == "insufficient_samples"
       and any(item["text"] == "当前是否移动" for item in few["pending"])
       and few["followup"] is not None
       and few["followup"]["action"] == "collect",
       f"reason={few['motion']['reason']} followup={few['followup']['action']}",
       attribution="rules")

# ==================== T6 窗口外陈旧 + 链路在线冲突 ====================

clear_all()
insert_imu(12.0)
set_device(1.0)
stale = ask("我在动吗？")
c1 = [c for c in stale["conflicts"] if c["kind"] == "link_ok_data_stale"]
report("T6 12s 无新样本：无法判断 + 链路在线冲突",
       stale["motion"]["state"] == "unknown"
       and stale["motion"]["latest_age_ms"] == 12000
       and c1
       and "12.0 秒" in stale["answer"]
       and "无法判断当前是否移动" in stale["answer"],
       f"reason={stale['motion']['reason']} conflicts={[c['kind'] for c in stale['conflicts']]}",
       attribution="time")

# ==================== T7 旧快照 + 位置问题 ====================

clear_all()
insert_imu(0.5)
set_device(0.2)
insert_image("old-location", "captured", age_s=700)
insert_inference("old-location", inferred_age_s=700)
location = ask("我现在在哪？")
c3 = [c for c in location["conflicts"] if c["kind"] == "stale_snapshot_inference"]
report("T7 位置问题：无定位来源，旧快照不包装成当前位置",
       location["topic"] == "location"
       and location["decision"]["id"] == "D4"
       and location["location"]["available"] is False
       and not has_coordinate_keys(location)
       and "本机没有定位来源" in location["answer"]
       and "不代表你现在的位置" in location["answer"]
       and c3,
       f"decision={location['decision']['id']} conflicts={[c['kind'] for c in location['conflicts']]}",
       attribution="interaction")

# ==================== T8 遮挡 + 旧图：不解释为危险 ====================

clear_all()
insert_imu(0.5)
set_device(0.2)
insert_image("old-blocked", "captured", age_s=120)
insert_image("blocked-now", "blocked", age_s=2, error="lens covered")
safety = ask("周围环境安全吗？")
c2 = [c for c in safety["conflicts"] if c["kind"] == "old_snapshot_plus_failure"]
danger_claim = re.search(r"(存在|没有|很|是)(危险|安全)", safety["answer"])
report("T8 遮挡：无法确认 + 不判危险 + 重拍追问",
       safety["hazard"]["judgement"] is None
       and c2
       and safety["followup"] is not None
       and safety["followup"]["action"] == "capture"
       and "无法确认当前环境" in safety["answer"]
       and danger_claim is None,
       f"decision={safety['decision']['id']} conflicts={[c['kind'] for c in safety['conflicts']]}",
       attribution="interaction")

# ==================== T9 模型推断与观测区分 ====================

clear_all()
motion_rows(8)
set_device(0.2)
insert_image("scene-1", "captured", age_s=30)
insert_inference("scene-1", label="computer keyboard", score=0.41,
                 version="mobilenetv2-12-onnx", inferred_age_s=30)
scene = ask("最近画面是什么？")
inference_facts = facts_of(scene, "inference")
report("T9 模型结果标为推断，带版本/分数，不写成观测",
       scene["topic"] == "scene"
       and inference_facts
       and inference_facts[0]["details"].get("model_version") == "mobilenetv2-12-onnx"
       and "未经校准" in scene["answer"]
       and "观测到" not in scene["answer"]
       and scene["decision"]["id"] == "D1"
       and scene["followup"] is None,
       f"facts={len(inference_facts)} label={inference_facts[0]['details'].get('label') if inference_facts else None}",
       attribution="model")

# ==================== T10 拒识不写成事实 ====================

clear_all()
insert_imu(0.5)
set_device(0.2)
insert_image("scene-bad", "captured", age_s=30)
insert_inference("scene-bad", label="keyboard", score=0.05,
                 rejected=1, reason="low_score", inferred_age_s=30)
rejected = ask("最近画面是什么？")
report("T10 模型拒识 → 未能确认 + 重拍追问，标签不进入回答",
       "模型未能确认" in rejected["answer"]
       and "keyboard" not in rejected["answer"]
       and rejected["followup"] is not None
       and rejected["followup"]["action"] == "capture",
       f"decision={rejected['decision']['id']}", attribution="model")

# ==================== T11 追问优先级：取图 > 采样 ====================

clear_all()
set_device(0.2)
insert_image("blocked-priority", "blocked", age_s=2, error="lens covered")
priority = ask("我在动吗？")
report("T11 运动问题优先补采样，不被旧画面劫持",
       priority["followup"] is not None
       and priority["followup"]["action"] == "collect"
       and len(priority["followup"].get("options", [])) <= 3,
       f"followup={priority['followup']['action'] if priority['followup'] else None}",
       attribution="rules")

# ==================== T12 安全问题的通用护栏 ====================

clear_all()
insert_imu(0.5)
set_device(0.2)
insert_image("scene-safe", "captured", age_s=30)
insert_inference("scene-safe", label="mask", score=0.55, inferred_age_s=30)
safe = ask("现在安全吗？")
report("T12 安全类问题：只描述画面，不给安全/危险结论",
       safe["hazard"]["judgement"] is None
       and "不做危险/安全判断" in safe["answer"]
       and re.search(r"(存在|没有|很|是)(危险|安全)", safe["answer"]) is None,
       safe["answer"][:60], attribution="interaction")

# ==================== T13 trace 落库与冲突筛选 ====================

clear_all()
insert_imu(0.5)
set_device(0.2)
insert_image("old-trace", "captured", age_s=700)
insert_inference("old-trace", inferred_age_s=700)
resp = client.post("/api/context", json={"question": "我现在在哪？"})
body = resp.get_json() if resp.is_json else {}
traces = client.get("/api/context/traces?only_conflict=1").get_json()
single = client.get(f"/api/context/traces/{body.get('trace_id', 'none')}").get_json()
report("T13 trace 保存规则版本/冲突，only_conflict 可取回",
       resp.status_code == 200
       and body.get("rule_version") == ctx.RULE_VERSION
       and traces.get("count", 0) >= 1
       and single.get("trace_id") == body.get("trace_id")
       and single.get("conflicts"),
       f"HTTP={resp.status_code} traces={traces.get('count')}", attribution="server")

# ==================== T14 只读：不改原始观测与图片 ====================

conn = db()
before_counts = {
    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    for table in ("imu_data", "image_records", "inference_results")
}
conn.close()
img_path = os.path.join(server_app.IMAGE_DIR, "old-trace.jpg")
before_hash = hashlib.sha256(open(img_path, "rb").read()).hexdigest()
client.post("/api/context", json={"question": "你现在知道什么？"})
conn = db()
after_counts = {
    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    for table in ("imu_data", "image_records", "inference_results")
}
after_hash = hashlib.sha256(open(img_path, "rb").read()).hexdigest()
conn.close()
report("T14 上下文解释只读：原始表与图片哈希不变",
       before_counts == after_counts and before_hash == after_hash,
       f"counts={after_counts} hash_same={before_hash == after_hash}", attribution="server")

# ==================== T15 回放来源标记 ====================

clear_all()
insert_imu(0.5, provenance="device")
set_device(0.2)
insert_image("replay-1", "captured", age_s=10, source="replay")
insert_inference("replay-1", inferred_age_s=10)
replay = ask("最近画面是什么？")
replay_camera = [f for f in facts_of(replay, "observation") if f["source"] == "camera"]
report("T15 replay 来源保留在事实与 trace 汇总中",
       replay_camera and replay_camera[0]["provenance"] == "replay"
       and replay["provenance"] == "mixed",
       f"camera_prov={replay_camera[0]['provenance'] if replay_camera else None} "
       f"summary={replay['provenance']}", attribution="replay")

# ==================== T16 助手白名单不劫持原命令 ====================

report("T16 白名单：上下文问句命中，取图/查询命令不命中",
       ctx.match_question("我在动吗？") == "motion"
       and ctx.match_question("我现在在哪") == "location"
       and ctx.match_question("你现在知道什么") == "overview"
       and ctx.match_question("看看周围环境") is None
       and ctx.match_question("查看上次数据") is None
       and ctx.match_question("重新采集一次") is None
       and server_app.classify_intent_by_rules("看看周围环境")["intent"] == "capture_image",
       "whitelist ok", attribution="interaction")

# ==================== T17 /api/nl_query 上下文路由 ====================

clear_all()
insert_imu(0.5)
set_device(0.2)
nl = client.post("/api/nl_query", json={"text": "我在动吗？"})
nl_body = nl.get_json() if nl.is_json else {}
report("T17 nl_query：上下文问句走 context_query",
       nl.status_code == 200
       and nl_body.get("intent") == "context_query"
       and nl_body.get("context", {}).get("rule_version") == ctx.RULE_VERSION
       and nl_body.get("message"),
       f"HTTP={nl.status_code} intent={nl_body.get('intent')}", attribution="interaction")

# ==================== T18 API 参数校验 ====================

empty_resp = client.post("/api/context", json={})
long_resp = client.post("/api/context", json={"question": "动" * 201})
missing_resp = client.get("/api/context/traces/not-a-real-trace")
report("T18 /api/context 参数校验",
       empty_resp.status_code == 400
       and long_resp.status_code == 400
       and missing_resp.status_code == 404,
       f"empty={empty_resp.status_code} long={long_resp.status_code} "
       f"missing={missing_resp.status_code}", attribution="server")

# ==================== T19 待确认与回答保持“未知” ====================

clear_all()
insert_imu(0.5)
set_device(0.2)
location_empty = ask("我的位置在哪？")
report("T19 无快照的位置问题：保留待确认，不补造位置",
       location_empty["location"]["available"] is False
       and any(item["text"] == "真实位置" for item in location_empty["pending"])
       and "无法给出坐标" in location_empty["answer"]
       and "经纬度" not in location_empty["answer"],
       f"decision={location_empty['decision']['id']}", attribution="interaction")

# ==================== 汇总与证据 ====================

summary = {
    "week": 11,
    "title": "根据多源证据组织交互上下文：事实/派生/决策三层与追问",
    "pass": PASS_COUNT,
    "fail": FAIL_COUNT,
    "total": PASS_COUNT + FAIL_COUNT,
    "config": {
        "ttl": TTL,
        "rule_version": ctx.RULE_VERSION,
        "thresholds": ctx.thresholds(),
        "rule_ids": [rule["id"] for rule in ctx.RULES],
        "decisions": ctx.DECISION_LABELS,
    },
    "note": "测试使用固定 now 注入与临时库；数据库/图片/日志均重定向，不影响生产数据。",
}

print("-" * 72)
print(f"通过 {PASS_COUNT} / 失败 {FAIL_COUNT} / 共 {PASS_COUNT + FAIL_COUNT}")
print("-" * 72)

if EVIDENCE_PATH:
    with open(EVIDENCE_PATH, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "cases": EVIDENCE}, f,
                  ensure_ascii=False, indent=2)
    print(f"原始记录已写入：{EVIDENCE_PATH}")

shutil.rmtree(TMP_DIR, ignore_errors=True)
sys.exit(1 if FAIL_COUNT else 0)

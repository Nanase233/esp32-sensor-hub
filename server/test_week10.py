# -*- coding: utf-8 -*-
"""第 10 周验收测试：区分传感器观测与当前状态。

覆盖：
  1. 旧库迁移幂等，原始观测不丢
  2. 四个必需状态：未尝试 / 成功 / 陈旧 / 失败，以及缺失/时效未知
  3. 观测时间、接收时间、传输时延与数据年龄
  4. 补传旧数据按原观测时间解释，不覆盖最新观测
  5. 失败不覆盖上一次有效观测；静默离线不写成设备故障
  6. 摄像头 blocked + 旧图 = "旧位置 + 本次 no_fix" 的同构案例
  7. 链路在线但 IMU 陈旧 = "网络在线但位置未更新"
  8. GNSS 未配置明确标缺失，任何响应都不含坐标
  9. /api/sources 契约与状态文案完整

运行（不依赖已启动的服务器，进程内 Flask test_client）：
    cd server
    venv\\Scripts\\python test_week10.py

隔离：数据库与图像目录重定向到临时目录；INTERACTION_LOG 也指向临时文件，
不影响正在运行的服务。
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = tempfile.mkdtemp(prefix="week10-")
os.environ["INTERACTION_LOG"] = os.path.join(TMP_DIR, "interaction.log")
sys.path.insert(0, HERE)

import app as server_app                  # noqa: E402
import inquiry as inquiry_mod             # noqa: E402
import source_state                       # noqa: E402

FIXED = time.time()
TTL = {"imu_ms": 5000, "camera_ms": 600000, "link_ms": 10000}
EVIDENCE_PATH = os.environ.get("WEEK10_EVIDENCE", "week10_evidence.json").strip()
EVIDENCE = []
PASS_COUNT = 0
FAIL_COUNT = 0


def report(name, ok, detail="", attribution="time", **extra):
    """记录用例并写入原始证据。"""
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
inquiry_mod.configure(server_app.DB_FILE, server_app.create_image_task)
inquiry_mod.init_db()
client = server_app.app.test_client()


def db():
    conn = sqlite3.connect(server_app.DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def insert_imu(observed_at, received_at, ax=1.0, ay=2.0, az=9.8,
               observed_uptime_ms=None, send_uptime_ms=None,
               age_basis="device_transit", boot_id="a1b2c3d4",
               provenance="device", timestamp=None):
    conn = db()
    conn.execute("""
        INSERT INTO imu_data (
            timestamp, ax, ay, az, observed_at, received_at,
            observed_uptime_ms, send_uptime_ms, boot_id, age_basis, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (timestamp if timestamp is not None else 1000000.0,
          ax, ay, az, observed_at, received_at,
          observed_uptime_ms, send_uptime_ms, boot_id, age_basis, provenance))
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return row_id


def set_device(last_seen, imu_attempt="ok", error=None, fails=0, boot="a1b2c3d4"):
    conn = db()
    conn.execute("""
        INSERT INTO device_status (device_id, last_seen_at, boot_id, uptime_ms,
                                   imu_attempt, imu_error, imu_fail_count, updated_at)
        VALUES ('esp32-s3-eye', ?, ?, 1000, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            boot_id=excluded.boot_id,
            imu_attempt=excluded.imu_attempt,
            imu_error=excluded.imu_error,
            imu_fail_count=excluded.imu_fail_count,
            updated_at=excluded.updated_at
    """, (last_seen, boot, imu_attempt, error, fails, last_seen))
    conn.commit()
    conn.close()


def clear_all():
    conn = db()
    for table in ("imu_data", "device_status", "image_records", "collection_tasks"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()


def source_map(report_data):
    return {src["source"]: src for src in report_data["sources"]}


def has_coordinate_keys(obj):
    """递归检查是否出现 lat/lon/position 等定位字段。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in ("lat", "lon", "lng", "latitude", "longitude", "position"):
                return True
            if has_coordinate_keys(value):
                return True
    elif isinstance(obj, list):
        return any(has_coordinate_keys(item) for item in obj)
    return False


# ==================== T1 迁移 ====================

conn = db()
columns = {row[1] for row in conn.execute("PRAGMA table_info(imu_data)")}
for required in ("observed_at", "received_at", "observed_uptime_ms",
                 "send_uptime_ms", "boot_id", "age_basis", "provenance"):
    if required not in columns:
        report("T1 迁移字段", False, f"缺少 {required}", attribution="server")
        break
else:
    report("T1 迁移字段", True, "7 个新字段存在", attribution="server")

conn.execute("""
    INSERT INTO imu_data (timestamp, ax, ay, az, created_at)
    VALUES (1000000, 0.1, 0.2, 9.7, '2026-01-01 00:00:00')
""")
conn.commit()
old_count = conn.execute("SELECT COUNT(*) FROM imu_data").fetchone()[0]
conn.close()
server_app.init_db()          # 再跑一次，模拟重启
conn = db()
new_count = conn.execute("SELECT COUNT(*) FROM imu_data").fetchone()[0]
conn.close()
report("T1 迁移幂等且保留旧行", old_count == new_count,
       f"迁移前后 {old_count} -> {new_count}", attribution="server")

clear_all()

# ==================== T2 空库 ====================

empty = source_map(source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL))
ok = (empty["imu"]["status"] == "not_attempted"
      and empty["camera"]["status"] == "not_attempted"
      and empty["link"]["status"] == "not_attempted"
      and empty["gnss"]["status"] == "absent"
      and not has_coordinate_keys(empty))
report("T2 空库：未尝试/缺失，且无坐标", ok,
       f"imu={empty['imu']['status']} camera={empty['camera']['status']} "
       f"link={empty['link']['status']} gnss={empty['gnss']['status']}",
       attribution="server")

# ==================== T3/T4 新鲜与 TTL 边界 ====================

insert_imu(FIXED - 0.4, FIXED - 0.39, observed_uptime_ms=1000, send_uptime_ms=1050)
set_device(FIXED - 0.1, "ok")
fresh = source_map(source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL))["imu"]
report("T3 新鲜 IMU = ok，年龄按观测时间", fresh["status"] == "ok"
       and fresh["age_basis"] == "device_transit"
       and fresh["transit_ms"] == 50
       and fresh["can_represent_current"] is True,
       f"status={fresh['status']} age={fresh['age_ms']} basis={fresh['age_basis']} "
       f"transit={fresh['transit_ms']}", attribution="time")

clear_all()
insert_imu(FIXED - 5.0, FIXED - 5.0)
set_device(FIXED, "ok")
edge_ok = source_map(source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL))["imu"]
clear_all()
insert_imu(FIXED - 5.001, FIXED - 5.001)
set_device(FIXED, "ok")
edge_stale = source_map(source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL))["imu"]
report("T4 TTL 边界：=TTL 新鲜，>TTL 陈旧",
       edge_ok["status"] == "ok" and edge_stale["status"] == "stale",
       f"5.000s={edge_ok['status']} 5.001s={edge_stale['status']}", attribution="time")

# ==================== T5 补传 ====================

clear_all()
insert_imu(FIXED - 1.0, FIXED - 1.0, ax=1.11, observed_uptime_ms=2000, send_uptime_ms=2050)
fresh_id = None
insert_imu(FIXED - 120.0, FIXED - 0.01, ax=9.99, observed_uptime_ms=1000, send_uptime_ms=121000)
set_device(FIXED, "ok")
report_data = source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL)
imu_now = source_map(report_data)["imu"]
history = client.get("/api/imu/history?limit=10").get_json()["data"]
backfill_row = next((row for row in history if row["ax"] == 9.99), None)
latest_api = client.get("/api/imu/latest").get_json()
backfill_age = None
if backfill_row and backfill_row["observed_at"] is not None:
    backfill_age = (FIXED - backfill_row["observed_at"]) * 1000.0
report("T5 补传按原观测时间解释，不覆盖最新",
       latest_api["ax"] == 1.11 and backfill_age is not None
       and backfill_age >= 119000 and imu_now["status"] == "ok",
       f"latest.ax={latest_api['ax']} backfill.age={None if backfill_age is None else round(backfill_age)} "
       f"imu.status={imu_now['status']}", attribution="time")

# ==================== T6 明确失败保留旧观测 ====================

before = db().execute("SELECT COUNT(*) FROM imu_data").fetchone()[0]
set_device(FIXED, "fail", error="read", fails=3)
failed = source_map(source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL))["imu"]
after = db().execute("SELECT COUNT(*) FROM imu_data").fetchone()[0]
report("T6 失败保留旧观测，不写成设备故障",
       failed["status"] == "failed" and failed["last_success"] is not None
       and before == after and "设备故障" not in failed["user_message"],
       f"status={failed['status']} rows={before}->{after}", attribution="device")

# ==================== T7 静默离线 ====================

clear_all()
insert_imu(FIXED - 60.0, FIXED - 60.0, observed_uptime_ms=1000, send_uptime_ms=1050)
set_device(FIXED - 60.0, "ok")
offline = source_map(source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL))["imu"]
report("T7 静默离线 = stale，不是 failed",
       offline["status"] == "stale" and offline["status_reason"] == "device_offline",
       f"status={offline['status']} reason={offline['status_reason']}", attribution="time")

# ==================== T8 上传停滞 ====================

clear_all()
insert_imu(FIXED - 12.0, FIXED - 12.0, observed_uptime_ms=1000, send_uptime_ms=1050)
set_device(FIXED - 1.0, "ok")
stalled = source_map(source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL))["imu"]
report("T8 设备在采样但服务器未收到 = upload_stalled",
       stalled["status"] == "stale" and stalled["status_reason"] == "upload_stalled",
       f"status={stalled['status']} reason={stalled['status_reason']}", attribution="interaction")

# ==================== T9 旧固件时间未知 ====================

clear_all()
conn = db()
conn.execute("""
    INSERT INTO imu_data (timestamp, ax, ay, az, received_at, created_at)
    VALUES (466792679, 0.1, 0.2, 9.7, ?, '2026-01-01 00:00:00')
""", (FIXED,))
conn.commit()
conn.close()
legacy = source_map(source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL))["imu"]
report("T9 旧固件只有 timestamp = unknown_age",
       legacy["status"] == "unknown_age" and legacy["age_basis"] == "unknown",
       f"status={legacy['status']} age={legacy['age_ms']}", attribution="time")

# ==================== T10 摄像头 blocked + 旧图 ====================

clear_all()
old_path = os.path.join(server_app.IMAGE_DIR, "old.jpg")
with open(old_path, "wb") as f:
    f.write(b"\xff\xd8" + b"old-image" * 20 + b"\xff\xd9")
old_hash = hashlib.sha256(open(old_path, "rb").read()).hexdigest()

conn = db()
conn.execute("""
    INSERT INTO image_records (request_id, device_id, status, source, file_path,
                               size_bytes, device_ts_us, captured_at, error_msg, created_at)
    VALUES ('old-capture', 'esp32-s3-eye', 'captured', 'device', ?, 200, 1000, ?, NULL, ?)
""", (old_path, FIXED - 120, FIXED - 120))
conn.execute("""
    INSERT INTO image_records (request_id, device_id, status, source, file_path,
                               size_bytes, device_ts_us, captured_at, error_msg, created_at)
    VALUES ('blocked-capture', 'esp32-s3-eye', 'blocked', 'device', NULL, NULL, NULL, NULL,
            'lens covered', ?)
""", (FIXED - 2,))
conn.commit()
conn.close()
camera = source_map(source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL))["camera"]
report("T10 旧图 + 本次 blocked：failed 且保留旧图",
       camera["status"] == "failed" and camera.get("failure_kind") == "blocked"
       and "不是设备故障" in camera["user_message"]
       and camera["last_success"]["age_ms"] >= 100000
       and camera["raw"]["raw_available"] is True
       and hashlib.sha256(open(old_path, "rb").read()).hexdigest() == old_hash,
       f"status={camera['status']} kind={camera.get('failure_kind')} "
       f"old_age={camera['last_success']['age_ms']}", attribution="device")

# ==================== T11 链路在线但数据陈旧 ====================

clear_all()
insert_imu(FIXED - 30.0, FIXED - 30.0, observed_uptime_ms=1000, send_uptime_ms=1050)
set_device(FIXED - 1.0, "ok")
report_data = source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL)
comparison = report_data["comparisons"]["link_ok_data_stale"]
report("T11 链路在线但 IMU 未更新 = 对照可用",
       comparison["available"] is True
       and source_map(report_data)["link"]["status"] == "ok"
       and source_map(report_data)["imu"]["status"] == "stale",
       comparison.get("message", ""), attribution="interaction")

# ==================== T12 GNSS 缺失且无坐标 ====================

gnss = source_map(source_state.build_report(server_app.DB_FILE, now=FIXED, ttl=TTL))["gnss"]
report("T12 GNSS 缺失：absent + 非故障 + 无坐标",
       gnss["status"] == "absent" and gnss.get("not_a_fault") is True
       and gnss["raw"] is None and not has_coordinate_keys(gnss),
       gnss["user_message"], attribution="server")

# ==================== T13 失败不改变原始记录 ====================

before = db().execute("SELECT COUNT(*) FROM imu_data").fetchone()[0]
before_hash = hashlib.sha256(open(old_path, "rb").read()).hexdigest()
client.get("/api/collect/pending?device_id=esp32-s3-eye&imu=fail&err=read&fails=9")
after = db().execute("SELECT COUNT(*) FROM imu_data").fetchone()[0]
after_hash = hashlib.sha256(open(old_path, "rb").read()).hexdigest()
report("T13 失败上报不改写原始观测/图片",
       before == after and before_hash == after_hash,
       f"imu_data={before}->{after} hash_same={before_hash == after_hash}",
       attribution="server")

# ==================== T14 /api/sources 契约 ====================

resp = client.get("/api/sources")
data = resp.get_json() if resp.is_json else {}
statuses = {src["source"]: src["status"] for src in data.get("sources", [])}
all_have_copy = all(src.get("status_label") and src.get("user_message")
                    for src in data.get("sources", []))
report("T14 /api/sources 契约完整",
       resp.status_code == 200 and set(statuses) == {"imu", "camera", "link", "gnss"}
       and all_have_copy and "server_time" in data and "ttl" in data,
       f"HTTP={resp.status_code} sources={statuses}", attribution="server")

# ==================== T15 新固件上报路径 ====================

clear_all()
r1 = client.post("/api/imu", json={
    "timestamp": 1000000, "ax": 1.11, "ay": 2.0, "az": 9.8,
    "observed_uptime_ms": 1000, "send_uptime_ms": 1050, "boot_id": "a1b2c3d4",
})
r2 = client.post("/api/imu", json={
    "timestamp": 1000000, "ax": 9.99, "ay": 2.0, "az": 9.8,
    "observed_uptime_ms": 1000, "send_uptime_ms": 121000, "boot_id": "a1b2c3d4",
})
api_latest = client.get("/api/imu/latest").get_json()
report("T15 上报路径：滞留计入年龄，最新仍是最新",
       r1.status_code == 201 and r2.status_code == 201
       and r1.get_json()["age_basis"] == "device_transit"
       and api_latest["ax"] == 1.11,
       f"age_basis={r1.get_json()['age_basis']} latest.ax={api_latest['ax']}",
       attribution="time")

# ==================== 汇总与证据 ====================

summary = {
    "week": 10,
    "title": "区分传感器观测与当前状态：多来源独立状态与数据年龄",
    "pass": PASS_COUNT,
    "fail": FAIL_COUNT,
    "total": PASS_COUNT + FAIL_COUNT,
    "config": {
        "ttl": TTL,
        "status_vocabulary": list(source_state.STATUS_LABELS.keys()),
        "time_model": "observed_at = received_at - (send_uptime_ms - observed_uptime_ms)/1000",
    },
    "note": "测试使用固定 now 注入；数据库/图片均重定向临时目录，不影响生产数据。",
}

print("-" * 72)
print(f"通过 {PASS_COUNT} / 失败 {FAIL_COUNT} / 共 {PASS_COUNT + FAIL_COUNT}")
print("-" * 72)

if EVIDENCE_PATH:
    with open(EVIDENCE_PATH, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "cases": EVIDENCE}, f, ensure_ascii=False, indent=2)
    print(f"原始记录已写入：{EVIDENCE_PATH}")

shutil.rmtree(TMP_DIR, ignore_errors=True)
sys.exit(1 if FAIL_COUNT else 0)

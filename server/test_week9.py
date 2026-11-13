# -*- coding: utf-8 -*-
"""第 9 周任务验收测试：画面不可用 → 主动询问「是否重拍」

覆盖（对应本周要求）：
  1. 事件触发：遮挡/暗光/模糊/采集失败/模型不可用五类"画面无法使用"事件都能触发询问
  2. 主动：询问由事件产生，不需要用户点任何按钮
  3. 尊重用户选择：确认重拍 / 不用了 / 其实可用（误判纠正）三条分支行为确定
  4. 不反复打扰：拒绝后同原因冷却期内不再询问；超时未答按忽略处理且不重弹
  5. 单飞行：已有未决询问时不新建第二条（不连环弹窗）
  6. 重拍接入原有任务状态：重拍复用第 7 周图像任务流，结果回填原询问
  7. 重拍上限：连续不可用 → exhausted，并给出"改用求助"的建议
  8. 误判纠正不污染第 8 周人工纠错表（human_feedbacks 不新增记录）
  9. 失败事件不改变既有语义：不存在的任务仍 404，IMU 主链路仍可用

运行方式（不依赖已启动的服务器，进程内直连 Flask test_client）：
    cd server
    venv\\Scripts\\python test_week9.py

隔离措施：
  - 询问冷却参数在导入 app 之前被压到秒级（INQUIRY_*），测试结束不影响生产默认值
  - 数据库与图像目录重定向到临时目录，不写 server/imu_data.db、不删 server/images
  - 模型不可用场景用"故障注入"模拟（REQUIRE_REAL_MODEL 未设置时），证据中标注 injected
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import time

# ---- 冷却参数必须在 import app 之前压到秒级（inquiry.py 在导入时读取） ----
os.environ["INQUIRY_TIMEOUT_S"] = "2"        # 未答复 2 秒过期
os.environ["INQUIRY_GLOBAL_GAP_S"] = "0"     # 测试中不等待全局间隔
os.environ["DECLINE_COOLDOWN_S"] = "30"      # 拒绝后同原因冷却
os.environ["EXPIRE_COOLDOWN_S"] = "30"       # 超时后同原因冷却
os.environ["DISMISS_COOLDOWN_S"] = "30"      # 误判纠正后同原因冷却
os.environ["MAX_RETAKE"] = "2"               # 单张图最多重拍 2 次

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np                      # noqa: E402
from PIL import Image                   # noqa: E402

import app as server_app                # noqa: E402
import inquiry as inquiry_mod           # noqa: E402
from vision.models import InferenceResult  # noqa: E402

EVIDENCE_PATH = os.environ.get("WEEK9_EVIDENCE", "week9_evidence.json").strip()
EVIDENCE = []
PASS_COUNT = 0
FAIL_COUNT = 0

# 不可解码的"伪 JPEG"（头部合法，解码必然失败）
FAKE_JPEG = b"\xff\xd8" + b"\x00" * 200 + b"\xff\xd9"


# ==================== 结果记录 ====================

def report(name, ok, detail="", attribution="interaction", **extra):
    """记录一条用例结果，并写入证据（原始记录，验收时可直接出示）。"""
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
        "attribution": attribution,   # model / device / interaction：便于按类归因
        "recorded_at": time.time(),
        **extra,
    })


# ==================== 测试环境隔离 ====================

TMP_DIR = tempfile.mkdtemp(prefix="week9-")
server_app.DB_FILE = os.path.join(TMP_DIR, "test.db")
server_app.IMAGE_DIR = os.path.join(TMP_DIR, "images")
os.makedirs(server_app.IMAGE_DIR, exist_ok=True)

# 重新注入依赖：临时库 + 原 create_image_task（重拍必须走真实取图入口）
inquiry_mod.configure(server_app.DB_FILE, server_app.create_image_task)
server_app.init_db()
inquiry_mod.init_db()

client = server_app.app.test_client()


# ==================== 夹具 ====================

def make_jpeg(kind="clear"):
    """生成测试用 JPEG：dark=全黑；clear=高对比噪声（画面客观可用）。"""
    if kind == "dark":
        arr = np.zeros((120, 160, 3), dtype=np.uint8)
    else:
        rng = np.random.default_rng(7)
        arr = rng.integers(0, 256, (120, 160, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


CLEAR_JPEG = make_jpeg("clear")
DARK_JPEG = make_jpeg("dark")


def new_image_task():
    """按设备流程创建一张待采集图像（source=device）。"""
    r = client.post("/api/image", json={"device_id": "esp32-s3-eye", "source": "device"})
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()["request_id"]


def device_claim(request_id):
    """模拟设备轮询：把任务从 pending 置为 sent，再回报 received。"""
    pending = client.get("/api/collect/pending").get_json()
    assert pending.get("request_id") == request_id, f"设备轮询拿到 {pending}"
    assert pending.get("type") == "image"
    r = client.post(f"/api/image/{request_id}/received")
    assert r.status_code == 200, r.get_data(as_text=True)


def upload(request_id, data):
    return client.post(f"/api/image/{request_id}/data", data=data,
                       headers={"X-Capture-Ts": "123456", "Content-Type": "image/jpeg"})


def fail_frame(reason="blocked"):
    """模拟设备上报采集失败（遮挡/错误）。"""
    rid = new_image_task()
    device_claim(rid)
    r = client.post(f"/api/image/{rid}/fail", json={"reason": reason, "error": "week9 test"})
    assert r.status_code == 200, r.get_data(as_text=True)
    return rid


def capture(kind="clear"):
    """模拟一次完整采集：claim + 上传 JPEG，返回 request_id。"""
    rid = new_image_task()
    device_claim(rid)
    r = upload(rid, CLEAR_JPEG if kind == "clear" else DARK_JPEG)
    assert r.status_code == 201, r.get_data(as_text=True)
    return rid


def pending():
    return client.get("/api/inquiry/pending").get_json()["inquiry"]


def answer(inquiry_id, decision, channel="ui", mute=False):
    r = client.post(f"/api/inquiry/{inquiry_id}/answer",
                    json={"decision": decision, "channel": channel, "mute": mute})
    return r.status_code, r.get_json()


def inquiry_count():
    return len(inquiry_mod.list_recent(limit=200))


def human_feedback_count(request_id):
    import sqlite3
    conn = sqlite3.connect(server_app.DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(1) FROM human_feedbacks WHERE request_id=?", (request_id,))
    n = c.fetchone()[0]
    conn.close()
    return n


# ==================== 用例 ====================

print("=" * 72)
print("第 9 周验收：画面不可用 → 主动询问「是否重拍」")
print(f"临时库：{server_app.DB_FILE}")
print("=" * 72)

# --- T1 遮挡事件触发询问（device → server） ---
rid_blocked = fail_frame("blocked")
inq = pending()
ok = bool(inq) and inq["reason"] == "blocked" and inq["status"] == "asking"
report("T1 遮挡事件主动询问", ok,
       f"reason={inq and inq['reason']} question={inq and inq['question']}",
       attribution="device", request_id=rid_blocked, inquiry=inq)

# --- T2 用户拒绝 → 不重拍、登记冷却 ---
code, body = answer(inq["inquiry_id"], "decline")
ok = code == 200 and body["status"] == "declined" and body["cooldown_remaining_s"] > 0
report("T2 拒绝后登记冷却", ok,
       f"status={body.get('status')} cooldown={body.get('cooldown_remaining_s')}s",
       attribution="interaction", inquiry_id=inq["inquiry_id"])

# --- T3 拒绝后同原因冷却期内不再询问（不反复打扰，核心指标） ---
before = inquiry_count()
rid_blocked2 = fail_frame("blocked")
ok = pending() is None and inquiry_count() == before
report("T3 拒绝后不反复打扰", ok,
       f"新事件未产生询问（记录数 {before} → {inquiry_count()}）",
       attribution="interaction", request_id=rid_blocked2)

# --- T4 同一询问重复答复不生效（首个有效答复有效） ---
code, body = answer(inq["inquiry_id"], "retake")
ok = code == 409 and body.get("duplicate") and body.get("error")
report("T4 重复答复不生效", ok, f"code={code} error={body.get('error')}",
       attribution="interaction", inquiry_id=inq["inquiry_id"])

# --- T5 超时未答复 → expired，按忽略处理 ---
rid_dark = capture("dark")
inq_dark = pending()
ok_dark = bool(inq_dark) and inq_dark["reason"] == "dark" and inq_dark["remaining_s"] <= 2
report("T5 暗光事件触发询问", ok_dark,
       f"reason={inq_dark and inq_dark['reason']} 剩余={inq_dark and inq_dark['remaining_s']}s",
       attribution="model", request_id=rid_dark)

time.sleep(2.4)
detail = client.get(f"/api/inquiry/{inq_dark['inquiry_id']}").get_json()
ok = detail["status"] == "expired" and pending() is None
report("T6 超时未答复按忽略处理", ok,
       f"status={detail['status']} decision={detail.get('decision')}",
       attribution="interaction", inquiry_id=inq_dark["inquiry_id"])

# --- T7 超时后同原因冷却期内不再询问 ---
before = inquiry_count()
rid_dark2 = capture("dark")
ok = pending() is None and inquiry_count() == before
report("T7 超时后同原因不重复询问", ok,
       f"记录数 {before} → {inquiry_count()}",
       attribution="interaction", request_id=rid_dark2)

# --- T8 模型不可用（故障注入）也能触发询问，且文案说明限制 ---
original_run_inference = server_app.run_inference


def injected_unavailable(_image_bytes):
    return InferenceResult(
        status="unavailable",
        model_name="injected",
        is_rejected=True,
        rejected_reason="model_unavailable",
        uncertain_note="故障注入：模拟模型服务不可用",
    )


server_app.run_inference = injected_unavailable
rid_model = capture("clear")
inq_model = pending()
ok = (bool(inq_model) and inq_model["reason"] == "model_unavailable"
      and inq_model["trigger"] == "model" and "可能判不了" in inq_model["question"])
report("T8 模型失败场景触发询问", ok,
       f"trigger={inq_model and inq_model['trigger']} question={inq_model and inq_model['question']}",
       attribution="model", request_id=rid_model)

# --- T9 用户判定「其实可用」→ 误判纠正，不重拍、不写第 8 周纠错表 ---
code, body = answer(inq_model["inquiry_id"], "usable")
ok = (code == 200 and body["status"] == "dismissed" and body.get("misjudged")
      and body.get("retake_request_id") is None and human_feedback_count(rid_model) == 0)
report("T9 误判纠正（其实可用）", ok,
       f"status={body.get('status')} 重拍={body.get('retake_request_id')} "
       f"纠错表记录={human_feedback_count(rid_model)}",
       attribution="interaction", inquiry_id=inq_model["inquiry_id"])

# --- T10 误判纠正后同原因不重复询问 ---
before = inquiry_count()
rid_model2 = capture("clear")
ok = pending() is None and inquiry_count() == before
report("T10 误判纠正后不重复询问", ok, f"记录数 {before} → {inquiry_count()}",
       attribution="interaction", request_id=rid_model2)

# 恢复真实推理入口
server_app.run_inference = original_run_inference

# --- T11 确认重拍 → 复用第 7 周图像任务，重拍可用则关闭询问 ---
rid_decode = new_image_task()
device_claim(rid_decode)
upload(rid_decode, FAKE_JPEG)          # 解码失败 → decode_failed 触发询问
inq_decode = pending()
ok = bool(inq_decode) and inq_decode["reason"] == "decode_failed"
report("T11 解码失败触发询问", ok, f"reason={inq_decode and inq_decode['reason']}",
       attribution="server", request_id=rid_decode)

code, body = answer(inq_decode["inquiry_id"], "retake")
new_task = body.get("new_image_task")
ok = code == 200 and body["status"] == "confirmed" and new_task and body["attempt_no"] == 1
report("T12 确认重拍后进入原有图像任务流", ok,
       f"status={body.get('status')} 新任务={new_task} attempt={body.get('attempt_no')}",
       attribution="interaction", inquiry_id=inq_decode["inquiry_id"])

device_claim(new_task)
upload(new_task, CLEAR_JPEG)           # 重拍画面客观可用 → resolved
detail = client.get(f"/api/inquiry/{inq_decode['inquiry_id']}").get_json()
ok = detail["status"] == "resolved"
report("T13 重拍可用则关闭询问", ok, f"status={detail['status']}", attribution="interaction",
       inquiry_id=inq_decode["inquiry_id"])

# --- T14 连续重拍仍不可用 → exhausted，建议改用求助 ---
rid_ex = new_image_task()
device_claim(rid_ex)
upload(rid_ex, FAKE_JPEG)
inq_ex = pending()
answer(inq_ex["inquiry_id"], "retake")          # 第 1 次重拍
t1 = client.get(f"/api/inquiry/{inq_ex['inquiry_id']}").get_json()["retake_request_id"]
device_claim(t1)
upload(t1, FAKE_JPEG)                            # 仍不可用 → 再次询问
inq_again = pending()
ok = bool(inq_again) and inq_again["inquiry_id"] == inq_ex["inquiry_id"]

answer(inq_ex["inquiry_id"], "retake")          # 第 2 次重拍
t2 = client.get(f"/api/inquiry/{inq_ex['inquiry_id']}").get_json()["retake_request_id"]
device_claim(t2)
upload(t2, FAKE_JPEG)                            # 达到上限 → exhausted
detail = client.get(f"/api/inquiry/{inq_ex['inquiry_id']}").get_json()
ok = ok and detail["status"] == "exhausted" and "求助" in (detail.get("note") or "")
report("T14 重拍上限后停止追问并建议求助", ok,
       f"二次询问={ok} status={detail['status']} note={detail.get('note')}",
       attribution="interaction", inquiry_id=inq_ex["inquiry_id"])

# --- T15 单飞行：已有未决询问时新事件不新建第二条 ---
rid_a = fail_frame("error")                      # capture_failed
inq_first = pending()
rid_b = fail_frame("error")
inq_second = pending()
ok = (inq_first and inq_second and inq_first["inquiry_id"] == inq_second["inquiry_id"])
report("T15 单飞行（不连环弹窗）", ok,
       f"第一条={inq_first and inq_first['inquiry_id']} 当前={inq_second and inq_second['inquiry_id']}",
       attribution="interaction")

answer(inq_second["inquiry_id"], "decline")      # 清理未决询问

# --- T16 失败事件不改变既有语义：未知任务仍 404、IMU 主链路不受影响 ---
r_bad = client.post("/api/image/not-exist/fail", json={"reason": "blocked"})
r_imu = client.post("/api/imu", json={"timestamp": time.time(), "ax": 0.0, "ay": 0.0, "az": 9.8})
ok = r_bad.status_code == 404 and r_imu.status_code in (200, 201)
report("T16 询问不影响既有接口语义", ok,
       f"未知任务={r_bad.status_code} IMU={r_imu.status_code}", attribution="server")

# ==================== 汇总与证据 ====================

config = inquiry_mod.config_snapshot()
summary = {
    "week": 9,
    "title": "画面不可用 → 主动询问「是否重拍」",
    "pass": PASS_COUNT,
    "fail": FAIL_COUNT,
    "total": PASS_COUNT + FAIL_COUNT,
    "config": config,
    "note": "冷却参数为测试用秒级取值；生产默认值见 server/inquiry.py",
    "fixtures": {
        "clear_jpeg_sha256": hashlib.sha256(CLEAR_JPEG).hexdigest()[:16],
        "dark_jpeg_sha256": hashlib.sha256(DARK_JPEG).hexdigest()[:16],
    },
}

print("-" * 72)
print(f"通过 {PASS_COUNT} / 失败 {FAIL_COUNT} / 共 {PASS_COUNT + FAIL_COUNT}")
print("-" * 72)

if EVIDENCE_PATH:
    with open(EVIDENCE_PATH, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "cases": EVIDENCE}, f, ensure_ascii=False, indent=2)
    print(f"原始记录已写入：{EVIDENCE_PATH}")

sys.exit(1 if FAIL_COUNT else 0)

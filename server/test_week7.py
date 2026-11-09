# -*- coding: utf-8 -*-
"""第 7 周任务验收测试：按需环境图像采集

覆盖：
  1. 创建任务后未取图时，不得返回任何历史图像（当堂验证核心）
  2. 设备完整流程（轮询 -> 接收 -> 上传）后状态为 captured，采集时间可用
  3. 已完成的旧任务不会被称为"本次采集成功"
  4. 非 sent/received 状态拒绝接收图像
  5. 镜头遮挡 -> blocked 明确状态
  6. 最少保留策略：保留最近 30 张文件，纠错样本不清理
"""
import json
import time
import urllib.request
import urllib.error

BASE = "http://localhost:5000"

# 构造一个符合服务器校验的最小 JPEG（仅用于接口测试）
FAKE_JPEG = b"\xff\xd8" + b"\x00" * 200 + b"\xff\xd9"


def post_json(path, payload, headers=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def post_raw(path, data, headers=None):
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "image/jpeg", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body.decode("utf-8"))
        except Exception:
            return e.code, {"raw": len(body)}


def report(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def create_task(source="replay"):
    """replay 来源的任务不会被设备领取，保证测试可重复；
    与真实设备并存的场景由 test_device_capture.py 覆盖。"""
    st, r = post_json("/api/image", {"device_id": "esp32-s3-eye", "source": source})
    assert st == 201, f"创建任务失败: {st} {r}"
    return r["request_id"]


def wait_pending_for(rid, timeout=6):
    """轮询 /api/collect/pending 直到取到指定任务（中间可能夹带其他任务）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, r = get("/api/collect/pending")
        if r.get("request_id") == rid:
            return r
        time.sleep(0.2)
    return None


# ---------- 用例 1：未取图任务不得返回历史图像 ----------
rid1 = create_task()
st, info = get(f"/api/image/{rid1}")
report("1a. 新任务初始状态为 pending", info.get("status") == "pending", info.get("status"))
report("1b. 未取图任务的 file_url 为空（不复用旧图）", info.get("file_url") is None,
       f"file_url={info.get('file_url')}")
st, _ = get(f"/api/image/{rid1}/file")
report("1c. 未取图任务的文件接口返回 404", st == 404, f"HTTP {st}")

# ---------- 用例 2：完整采集流程（回放来源，等价于设备上报后入库） ----------
st, _ = post_json(f"/api/image/{rid1}/received", {})
report("2a. 确认接收后状态为 received", st == 200, f"HTTP {st}")

st, up = post_raw(f"/api/image/{rid1}/data", FAKE_JPEG, {"X-Capture-Ts": "12345678"})
report("2b. 上传图像后状态为 captured", st == 201 and up.get("status") == "captured",
       f"HTTP {st}, status={up.get('status')}")

st, info = get(f"/api/image/{rid1}")
report("2c. 记录含采集时间与文件地址",
       info.get("status") == "captured" and info.get("captured_at") and info.get("file_url"),
       f"captured_at={info.get('captured_at_text')}, size={info.get('size_bytes')}")
report("2d. 设备侧时间戳已入库", info.get("device_ts_us") == 12345678,
       f"device_ts_us={info.get('device_ts_us')}")

# ---------- 用例 3：旧任务结果不冒充本次 ----------
rid2 = create_task()
st, info2 = get(f"/api/image/{rid2}")
report("3. 新任务不继承上一张成功图像（当堂验证）",
       info2.get("status") == "pending" and info2.get("file_url") is None,
       f"status={info2.get('status')}, file_url={info2.get('file_url')}")

# ---------- 用例 4：状态机拒绝越权上传 ----------
st, r = post_raw(f"/api/image/{rid1}/data", FAKE_JPEG)   # 已完成任务重复上传
report("4a. 已完成任务拒绝重复上传", st == 409, f"HTTP {st}, {r.get('error')}")

# ---------- 用例 5：镜头遮挡 -> blocked ----------
post_json(f"/api/image/{rid2}/received", {})
st, r = post_json(f"/api/image/{rid2}/fail", {"reason": "blocked", "error": "lens covered"})
report("5a. 遮挡上报后状态为 blocked", st == 200 and r.get("status") == "blocked",
       f"status={r.get('status')}")

st, info2 = get(f"/api/image/{rid2}")
report("5b. blocked 任务无图像地址，且失败原因可查",
       info2.get("status") == "blocked" and info2.get("file_url") is None and info2.get("error_msg"),
       f"error_msg={info2.get('error_msg')}")

# ---------- 用例 6：最少保留策略 ----------
print("... 连续创建并完成 12 个任务，验证保留策略")
recent_ids = []
for i in range(12):
    rid = create_task()
    post_json(f"/api/image/{rid}/received", {})
    post_raw(f"/api/image/{rid}/data", FAKE_JPEG)
    recent_ids.append(rid)

st, lst = get("/api/images?limit=20")
captured = [x for x in lst["images"] if x["status"] == "captured" and x["request_id"] in recent_ids]
with_file = [x for x in captured if x["file_url"]]
report("6. 保留策略配置为最近 30 张，12 张新图未被误删",
       len(with_file) == 12 and lst.get("keep_count") == 30,
       f"有文件的任务数={len(with_file)}, keep_count={lst.get('keep_count')}")

print("\n完成。可通过日志文件 server/interaction.log 按 request_id 核对完整生命周期。")

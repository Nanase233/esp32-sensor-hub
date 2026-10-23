# -*- coding: utf-8 -*-
"""第 6 周任务验收测试：澄清、取消、慢响应、迟到结果隔离、request_id 日志关联"""
import threading
import time
import json
import urllib.request

BASE = "http://localhost:5000"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def report(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


results = {}

# ---------- 测试 1：模糊输入 → 澄清 ----------
st, r = post("/api/nl_query", {"text": "查看数据并重新采集", "request_id": "t1-clarify"})
report("1. 澄清：模糊输入返回澄清提示", r.get("intent") == "ambiguous" and "明确" in r.get("message", ""),
       f"intent={r.get('intent')}, msg={r.get('message', '')[:40]}")
results["clarify_ms"] = r.get("processing_time_ms")

# ---------- 测试 2：正常查询（快速路径 + 首次反馈时间） ----------
st, r = post("/api/nl_query", {"text": "查看上次数据", "request_id": "t2-query"})
report("2. 查询：query_last 正常返回", r.get("intent") == "query_last",
       f"processing={r.get('processing_time_ms')}ms, success={r.get('success')}")
results["query_ms"] = r.get("processing_time_ms")

# ---------- 测试 3：慢响应 + 中途取消 + 迟到结果隔离 ----------
slow_resp = {}

def slow_task():
    slow_resp["r"] = post("/api/nl_query", {"text": "重新采集一次", "request_id": "t3-slow"})[1]

t = threading.Thread(target=slow_task)
t.start()
time.sleep(2)  # 让慢任务先进入 30s 轮询等待

st, r = post("/api/cancel", {"request_id": "t3-slow"})
report("3a. 中途取消：/api/cancel 接受取消", r.get("status") == "cancelled", str(r))

# 取消后重新输入（新任务，新 request_id）
st, r2 = post("/api/nl_query", {"text": "查看上次数据", "request_id": "t3-new"})
report("3b. 取消后重新输入：新任务正常执行且不被旧任务影响",
       r2.get("request_id") == "t3-new" and r2.get("intent") == "query_last",
       f"request_id={r2.get('request_id')}, intent={r2.get('intent')}")

t.join()  # 等慢任务结束（约 30s）
late = slow_resp.get("r", {})
report("3c. 慢响应：旧任务 30s 超时返回", late.get("source") == "timeout",
       f"source={late.get('source')}, request_id={late.get('request_id')}")
# 前端会因 result.request_id('t3-slow') != currentRequestId(已被取消置空/换新) 丢弃该结果
report("3d. 迟到结果隔离：旧任务结果 request_id 与新任务不同",
       late.get("request_id") == "t3-slow" and r2.get("request_id") == "t3-new")

# ---------- 测试 4：取消后同 ID 再提交 → 服务端直接返回 cancelled ----------
st, r = post("/api/nl_query", {"text": "重新采集一次", "request_id": "t3-slow"})
report("4. 已取消的 request_id 再提交被拦截", r.get("status") == "cancelled", str(r.get("status")))

# ---------- 测试 5：连续两次查询（新结果覆盖旧结果方向正确） ----------
st, r1 = post("/api/nl_query", {"text": "查看上次数据", "request_id": "t5-first"})
st, r2 = post("/api/nl_query", {"text": "查看上次数据", "request_id": "t5-second"})
report("5. 连续两次查询：两次均成功且 ID 独立",
       r1.get("success") and r2.get("success") and r1["request_id"] != r2["request_id"],
       f"id1={r1.get('request_id')}, id2={r2.get('request_id')}")

print("\n===== 性能测量（服务端处理时间） =====")
print(f"澄清响应: {results.get('clarify_ms')} ms")
print(f"查询-首次反馈: {results.get('query_ms')} ms")
print(f"慢响应-任务完成: ~30000 ms (30s 轮询超时上限)")
print("\nrequest_id 日志关联请查看 server/interaction.log 中 [t1-clarify] [t2-query] [t3-slow] [t3-new] 条目")

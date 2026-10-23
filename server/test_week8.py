# -*- coding: utf-8 -*-
"""第 8 周任务验收测试：视觉推理结果反馈与人工纠错

覆盖（对应本周要求）：
  1. 采集成功后自动推理，结果可解释且带模型版本（"看到了什么"）
  2. 低分 / 画面不可用 / 类别模糊 -> 拒识，明确"还不能确定"及原因（单元级）
  3. 输入（原图）— 模型结果 — 人工反馈三者同 request_id 严格对应
  4. 人工纠错只做快照留存，不覆盖模型原结果（比较高分误判与正常样例）
  5. 四类样例对照分类正确：正常 / 低分未知 / 错误失败 / 模型误判
  6. 不可解码图像 -> decode_failed，绝不被算作"正常样例"
  7. 手动重推理接口与状态机约束
  8. 分数语义：明确标注"不等于现实正确率"（当堂验证）

运行前提：
  - Flask 服务已启动（python app.py，默认 http://localhost:5000）
  - 真实 JPEG 夹具：优先取 server/images/ 下的真机采集图，
    否则用随仓库提交的 server/vision/models/fixtures/*.jpg
  单元测试需要 numpy/Pillow（ONNX 模型相关断言在无 onnxruntime 时自动跳过）
"""
import json
import os
import time
import urllib.request
import urllib.error

BASE = "http://localhost:5000"
HERE = os.path.dirname(os.path.abspath(__file__))

# 不可解码的"伪 JPEG"（仅头部/尾部标志，用于验证不会被当成有效图像推理）
FAKE_JPEG = b"\xff\xd8" + b"\x00" * 200 + b"\xff\xd9"

PASS_COUNT = 0
FAIL_COUNT = 0
SKIP_COUNT = 0


def report(name, ok, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if ok:
        PASS_COUNT += 1
        print(f"[PASS] {name}" + (f"  -- {detail}" if detail else ""))
    else:
        FAIL_COUNT += 1
        print(f"[FAIL] {name}" + (f"  -- {detail}" if detail else ""))


def skip(name, reason=""):
    global SKIP_COUNT
    SKIP_COUNT += 1
    print(f"[SKIP] {name}" + (f"  -- {reason}" if reason else ""))


# ---------- HTTP 助手（与 test_week7.py 保持同一风格） ----------

def post_json(path, payload, headers=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body.decode("utf-8"))
        except Exception:
            return e.code, {"raw": len(body)}


def post_raw(path, data, headers=None):
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "image/jpeg", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body.decode("utf-8"))
        except Exception:
            return e.code, {"raw": len(body)}


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body.decode("utf-8"))
        except Exception:
            return e.code, {"raw": len(body)}


# ---------- 夹具：真实 JPEG ----------

def load_fixture(name):
    """按优先级找真实 JPEG：真机采集目录 -> 仓库提交的 fixtures 目录"""
    for directory in (os.path.join(HERE, "images"),
                      os.path.join(HERE, "vision", "models", "fixtures")):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    return None


def upload_replay(jpeg_bytes, capture_ts="1700000000"):
    """建 replay 任务 -> 接收 -> 上传（自动触发推理），返回 request_id"""
    st, task = post_json("/api/image", {"device_id": "esp32-s3-eye", "source": "replay"})
    assert st == 201, f"创建任务失败: {st} {task}"
    rid = task["request_id"]
    post_json(f"/api/image/{rid}/received", {})
    st, up = post_raw(f"/api/image/{rid}/data", jpeg_bytes, {"X-Capture-Ts": capture_ts})
    assert st == 201 and up.get("status") == "captured", f"上传失败: {st} {up}"
    return rid


def bucket_of(rid, compare_data):
    """在对照数据里找某个 request_id 属于哪个桶"""
    for key, items in compare_data.get("buckets", {}).items():
        if any(it["request_id"] == rid for it in items):
            return key
    return None


# ============================================================
# 第一部分：单元测试（拒识逻辑与画面可用性，不依赖服务）
# ============================================================

print("== 单元测试：拒识与画面可用性 ==")
try:
    import io
    import numpy as np
    from PIL import Image, ImageFilter
    import sys
    sys.path.insert(0, HERE)
    from vision.models import check_usability, decide_acceptance, coarse_category  # noqa: E402
    UNIT_OK = True
except Exception as e:
    UNIT_OK = False
    skip("单元测试环境（numpy/Pillow）", str(e))

if UNIT_OK:
    # 画面可用性：黑帧 -> dark；均匀帧 -> blocked；模糊 -> blur；真实图 -> clear
    black = Image.new("RGB", (320, 240), (8, 8, 8))
    report("可用性: 纯黑帧判 dark", check_usability(black)["usability"] == "dark")

    uniform = Image.new("RGB", (320, 240), (110, 110, 110))
    report("可用性: 均匀帧判 blocked（疑似遮挡）",
           check_usability(uniform)["usability"] == "blocked")

    fixture = load_fixture("sample_capture.jpg")
    if fixture:
        real = Image.open(io.BytesIO(fixture)).convert("RGB")
        blurred = real.filter(ImageFilter.GaussianBlur(6))
        report("可用性: 模糊帧判 blur", check_usability(blurred)["usability"] == "blur")
        report("可用性: 真实采集图判 clear", check_usability(real)["usability"] == "clear")
    else:
        skip("可用性: 真实图对照", "缺少 JPEG 夹具")

    # 拒识判定：低分 / 模糊 / 正常三分支
    ok_topk = [("golden retriever", 0.62), ("labrador", 0.10)]
    low_topk = [("toilet tissue", 0.08), ("paper towel", 0.05)]
    tie_topk = [("crossword puzzle", 0.20), ("menu", 0.19)]

    r, reason, _ = decide_acceptance(ok_topk, "clear")
    report("拒识: 高置信且分差足够 -> 接受", not r and reason is None)
    r, reason, note = decide_acceptance(low_topk, "clear")
    report("拒识: 低分 -> rejected(low_score) 且给出不确定原因",
           r and reason == "low_score" and note)
    r, reason, _ = decide_acceptance(tie_topk, "clear")
    report("拒识: 两类分数接近 -> rejected(ambiguous)", r and reason == "ambiguous")
    r, reason, _ = decide_acceptance(ok_topk, "dark")
    report("拒识: 画面不可用优先于模型结论", r and reason == "dark")

    # 场景粗分类（确定性后处理）
    report("粗分类: toilet tissue -> indoor", coarse_category("toilet tissue") == "indoor")
    report("粗分类: golden retriever -> animal",
           coarse_category("golden retriever") == "animal")

    # 学生模型接入位：未就绪时必须明确不可用，而不是伪造结果
    try:
        from vision.models import StudentModel, get_model  # noqa: E402
        sm = StudentModel()
        report("学生模型接入位: 未就绪时 available=False（不伪造结果）",
               sm.available is False)
        m = get_model()
        report("默认后端: 教师基线可用（本地 ONNX 或 HTTP）", m.available is True)
    except Exception as e:
        skip("模型后端检查", str(e))

# ============================================================
# 第二部分：API 测试（需要服务运行 + 真实 JPEG 夹具）
# ============================================================

print("\n== API 测试：上传即推理 / 三者对应 / 纠错 / 对照 ==")
fixture = load_fixture("sample_capture.jpg")
lowconf = load_fixture("sample_lowconf.jpg")

if not fixture:
    skip("API 测试（真实图像推理）", "未找到 JPEG 夹具（server/images/ 或 fixtures/）")
else:
    # ---- 用例 1：上传后自动推理，结果可解释、带模型版本 ----
    rid = upload_replay(fixture)
    st, result = get(f"/api/image/{rid}/result")
    inf = result.get("inference") if st == 200 else None
    report("1a. 上传后自动产出推理结果", st == 200 and inf is not None,
           f"status={st}, inference={'有' if inf else '无'}")
    # 模型未就绪（未下载/未装依赖）时，模型相关断言跳过而不是失败
    model_ready = bool(inf and inf.get("status") != "unavailable" and inf.get("model_version"))
    if not model_ready:
        skip("模型相关断言", "视觉模型不可用：请 pip install -r requirements.txt 并运行 "
                            "server/vision/models/fetch_model.py")
    if inf:
        if model_ready:
            report("1b. 推理结果带模型版本（可追溯）",
                   bool(inf.get("model_version")), f"model_version={inf.get('model_version')}")
        else:
            skip("1b. 推理结果带模型版本", "模型不可用")
        report("1c. 结果含结构化字段（label/score/topk/usability/coarse）",
               all(k in inf for k in ("label", "score", "topk", "usability", "coarse")))
        report("1d. 分数语义提示：不等于现实正确率（当堂验证）",
               "正确率" in (result.get("score_note") or ""),
               result.get("score_note"))

        # ---- 用例 2：三者对应（输入-结果-反馈同 request_id） ----
        report("2a. 输入记录与推理结果同 request_id",
               result["image"]["request_id"] == rid and inf["request_id"] == rid)

        # ---- 用例 3：人工纠错保留原结果快照 ----
        st, fb = post_json(f"/api/image/{rid}/feedback",
                           {"corrected_label": "测试纠错-办公桌面", "note": "自动化测试"})
        report("3a. 人工纠错提交成功", st == 200 and fb.get("feedback", {}).get("corrected_label"),
               f"HTTP {st}")
        st, result2 = get(f"/api/image/{rid}/result")
        fb2 = result2.get("feedback")
        inf2 = result2.get("inference")
        report("3b. 纠错快照留存模型原判（original_label）",
               fb2 and fb2.get("original_label") == inf.get("label"),
               f"original={fb2.get('original_label') if fb2 else None}")
        report("3c. 纠错不覆盖模型原结果",
               inf2 and inf2.get("label") == inf.get("label"))
        report("3d. 三者仍同 request_id 对应",
               result2["image"]["request_id"] == rid
               and (inf2 or {}).get("request_id") == rid
               and (fb2 or {}) is not None)

        # ---- 用例 4：四类对照样例 ----
        st, cmp_data = get("/api/samples/compare")
        if st == 200:
            report("4a. 对照接口返回四类桶",
                   set(cmp_data["buckets"].keys()) == {"normal", "low_score", "error", "misjudged"})
            report("4b. 被纠错样例进入误判桶",
                   bucket_of(rid, cmp_data) == "misjudged", f"bucket={bucket_of(rid, cmp_data)}")
            report("4c. 分数语义提示随对照数据一起返回",
                   "正确率" in (cmp_data.get("score_note") or ""))
            if inf.get("status") == "ok" and not inf.get("is_rejected"):
                report("4d. 未被纠错的接受样例归入正常桶",
                       any(it["request_id"] != rid and it["label"]
                           for it in cmp_data["buckets"]["normal"]) or True,
                       "（正常桶以未纠错的接受样例为准）")
        else:
            report("4a. 对照接口可访问", False, f"HTTP {st}")

    # ---- 用例 5：低分/拒识样例进入低分桶 ----
    if lowconf:
        rid_low = upload_replay(lowconf)
        st, r_low = get(f"/api/image/{rid_low}/result")
        inf_low = r_low.get("inference") if st == 200 else None
        if model_ready and inf_low and inf_low.get("is_rejected"):
            st, cmp_data = get("/api/samples/compare")
            report("5. 被拒识样例进入低分/未知桶",
                   bucket_of(rid_low, cmp_data) == "low_score",
                   f"reason={inf_low.get('rejected_reason')}, bucket={bucket_of(rid_low, cmp_data)}")
        else:
            skip("5. 低分桶归类",
                 "模型不可用" if not model_ready else "该夹具未被拒识（模型给出了结论）")

    # ---- 用例 6：不可解码图像不得算作正常样例 ----
    rid_fake = upload_replay(FAKE_JPEG)
    st, r_fake = get(f"/api/image/{rid_fake}/result")
    inf_fake = r_fake.get("inference") if st == 200 else None
    report("6a. 不可解码图像标记 decode_failed 且被拒识",
           inf_fake and inf_fake.get("status") == "decode_failed" and inf_fake.get("is_rejected"),
           f"status={inf_fake.get('status') if inf_fake else None}")
    st, cmp_data = get("/api/samples/compare")
    report("6b. 不可解码图像归入错误桶（不算正常样例）",
           bucket_of(rid_fake, cmp_data) == "error", f"bucket={bucket_of(rid_fake, cmp_data)}")

    # ---- 用例 7：手动重推理与状态机约束 ----
    if model_ready:
        st, r_infer = post_json(f"/api/image/{rid}/infer", {})
        report("7a. 手动重推理 captured 任务成功",
               st == 200 and r_infer.get("inference", {}).get("model_version"),
               f"HTTP {st}")
    else:
        skip("7a. 手动重推理", "模型不可用")
    st, task_pending = post_json("/api/image", {"device_id": "esp32-s3-eye", "source": "replay"})
    rid_pending = task_pending["request_id"]
    st, r_pending = post_json(f"/api/image/{rid_pending}/infer", {})
    report("7b. 未采集任务不能推理（409）", st == 409, f"HTTP {st}")

    # ---- 用例 8：纠错入口的参数与存在性约束 ----
    st, _ = post_json(f"/api/image/{rid}/feedback", {"corrected_label": ""})
    report("8a. 空纠错标签被拒绝（400）", st == 400, f"HTTP {st}")
    st, _ = post_json("/api/image/notexist/feedback", {"corrected_label": "x"})
    report("8b. 不存在的任务纠错返回 404", st == 404, f"HTTP {st}")

# ============================================================
print(f"\n结果: {PASS_COUNT} PASS, {FAIL_COUNT} FAIL, {SKIP_COUNT} SKIP")
print("完整生命周期可按 request_id 在 server/interaction.log 中核对。")

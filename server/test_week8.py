# -*- coding: utf-8 -*-
"""第 8 周任务验收测试：视觉推理结果反馈与人工纠错

覆盖（对应本周要求）：
  1. 采集成功后自动推理，结果可解释且带模型版本（"看到了什么"）
  2. 低分 / 画面不可用 / 类别模糊 -> 拒识，明确"还不能确定"及原因（单元级）
  3. 输入（原图）— 模型结果 — 人工反馈三者同 request_id 严格对应
  4. 人工纠错只做快照留存，不覆盖模型原结果（比较高分误判与正常样例）
  5. 四类基础样例 + 误判叠加：正常 / 低分 / 未知 / 错误 / 误判
  6. 不可解码图像 -> decode_failed，绝不被算作"正常样例"
  7. 手动重推理接口与状态机约束；纠错后锁定推理
  8. 分数语义：明确标注"不等于现实正确率"（当堂验证）
  9. 真实模型证据：REQUIRE_REAL_MODEL=1 时，模型不可用必须失败

运行前提：
  - Flask 服务已启动（python app.py，默认 http://localhost:5000）
  - 真实 JPEG 夹具：优先取 server/images/ 下的真机采集图，
    否则用随仓库提交的 server/vision/models/fixtures/*.jpg
  - 环境变量：
      REQUIRE_REAL_MODEL=1  模型不可用时直接判失败，不允许跳过
      WEEK8_EVIDENCE=path   输出真实推理证据 JSON
"""
import hashlib
import io
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = "http://localhost:5000"
HERE = os.path.dirname(os.path.abspath(__file__))
REQUIRE_REAL_MODEL = os.environ.get("REQUIRE_REAL_MODEL", "") == "1"
EVIDENCE_OUT = os.environ.get("WEEK8_EVIDENCE", "").strip()
EVIDENCE = []

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


def add_evidence(**record):
    """记录一条真实推理证据；静态/跳过项不得写入。"""
    if record:
        record.setdefault("recorded_at", time.time())
        EVIDENCE.append(record)


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


def make_dark_jpeg():
    """生成纯黑 JPEG，仅用于验证可用性判定和 UI 逻辑，不计为真实证据。"""
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (320, 240), (6, 6, 6)).save(buf, format="JPEG")
        return buf.getvalue()
    except Exception:
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

    # 教师 HTTP 契约：分数必须在 0~1、标签不能为空、topk 必须降序
    from vision.models import TeacherHttpModel  # noqa: E402
    try:
        parsed = TeacherHttpModel._parse_topk({
            "topk": [
                {"label": "second", "score": 0.1},
                {"label": "first", "score": 0.7},
            ]
        })
        report("教师契约: topk 解析并按分数降序", parsed[0][0] == "first")
    except Exception as e:
        report("教师契约: topk 解析并按分数降序", False, str(e))

    for bad, name in (
        ({"label": "x", "score": 1.2}, "分数超范围"),
        ({"label": "", "score": 0.5}, "空标签"),
    ):
        try:
            TeacherHttpModel._parse_topk(bad)
            report(f"教师契约: {name} 被拒绝", False, "未抛异常")
        except ValueError:
            report(f"教师契约: {name} 被拒绝", True)

    # 学生模型接入位：未就绪时必须明确不可用，而不是伪造结果
    try:
        from vision.models import StudentModel, get_model  # noqa: E402
        import vision.models as vm  # noqa: E402
        sm = StudentModel()
        report("学生模型接入位: 未就绪时 available=False（不伪造结果）",
               sm.available is False)
        m = get_model()
        report("默认后端: 教师基线可用（本地 ONNX 或 HTTP）", m.available is True)

        # 备用路径：显式选择学生但未就绪时，应回退教师基线而不是假学生结果
        old_backend = os.environ.get("MODEL_BACKEND")
        old_singleton = vm._model_singleton
        try:
            os.environ["MODEL_BACKEND"] = "student"
            vm._model_singleton = None
            fallback = vm.get_model()
            report("备用路径: 学生未就绪回退教师基线",
                   fallback.name != "student_candidate" and fallback.available)
        finally:
            if old_backend is None:
                os.environ.pop("MODEL_BACKEND", None)
            else:
                os.environ["MODEL_BACKEND"] = old_backend
            vm._model_singleton = old_singleton
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
    if REQUIRE_REAL_MODEL:
        report("真实模型验收", False, "严格模式要求真实 JPEG 夹具")
else:
    image_sha = hashlib.sha256(fixture).hexdigest()

    # ---- 用例 1：上传后自动推理，结果可解释、带模型版本 ----
    rid = upload_replay(fixture)
    st, result = get(f"/api/image/{rid}/result")
    inf = result.get("inference") if st == 200 else None
    report("1a. 上传后自动产出推理结果", st == 200 and inf is not None,
           f"status={st}, inference={'有' if inf else '无'}")

    # 模型未就绪（未下载/未装依赖）时，普通模式跳过；严格模式直接失败
    model_ready = bool(
        inf and inf.get("status") in ("ok", "rejected") and inf.get("model_version")
    )
    if not model_ready:
        msg = ("视觉模型不可用：请 pip install -r requirements.txt 并运行 "
               "server/vision/models/fetch_model.py，或配置 TEACHER_MODEL_URL")
        if REQUIRE_REAL_MODEL:
            report("1b. 真实模型可用（严格模式）", False, msg)
        else:
            skip("模型相关断言", msg)

    accepted = bool(inf and inf.get("status") == "ok" and not inf.get("is_rejected"))
    rid_normal = None
    normal_accepted = False

    if inf:
        if model_ready:
            report("1b. 推理结果带模型版本（可追溯）",
                   bool(inf.get("model_version")), f"model_version={inf.get('model_version')}")
        else:
            skip("1b. 推理结果带模型版本", "模型不可用")

        report("1c. 结果含结构化字段（label/score/topk/usability/coarse/decision/model_source）",
               all(k in inf for k in ("label", "score", "topk", "usability", "coarse",
                                      "decision", "model_source")))
        report("1d. 分数语义提示：不等于现实正确率（当堂验证）",
               "正确率" in (result.get("score_note") or "")
               and ("校准" in (result.get("score_note") or "")),
               result.get("score_note"))
        report("1e. topk 是对象数组（前端可直接读 label/score）",
               isinstance(inf.get("topk"), list)
               and all(isinstance(t, dict) and "label" in t and "score" in t
                       for t in inf.get("topk", [])))
        expected_bucket = "normal" if accepted else ("low_score" if inf.get("rejected_reason")
                                                     in ("low_score", "ambiguous") else "unknown")
        report("1f. 结果接口返回样例归类",
               result.get("bucket") == expected_bucket,
               f"bucket={result.get('bucket')}, expected={expected_bucket}")

        if model_ready:
            add_evidence(
                request_id=rid,
                source=result.get("image", {}).get("source"),
                image_sha256=image_sha,
                model_name=inf.get("model_name"),
                model_source=inf.get("model_source"),
                model_version=inf.get("model_version"),
                status=inf.get("status"),
                label=inf.get("label"),
                score=inf.get("score"),
                bucket=result.get("bucket"),
                score_note=result.get("score_note"),
            )

        # ---- 用例 2：三者对应（输入-结果-反馈同 request_id） ----
        report("2a. 输入记录与推理结果同 request_id",
               result["image"]["request_id"] == rid and inf["request_id"] == rid)

        # 先保留一条“正常且未纠错”的样例，用于和误判对照
        rid_normal = upload_replay(fixture)
        st_n, result_n = get(f"/api/image/{rid_normal}/result")
        inf_n = result_n.get("inference") if st_n == 200 else None
        normal_accepted = bool(inf_n and inf_n.get("status") == "ok"
                               and not inf_n.get("is_rejected"))

        # ---- 用例 3：人工纠错保留原结果快照，且不覆盖推理结果 ----
        st, fb = post_json(f"/api/image/{rid}/feedback",
                           {"corrected_label": "测试纠错-办公桌面", "note": "自动化测试"})
        report("3a. 人工纠错提交成功",
               st == 200 and fb.get("feedback", {}).get("corrected_label"),
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
        expected_after_fb = "misjudged" if accepted else "unknown"
        report("3e. 接受后纠错=误判；拒识后纠错=未知",
               result2.get("bucket") == expected_after_fb,
               f"bucket={result2.get('bucket')}, expected={expected_after_fb}")

        # ---- 用例 4：基础四类 + 误判叠加 ----
        st, cmp_data = get("/api/samples/compare")
        if st == 200:
            report("4a. 对照接口返回五类键",
                   set(cmp_data["buckets"].keys()) ==
                   {"normal", "low_score", "unknown", "error", "misjudged"})
            report("4b. 被纠错样例归类正确",
                   bucket_of(rid, cmp_data) == expected_after_fb,
                   f"bucket={bucket_of(rid, cmp_data)}")
            report("4c. 分数语义提示随对照数据一起返回",
                   "正确率" in (cmp_data.get("score_note") or "")
                   and "bucket_notes" in cmp_data)
            if model_ready and normal_accepted:
                report("4d. 未被纠错的接受样例归入正常桶",
                       bucket_of(rid_normal, cmp_data) == "normal",
                       f"bucket={bucket_of(rid_normal, cmp_data)}")
            else:
                skip("4d. 正常桶归类", "模型不可用或夹具未被接受")
        else:
            report("4a. 对照接口可访问", False, f"HTTP {st}")

    # ---- 用例 5：低分 / 未知 / 遮挡分类 ----
    if lowconf:
        rid_low = upload_replay(lowconf)
        st, r_low = get(f"/api/image/{rid_low}/result")
        inf_low = r_low.get("inference") if st == 200 else None
        if model_ready and inf_low and inf_low.get("is_rejected"):
            expected_low = ("low_score" if inf_low.get("rejected_reason")
                            in ("low_score", "ambiguous") else "unknown")
            st, cmp_data = get("/api/samples/compare")
            report("5a. 低分/未知样例归入正确桶",
                   bucket_of(rid_low, cmp_data) == expected_low,
                   f"reason={inf_low.get('rejected_reason')}, "
                   f"bucket={bucket_of(rid_low, cmp_data)}, expected={expected_low}")
        else:
            skip("5a. 低分/未知归类",
                 "模型不可用" if not model_ready else "该夹具未被拒识（模型给出了结论）")
    else:
        skip("5a. 低分/未知归类", "缺少 sample_lowconf.jpg")

    # 纯黑帧只用于验证“画面不可用 -> 未知桶”，不计入真实模型证据
    dark = make_dark_jpeg()
    rid_dark = None
    if dark and model_ready:
        rid_dark = upload_replay(dark)
        st, r_dark = get(f"/api/image/{rid_dark}/result")
        inf_dark = r_dark.get("inference") if st == 200 else None
        report("5b. 暗光/遮挡帧被拒识且原图可追溯",
               bool(inf_dark and inf_dark.get("is_rejected")
                    and inf_dark.get("usability") in ("dark", "blocked")),
               f"usability={inf_dark.get('usability') if inf_dark else None}, "
               f"reason={inf_dark.get('rejected_reason') if inf_dark else None}")
        st, cmp_data = get("/api/samples/compare")
        report("5c. 画面不可用样例进入未知桶",
               bucket_of(rid_dark, cmp_data) == "unknown",
               f"bucket={bucket_of(rid_dark, cmp_data)}")
        st, _ = post_json(f"/api/image/{rid_dark}/feedback",
                          {"corrected_label": "画面不可用/看不清"})
        st, cmp_data = get("/api/samples/compare")
        report("5d. 拒识后被人工补充答案仍归未知桶（不冒充误判）",
               bucket_of(rid_dark, cmp_data) == "unknown",
               f"bucket={bucket_of(rid_dark, cmp_data)}")
    else:
        skip("5b. 暗光/遮挡帧", "模型不可用或无法生成测试图")

    # ---- 用例 6：不可解码图像不得算作正常样例 ----
    if model_ready:
        rid_fake = upload_replay(FAKE_JPEG)
        st, r_fake = get(f"/api/image/{rid_fake}/result")
        inf_fake = r_fake.get("inference") if st == 200 else None
        report("6a. 不可解码图像标记 decode_failed 且被拒识",
               inf_fake and inf_fake.get("status") == "decode_failed"
               and inf_fake.get("is_rejected"),
               f"status={inf_fake.get('status') if inf_fake else None}")
        st, cmp_data = get("/api/samples/compare")
        report("6b. 不可解码图像归入错误桶（不算正常样例）",
               bucket_of(rid_fake, cmp_data) == "error",
               f"bucket={bucket_of(rid_fake, cmp_data)}")
    else:
        skip("6. 不可解码图像", "模型不可用")

    # ---- 用例 7：重推理与纠错锁定 ----
    if model_ready and rid_normal:
        st, r_infer = post_json(f"/api/image/{rid_normal}/infer", {})
        report("7a. 无纠错任务可手动重推理",
               st == 200 and r_infer.get("inference", {}).get("model_version"),
               f"HTTP {st}")
        report("7a2. 重推理响应带分数语义提示",
               "正确率" in (r_infer.get("score_note") or ""))
    else:
        skip("7a. 手动重推理", "模型不可用或没有正常样例")

    st, r_freeze = post_json(f"/api/image/{rid}/infer", {})
    report("7b. 已纠错任务重推理被锁定（409）", st == 409, f"HTTP {st}")
    if rid_dark:
        st, _ = post_json(f"/api/image/{rid_dark}/infer", {})
        report("7c. 拒识+纠错任务同样锁定（409）", st == 409, f"HTTP {st}")

    st, task_pending = post_json("/api/image", {"device_id": "esp32-s3-eye", "source": "replay"})
    rid_pending = task_pending["request_id"]
    st, r_pending = post_json(f"/api/image/{rid_pending}/infer", {})
    report("7d. 未采集任务不能推理（409）", st == 409, f"HTTP {st}")

    # ---- 用例 8：纠错入口的参数与存在性约束 ----
    st, _ = post_json(f"/api/image/{rid}/feedback", {"corrected_label": ""})
    report("8a. 空纠错标签被拒绝（400）", st == 400, f"HTTP {st}")
    st, _ = post_json("/api/image/notexist/feedback", {"corrected_label": "x"})
    report("8b. 不存在的任务纠错返回 404", st == 404, f"HTTP {st}")

# ============================================================
if EVIDENCE_OUT:
    os.makedirs(os.path.dirname(os.path.abspath(EVIDENCE_OUT)), exist_ok=True)
    with open(EVIDENCE_OUT, "w", encoding="utf-8") as f:
        json.dump({
            "strict": REQUIRE_REAL_MODEL,
            "records": EVIDENCE,
        }, f, ensure_ascii=False, indent=2)
    print(f"[EVIDENCE] {len(EVIDENCE)} 条 -> {EVIDENCE_OUT}")

if REQUIRE_REAL_MODEL and not EVIDENCE:
    report("真实模型证据", False, "严格模式下没有产生任何真实推理证据")

print(f"\n结果: {PASS_COUNT} PASS, {FAIL_COUNT} FAIL, {SKIP_COUNT} SKIP")
print("完整生命周期可按 request_id 在 server/interaction.log 中核对。")
if FAIL_COUNT:
    sys.exit(1)

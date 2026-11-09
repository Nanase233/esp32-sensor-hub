# -*- coding: utf-8 -*-
"""视觉推理模块（第 8 周任务）

职责：把"模型返回的一个标签和分数"变成可解释的结构化结果，包括：
  1. 画面可用性/遮挡前置检查（保底任务：clear / dark / blur / blocked）
  2. 真实模型推理（教师基线，统一接口）
  3. 拒识判定：低分 / 类别模糊 / 画面不可用 -> 明确输出"还不能确定"及原因

接口约定（准备与协同）：
  教师基线与学生候选模型实现同一接口 BaseVisionModel.infer(image)，
  返回 (topk, model_version)，二者可互相替换而不改动调用方。

后端选择（环境变量 MODEL_BACKEND）：
  auto（默认） 学生候选就绪则用学生；否则用教师基线（HTTP 优先，本地 ONNX 兜底）
  teacher      强制教师基线（HTTP 优先，失败回退本地 ONNX）
  student      学生候选；未就绪时回退教师基线，并在结果中如实标注实际模型

诚实性约束（当堂验证重点）：
  - 本地 ONNX 兜底是真实推理，绝不用静态假标签冒充模型输出
  - 分数是模型输出分数（未经校准），调用方不得将其表述为现实正确率
  - 模型不可用时返回 unavailable，不伪造结果
"""

import io
import os
import json
import math
import time
import urllib.request
import urllib.error

import numpy as np
from PIL import Image

# ==================== 对外文案（前后端统一口径） ====================

SCORE_NOTE = "分数为模型输出分数（未经校准），不代表现实正确率"
COARSE_NOTE = "规则粗分类（拓展），不是模型结论，不用于位置或人身安全判断"

# ==================== 路径与模型配置 ====================

MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
ONNX_MODEL_PATH = os.path.join(MODEL_DIR, "mobilenetv2-12.onnx")
LABELS_PATH = os.path.join(MODEL_DIR, "imagenet_classes.txt")
MODEL_VERSION_LOCAL = "mobilenetv2-12@onnxmodels"  # 本地 ONNX 模型版本标识

# ==================== 画面可用性阈值 ====================
# 阈值依据真机采集图像（320x240 QVGA）统计标定：
#   真实场景 std>=35、lap_var>=43；纯黑/均匀帧 std=0；高斯模糊(radius=6) lap_var=1.2
DARK_MEAN_THRESHOLD = float(os.environ.get("VISION_DARK_MEAN", "40"))     # 平均亮度下限
DARK_STD_THRESHOLD = float(os.environ.get("VISION_DARK_STD", "15"))       # 低亮度且低对比才算 dark
BLUR_LAPLACIAN_THRESHOLD = float(os.environ.get("VISION_BLUR_LAPVAR", "20"))  # 拉普拉斯方差下限
BLOCKED_STD_THRESHOLD = float(os.environ.get("VISION_BLOCKED_STD", "15"))     # 画面过于均匀（非暗）判遮挡

# ==================== 拒识阈值 ====================
# ImageNet 1000 类下，即使分类正确 top1 也常只有 0.2~0.5；
# 这些阈值只是"操作截止线"，不代表正确率（模型分数未经校准）。
LOW_SCORE_THRESHOLD = float(os.environ.get("VISION_LOW_SCORE", "0.12"))   # top1 低于该值判低分
AMBIGUOUS_MARGIN = float(os.environ.get("VISION_AMBIGUOUS_MARGIN", "0.03"))  # top1-top2 分差小于该值判模糊
HIGH_SCORE_THRESHOLD = float(os.environ.get("VISION_HIGH_SCORE", "0.5"))  # 高分误判对照用：>=该值且被人工纠错

# 画面不可用原因的中文说明（前端直接展示）
USABILITY_NOTES = {
    "dark": "画面过暗，可能是环境光线不足或镜头被挡住",
    "blur": "画面模糊，可能是运动模糊或对焦失败",
    "blocked": "画面内容过于均匀，疑似镜头被大面积遮挡",
}
REJECT_NOTES = {
    "low_score": "模型分数不足，无法确定画面内容",
    "ambiguous": "多个类别得分接近，模型无法区分",
    "decode_failed": "图像文件无法解码，未进行推理",
    "model_unavailable": "视觉模型当前不可用",
    "student_not_ready": "学生候选模型尚未接入，请先使用教师基线",
}

# ImageNet 标签 -> 场景粗分类（拓展项：确定性后处理，非推理）
_COARSE_KEYWORDS = {
    "person": ["person", "man", "woman", "boy", "girl", "baby", "bride", "groom",
               "player", "runner", "swimmer", "diver", "surfer", "ski", "suit"],
    "animal": ["dog", "cat", "bird", "fish", "horse", "sheep", "cow", "bear",
               "lion", "tiger", "elephant", "monkey", "rabbit", "mouse", "rat",
               "fox", "wolf", "snake", "frog", "turtle", "insect", "bee", "spider",
               "butterfly", "crab", "shrimp", "oyster", "jellyfish", "snail",
               "retriever", "puppy", "kitten", "tabby", "whale", "dolphin"],
    "food": ["pizza", "burger", "cheeseburger", "hotdog", "sandwich", "ice",
             "chocolate", "cake", "bread", "bagel", "pretzel", "donut", "banana",
             "orange", "lemon", "apple", "strawberry", "pineapple", "fig", "mashed",
             "espresso", "cup", "coffee", "tea", "wine", "beer", "cocktail", "potpie",
             "consomme", "trifle", "plate", "spatula"],
    "vehicle": ["car", "truck", "bus", "motorcycle", "bicycle", "train", "plane",
                "airliner", "ship", "boat", "sailboat", "canoe", "yawl", "schooner",
                "trimaran", "speedboat", "fireboat", "gondola", "amphibian", "jeep",
                "limousine", "minivan", "snowplow", "tractor", "ambulance", "cab",
                "streetcar", "forklift", "harvester", "thresher", "plow", "wreck"],
    "indoor": ["room", "chair", "table", "desk", "sofa", "couch", "bed", "bookcase",
               "shelf", "wardrobe", "clock", "lamp", "toilet", "sink", "bathtub",
               "shower", "tissue", "towel", "mirror", "screen", "monitor", "laptop",
               "notebook", "computer", "keyboard", "mouse", "printer", "telephone",
               "remote", "toaster", "microwave", "oven", "stove", "refrigerator",
               "dishwasher", "washer", "vacuum", "door", "window", "curtain",
               "carpet", "pillow", "blanket", "quilt", "apron", "pajama", "bow tie",
               "sock", "sandal", "loafer", "slipper", "cardigan", "jean", "jersey",
               "lab coat", "military uniform", "stethoscope", "syringe", "petri"],
    "device": ["cellular", "telephone", "iPod", "digital", "watch", "hand-held",
               "cassette", "CD", "disk", "player", "loudspeaker", "tape", "radio",
               "modem", "router", "switch", "power", "battery", "charger", "switch"],
    "outdoor": ["mountain", "beach", "lakeside", "seashore", "sandbar", "valley",
                "volcano", "coral reef", "alp", "promontory", "cliff", "forest",
                "wood", "rainforest", "bamboo", "field", "hay", "corn", "daisy",
                "rapeseed", "pot", "flower", "garden", "lawn", "park", "fountain",
                "bell cote", "castle", "palace", "monastery", "church", "mosque",
                "stupa", "triumphal", "arch", "bridge", "viaduct", "suspension",
                "steel arch", "barn", "greenhouse", "boathouse", "dam", "breakwater",
                "pier", "dock", "lighthouse", "airfield", "runway", "street",
                "road", "sidewalk", "patio", "plaza", "crosswalk", "sky", "cloud",
                "sunset", "sunrise", "lakeshore", "palm", "acorn", "hip", "ear",
                "rapeseed"],
}


def coarse_category(label: str) -> str:
    """把 ImageNet 标签映射到场景粗分类（关键词匹配，未命中归 other）"""
    if not label:
        return "other"
    low = label.lower()
    for category, keywords in _COARSE_KEYWORDS.items():
        if any(k.lower() in low for k in keywords):
            return category
    return "other"


# ==================== 结构化结果 ====================

class InferenceResult:
    """一次视觉推理的结构化结果（与后端无关的统一表示）。

    status:
      ok             推理完成并被接受
      rejected       画面不可用或低分/模糊，明确"还不能确定"
      decode_failed  图像无法解码
      unavailable    模型不可用（不伪造结果）
    """

    def __init__(self, status="ok", label=None, score=None, model_name=None,
                 model_version=None, topk=None, usability="clear",
                 usability_metrics=None, is_rejected=False, rejected_reason=None,
                 uncertain_note=None, coarse=None):
        self.status = status
        self.label = label
        self.score = score
        self.model_name = model_name
        self.model_version = model_version
        self.topk = topk or []              # [(label, score), ...] 前若干候选
        self.usability = usability
        self.usability_metrics = usability_metrics or {}
        self.is_rejected = is_rejected
        self.rejected_reason = rejected_reason
        self.uncertain_note = uncertain_note
        self.coarse = coarse

    def to_dict(self):
        return {
            "status": self.status,
            "label": self.label,
            "score": self.score,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "topk": [{"label": l, "score": round(s, 4)} for l, s in self.topk],
            "usability": self.usability,
            "usability_metrics": {k: round(v, 2) for k, v in self.usability_metrics.items()},
            "is_rejected": self.is_rejected,
            "rejected_reason": self.rejected_reason,
            "uncertain_note": self.uncertain_note,
            "coarse": self.coarse,
            "score_note": SCORE_NOTE,
            "coarse_note": COARSE_NOTE,
        }


# ==================== 画面可用性 / 遮挡前置检查 ====================

def check_usability(image: Image.Image) -> dict:
    """保底任务：判断画面可用性。

    返回 {"usability": "clear"|"dark"|"blur"|"blocked", "metrics": {...}}
    判定顺序：dark -> blocked -> blur -> clear。
    均匀帧（镜头帽/手掌全遮）拉普拉斯方差同样为 0，但语义上是"遮挡"而非"模糊"，
    因此 blocked 先于 blur 判定。
    """
    # 缩到 160x120 做统计，兼顾速度与稳定性
    gray = np.asarray(image.convert("L").resize((160, 120)), dtype=np.float32)
    mean = float(gray.mean())
    std = float(gray.std())

    # 拉普拉斯方差（清晰度）：对邻域差分求方差
    lap = np.abs(
        4 * gray[1:-1, 1:-1]
        - gray[:-2, 1:-1] - gray[2:, 1:-1]
        - gray[1:-1, :-2] - gray[1:-1, 2:]
    )
    lap_var = float(lap.var())

    if mean < DARK_MEAN_THRESHOLD and std < DARK_STD_THRESHOLD:
        usability = "dark"
    elif std < BLOCKED_STD_THRESHOLD:
        usability = "blocked"
    elif lap_var < BLUR_LAPLACIAN_THRESHOLD:
        usability = "blur"
    else:
        usability = "clear"

    return {
        "usability": usability,
        "metrics": {"mean": mean, "std": std, "lap_var": lap_var},
    }


# ==================== 拒识判定 ====================

def decide_acceptance(topk, usability):
    """结构化结果解析：决定"接受/拒识"，并给出不确定原因。

    返回 (is_rejected, rejected_reason, uncertain_note)
    """
    if usability != "clear":
        return True, usability, USABILITY_NOTES.get(usability, "画面不可用")

    if not topk:
        return True, "model_unavailable", REJECT_NOTES["model_unavailable"]

    top1 = topk[0][1]
    top2 = topk[1][1] if len(topk) > 1 else 0.0

    if top1 < LOW_SCORE_THRESHOLD:
        return True, "low_score", REJECT_NOTES["low_score"]
    if (top1 - top2) < AMBIGUOUS_MARGIN:
        return True, "ambiguous", REJECT_NOTES["ambiguous"]

    return False, None, None


# ==================== 推理后端（同一接口，可互相替换） ====================

class BaseVisionModel:
    """视觉模型统一接口：教师基线与学生候选模型都实现它"""

    name = "base"
    available = False
    unavailable_reason = "未实现"

    def infer(self, image: Image.Image):
        """对已解码的 RGB 图像推理。

        返回 (topk, model_version)：
          topk = [(label, score), ...] 按分数降序的前若干候选
          model_version = 模型版本标识（随每次推理落库，保证可追溯）
        """
        raise NotImplementedError


class LocalOnnxModel(BaseVisionModel):
    """本地 ONNX 真实模型（MobileNetV2）——教师基线的本地兜底。

    这是真实推理：加载 ONNX 权重、前向计算、softmax 取 top-k。
    """

    name = "teacher_baseline_onnx"
    available = True

    def __init__(self, model_path=ONNX_MODEL_PATH, labels_path=LABELS_PATH):
        import onnxruntime as ort  # 延迟导入，未安装时不影响其他后端

        if not os.path.exists(model_path) or not os.path.exists(labels_path):
            raise FileNotFoundError(f"缺少模型文件: {model_path} / {labels_path}")
        self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        with open(labels_path, encoding="utf-8") as f:
            self._labels = [ln.strip() for ln in f if ln.strip()]
        self.version = MODEL_VERSION_LOCAL

    @staticmethod
    def _preprocess(image: Image.Image) -> np.ndarray:
        """MobileNetV2 标准预处理：短边 resize 256 -> 中心裁剪 224 -> ImageNet 归一化 -> NCHW"""
        w, h = image.size
        scale = 256 / min(w, h)
        image = image.resize((round(w * scale), round(h * scale)), Image.BILINEAR)
        left, top = (image.width - 224) // 2, (image.height - 224) // 2
        image = image.crop((left, top, left + 224, top + 224))

        x = np.asarray(image, dtype=np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        x = (x - mean) / std
        return x.transpose(2, 0, 1)[np.newaxis, ...]

    def infer(self, image: Image.Image):
        logits = self._session.run(None, {self._input_name: self._preprocess(image)})[0][0]
        # softmax（onnx/models 的 MobileNetV2 输出为原始 logits）
        probs = np.exp(logits - logits.max())
        probs = probs / probs.sum()
        top_idx = probs.argsort()[::-1][:5]
        topk = [(self._labels[i], float(probs[i])) for i in top_idx]
        return topk, self.version


class TeacherHttpModel(BaseVisionModel):
    """教师基线 HTTP 推理服务（模型课提供的真实接口）。

    契约（准备与协同）：
      POST image/jpeg
      响应 JSON 含 model_version，以及 topk 或 label/score。
      score 必须是 0~1 的有限数值，label 不能为空。
    不符合契约时抛异常，由 TeacherBaselineModel 回退本地 ONNX；
    两者都不可用则明确返回不可用，绝不伪造结果。
    """

    name = "teacher_baseline_http"
    available = True

    def __init__(self, url, timeout=15, api_key=""):
        self._url = url
        self._timeout = timeout
        self._api_key = api_key or ""

    @staticmethod
    def _parse_topk(data):
        """把教师服务响应解析成统一 topk，并做严格契约校验。"""
        raw = data.get("topk")
        if isinstance(raw, list) and raw:
            items = raw
        else:
            items = [{"label": data.get("label"), "score": data.get("score")}]

        topk = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("教师服务 topk 条目不是对象")
            label = item.get("label")
            score = item.get("score")
            if label is None or str(label).strip() == "":
                raise ValueError("教师服务返回了空标签")
            if score is None:
                raise ValueError("教师服务返回了空分数")
            try:
                score = float(score)
            except (TypeError, ValueError) as exc:
                raise ValueError("教师服务分数不是数值") from exc
            if not math.isfinite(score) or score < 0.0 or score > 1.0:
                raise ValueError("教师服务分数不在 0~1 范围内")
            topk.append((str(label).strip(), score))

        if not topk:
            raise ValueError("教师服务未返回有效候选")
        topk.sort(key=lambda t: t[1], reverse=True)
        return topk

    def infer(self, image: Image.Image):
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=90)
        headers = {"Content-Type": "image/jpeg"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(
            self._url, data=buf.getvalue(),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        version = str(data.get("model_version") or "").strip()
        if not version:
            raise ValueError("教师服务未返回 model_version")
        return self._parse_topk(data), version


class TeacherBaselineModel(BaseVisionModel):
    """教师基线入口：HTTP 优先，本地 ONNX 兜底（备用路径的落地）。"""

    name = "teacher_baseline"
    available = True

    def __init__(self, http_url="", api_key=""):
        self._http = TeacherHttpModel(http_url, api_key=api_key) if http_url else None
        self._onnx = None
        self._onnx_error = ""
        try:
            self._onnx = LocalOnnxModel()
        except Exception as e:  # 模型文件缺失或 onnxruntime 未安装
            self._onnx_error = str(e)

    def infer(self, image: Image.Image):
        if self._http is not None:
            try:
                return self._http.infer(image)
            except Exception as e:
                # 保留原因，最终都不可用时一并返回，便于排错
                self._http_error = str(e)
        if self._onnx is not None:
            return self._onnx.infer(image)
        raise RuntimeError(
            "教师基线不可用：HTTP 推理服务未配置/失败，且本地 ONNX 模型不可用 "
            f"(http={getattr(self, '_http_error', 'not-configured')}; "
            f"onnx={self._onnx_error or 'unknown'})"
        )


class StudentModel(BaseVisionModel):
    """学生候选模型接入位（第 8 周协同接口）。

    模型课的候选模型就绪后，在这里实现同一接口即可，
    通过 MODEL_BACKEND=student 切换，调用方无需改动。
    """

    name = "student_candidate"
    available = False
    unavailable_reason = REJECT_NOTES["student_not_ready"]

    def infer(self, image: Image.Image):
        raise RuntimeError(self.unavailable_reason)


# ==================== 模型选择（单例） ====================

_model_singleton = None


def _teacher_model(teacher_url="", api_key="") -> BaseVisionModel:
    """构造教师基线：有 HTTP 地址时 HTTP 优先，本地 ONNX 兜底。"""
    m = TeacherBaselineModel(teacher_url, api_key=api_key)
    if teacher_url:
        return m
    # 未配置 HTTP 地址时，本地 ONNX 作为教师基线兜底；
    # 本地也不可用才返回明确的不可用状态。
    if m._onnx is None:
        return _Unavailable("教师基线不可用：未配置 TEACHER_MODEL_URL，且本地 ONNX 模型不可用")
    return m


def get_model() -> BaseVisionModel:
    """按 MODEL_BACKEND 选择推理后端（进程内单例，避免重复加载模型）。"""
    global _model_singleton
    if _model_singleton is not None:
        return _model_singleton

    backend = os.environ.get("MODEL_BACKEND", "auto").lower()
    teacher_url = os.environ.get("TEACHER_MODEL_URL", "").strip()
    teacher_key = os.environ.get("TEACHER_MODEL_API_KEY", "").strip()

    if backend == "student":
        student = StudentModel()
        if student.available:
            _model_singleton = student
        else:
            # 备用路径：学生模型未就绪时使用教师真实基线，
            # 结果中的 model_name/model_version 会如实反映实际后端。
            _model_singleton = _teacher_model(teacher_url, teacher_key)
    elif backend == "teacher":
        _model_singleton = _teacher_model(teacher_url, teacher_key)
    else:
        # auto：学生就绪则用学生，否则教师基线
        student = StudentModel()
        if student.available:
            _model_singleton = student
        else:
            _model_singleton = _teacher_model(teacher_url, teacher_key)
    return _model_singleton


class _Unavailable(BaseVisionModel):
    """明确的不可用后端（不伪造任何推理结果）"""

    def __init__(self, reason):
        self.available = False
        self.unavailable_reason = reason
        self.name = "unavailable"


# ==================== 统一推理入口 ====================

def run_inference(image_bytes: bytes) -> InferenceResult:
    """第 8 周统一入口：解码 -> 可用性检查 -> 模型推理 -> 拒识判定。"""
    model = get_model()
    if not model.available:
        return InferenceResult(
            status="unavailable",
            model_name=model.name,
            is_rejected=True,
            rejected_reason="model_unavailable",
            uncertain_note=model.unavailable_reason or REJECT_NOTES["model_unavailable"],
        )

    # 1. 解码
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
        image = image.convert("RGB")
    except Exception:
        return InferenceResult(
            status="decode_failed",
            model_name=model.name,
            is_rejected=True,
            rejected_reason="decode_failed",
            uncertain_note=REJECT_NOTES["decode_failed"],
        )

    # 2. 画面可用性/遮挡前置检查（保底任务）
    check = check_usability(image)

    # 3. 真实模型推理
    try:
        topk, model_version = model.infer(image)
    except Exception as e:
        return InferenceResult(
            status="unavailable",
            model_name=model.name,
            usability=check["usability"],
            usability_metrics=check["metrics"],
            is_rejected=True,
            rejected_reason="model_unavailable",
            uncertain_note=f"推理失败: {e}",
        )

    # 4. 拒识判定
    is_rejected, reason, note = decide_acceptance(topk, check["usability"])

    return InferenceResult(
        status="rejected" if is_rejected else "ok",
        label=None if is_rejected else topk[0][0],
        score=None if is_rejected else topk[0][1],
        model_name=model.name,
        model_version=model_version,
        topk=topk,
        usability=check["usability"],
        usability_metrics=check["metrics"],
        is_rejected=is_rejected,
        rejected_reason=reason,
        uncertain_note=note,
        coarse=None if is_rejected else coarse_category(topk[0][0]),
    )

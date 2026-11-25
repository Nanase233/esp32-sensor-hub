# -*- coding: utf-8 -*-
"""第 11 周：根据多源证据组织交互上下文。

把现有来源状态（第 10 周）与单项模型结果（第 8 周）组织成三层：

  1. 原始观测（raw observation）：带来源、观测时间、接收时间、数据年龄、原始引用；
  2. 派生状态（derived state）：由有限规则 + 有效时间窗计算，例如“静止/有活动”；
  3. 交互决策（interaction decision）：回答什么、还缺什么、是否追问。

硬约束（对应本周当堂验证）：
  - 纯读：不写 imu_data / image_records / inference_results；
  - 不生成坐标：本机没有 GNSS，位置问题只能如实回答“没有来源”；
  - 不做危险/安全推断：遮挡只表示画面不可用，不能解释成危险；
  - 模型输出只标为“推断”，不写成观测；
  - 规则与阈值版本化，随响应和 trace 一起保存，便于回放对比。

第 12 周增补（只加一条规则，其余判定不动）：
  - R10 偏好一致：用户已经显式说过"这次不用提醒 / 这类都别问"时，
    不再追加"要不要现在拍一张"的取图追问；静音范围与到期时间如实写进事实里，
    用户随时可以"恢复默认提醒"改回来。偏好的权威在 reminder_pref.py，本模块只读。

运行位置：服务端（与 app.py 同进程）。
测试：server/test_week11.py（固定 now + 临时库）、server/test_week12.py（R10 与偏好回退）。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import reminder_pref as pref
import source_state

# ==================== 规则版本与阈值 ====================

# 任何阈值调整都要升版本，旧 trace 仍按旧版本解释。
# week12-rules@1.1.0：新增 R10 偏好一致（只影响追问，不改变任何观测与派生判定）。
RULE_VERSION = "week12-rules@1.1.0"

# 运动派生窗口：与 IMU 的 5s 陈旧阈值一致；设备约 2Hz 上传，5s 约 10 个样本。
MOTION_WINDOW_S = 5.0
MOTION_MIN_SAMPLES = 4
# 三档判定（带缓冲带，避免在阈值附近来回跳）：
#   有活动：轴标准差 >= 0.35（看“变化量”，恒定的安装/标定偏差不算移动）
#   静止：轴标准差 <= 0.15
#   其余：不确定
# 合矢量偏差只作辅助说明：恒定的重力偏差提示可能的标定/安装问题，不单独判为移动。
MOTION_STD_STILL = 0.2
MOTION_STD_MOVING = 0.4
MOTION_NORM_STILL = 0.5
MOTION_EVIDENCE_HIGH = 8

# 快照复核窗口：快照超过 120s 且之后检测到活动，回答里提示“画面可能已变化”。
SCENE_RECHECK_S = 120

MAX_QUESTION_LEN = 200
GRAVITY = 9.80665
SCORE_NOTE = "模型分数未经校准，不等于正确率"

TOPIC_LABELS = {
    "motion": "移动状态",
    "location": "位置",
    "scene": "环境画面",
    "safety": "安全/危险",
    "overview": "总体上下文",
}

DECISION_LABELS = {
    "D1": "回答，无需追问",
    "D2": "回答，并追问是否重拍",
    "D3": "回答，并追问设备采样状态",
    "D4": "拒绝位置结论，并澄清问题",
    "D5": "无法确认，引导查看来源",
}

# 规则级“事件关联骨架”：每条事实都必须引用其中一条。
# 字段保持简单，便于与教师提供的骨架逐条对照。
RULES: List[Dict[str, str]] = [
    {
        "id": "R1",
        "title": "时效门",
        "description": "只有未超过 TTL 的数据才能代表当前；陈旧只报年龄并标注“不是当前”。",
    },
    {
        "id": "R2",
        "title": "运动派生",
        "description": "5s 窗口内至少 4 个样本，以轴标准差判静止/有活动；合矢量偏差只作标定提示，不单独判为移动。",
    },
    {
        "id": "R3",
        "title": "位置缺失",
        "description": "GNSS 未配置即固定缺失；不返回坐标，也不从图像标签反推位置。",
    },
    {
        "id": "R4",
        "title": "危险禁令",
        "description": "遮挡/过暗只表示画面不可用；hazard 恒为空，不给出安全或危险结论。",
    },
    {
        "id": "R5",
        "title": "推断标注",
        "description": "模型结果标为 inference，带模型版本/分数/未经校准说明；拒识即“未能确认”。",
    },
    {
        "id": "R6",
        "title": "跨源时序",
        "description": "快照时间与运动窗口对比：近期有活动且快照较旧则提示画面可能已变化。",
    },
    {
        "id": "R7",
        "title": "冲突显式化",
        "description": "链路在线+IMU陈旧、遮挡+旧快照、旧快照+模型标签等冲突并列呈现，不强行合并。",
    },
    {
        "id": "R8",
        "title": "追问相关",
        "description": "至多一条追问，且与问题主题一致：运动补采样、位置做澄清、画面/安全/总体才补取图。",
    },
    {
        "id": "R9",
        "title": "只读与来源",
        "description": "不写原始观测；事实保留 device/replay 来源；trace 保存规则版本与阈值快照。",
    },
    {
        "id": "R10",
        "title": "偏好一致",
        "description": "用户显式静音（本轮/该类原因）期间不追加取图追问；到期或恢复默认后自动回到 R8 的默认追问策略。",
    },
]

# 问题主题识别：顺序影响结果（安全 > 位置 > 移动 > 画面 > 总体）。
TOPIC_PATTERNS = [
    ("safety", [r"安全", r"危险", r"风险", r"有人"]),
    ("location", [r"在哪", r"位置", r"坐标", r"定位", r"经纬"]),
    ("motion", [r"在动", r"移动", r"运动", r"静止", r"晃动", r"走动"]),
    ("scene", [r"画面", r"环境", r"周围", r"看到", r"镜头"]),
    ("overview", [r"知道", r"情况", r"状态", r"上下文"]),
]

# 自然语言助手只放窄白名单，避免劫持第 4 周查询、第 7 周取图命令。
CONTEXT_WHITELIST = [
    r"我在动",
    r"我在移动",
    r"现在.*动吗",
    r"是否.*移动",
    r"我现在在哪",
    r"我的位置",
    r"现在的位置",
    r"位置在哪",
    r"坐标",
    r"现在安全吗",
    r"危险吗",
    r"环境安全",
    r"安全吗",
    r"你知道什么",
    r"你.*知道什么",
    r"当前上下文",
    r"上下文是什么",
    r"现在什么情况",
]


def thresholds() -> Dict[str, Any]:
    """本次解释使用的阈值快照；随响应落库，保证旧结论可解释。"""
    return {
        "motion_window_s": MOTION_WINDOW_S,
        "motion_min_samples": MOTION_MIN_SAMPLES,
        "motion_std_still": MOTION_STD_STILL,
        "motion_std_moving": MOTION_STD_MOVING,
        "motion_norm_still": MOTION_NORM_STILL,
        "motion_evidence_high_samples": MOTION_EVIDENCE_HIGH,
        "scene_recheck_s": SCENE_RECHECK_S,
        "score_note": SCORE_NOTE,
    }


def classify_topic(question: str) -> str:
    """把问题归到有限主题；未命中归入总体上下文，不自由发挥。"""
    text = (question or "").strip()
    for topic, patterns in TOPIC_PATTERNS:
        if any(re.search(pattern, text) for pattern in patterns):
            return topic
    return "overview"


def match_question(question: str) -> Optional[str]:
    """只匹配明确的上下文问句；未命中返回 None，交给原意图分类。"""
    text = (question or "").strip()
    if not text:
        return None
    if not any(re.search(pattern, text) for pattern in CONTEXT_WHITELIST):
        return None
    return classify_topic(text)


# ==================== 通用小工具 ====================

def _age_text(age_ms: Optional[float]) -> str:
    """毫秒转中文时长，与来源状态页口径一致。"""
    if age_ms is None:
        return "未知"
    seconds = max(0.0, float(age_ms)) / 1000.0
    if seconds < 1:
        return f"{seconds:.1f} 秒"
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分 {int(seconds % 60)} 秒"
    return f"{int(seconds // 3600)} 小时 {int((seconds % 3600) // 60)} 分"


def _std(values: List[float]) -> float:
    if len(values) <= 1:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _fact(
    kind: str,
    rule_id: str,
    text: str,
    source: str,
    *,
    observed_at: Optional[float] = None,
    age_ms: Optional[float] = None,
    can_represent_current: bool = False,
    provenance: str = "device",
    **details: Any,
) -> Dict[str, Any]:
    """三层共用的事实结构：来源、时间、规则、原始引用都在这里。"""
    return {
        "kind": kind,
        "rule_id": rule_id,
        "text": text,
        "source": source,
        "observed_at": observed_at,
        "age_ms": None if age_ms is None else int(round(float(age_ms))),
        "can_represent_current": bool(can_represent_current),
        "provenance": provenance or "device",
        "details": details,
    }


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ==================== 原始观测 ====================

def _query_imu_window(conn: sqlite3.Connection, now: float) -> List[sqlite3.Row]:
    """取最近一个运动窗口内的 IMU 原始行；按观测时间排序。"""
    return conn.execute(
        """
        SELECT id, ax, ay, az, observed_at, received_at, provenance
        FROM imu_data
        WHERE observed_at IS NOT NULL
          AND observed_at >= ?
          AND observed_at <= ?
        ORDER BY observed_at ASC
        """,
        (now - MOTION_WINDOW_S, now + 2.0),
    ).fetchall()


def _derive_motion(rows: List[sqlite3.Row], now: float, ttl_ms: int) -> Dict[str, Any]:
    """R2：由有限窗口和固定阈值派生运动状态，不做速度积分。"""
    motion: Dict[str, Any] = {
        "state": "unknown",
        "state_label": "无法判断",
        "reason": "",
        "window_s": MOTION_WINDOW_S,
        "sample_count": len(rows),
        "latest_age_ms": None,
        "valid_until": None,
        "std_max": None,
        "norm_delta": None,
        "evidence": "none",
    }
    if not rows:
        motion["reason"] = "no_samples"
        return motion

    latest = rows[-1]
    latest_age_ms = max(0.0, (now - float(latest["observed_at"])) * 1000.0)
    motion["latest_age_ms"] = int(round(latest_age_ms))

    if latest_age_ms > ttl_ms:
        motion["reason"] = "stale_window"
        return motion
    if len(rows) < MOTION_MIN_SAMPLES:
        motion["reason"] = "insufficient_samples"
        return motion

    xs = [float(row["ax"]) for row in rows]
    ys = [float(row["ay"]) for row in rows]
    zs = [float(row["az"]) for row in rows]
    std_max = max(_std(xs), _std(ys), _std(zs))
    norm_delta = sum(
        abs(math.sqrt(x * x + y * y + z * z) - GRAVITY)
        for x, y, z in zip(xs, ys, zs)
    ) / len(rows)

    motion["std_max"] = round(std_max, 3)
    motion["norm_delta"] = round(norm_delta, 3)
    motion["valid_until"] = float(latest["observed_at"]) + ttl_ms / 1000.0

    if std_max >= MOTION_STD_MOVING:
        motion["state"] = "moving"
        motion["state_label"] = "有活动"
    elif std_max <= MOTION_STD_STILL:
        if norm_delta <= MOTION_NORM_STILL:
            motion["state"] = "still"
            motion["state_label"] = "静止"
        elif math.sqrt(
            sum((x * x + y * y + z * z) for x, y, z in zip(xs, ys, zs)) / len(rows)
        ) < 3.0:
            # 合矢量接近 0：可能失重/自由落体，不当作静止。
            motion["state"] = "uncertain"
            motion["state_label"] = "不确定"
            motion["reason"] = "near_weightless"
        else:
            # 恒定的大偏差更可能是安装/标定问题；没有变化量就不判“移动”。
            motion["state"] = "still"
            motion["state_label"] = "静止"
            motion["note"] = (
                f"合矢量与重力相差 {norm_delta:.2f} m/s²，"
                "可能是标定或量程偏差；这里只表示没有明显活动。"
            )
    else:
        motion["state"] = "uncertain"
        motion["state_label"] = "不确定"

    motion["evidence"] = "high" if len(rows) >= MOTION_EVIDENCE_HIGH else "medium"
    return motion


def _query_snapshot(conn: sqlite3.Connection, now: float) -> Dict[str, Any]:
    """最近一次成功快照 + 最近一次取图尝试 + 对应模型结果。"""
    capture = conn.execute(
        """
        SELECT request_id, status, source, captured_at, file_path, error_msg
        FROM image_records
        WHERE status='captured' AND captured_at IS NOT NULL
        ORDER BY captured_at DESC
        LIMIT 1
        """
    ).fetchone()
    latest = conn.execute(
        """
        SELECT request_id, status, source, captured_at, created_at, error_msg
        FROM image_records
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()

    inference = None
    if capture is not None:
        inference = conn.execute(
            "SELECT * FROM inference_results WHERE request_id=?",
            (capture["request_id"],),
        ).fetchone()

    snapshot: Dict[str, Any] = {
        "has_success": capture is not None,
        "request_id": None,
        "captured_at": None,
        "age_ms": None,
        "source": None,
        "file_url": None,
        "latest_status": latest["status"] if latest is not None else None,
        "latest_request_id": latest["request_id"] if latest is not None else None,
        "latest_error": (latest["error_msg"] if latest is not None else None),
        "latest_created_age_ms": None,
        "inference": None,
        "provenance": "device",
    }
    if latest is not None and latest["created_at"] is not None:
        snapshot["latest_created_age_ms"] = int(
            round(max(0.0, (now - float(latest["created_at"])) * 1000.0))
        )

    if capture is not None:
        captured_at = float(capture["captured_at"])
        snapshot.update({
            "request_id": capture["request_id"],
            "captured_at": captured_at,
            "age_ms": int(round(max(0.0, (now - captured_at) * 1000.0))),
            "source": capture["source"] or "device",
            "provenance": capture["source"] or "device",
            "file_url": f"/api/image/{capture['request_id']}/file",
        })

    if inference is not None:
        snapshot["inference"] = {
            "request_id": inference["request_id"],
            "label": inference["label"],
            "score": inference["score"],
            "coarse": inference["coarse"],
            "model_name": inference["model_name"],
            "model_version": inference["model_version"],
            "usability": inference["usability"],
            "status": inference["status"],
            "is_rejected": bool(inference["is_rejected"]),
            "rejected_reason": inference["rejected_reason"],
            "uncertain_note": inference["uncertain_note"],
            "inferred_at": inference["inferred_at"],
        }
    return snapshot


def _inference_usable(inference: Optional[Dict[str, Any]]) -> bool:
    return bool(
        inference
        and inference.get("status") == "ok"
        and not inference.get("is_rejected")
        and inference.get("label")
    )


# ==================== 冲突与待确认 ====================

def _conflicts(
    sources: Dict[str, Dict[str, Any]],
    snapshot: Dict[str, Any],
    ttl: Dict[str, int],
) -> List[Dict[str, Any]]:
    """R7：把跨来源矛盾显式列出，不替用户合并成一个结论。"""
    conflicts: List[Dict[str, Any]] = []
    imu = sources["imu"]
    link = sources["link"]
    camera = sources["camera"]

    if link["status"] == "ok" and imu["status"] == "stale":
        conflicts.append({
            "id": "C1",
            "kind": "link_ok_data_stale",
            "title": "链路在线但 IMU 未更新",
            "sources": ["link", "imu"],
            "message": (
                f"链路 {_age_text(link.get('age_ms'))} 前还有请求，"
                f"但加速度数据已 {_age_text(imu.get('age_ms'))} 未更新；"
                "网络在线不等于数据新鲜。"
            ),
        })

    failure_kind = camera.get("failure_kind")
    if camera["status"] == "failed" and failure_kind in ("blocked", "error", "timeout") \
            and snapshot["has_success"]:
        conflicts.append({
            "id": "C2",
            "kind": "old_snapshot_plus_failure",
            "title": "本次取图失败 + 上次成功快照",
            "sources": ["camera"],
            "message": (
                f"本次取图失败（{failure_kind}）；"
                f"上次成功快照在 {_age_text(snapshot.get('age_ms'))} 前，"
                "旧图仍是历史，不代表当前画面。"
            ),
        })

    inference = snapshot.get("inference")
    if snapshot["has_success"] and _inference_usable(inference) \
            and snapshot["age_ms"] is not None \
            and snapshot["age_ms"] > ttl["camera_ms"]:
        conflicts.append({
            "id": "C3",
            "kind": "stale_snapshot_inference",
            "title": "模型标签来自旧快照",
            "sources": ["camera", "model"],
            "message": (
                f"模型标签对应的是 {_age_text(snapshot.get('age_ms'))} 前的快照，"
                "不是当前画面，也不能当作当前位置。"
            ),
        })

    return conflicts


def _pending(
    topic: str,
    motion: Dict[str, Any],
    snapshot: Dict[str, Any],
    sources: Dict[str, Dict[str, Any]],
    ttl: Dict[str, int],
) -> List[Dict[str, str]]:
    """待确认信息：只列“现有证据回答不了、但用户可能关心”的点。"""
    pending: List[Dict[str, str]] = []
    inference = snapshot.get("inference")

    if motion["state"] == "unknown":
        pending.append({
            "text": "当前是否移动",
            "reason": "运动派生缺少足够的当前样本",
            "how_to_confirm": "重新采集一次，或检查设备是否暂停采样",
        })
    if not snapshot["has_success"]:
        pending.append({
            "text": "当前画面",
            "reason": "还没有成功取图",
            "how_to_confirm": "现在拍一张",
        })
    elif snapshot["latest_status"] in ("blocked", "failed", "timeout"):
        pending.append({
            "text": "当前画面",
            "reason": f"最近一次取图失败（{snapshot['latest_status']}）",
            "how_to_confirm": "现在重拍一张",
        })
    elif snapshot["age_ms"] is not None and snapshot["age_ms"] > ttl["camera_ms"]:
        pending.append({
            "text": "当前画面",
            "reason": "最近一次成功快照已超过快照有效期",
            "how_to_confirm": "现在拍一张",
        })
    if snapshot["has_success"] and inference is not None and not _inference_usable(inference):
        pending.append({
            "text": "画面内容",
            "reason": "模型未能确认这张快照",
            "how_to_confirm": "重拍或人工确认",
        })
    if sources["link"]["status"] == "stale":
        pending.append({
            "text": "设备是否在线",
            "reason": f"链路已 {_age_text(sources['link'].get('age_ms'))} 没有请求",
            "how_to_confirm": "检查设备电源与 WiFi",
        })
    if topic == "location":
        pending.append({
            "text": "真实位置",
            "reason": "本机没有定位来源（GNSS 未配置）",
            "how_to_confirm": "无法确认；只能核对最近一次画面",
        })

    # 去重，保持顺序
    seen = set()
    unique = []
    for item in pending:
        if item["text"] in seen:
            continue
        seen.add(item["text"])
        unique.append(item)
    return unique


def _followup(
    topic: str,
    motion: Dict[str, Any],
    snapshot: Dict[str, Any],
    sources: Dict[str, Dict[str, Any]],
    all_missing: bool,
    ttl: Dict[str, int],
) -> Optional[Dict[str, Any]]:
    """R8：至多一条追问，且必须与问题主题相关。

    运动问题补“采样”，位置问题做“澄清”，画面/安全/总体问题才补“取图”；
    这样追问补到的证据才可能改变回答，不会被无关的旧画面劫持。
    """
    if all_missing:
        return {
            "ask": "目前没有收到任何来源数据。要查看来源状态，还是先重新采集一次？",
            "why": "没有观测就无法判断当前状态；没有数据不等于设备故障。",
            "action": "sources",
            "options": [
                {"id": "sources", "label": "查看来源状态", "action": "sources"},
                {"id": "collect", "label": "重新采集一次", "action": "collect"},
                {"id": "dismiss", "label": "先这样", "action": "dismiss"},
            ],
        }

    collect_followup = {
        "ask": "设备现在还在采样吗？可以重新采集一次来确认。",
        "why": "运动状态缺少足够的当前样本；重新采集能得到新的观测。",
        "action": "collect",
        "options": [
            {"id": "collect", "label": "重新采集一次", "action": "collect"},
            {"id": "sources", "label": "查看来源状态", "action": "sources"},
            {"id": "dismiss", "label": "先这样", "action": "dismiss"},
        ],
    }
    clarify_followup = {
        "ask": "你是指最近一次画面，还是想知道设备位置？本机没有定位来源。",
        "why": "无法给出坐标；只能核对画面或说明缺失。",
        "action": "clarify",
        "options": [
            {"id": "retake", "label": "拍一张画面", "action": "capture"},
            {"id": "dismiss", "label": "先这样", "action": "dismiss"},
        ],
    }
    retake_followup = {
        "ask": "要现在拍一张画面确认吗？",
        "why": "画面是快照，且当前证据已不足或可能变化；补一张新图能改变回答。",
        "action": "capture",
        "options": [
            {"id": "retake", "label": "现在拍一张", "action": "capture"},
            {"id": "dismiss", "label": "先这样", "action": "dismiss"},
        ],
    }

    if topic == "motion":
        return collect_followup if motion["state"] == "unknown" else None

    if topic == "location":
        return clarify_followup

    camera_failed = sources["camera"]["status"] == "failed" or snapshot["latest_status"] in (
        "blocked", "failed", "timeout",
    )
    snapshot_stale = (
        snapshot["age_ms"] is not None and snapshot["age_ms"] > ttl["camera_ms"]
    )
    scene_may_changed = (
        motion["state"] == "moving"
        and snapshot["age_ms"] is not None
        and snapshot["age_ms"] > SCENE_RECHECK_S * 1000
    )
    inference = snapshot.get("inference")
    inference_unusable = (
        snapshot["has_success"]
        and inference is not None
        and not _inference_usable(inference)
        and snapshot["age_ms"] is not None
        and snapshot["age_ms"] <= ttl["camera_ms"]
    )

    if camera_failed or snapshot_stale or scene_may_changed or inference_unusable:
        return retake_followup

    if motion["state"] == "unknown":
        return collect_followup

    return None


def _preference_gate(followup: Optional[Dict[str, Any]], db_path: str,
                     now: float) -> Dict[str, Any]:
    """R10：取图追问是否被用户的显式偏好压掉（第 12 周，只读判定）。

    只压 action="capture" 的追问：采样/澄清/来源这三类追问与"重复提醒"无关，
    压掉只会让回答变弱。恢复默认或本轮到期后 blocks_capture_prompt 自动为假，
    于是回到 R8 的默认策略 —— 这就是"记住当前选择，又能恢复默认"的落点。
    """
    if not followup or followup.get("action") != "capture":
        return {"blocked": False}
    return pref.blocks_capture_prompt(db_path, now)


def _preference_text(gate: Dict[str, Any]) -> str:
    """把 R10 命中写成一句人能读懂的说明（同时作为事实文本与回答后缀）。"""
    if gate.get("session_muted"):
        scope = f"本轮，剩余 {max(0, int(round(gate.get('session_remaining_s') or 0)))} 秒"
    else:
        scope = "该类原因（" + "、".join(gate.get("muted_reason_labels") or []) + "）"
    return (f"按你之前的反馈（作用域：{scope}），这里不再提示重拍"
            f"（配置 v{gate.get('config_version')}）；说“恢复默认提醒”随时可以改回来。")


# ==================== 回答模板 ====================

def _motion_sentence(motion: Dict[str, Any]) -> str:
    state = motion["state"]
    count = motion["sample_count"]
    age = motion["latest_age_ms"]

    if state == "still":
        sentence = (
            f"最近 {MOTION_WINDOW_S:.0f} 秒内收到 {count} 个加速度样本，"
            f"最新一条在 {_age_text(age)} 前；按规则 R2 判为“静止”"
            f"（轴标准差 {motion['std_max']:.2f} m/s²，阈值 {MOTION_STD_STILL:.2f}）。"
            "这是由加速度派生的状态，不是直接测量。"
        )
        if motion.get("note"):
            sentence += " " + motion["note"]
        return sentence
    if state == "moving":
        return (
            f"最近 {MOTION_WINDOW_S:.0f} 秒内收到 {count} 个加速度样本，"
            f"最新一条在 {_age_text(age)} 前；按规则 R2 判为“有活动”"
            f"（轴标准差 {motion['std_max']:.2f} m/s²，阈值 {MOTION_STD_MOVING:.2f}）。"
            "这是由加速度派生的状态，不是直接测量。"
        )
    if state == "uncertain":
        if motion.get("reason") == "near_weightless":
            return (
                f"最近 {MOTION_WINDOW_S:.0f} 秒内收到 {count} 个样本，"
                "合矢量接近 0，可能处于失重/自由落体；无法判断是否在移动（R2）。"
            )
        return (
            f"最近 {MOTION_WINDOW_S:.0f} 秒内收到 {count} 个样本，"
            f"波动处在静止/活动的阈值之间（标准差 {motion['std_max']:.2f} m/s²）；"
            "无法确定当前是否在移动（R2）。"
        )

    reason = motion.get("reason")
    if reason == "no_samples":
        detail = f"最近 {MOTION_WINDOW_S:.0f} 秒没有收到加速度样本"
        if motion.get("latest_age_ms") is not None:
            detail += f"（最近一条在 {_age_text(motion['latest_age_ms'])} 前）"
    elif reason == "insufficient_samples":
        detail = f"最近 {MOTION_WINDOW_S:.0f} 秒只有 {count} 个加速度样本（至少需要 {MOTION_MIN_SAMPLES} 个）"
    elif reason == "stale_window":
        detail = f"最新加速度样本在 {_age_text(age)} 前，已超过 {MOTION_WINDOW_S:.0f} 秒有效窗"
    else:
        detail = "缺少可比较的当前样本"
    return f"{detail}；无法判断当前是否移动。"


def _snapshot_sentence(snapshot: Dict[str, Any]) -> str:
    if not snapshot["has_success"]:
        return "还没有可用的环境快照。"
    inference = snapshot.get("inference")
    text = f"最近一次成功取图在 {_age_text(snapshot.get('age_ms'))} 前"
    if _inference_usable(inference):
        text += (
            f"，模型标签“{inference['label']}”"
            f"（模型分数 {float(inference['score']):.3f}，{SCORE_NOTE}，"
            f"模型版本 {inference.get('model_version') or '未知'}）"
        )
    elif inference is not None:
        text += "，模型未能确认这张图的内容"
    text += "。它只是快照，不代表当前画面。"
    return text


def _link_sentence(link: Dict[str, Any]) -> str:
    if link["status"] == "not_attempted":
        return "尚未收到设备心跳，无法判断链路状态。"
    if link["status"] == "ok":
        return f"设备链路在线（{_age_text(link.get('age_ms'))} 前有请求），但在线不等于各来源数据新鲜。"
    if link["status"] == "stale":
        return f"设备链路已 {_age_text(link.get('age_ms'))} 没有请求；无法确认设备状态。"
    return f"链路状态：{link.get('status_label') or link.get('status')}。"


def _answer(
    topic: str,
    motion: Dict[str, Any],
    snapshot: Dict[str, Any],
    sources: Dict[str, Dict[str, Any]],
    pending: List[Dict[str, str]],
    conflicts: List[Dict[str, Any]],
) -> str:
    """按主题拼装可解释回答；只使用证据能支撑的措辞。"""
    imu = sources["imu"]
    link = sources["link"]
    camera = sources["camera"]
    inference = snapshot.get("inference")

    if topic == "motion":
        answer = "已知：" + _motion_sentence(motion)
        if any(c["kind"] == "link_ok_data_stale" for c in conflicts):
            answer += " 另外，链路虽然在线，但加速度数据未更新（R7），不能把旧样本当作当前状态。"
        if imu["status"] == "failed":
            answer += f" 最近一次读取失败（{imu.get('status_reason')}），失败不覆盖上次观测。"
        return answer

    if topic == "location":
        answer = "本机没有定位来源（GNSS 未配置，R3），无法给出坐标，也不能回答“你现在在哪”。"
        answer += _snapshot_sentence(snapshot)
        if _inference_usable(inference):
            answer += " 模型标签只描述那张图，不代表你现在的位置。"
        return answer

    if topic == "scene":
        answer = ""
        if camera["status"] == "failed":
            answer += f"本次取图失败（{camera.get('failure_kind') or '画面不可用'}）。"
        answer += _snapshot_sentence(snapshot)
        if motion["state"] == "moving" and snapshot["age_ms"] is not None \
                and snapshot["age_ms"] > SCENE_RECHECK_S * 1000:
            answer += (
                f" 快照之后设备检测到活动（R6），画面可能已变化，"
                f"建议重新取图（快照复核阈值 {SCENE_RECHECK_S}s）。"
            )
        elif motion["state"] == "still" and snapshot["age_ms"] is not None \
                and snapshot["age_ms"] <= SCENE_RECHECK_S * 1000:
            answer += " 之后未检测到明显活动，画面可能仍相近，但不能确认。"
        return answer

    if topic == "safety":
        answer = ""
        if camera["status"] == "failed":
            answer += (
                f"最近一次取图失败（{camera.get('failure_kind') or '画面不可用'}）；"
                "无法确认当前环境。"
            )
        else:
            answer += _snapshot_sentence(snapshot)
        answer += " 本系统不做危险/安全判断（R4），上述内容不能作为安全或危险的结论。"
        return answer

    # overview：只汇总事实，逐项标注“不能确认”
    parts = [
        _motion_sentence(motion),
        _snapshot_sentence(snapshot),
        _link_sentence(link),
    ]
    answer = "现在能确认的：" + " ".join(parts)
    answer += " 本机没有定位来源（GNSS 未配置，R3），无法回答位置。"
    if pending:
        answer += " 仍不能确认：" + "；".join(item["text"] for item in pending) + "。"
    else:
        answer += " 目前没有需要追加确认的缺口。"
    return answer


def _decision_id(
    topic: str,
    all_missing: bool,
    followup: Optional[Dict[str, Any]],
) -> str:
    if all_missing:
        return "D5"
    if topic == "location":
        return "D4"
    if followup is not None:
        action = followup.get("action")
        if action == "capture":
            return "D2"
        if action == "collect":
            return "D3"
        return "D5"
    return "D1"


# ==================== 入口 ====================

def build_context(
    db_path: str,
    question: str,
    now: Optional[float] = None,
    ttl: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """构造一次上下文解释；纯读，不修改原始观测。

    Args:
        db_path: SQLite 数据库路径
        question: 用户问题（已由调用方做长度校验）
        now: 测试可注入的固定服务器时间（epoch 秒）
        ttl: 测试可覆盖的阈值；默认读环境变量
    """
    now = time.time() if now is None else float(now)
    ttl = source_state.ttl_config() if ttl is None else ttl
    question = (question or "").strip()
    topic = classify_topic(question)

    report = source_state.build_report(db_path, now=now, ttl=ttl)
    sources = {src["source"]: src for src in report["sources"]}

    conn = _connect(db_path)
    try:
        rows = _query_imu_window(conn, now)
        snapshot = _query_snapshot(conn, now)
    finally:
        conn.close()

    motion = _derive_motion(rows, now, ttl["imu_ms"])
    imu = sources["imu"]
    camera = sources["camera"]
    link = sources["link"]

    # 窗口外没有样本时，仍把“最近一条观测的年龄”带进回答，方便用户判断是否离线。
    if motion["state"] == "unknown" and motion.get("reason") == "no_samples" \
            and imu.get("age_ms") is not None:
        motion["latest_age_ms"] = imu.get("age_ms")

    # ---------- 原始观测 ----------
    facts: List[Dict[str, Any]] = []

    if imu.get("raw"):
        facts.append(_fact(
            "observation", "R1",
            f"最近一次加速度观测 ax={float(imu['raw']['ax']):.3f}、"
            f"ay={float(imu['raw']['ay']):.3f}、az={float(imu['raw']['az']):.3f} m/s²"
            f"（{_age_text(imu.get('age_ms'))} 前）。",
            "imu",
            observed_at=imu.get("observed_at"),
            age_ms=imu.get("age_ms"),
            can_represent_current=bool(imu.get("can_represent_current")),
            provenance=imu.get("provenance", "device"),
            raw_ref=imu.get("raw_ref"),
        ))
    else:
        facts.append(_fact(
            "absence", "R1",
            imu.get("user_message") or "尚未收到加速度观测；没有数据不等于设备故障。",
            "imu",
            provenance="device",
        ))

    if camera["status"] in ("failed", "in_progress") and camera.get("last_attempt"):
        facts.append(_fact(
            "observation", "R1",
            f"最近一次取图状态：{camera.get('status_label')}（{camera.get('user_message')}）",
            "camera",
            observed_at=camera.get("last_attempt", {}).get("at"),
            age_ms=None,
            provenance=snapshot.get("provenance", "device"),
            request_id=camera.get("last_attempt", {}).get("request_id"),
        ))

    if snapshot["has_success"]:
        facts.append(_fact(
            "observation", "R1",
            f"上次成功取图在 {_age_text(snapshot.get('age_ms'))} 前"
            f"（request_id={snapshot['request_id']}）；图像是快照。",
            "camera",
            observed_at=snapshot.get("captured_at"),
            age_ms=snapshot.get("age_ms"),
            provenance=snapshot.get("provenance", "device"),
            request_id=snapshot["request_id"],
            image_url=snapshot.get("file_url"),
        ))
    else:
        facts.append(_fact(
            "absence", "R1",
            "还没有成功取图；没有图像不等于摄像头故障。",
            "camera",
            provenance="device",
        ))

    if snapshot["has_success"] and snapshot.get("inference") is not None:
        inference = snapshot["inference"]
        if _inference_usable(inference):
            facts.append(_fact(
                "inference", "R5",
                f"模型对上次快照的分类：“{inference['label']}”，"
                f"模型分数 {float(inference['score']):.3f}（{SCORE_NOTE}），"
                f"模型版本 {inference.get('model_version') or '未知'}。",
                "model",
                observed_at=inference.get("inferred_at"),
                age_ms=None,
                provenance=snapshot.get("provenance", "device"),
                model_version=inference.get("model_version"),
                score=inference.get("score"),
                label=inference.get("label"),
                usable=True,
            ))
        else:
            facts.append(_fact(
                "inference", "R5",
                f"模型未能确认上次快照内容（{inference.get('rejected_reason') or inference.get('uncertain_note') or '拒识'}）。",
                "model",
                observed_at=inference.get("inferred_at"),
                provenance=snapshot.get("provenance", "device"),
                model_version=inference.get("model_version"),
                usable=False,
            ))
    elif snapshot["has_success"]:
        facts.append(_fact(
            "absence", "R5",
            "上次快照没有可用的模型结果；没有结果不等于画面内容已知。",
            "model",
            provenance=snapshot.get("provenance", "device"),
        ))

    if link["status"] == "not_attempted":
        facts.append(_fact(
            "absence", "R1",
            "尚未收到设备心跳，无法判断链路状态。",
            "link",
            provenance="device",
        ))
    else:
        facts.append(_fact(
            "observation", "R1",
            _link_sentence(link),
            "link",
            observed_at=link.get("observed_at"),
            age_ms=link.get("age_ms"),
            provenance="device",
        ))

    facts.append(_fact(
        "absence", "R3",
        "定位来源缺失（ESP32-S3-EYE 未配置 GNSS）；不提供任何坐标。",
        "gnss",
        provenance="device",
    ))

    # 派生状态事实（R2）
    motion_text = _motion_sentence(motion)
    facts.append(_fact(
        "derived", "R2",
        motion_text,
        "imu",
        observed_at=rows[-1]["observed_at"] if rows else None,
        age_ms=motion.get("latest_age_ms"),
        can_represent_current=(
            motion["state"] in ("still", "moving", "uncertain")
            and motion.get("latest_age_ms") is not None
            and motion["latest_age_ms"] <= ttl["imu_ms"]
        ),
        provenance=(rows[-1]["provenance"] if rows else "device") or "device",
        state=motion["state"],
        sample_count=motion["sample_count"],
        window_s=MOTION_WINDOW_S,
        std_max=motion.get("std_max"),
        norm_delta=motion.get("norm_delta"),
        evidence=motion.get("evidence"),
        valid_until=motion.get("valid_until"),
    ))

    # ---------- 冲突 / 待确认 / 追问 ----------
    conflicts = _conflicts(sources, snapshot, ttl)
    pending = _pending(topic, motion, snapshot, sources, ttl)
    all_missing = all(
        sources[key]["status"] == "not_attempted" for key in ("imu", "camera", "link")
    )
    followup = _followup(topic, motion, snapshot, sources, all_missing, ttl)
    # ---------- R10：用户显式偏好优先于默认追问策略（第 12 周） ----------
    preference = _preference_gate(followup, db_path, now)
    if preference.get("blocked"):
        facts.append(_fact(
            "preference", "R10", _preference_text(preference), "user",
            observed_at=now,
            can_represent_current=True,
            scope="session" if preference.get("session_muted") else "event_reason",
            config_version=preference.get("config_version"),
            session_remaining_s=preference.get("session_remaining_s"),
            muted_reasons=preference.get("muted_reasons"),
        ))
        followup = None
    decision_id = _decision_id(topic, all_missing, followup)

    answer = _answer(topic, motion, snapshot, sources, pending, conflicts)
    if all_missing:
        answer = (
            "目前没有收到任何来源数据：加速度计、摄像头和链路都没有可核对的观测，"
            "无法回答当前状态；没有数据不等于设备故障。"
        )
    if preference.get("blocked"):
        # 如实说明"为什么这次没有追问"，否则用户会以为系统忘了他的选择。
        answer = f"{answer} {_preference_text(preference)}"

    # 来源汇总：只统计“观测”事实，缺失项不算回放/真机证据。
    observation_provenance = {
        fact["provenance"] for fact in facts if fact["kind"] == "observation"
    }
    if observation_provenance == {"replay"}:
        provenance = "replay"
    elif "replay" in observation_provenance:
        provenance = "mixed"
    else:
        provenance = "device"

    layers = {
        "raw_observations": [f for f in facts if f["kind"] == "observation"],
        "derived_states": [f for f in facts if f["kind"] == "derived"],
        "inference": [f for f in facts if f["kind"] == "inference"],
        "absence": [f for f in facts if f["kind"] == "absence"],
        "interaction": {
            "decision_id": decision_id,
            "decision_label": DECISION_LABELS[decision_id],
            "answer": answer,
            "pending": pending,
            "followup": followup,
            "conflicts": conflicts,
            "preference": preference,
        },
    }

    return {
        "trace_id": uuid.uuid4().hex[:8],
        "rule_version": RULE_VERSION,
        "rules": RULES,
        "thresholds": thresholds(),
        "generated_at": now,
        "generated_at_text": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "question": question,
        "topic": topic,
        "topic_label": TOPIC_LABELS.get(topic, topic),
        "decision": {"id": decision_id, "label": DECISION_LABELS[decision_id]},
        "answer": answer,
        "facts": facts,
        "pending": pending,
        "followup": followup,
        "preference": preference,
        "conflicts": conflicts,
        "layers": layers,
        "motion": motion,
        "snapshot": snapshot,
        "hazard": {
            "judgement": None,
            "note": "本系统不做危险/安全判断；遮挡只表示画面不可用。",
        },
        "location": {
            "available": False,
            "reason": "gnss_absent",
            "note": "ESP32-S3-EYE 未配置 GNSS；不生成任何坐标。",
        },
        "provenance": provenance,
    }


# ==================== trace 持久化（验收取证用） ====================

def save_trace(db_path: str, result: Dict[str, Any]) -> None:
    """把一次上下文解释落库；只写 context_traces，不动原始观测。"""
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO context_traces (
                trace_id, question, topic, rule_version, generated_at, answer,
                facts_json, pending_json, followup_json, layers_json,
                conflicts_json, provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["trace_id"],
                result["question"],
                result["topic"],
                result["rule_version"],
                result["generated_at"],
                result["answer"],
                json.dumps(result["facts"], ensure_ascii=False),
                json.dumps(result["pending"], ensure_ascii=False),
                json.dumps(result["followup"], ensure_ascii=False),
                json.dumps(result["layers"], ensure_ascii=False),
                json.dumps(result["conflicts"], ensure_ascii=False),
                result.get("provenance", "device"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _trace_row(row: sqlite3.Row) -> Dict[str, Any]:
    def loads(value: str) -> Any:
        try:
            return json.loads(value) if value else None
        except (TypeError, ValueError):
            return None

    return {
        "trace_id": row["trace_id"],
        "question": row["question"],
        "topic": row["topic"],
        "rule_version": row["rule_version"],
        "generated_at": row["generated_at"],
        "answer": row["answer"],
        "facts": loads(row["facts_json"]) or [],
        "pending": loads(row["pending_json"]) or [],
        "followup": loads(row["followup_json"]),
        "layers": loads(row["layers_json"]) or {},
        "conflicts": loads(row["conflicts_json"]) or [],
        "provenance": row["provenance"],
    }


def list_traces(
    db_path: str,
    limit: int = 20,
    only_conflict: bool = False,
) -> List[Dict[str, Any]]:
    """最近上下文解释；only_conflict=1 时只返回带冲突的样例。"""
    limit = max(1, min(int(limit), 100))
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM context_traces ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    items = [_trace_row(row) for row in rows]
    if only_conflict:
        items = [item for item in items if item["conflicts"]]
    return items


def get_trace(db_path: str, trace_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM context_traces WHERE trace_id=?", (trace_id,)
        ).fetchone()
    finally:
        conn.close()
    return _trace_row(row) if row is not None else None

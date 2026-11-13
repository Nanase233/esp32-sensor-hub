# -*- coding: utf-8 -*-
"""第 10 周：多来源观测状态与数据年龄。

本模块只读聚合现有表，不修改任何原始观测：
  - imu_data      每次成功读取的加速度原始值
  - image_records 每次按需取图的任务与结果
  - device_status 设备最近一次心跳/采样状态（由 app.py 维护）

时间口径（详见 README 第 10 周）：
  - 设备观测时间：observed_uptime_ms（开机毫秒）
  - 设备发送时间：send_uptime_ms（HTTP 发出前的开机毫秒）
  - 服务器接收时间：received_at（epoch 秒）
  - 观测年龄：age_ms = now - observed_at
    observed_at = received_at - (send_uptime_ms - observed_uptime_ms) / 1000

旧固件没有显式时间字段时不猜测 timestamp 的单位，统一返回
age_basis="unknown"，页面只能显示"时效未知"，不能当成当前状态。
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ==================== 默认阈值 ====================
# IMU 采样约 20Hz、约 1s 上传一次；5s 未更新即视为陈旧。
DEFAULT_IMU_TTL_MS = 5000
# 图像是快照而非连续流；10 分钟只用于提示"旧图"，不代表当前画面。
DEFAULT_CAMERA_TTL_MS = 600000
# 设备每 2s 轮询一次；10s 没有请求即视为链路离线。
DEFAULT_LINK_TTL_MS = 10000
# 观测到发送的最大合理滞留时间，超过则时间不可信。
MAX_TRANSIT_MS = 3600000

STATUS_LABELS = {
    "not_attempted": "未尝试",
    "in_progress": "采集中",
    "ok": "成功",
    "stale": "陈旧",
    "failed": "失败",
    "unknown_age": "时效未知",
    "absent": "缺失",
}

STATUS_ORDER = ["imu", "camera", "link", "gnss"]


def now_epoch() -> float:
    """当前服务器时间（单独封装，便于测试注入）。"""
    return time.time()


def _env_int(name: str, default: int) -> int:
    """读取正整数环境变量，非法值回退默认值。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def ttl_config() -> Dict[str, int]:
    """本周使用的三个 TTL（毫秒），可用环境变量覆盖。"""
    return {
        "imu_ms": _env_int("IMU_STALE_MS", DEFAULT_IMU_TTL_MS),
        "camera_ms": _env_int("CAMERA_STALE_MS", DEFAULT_CAMERA_TTL_MS),
        "link_ms": _env_int("LINK_OFFLINE_MS", DEFAULT_LINK_TTL_MS),
    }


# ==================== 通用小工具 ====================

def _row_get(row: Optional[sqlite3.Row], key: str, default: Any = None) -> Any:
    """兼容旧库：列不存在时返回默认值，不抛异常。"""
    if row is None:
        return default
    return row[key] if key in row.keys() else default


def _age_ms(now: float, timestamp: Optional[float]) -> Optional[float]:
    """epoch 时间差转毫秒；未来时间超过 2s 视为时钟异常。"""
    if timestamp is None:
        return None
    age = (now - float(timestamp)) * 1000.0
    if age < -2000.0:
        return None
    return max(0.0, age)


def _round_ms(value: Optional[float]) -> Optional[int]:
    return None if value is None else int(round(value))


def _age_text(age_ms: Optional[float]) -> str:
    """把毫秒转成页面/文案可读的中文时长。"""
    if age_ms is None:
        return "未知"
    if age_ms < 1000:
        return f"{age_ms / 1000.0:.1f} 秒"
    seconds = age_ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分 {int(seconds % 60)} 秒"
    return f"{int(seconds // 3600)} 小时 {int((seconds % 3600) // 60)} 分"


def _created_at_epoch(value: Optional[str]) -> Optional[float]:
    """把 SQLite CURRENT_TIMESTAMP（UTC 文本）转成 epoch 秒。"""
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.timestamp()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _base_source(source: str, label: str, nature: str) -> Dict[str, Any]:
    return {
        "source": source,
        "label": label,
        "nature": nature,
        "status": "not_attempted",
        "status_label": STATUS_LABELS["not_attempted"],
        "status_reason": "never_attempted",
        "user_message": "",
        "can_represent_current": False,
        "age_ms": None,
        "age_basis": "unknown",
        "observed_at": None,
        "received_at": None,
        "transit_ms": None,
        "last_attempt": {"result": "none", "at": None, "inferred": False},
        "last_success": None,
        "ttl_ms": None,
        "raw": None,
        "raw_ref": None,
        "provenance": "device",
    }


def _finish_status(src: Dict[str, Any], status: str, reason: str, message: str) -> Dict[str, Any]:
    src["status"] = status
    src["status_label"] = STATUS_LABELS.get(status, status)
    src["status_reason"] = reason
    src["user_message"] = message
    return src


# ==================== IMU ====================

def _query_latest_imu(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """按观测时间取最新一条，而不是按插入 id。"""
    return conn.execute(
        """
        SELECT *
        FROM imu_data
        ORDER BY COALESCE(observed_at, received_at, 0) DESC, id DESC
        LIMIT 1
        """
    ).fetchone()


def _query_device_status(conn: sqlite3.Connection, device_id: str = "esp32-s3-eye") -> Optional[sqlite3.Row]:
    if not _table_exists(conn, "device_status"):
        return None
    return conn.execute(
        "SELECT * FROM device_status WHERE device_id=?", (device_id,)
    ).fetchone()


def _imu_source(
    conn: sqlite3.Connection,
    now: float,
    ttl: Dict[str, int],
    row: Optional[sqlite3.Row],
    ds: Optional[sqlite3.Row],
) -> Dict[str, Any]:
    src = _base_source("imu", "加速度计", "stream")
    src["ttl_ms"] = ttl["imu_ms"]
    src["provenance"] = _row_get(row, "provenance", "device") or "device"

    attempt_code = (_row_get(ds, "imu_attempt", "") or "").lower()
    attempt_at = _row_get(ds, "updated_at")
    last_seen = _row_get(ds, "last_seen_at")
    link_age = _age_ms(now, last_seen)
    online = link_age is not None and link_age <= ttl["link_ms"]

    if attempt_code in ("ok", "fail", "busy"):
        src["last_attempt"] = {
            "result": attempt_code,
            "at": attempt_at,
            "inferred": False,
            "error": _row_get(ds, "imu_error"),
        }
    elif row is not None:
        # 旧固件没有心跳：只能从"有数据"反推最近一次成功，明确标注 inferred。
        src["last_attempt"] = {
            "result": "ok",
            "at": _row_get(row, "received_at"),
            "inferred": True,
            "error": None,
        }

    received_at = _row_get(row, "received_at")
    if received_at is None:
        received_at = _created_at_epoch(_row_get(row, "created_at"))
    observed_at = _row_get(row, "observed_at")
    observed_uptime = _row_get(row, "observed_uptime_ms")
    send_uptime = _row_get(row, "send_uptime_ms")
    transit_ms: Optional[float] = None

    if observed_uptime is not None and send_uptime is not None:
        candidate_transit = float(send_uptime) - float(observed_uptime)
        if 0 <= candidate_transit <= MAX_TRANSIT_MS:
            transit_ms = candidate_transit
            if observed_at is None and received_at is not None:
                observed_at = float(received_at) - transit_ms / 1000.0

    age_ms = _age_ms(now, observed_at)
    stored_basis = _row_get(row, "age_basis")
    if observed_at is not None:
        age_basis = stored_basis or ("device_transit" if transit_ms is not None else "server_received")
    else:
        age_basis = "unknown"

    src.update({
        "age_ms": _round_ms(age_ms),
        "age_basis": age_basis,
        "observed_at": observed_at,
        "received_at": received_at,
        "transit_ms": _round_ms(transit_ms),
        "provenance": _row_get(row, "provenance", "device") or "device",
    })
    if row is not None:
        src["raw"] = {
            "ax": _row_get(row, "ax"),
            "ay": _row_get(row, "ay"),
            "az": _row_get(row, "az"),
            "timestamp": _row_get(row, "timestamp"),
            "observed_uptime_ms": observed_uptime,
            "send_uptime_ms": send_uptime,
            "boot_id": _row_get(row, "boot_id"),
        }
        src["raw_ref"] = f"imu_data:{_row_get(row, 'id')}"
        src["last_success"] = {
            "age_ms": _round_ms(age_ms),
            "observed_at": observed_at,
            "received_at": received_at,
        }
        src["received_age_ms"] = _round_ms(_age_ms(now, received_at))

    # 状态优先级：明确失败 → 无观测 → 时间未知 → 超过 TTL → 新鲜。
    if attempt_code == "fail":
        failure = _row_get(ds, "imu_error") or "read"
        return _finish_status(
            src,
            "failed",
            "attempt_failed",
            f"最近一次加速度计读取失败（{failure}）；"
            + (
                f"下面显示上次有效观测（{_age_text(age_ms)} 前），不代表当前状态。"
                if age_ms is not None
                else "没有可用的上次观测。"
            ),
        )

    if row is None:
        if attempt_code == "ok" and online:
            return _finish_status(
                src,
                "failed",
                "no_observation",
                "设备已采样，但服务器尚未收到观测数据；不能当作当前状态。",
            )
        return _finish_status(
            src,
            "not_attempted",
            "never_attempted",
            "尚未收到加速度计观测；没有数据不等于设备故障。",
        )

    if age_ms is None:
        return _finish_status(
            src,
            "unknown_age",
            "time_unknown",
            "该观测缺少可比较的时间（旧固件只上报 timestamp）；时效未知，不能当作当前状态。",
        )

    if age_ms > ttl["imu_ms"]:
        if not online:
            reason = "device_offline"
            message = (
                f"设备链路已离线，最后一条观测在 {_age_text(age_ms)} 前；"
                "无法判断当前状态，也不据此判定设备故障。"
            )
        elif attempt_code == "ok":
            reason = "upload_stalled"
            message = (
                f"设备仍在采样，但服务器已 {_age_text(age_ms)} 未收到新数据；"
                "不能当作当前状态。"
            )
        elif attempt_code == "busy":
            reason = "paused"
            message = (
                f"加速度计因摄像头占用而暂停，最后一条观测在 {_age_text(age_ms)} 前；"
                "暂停不是设备故障。"
            )
        else:
            reason = "ttl_expired"
            message = f"最后一条观测在 {_age_text(age_ms)} 前，已超过 {ttl['imu_ms']}ms 阈值；不能当作当前状态。"
        return _finish_status(src, "stale", reason, message)

    src["can_represent_current"] = True
    return _finish_status(
        src,
        "ok",
        "fresh",
        f"数据新鲜（观测于 {_age_text(age_ms)} 前）。",
    )


# ==================== 摄像头快照 ====================

def _query_latest_image(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    if not _table_exists(conn, "image_records"):
        return None
    return conn.execute(
        "SELECT * FROM image_records ORDER BY created_at DESC LIMIT 1"
    ).fetchone()


def _query_latest_capture(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    if not _table_exists(conn, "image_records"):
        return None
    return conn.execute(
        """
        SELECT * FROM image_records
        WHERE status='captured' AND captured_at IS NOT NULL
        ORDER BY captured_at DESC LIMIT 1
        """
    ).fetchone()


def _camera_source(
    conn: sqlite3.Connection,
    now: float,
    ttl: Dict[str, int],
    latest: Optional[sqlite3.Row],
    capture: Optional[sqlite3.Row],
) -> Dict[str, Any]:
    src = _base_source("camera", "环境图像", "snapshot")
    src["ttl_ms"] = ttl["camera_ms"]
    src["can_represent_current"] = False

    capture_age = _age_ms(now, _row_get(capture, "captured_at"))
    if capture is not None:
        file_path = _row_get(capture, "file_path")
        available = bool(file_path and os.path.exists(file_path))
        src["last_success"] = {
            "age_ms": _round_ms(capture_age),
            "observed_at": _row_get(capture, "captured_at"),
            "received_at": _row_get(capture, "captured_at"),
        }
        src["raw"] = {
            "request_id": _row_get(capture, "request_id"),
            "image_url": f"/api/image/{_row_get(capture, 'request_id')}/file" if available else None,
            "size_bytes": _row_get(capture, "size_bytes"),
            "device_ts_us": _row_get(capture, "device_ts_us"),
            "captured_at": _row_get(capture, "captured_at"),
            "raw_available": available,
        }
        src["raw_ref"] = f"image_records:{_row_get(capture, 'request_id')}"
        src["age_ms"] = _round_ms(capture_age)
        src["age_basis"] = "server_received"
        src["observed_at"] = _row_get(capture, "captured_at")
        src["received_at"] = _row_get(capture, "captured_at")

    if latest is None:
        return _finish_status(
            src,
            "not_attempted",
            "never_attempted",
            "尚未执行按需取图；没有图像不等于摄像头故障。",
        )

    status = (_row_get(latest, "status") or "").lower()
    request_id = _row_get(latest, "request_id")
    error_msg = _row_get(latest, "error_msg") or ""
    created_age = _age_ms(now, _row_get(latest, "created_at"))
    src["last_attempt"] = {
        "result": "in_progress" if status in ("pending", "sent", "received") else (
            "ok" if status == "captured" else "fail"
        ),
        "at": _row_get(latest, "created_at"),
        "inferred": False,
        "error": error_msg or None,
        "request_id": request_id,
        "image_status": status,
    }

    if status in ("pending", "sent", "received"):
        # 超过图像任务超时时间仍未完成，按失败处理；不修改数据库。
        if created_age is not None and created_age > 30000:
            return _finish_status(
                src,
                "failed",
                "timeout",
                "本次取图超时；不是设备故障。"
                + (f"下面显示上次成功图像（{_age_text(capture_age)} 前）。" if capture is not None else ""),
            )
        return _finish_status(src, "in_progress", "in_progress", "正在采集图像，请稍候。")

    if status in ("blocked", "failed", "timeout"):
        failure_kind = "blocked" if status == "blocked" else ("timeout" if status == "timeout" else "error")
        src["failure_kind"] = failure_kind
        if status == "blocked":
            detail = "画面不可用（遮挡/无效画面）"
        elif status == "timeout":
            detail = "取图超时"
        else:
            detail = f"取图失败（{error_msg or '未知错误'}）"
        return _finish_status(
            src,
            "failed",
            failure_kind,
            f"本次{detail}，不是设备故障；"
            + (
                f"下面显示上次成功图像（{_age_text(capture_age)} 前），它只是快照，不代表当前画面。"
                if capture is not None
                else "没有可用的上次成功图像。"
            ),
        )

    # captured：成功但只是快照；超过 TTL 标陈旧。
    if capture_age is None:
        return _finish_status(src, "unknown_age", "time_unknown", "图像缺少拍摄时间，时效未知。")
    if capture_age > ttl["camera_ms"]:
        return _finish_status(
            src,
            "stale",
            "ttl_expired",
            f"最近一次取图在 {_age_text(capture_age)} 前，已超过 {ttl['camera_ms']}ms；"
            "它是快照，不能代表当前画面。",
        )
    return _finish_status(
        src,
        "ok",
        "fresh_snapshot",
        f"最近一次取图成功（{_age_text(capture_age)} 前）；图像是快照，不代表当前画面。",
    )


# ==================== 网络链路 ====================

def _link_source(
    conn: sqlite3.Connection,
    now: float,
    ttl: Dict[str, int],
    ds: Optional[sqlite3.Row],
    imu_row: Optional[sqlite3.Row],
) -> Dict[str, Any]:
    src = _base_source("link", "网络链路", "link")
    src["ttl_ms"] = ttl["link_ms"]
    src["can_represent_current"] = False

    if ds is None:
        return _finish_status(
            src,
            "not_attempted",
            "never_attempted",
            "尚未收到设备心跳；无法判断链路状态。",
        )

    last_seen = _row_get(ds, "last_seen_at")
    age_ms = _age_ms(now, last_seen)
    src["age_ms"] = _round_ms(age_ms)
    src["observed_at"] = last_seen
    src["received_at"] = last_seen
    src["age_basis"] = "server_seen"
    src["raw"] = {
        "boot_id": _row_get(ds, "boot_id"),
        "uptime_ms": _row_get(ds, "uptime_ms"),
        "imu_attempt": _row_get(ds, "imu_attempt"),
        "imu_fail_count": _row_get(ds, "imu_fail_count"),
    }
    src["raw_ref"] = f"device_status:{_row_get(ds, 'device_id')}"
    upload_at = _row_get(imu_row, "received_at")
    src["last_upload"] = {
        "at": upload_at,
        "age_ms": _round_ms(_age_ms(now, upload_at)),
    }
    src["last_attempt"] = {
        "result": "ok",
        "at": last_seen,
        "inferred": False,
        "error": None,
    }

    if age_ms is None:
        return _finish_status(src, "unknown_age", "time_unknown", "链路时间不可比较，无法判断在线状态。")
    if age_ms > ttl["link_ms"]:
        return _finish_status(
            src,
            "stale",
            "offline",
            f"设备已 {_age_text(age_ms)} 没有请求；链路离线，无法确认设备状态，也不据此判定故障。",
        )
    return _finish_status(
        src,
        "ok",
        "online",
        "设备链路可达；在线只代表能收到请求，不代表各传感器数据已更新。",
    )


def _gnss_source() -> Dict[str, Any]:
    src = _base_source("gnss", "定位", "absent")
    src["not_a_fault"] = True
    src["last_attempt"] = {"result": "none", "at": None, "inferred": False}
    return _finish_status(
        src,
        "absent",
        "missing_hardware",
        "ESP32-S3-EYE 未配置 GNSS 模块；这是来源缺失，不是设备故障，也不提供任何坐标。",
    )


# ==================== 对照案例 ====================

def _comparisons(sources: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把课程要求的两个反例做成可直接渲染的对照项。"""
    camera = sources["camera"]
    imu = sources["imu"]
    link = sources["link"]

    old_but_failed: Dict[str, Any] = {"available": False, "message": ""}
    failed_source = None
    for key in ("camera", "imu"):
        src = sources[key]
        if src["status"] == "failed" and src.get("last_success"):
            failed_source = src
            break
    if failed_source is not None:
        failure = failed_source.get("failure_kind") or failed_source.get("status_reason")
        last_age = failed_source["last_success"].get("age_ms")
        old_but_failed = {
            "available": True,
            "source": failed_source["source"],
            "title": "旧数据 + 本次失败",
            "failure": failure,
            "last_success_age_ms": last_age,
            "message": (
                f"本次 {failure} 失败；上次成功数据在 {_age_text(last_age)} 前。"
                "旧数据仍保留，但不能当作当前状态。"
            ),
        }

    link_ok_data_stale: Dict[str, Any] = {"available": False, "message": ""}
    if link["status"] == "ok":
        # 只拿 IMU 做"网络在线但数据未更新"的对照：
        # 摄像头本来就是按需快照，旧图不应被误读成链路问题。
        src = sources["imu"]
        if src["status"] == "stale":
            link_ok_data_stale = {
                "available": True,
                "source": src["source"],
                "title": "网络在线但数据未更新",
                "link_age_ms": link.get("age_ms"),
                "data_age_ms": src.get("age_ms"),
                "message": (
                    f"链路在线（{_age_text(link.get('age_ms'))} 前有请求），"
                    f"但{src['label']}已 {_age_text(src.get('age_ms'))} 未更新；"
                    "网络在线不等于数据新鲜。"
                ),
            }

    return {
        "old_but_failed": old_but_failed,
        "link_ok_data_stale": link_ok_data_stale,
    }


# ==================== 入口 ====================

def build_report(
    db_path: str,
    now: Optional[float] = None,
    ttl: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """构造多来源状态报告；纯读，不修改数据库。

    Args:
        db_path: SQLite 数据库路径
        now: 测试可注入的固定服务器时间（epoch 秒）
        ttl: 测试可覆盖的阈值；默认读环境变量

    Returns:
        可直接 jsonify 的字典；sources 顺序固定为 imu/camera/link/gnss。
    """
    now = now_epoch() if now is None else float(now)
    ttl = ttl_config() if ttl is None else ttl

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        imu_row = _query_latest_imu(conn)
        ds = _query_device_status(conn)
        latest_image = _query_latest_image(conn)
        capture = _query_latest_capture(conn)

        imu = _imu_source(conn, now, ttl, imu_row, ds)
        camera = _camera_source(conn, now, ttl, latest_image, capture)
        link = _link_source(conn, now, ttl, ds, imu_row)
        gnss = _gnss_source()
        by_source = {"imu": imu, "camera": camera, "link": link, "gnss": gnss}
    finally:
        conn.close()

    return {
        "server_time": now,
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "ttl": ttl,
        "sources": [by_source[key] for key in STATUS_ORDER],
        "comparisons": _comparisons(by_source),
    }

# -*- coding: utf-8 -*-
"""下载视觉推理模型文件（第 8 周任务）

模型二进制（约 14MB）不入库，首次部署时运行本脚本获取：
    python server/vision/models/fetch_model.py

文件来源：ONNX Model Zoo（onnx/models 仓库，Apache-2.0）
  - mobilenetv2-12.onnx   MobileNetV2 图像分类模型（教师基线本地兜底）
  - imagenet_classes.txt  ImageNet 1000 类标签

已存在的文件会跳过；也可设置环境变量 MODEL_BACKEND/TEACHER_MODEL_URL
直接使用模型课提供的推理服务，无需本地模型。
"""
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

FILES = {
    "mobilenetv2-12.onnx":
        "https://github.com/onnx/models/raw/main/validated/vision/classification/mobilenet/model/mobilenetv2-12.onnx",
    "imagenet_classes.txt":
        "https://raw.githubusercontent.com/pytorch/hub/master/imagenet_classes.txt",
}


def fetch(name, url):
    dest = os.path.join(HERE, name)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"[SKIP] {name} 已存在")
        return True
    print(f"[DOWNLOAD] {name} <- {url}")
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as e:
        print(f"[FAIL] {name}: {e}")
        if os.path.exists(dest):
            os.remove(dest)
        return False
    size = os.path.getsize(dest)
    if size == 0:
        print(f"[FAIL] {name} 下载结果为空")
        os.remove(dest)
        return False
    print(f"[OK] {name} ({size / 1024:.0f} KB)")
    return True


if __name__ == "__main__":
    ok = all(fetch(name, url) for name, url in FILES.items())
    if not ok:
        print("\n部分文件下载失败。若无法访问外网，可改用模型课推理服务：")
        print("  设置环境变量 TEACHER_MODEL_URL 后重启服务（MODEL_BACKEND=teacher）。")
        sys.exit(1)
    print("\n模型文件就绪。安装依赖后重启服务即可：pip install -r requirements.txt")

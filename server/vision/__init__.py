# -*- coding: utf-8 -*-
"""视觉推理包（第 8 周任务）

对外只暴露统一入口 run_inference 与模型选择 get_model，
app.py 不直接依赖具体后端（HTTP/ONNX/学生模型），便于替换。
"""

from .models import (  # noqa: F401
    InferenceResult,
    BaseVisionModel,
    LocalOnnxModel,
    TeacherHttpModel,
    TeacherBaselineModel,
    StudentModel,
    get_model,
    run_inference,
    check_usability,
    decide_acceptance,
    coarse_category,
    HIGH_SCORE_THRESHOLD,
    LOW_SCORE_THRESHOLD,
    AMBIGUOUS_MARGIN,
    SCORE_NOTE,
    COARSE_NOTE,
)

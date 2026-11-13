# ESP32-S3 IMU 数据采集系统

基于 ESP32-S3-EYE 开发板的 IMU（加速度计）数据采集与实时展示系统。

## 系统架构

```
┌─────────────────┐      WiFi/HTTP      ┌──────────────────┐
│   ESP32-S3-EYE  │  ───────────────→   │   Flask 服务器    │
│                 │   JSON 数据上传      │                  │
│  • QMA7981 IMU  │                     │  • 数据接收 API   │
│  • WiFi 上传    │                     │  • SQLite 存储    │
│  • 串口调试     │                     │  • Web 实时展示   │
└─────────────────┘                     └──────────────────┘
```

## 功能特性

### ESP32 端
- ✅ QMA7981 加速度计 I2C 读取
- ✅ WiFi 连接与自动重连
- ✅ HTTP POST 上传 IMU 数据
- ✅ 串口实时输出（调试用）
- ✅ 批量上传优化（减少网络请求）
- ✅ OV2640 摄像头环境图像按需采集（JPEG 抓取；暗光/遮挡帧仍上传）
- ✅ 图像采集任务状态机（pending → sent → received → captured / failed）
- ✅ IMU I2C 总线与摄像头 SCCB 时分复用，采集后自动恢复 IMU 采样
- ✅ 主动询问的实体答复通道（第 9 周）：询问下发、LED 双闪、短按=重拍 / 长按=不用了
- ✅ 多来源独立状态与数据年龄（第 10 周）：IMU / 摄像头 / 链路 / GNSS 缺失分开显示
- ✅ 观测时间与接收时间分离：补传旧数据按原观测时间解释，不覆盖最新数据
- ✅ 陈旧、失败、未尝试、缺失、时效未知五类状态与用户提示对照
- ✅ GNSS 未配置明确标缺失，不生成任何坐标；失败/离线不写成设备故障

### 服务端
- ✅ Flask REST API 接收数据
- ✅ SQLite 数据库持久化存储
- ✅ Web 前端实时数据展示
- ✅ 历史数据图表（Chart.js）
- ✅ 数据统计与采样率计算
- ✅ 环境图像按需采集 API（下发任务 / 上传 JPEG / 状态查询）
- ✅ 图像采集失败记录（遮挡 blocked / 错误 error）与保留策略（保留最近 30 张，纠错样本永不清理）
- ✅ 自然语言助手支持「环境取图」意图
- ✅ 视觉推理结果结构化反馈：标签 / 模型分数 / 模型版本 / 判定依据 / 不确定说明（第 8 周）
- ✅ 低分 / 画面不可用（暗、模糊、遮挡）→ 拒识，明确“还不能确定”及原因（第 8 周）
- ✅ 人工纠错入口：模型原结果快照留存；纠错后锁定推理，保证结果—反馈对应（第 8 周）
- ✅ 样例对照：正常 / 低分 / 未知 / 错误 / 误判（高分误判高亮）（第 8 周）
- ✅ 画面不可用 → 主动询问「是否重拍」：事件触发、确认/忽略/误判纠正、冷却与单飞行（第 9 周）
- ✅ 重拍复用原有图像任务流，结果回填原询问；达重拍上限转求助建议（第 9 周）

## 硬件要求

- **开发板**：ESP32-S3-EYE
- **IMU 传感器**：QMA7981（I2C 接口）
- **接线**：
  - SDA → GPIO4
  - SCL → GPIO5
  - VCC → 3.3V
  - GND → GND

## 软件要求

### ESP32 端
- ESP-IDF v5.4.4 或更高版本
- Python 3.8+（ESP-IDF 依赖）

### 服务端
- Python 3.8+
- Flask 3.0+
- Flask-CORS 4.0+

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/yourusername/esp32-imu.git
cd esp32-imu
```

### 2. ESP32 固件编译与烧录

```bash
# 设置 ESP-IDF 环境
. $IDF_PATH/export.sh  # Linux/Mac
# 或
.\export.ps1           # Windows PowerShell

# 设置目标芯片
idf.py set-target esp32s3

# 配置 WiFi（编辑 main/wifi_manager.h）
# 修改 WIFI_SSID 和 WIFI_PASS

# 配置服务器地址（编辑 main/http_uploader.h）
# 修改 SERVER_URL

# 编译
idf.py build

# 烧录（替换 COMx 为实际串口）
idf.py -p COM5 flash monitor
```

### 3. 启动服务端

```bash
cd server

# 安装依赖
pip install -r requirements.txt

# 第 8 周：下载视觉推理模型（约 14MB，也可改用模型课推理服务，见下）
python vision/models/fetch_model.py

# 启动服务器
python app.py
```

访问 http://localhost:5000 查看实时数据。

> **第 8 周模型后端切换**（教师基线与学生候选模型同一接口）：
> - `MODEL_BACKEND=auto`：学生候选就绪则用学生，否则用教师基线（HTTP 优先，本地 ONNX 兜底）
> - `MODEL_BACKEND=teacher`：强制教师基线；`MODEL_BACKEND=student`：学生候选，未就绪时回退教师基线
> - 模型分数是**未经校准的模型输出分数**，界面只表述为"模型分数"，不写成现实正确率
> - 教师 HTTP 契约：`POST image/jpeg`，返回 `model_version` 和 `topk`（或 `label/score`）；分数必须在 0–1，标签和模型版本不能为空

## 项目结构

```
esp32-imu/
├── main/                      # ESP32 固件源码
│   ├── main.c                 # 主程序
│   ├── qma7981.h              # QMA7981 驱动头文件
│   ├── qma7981.c              # QMA7981 驱动实现
│   ├── wifi_manager.h         # WiFi 管理器头文件
│   ├── wifi_manager.c         # WiFi 管理器实现
│   ├── http_uploader.h        # HTTP 上传器头文件（含服务器地址）
│   ├── http_uploader.c        # HTTP 上传器实现
│   ├── command_handler.c      # 命令轮询与任务路由（含图像任务）
│   ├── image_capture.h        # OV2640 拍照模块头文件
│   ├── image_capture.c        # OV2640 拍照模块实现
│   ├── inquiry_handler.h      # 第 9 周：主动询问的设备侧（答复与状态）
│   ├── inquiry_handler.c      # 第 9 周：询问接收、按键答复、超时清理
│   ├── http_client_util.h     # 第 9 周：从 command_handler 原样提取的 HTTP 工具
│   ├── http_client_util.c     # GET 文本 / POST JSON（command_handler 与询问共用）
│   ├── sampling_state.h       # 第 10 周：最近一次采样尝试/boot_id（只记录状态）
│   ├── sampling_state.c       # 第 10 周：线程安全的采样状态实现
│   └── idf_component.yml      # ESP-IDF 组件依赖（esp32-camera）
├── server/                    # 服务端代码
│   ├── app.py                 # Flask 应用主程序
│   ├── inquiry.py             # 第 9 周：主动询问的门控/状态机/冷却与 API
│   ├── requirements.txt       # Python 依赖
│   ├── test_week6.py          # 第 6 周功能自动化测试
│   ├── test_week7.py          # 第 7 周功能自动化测试
│   ├── test_week8.py          # 第 8 周功能自动化测试
│   ├── test_week9.py          # 第 9 周功能自动化测试（含原始记录导出）
│   ├── week9_evidence.json    # 第 9 周完整任务测试原始记录（提交物）
│   ├── source_state.py        # 第 10 周：来源状态/年龄/对照聚合（纯读）
│   ├── test_week10.py         # 第 10 周时间处理与来源缺失测试
│   ├── week10_evidence.json   # 第 10 周测试原始记录（提交物）
│   ├── images/                # 采集到的环境图像（运行时生成）
│   ├── vision/                # 视觉推理模块（第 8 周）
│   │   ├── models.py          # 统一推理接口/教师基线/学生接入位/拒识逻辑
│   │   └── models/            # 模型文件与测试夹具
│   │       ├── fetch_model.py # 模型下载脚本（onnx 不入库）
│   │       └── fixtures/      # 测试夹具
│   │           ├── manifest.json  # 夹具来源与 SHA-256
│   │           └── *.jpg      # 真实 JPEG 测试样本
│   └── templates/
│       └── index.html         # Web 前端页面
├── partitions.csv             # 8MB Flash 分区表（factory 3MB）
├── CMakeLists.txt             # ESP-IDF 项目配置
├── sdkconfig.defaults         # ESP-IDF 默认配置
├── .gitignore                 # Git 忽略文件
└── README.md                  # 项目说明
```

## API 文档

### 接收 IMU 数据

**POST** `/api/imu`

请求体：
```json
{
  "timestamp": 1234567890.123,
  "ax": 0.123,
  "ay": -0.456,
  "az": 9.780
}
```

响应：
```json
{
  "status": "success",
  "message": "数据接收成功",
  "timestamp": 1234567890.123
}
```

### 获取最新数据

**GET** `/api/imu/latest`

响应：
```json
{
  "timestamp": 1234567890.123,
  "ax": 0.123,
  "ay": -0.456,
  "az": 9.780,
  "created_at": "2024-01-01 12:00:00"
}
```

### 获取历史数据

**GET** `/api/imu/history?limit=100`

响应：
```json
{
  "data": [...],
  "count": 100
}
```

### 获取统计信息

**GET** `/api/stats`

响应：
```json
{
  "total_records": 12345,
  "latest_record": "2024-01-01 12:00:00"
}
```

### 环境图像按需采集（第 7 周）

前端点击「采集一张」后，服务端创建一条图像任务（状态 `pending`），ESP32 轮询到任务后拍照并回传 JPEG。相关接口：

**POST** `/api/image`
创建一条图像采集任务（默认 `source=device`）。
```json
{ "source": "device" }
```
响应：
```json
{ "request_id": "xxxx", "status": "pending", "source": "device" }
```

**GET** `/api/collect/pending`
ESP32 轮询待处理任务；图像任务会携带 `type=image`、`sensor=camera`。

**POST** `/api/image/<id>/received`
ESP32 取到任务后回报已接收（状态 `sent` → `received`）。

**POST** `/api/image/<id>/data`（raw JPEG body）
ESP32 上传 JPEG 图像，需携带 `X-Capture-Ts` 头（设备微秒时间戳）；服务端校验 JPEG 头 `\xff\xd8`。成功后状态置为 `captured`。

**POST** `/api/image/<id>/fail`
采集失败回报，body：`{ "reason": "blocked" | "error" }`。

**GET** `/api/image/<id>`
查询任务状态与元数据（status、captured_at、device_ts_us、size_bytes 等）。

**GET** `/api/image/<id>/file`
下载该任务对应的 JPEG 文件。

**GET** `/api/images`
列出全部图像任务（保留最近 30 张；已有人工纠错的图片永不清理）。

### 视觉推理与人工纠错（第 8 周）

采集成功后自动推理一次。所有结果按 `request_id` 与输入图像、人工反馈严格对应；分数只表述为“模型输出分数（未经校准）”，不写成现实正确率。

**POST** `/api/image/<id>/infer`

对已采集图像执行（重新）推理。已有人工纠错时返回 409（锁定推理结果，避免覆盖反馈对应的原结果）；模型不可用时返回 503（不伪造结果）。

**GET** `/api/image/<id>/result`

结构化结果：

- `image`：输入记录（原图 URL、采集时间、来源）；
- `inference`：`label`、`score`、`model_name`、`model_version`、`model_source`、`topk`、`usability`、`is_rejected`、`rejected_reason`、`uncertain_note`；
- `decision`：判定依据（top1/top2/分差/阈值/画面指标）；
- `bucket`：样例归类；
- `feedback`：人工纠错快照；
- `score_note`：分数语义提示。

**POST** `/api/image/<id>/feedback`

人工纠错：`{corrected_label, note}`。模型原结果以快照留存（original_label/original_score），不覆盖推理结果；提交后该任务的推理结果锁定。

**GET** `/api/samples/compare?limit_per_bucket=8`

样例对照（四类基础 + 误判叠加）：

| 桶 | 含义 |
|---|---|
| `normal` | 模型接受且无纠错；不代表真实正确率 |
| `low_score` | 模型拒识为低分或候选接近 |
| `unknown` | 画面不可用 / 模型不可用；拒识后被人工补充答案也在此桶 |
| `error` | 采集失败、超时或图像无法解码 |
| `misjudged` | 模型接受但被人工纠错；`is_high_score` 标记高分误判 |

响应同时返回 `bucket_notes`、`score_note` 和每桶数量，前端直接展示，避免口径分叉。

### 第 8 周验收与演示

**真实模型准备（二选一）**

```bash
# 本地 ONNX 教师基线兜底
python server/vision/models/fetch_model.py

# 或使用模型课教师 HTTP 服务
export MODEL_BACKEND=teacher
export TEACHER_MODEL_URL=http://<teacher-host>/infer
export TEACHER_MODEL_API_KEY=<optional>
```

**严格测试（模型不可用即失败）**

```bash
cd server
REQUIRE_REAL_MODEL=1 WEEK8_EVIDENCE=week8_evidence.json python test_week8.py
```

**5 分钟演示顺序**

1. 正常画面：展示标签、模型分数（小数，未校准）、模型版本和原图；
2. 遮住镜头：展示黑图 + “画面过暗/疑似遮挡” + 重采建议，证明失败画面仍有输入；
3. 低分图：展示“还不能确定”、top1/top2/分差和拒识阈值；
4. 人工纠错：展示“模型原判 → 人工修正”，再点对照样例，原图/结果/反馈三者同屏；
5. 样例对照：正常、低分、未知、错误、误判五列，高分误判高亮。

**证据口径**

- `source=device` 的真机采集 + 真实模型结果，计入端到端证据；
- `source=replay` 的真实 JPEG + 真实模型，计入自动化回归，但不替代真机证据；
- 静态标签、mock 结果只用于 UI 测试，不写入推理结果表，不计入验收；
- 不计算、不宣称模型准确率；`normal` 只表示“未被人工否定”；
- 场景粗分类是规则后处理，不用于精确位置、危险程度或人身安全判断。

### 画面不可用的主动询问（第 9 周）

本周问题：画面无法使用时，系统能否主动询问「是否重拍」，并尊重用户选择？

**事件来源（三源归一）**

| 来源 | 触发点 | `trigger` |
|---|---|---|
| 设备 | `POST /api/image/<id>/fail` 上报 `blocked` | `device` |
| 服务端 | 推理后 `usability` 为 `dark` / `blur` / `blocked` | `server` |
| 模型 | 推理 `status` 为 `unavailable` / `decode_failed` | `model` |

低分（`low_score`）与候选接近（`ambiguous`）属于"模型不确定"，不是"画面无法使用"，**不触发**询问。

**状态机**

```mermaid
stateDiagram-v2
    [*] --> asking: 事件 + 门控通过
    asking --> confirmed: 重拍
    asking --> declined: 不用了
    asking --> dismissed: 其实可用（误判纠正）
    asking --> expired: 30s 未答复
    confirmed --> asking: 重拍仍不可用且 attempt<2
    confirmed --> resolved: 重拍后画面可用
    confirmed --> exhausted: attempt=2 仍不可用
```

**门控与冷却（默认值，环境变量可覆盖）**

| 参数 | 默认 | 说明 |
|---|---|---|
| `INQUIRY_TIMEOUT_S` | 30 | 未答复自动按忽略处理，不重投、不重弹 |
| `INQUIRY_GLOBAL_GAP_S` | 60 | 任意两条询问之间的最小间隔 |
| `DECLINE_COOLDOWN_S` | 600 | 拒绝后同原因冷却（"拒绝后不反复打扰"的硬指标） |
| `EXPIRE_COOLDOWN_S` | 300 | 超时后同原因冷却 |
| `DISMISS_COOLDOWN_S` | 1800 | 判定"其实可用"后同原因冷却 |
| `MAX_RETAKE` | 2 | 单张图重拍次数上限，超限转 `exhausted` 并建议走求助 |
| `INQUIRY_ALLOW_REPLAY` | 0 | 回放来源是否参与（默认 0：回放只做演示/回归） |

单飞行：同一时刻只允许一条未决询问；同一 `request_id` 只问一次；重拍产生的新任务归属原询问，不再新开一条。

**反馈优先级**：求助回应（第 3 周，P0）> 主动询问（第 9 周，P1）> 普通状态提示（P2）。设备端 LED 由求助通道优先占用，求助空闲时才用双闪提示询问。

**接口**

| 端点 | 说明 |
|---|---|
| `GET /api/inquiry/pending` | 网页轮询当前未决询问（含剩余秒数与冷却参数） |
| `POST /api/inquiry/<id>/answer` | `{"decision": "retake"\|"decline"\|"usable", "channel": "ui"\|"voice"\|"device_key", "mute": false}` |
| `GET /api/inquiry/<id>` | 单条询问详情 |
| `GET /api/inquiries?limit=20` | 最近询问列表（验收取证用） |
| `GET /api/collect/pending` | 设备通道：新增 `type=inquiry`，复用既有轮询，设备 HTTP 次数不变 |

答复语义：

- `retake`：复用第 7 周图像任务流新建一次采集（`new_image_task`），结果回填原询问；
- `decline`：登记冷却，同原因不再追问（`mute=true` 时长更长）；
- `usable`：只记录误判纠正，不重拍，**不写**第 8 周人工纠错表（避免口径混用）。

重复答复返回 409：只认第一个有效答复，后续答复仅在日志留痕，不重复提示。

**三通道协同**：网页卡片（视觉）＋ 浏览器 TTS 播报与语音答复（语音）＋ 板载按键 / LED（实体）。实体：短按 = 重拍，长按 ≥800ms = 不用了；无询问时按键行为与第 3 周完全一致。

**第 9 周验收与演示**

```bash
cd server
venv\Scripts\python.exe test_week9.py    # 16 项；失败即非零退出
# 原始记录写入 server/week9_evidence.json
```

演示顺序（约 5 分钟）：

1. 正常采集：IMU 曲线照常，说明底层采集链路未被改动；
2. 遮住镜头取图：网页出现"画面被遮挡，要重拍一张吗？"，同时 LED 双闪、语音播报；
3. 点"重拍一张"：生成新的图像任务，设备真实取图，重拍结果回填原询问；
4. 点"不用了"（或长按按键）：10 分钟内同类情况不再询问，连续遮挡不再打扰；
5. 点"其实可用"：记录误判纠正，不重拍；
6. 说明运行位置：模型跑在服务端（本地 ONNX 或教师 HTTP），设备端只做采集与按键，服务在本机 `:5000`。

**证据口径**

- `source=device` 的真机采集计入端到端证据；
- `replay` 仅用于演示与自动化回归，默认不触发询问；
- `test_week9.py` 中"模型不可用"用例为故障注入，证据里标注 `injected`；
- 冷却参数在测试中被压到秒级，证据 `summary.config` 会如实记录，生产默认值见 `server/inquiry.py`。

### 区分传感器观测与当前状态（第 10 周）

本周问题：定位、运动和网络信息不是同时到达，怎样避免把旧数据当成当前状态？

本项目没有 GNSS，**不补造位置**；真实来源是 IMU、摄像头快照和网络链路。课程中的
“旧位置 + 本次 no_fix”用“旧图 + 本次 blocked”做同构案例，“网络在线但位置未更新”
用“链路在线 + IMU 陈旧”做真实案例。

**来源与阈值**

| source | 页面 | 性质 | 默认 TTL | 缺失/离线时 |
|---|---|---|---|---|
| `imu` | 加速度计 | 连续流 | `IMU_STALE_MS=5000` | 只标陈旧，不判故障 |
| `camera` | 环境图像 | 快照 | `CAMERA_STALE_MS=600000` | 失败保留旧图 |
| `link` | 网络链路 | 链路状态 | `LINK_OFFLINE_MS=10000` | 离线只表示无法确认 |
| `gnss` | 定位 | 未配置 | — | 固定 `absent`，不返回坐标 |

**三段时间与年龄**

```text
设备观测：observed_uptime_ms（qma7981_read_accel 返回后）
设备发送：send_uptime_ms（HTTP 发出前）
服务器接收：received_at（epoch 秒）

transit_ms  = send_uptime_ms - observed_uptime_ms
observed_at = received_at - transit_ms / 1000
age_ms      = now - observed_at
```

- 补传 120 秒前的样本时，滞留时间会进入 `transit_ms`，年龄仍是 120 秒；
- `/api/imu/latest` 按 `COALESCE(observed_at, received_at)` 取最新，补传旧行不会顶掉新行；
- 旧固件没有新字段时 `observed_at=NULL`、状态为 `unknown_age`，**不猜测** `timestamp` 的单位；
- 跨重启或 `transit_ms` 非法/超过 1 小时时同样标 `unknown_age`，不给出“新鲜”结论。

**状态语义**

| status | 含义 | 页面提示 |
|---|---|---|
| `not_attempted` | 从未尝试/从未收到 | 无法判断；没有数据不等于故障 |
| `in_progress` | 图像任务进行中 | 正在采集，请稍候 |
| `ok` | 最近成功且未超过 TTL | IMU 可作当前值；图像仍是快照；链路仅代表可达 |
| `stale` | 超过 TTL、设备离线或上传停滞 | 已 X 秒未更新，不能当作当前状态 |
| `failed` | 有明确失败证据（读失败/blocked/error/no_fix） | 本次失败；下面显示上次有效值，不代表现在 |
| `unknown_age` | 缺少可比较的观测时间 | 时效未知，不能当作当前状态 |
| `absent` | 来源未配置/硬件缺失 | 未配置此来源，不是设备故障 |

硬规则：

1. **静默不等于失败**：设备离线只标 `stale`，不写“设备故障”；
2. **失败不覆盖旧观测**：`failed` 只更新尝试状态，`imu_data` 旧行和旧 JPEG 原样保留；
3. **链路在线不等于数据新鲜**：`link.status=ok` 与 `imu.status=stale` 可以同时成立；
4. **图像是快照**：`camera.ok` 只表示最近一次取图成功，不代表当前画面。

**接口**

`GET /api/sources`：

```json
{
  "server_time": 1763000000.123,
  "ttl": {"imu_ms": 5000, "camera_ms": 600000, "link_ms": 10000},
  "sources": [
    {
      "source": "imu",
      "status": "stale",
      "status_reason": "upload_stalled",
      "status_label": "陈旧",
      "user_message": "设备仍在采样，但服务器已 12.3s 未收到新数据；不能当作当前状态。",
      "can_represent_current": false,
      "age_ms": 12300,
      "age_basis": "device_transit",
      "observed_at": 1762999987.0,
      "received_at": 1762999987.02,
      "transit_ms": 21,
      "last_attempt": {"result": "ok"},
      "last_success": {"age_ms": 12300},
      "raw": {"ax": 0.12, "ay": -0.03, "az": 9.79},
      "raw_ref": "imu_data:66846",
      "provenance": "device"
    }
  ],
  "comparisons": {
    "old_but_failed": {"available": true, "message": "..."},
    "link_ok_data_stale": {"available": true, "message": "..."}
  }
}
```

`/api/imu/latest` 新增 `observed_at / received_at / age_ms / age_basis / freshness /
is_stale / derived / derived_note`；陀螺仪与姿态仍保留兼容字段，但明确标注“由加速度估算”。

设备端心跳复用原有 2 秒轮询：

```text
GET /api/collect/pending?device_id=esp32-s3-eye&imu=ok|fail|busy&err=read&fails=N
```

不新增 HTTP 请求；`device_status` 只记录最近心跳与尝试结果。

**前端**

页面顶部新增“来源状态与数据年龄”区：

- 四张卡分别显示状态、年龄、观测/接收时刻、传输时延、本次尝试、原始值；
- 年龄每秒按服务端 `server_time` 递增，避免浏览器时钟偏差；
- IMU 非 `ok` 时，六轴数值上方出现“最近一次有效观测，不是当前状态”横幅；
- 下方自动展示两个对照：旧数据 + 本次失败、链路在线 + 数据未更新。

**验收与演示**

```bash
cd server
venv\Scripts\python.exe test_week10.py     # 16 项；失败即非零退出
# 原始记录写入 server/week10_evidence.json
```

演示顺序（约 5 分钟）：

1. 正常采样：IMU 卡 `ok`，年龄 <1s，`provenance=device`；
2. 遮挡镜头取图：本次 `blocked`，旧图仍在，出现“旧图 + 本次失败”对照；
3. 拔掉/遮挡 IMU：轮询上报 `fail`，IMU 卡 `failed`，旧值带年龄保留；
4. 断开设备：10s 后链路和 IMU 变 `stale`，不显示“设备故障”；
5. 补传一条 2 分钟前的样本：`/api/imu/latest` 不被覆盖，历史中该样本年龄为 2 分钟；
6. 说明 GNSS 卡固定为“缺失”，系统没有任何经纬度字段。

**证据口径**

- `source=device` 的真实设备数据计入端到端证据；
- `test_week10.py` 使用固定 `now` 注入与临时库，不替代真机复验；
- 不计算、不宣称位置精度；GNSS 缺失与摄像头 blocked 都不写成设备故障；
- TTL 只影响“能否当当前状态”，不删除历史观测。

## 排错指南

### ESP32 无法读取 IMU

1. **检查接线**：确认 SDA=GPIO4, SCL=GPIO5
2. **检查上拉电阻**：I2C 总线需要 4.7kΩ 上拉至 3.3V
3. **检查芯片 ID**：串口输出应显示 `芯片 ID 校验通过: 0x90`
4. **降低 I2C 频率**：修改 `qma7981.h` 中的 `QMA7981_I2C_FREQ_HZ` 为 100000

### WiFi 连接失败

1. **检查 SSID 和密码**：确认 `wifi_manager.h` 中的配置正确
2. **检查路由器**：确认 2.4GHz 频段可用（ESP32 不支持 5GHz）
3. **查看串口日志**：会显示详细的连接状态和错误信息

### 服务器无法接收数据

1. **检查服务器地址**：确认 `http_uploader.h` 中的 `SERVER_URL` 正确
2. **检查防火墙**：确保服务器端口 5000 可访问
3. **测试 API**：使用 curl 或 Postman 测试 `/api/imu` 接口

### 画面不可用但没有出现询问

1. **是否在冷却期**：`GET /api/inquiries?limit=5` 看最近一条的 `cooldown_until`；拒绝过同类情况后 10 分钟内不会再问
2. **是否已有未决询问**：单飞行，已有一条 `asking` 时新事件不会新建
3. **原因是否可询问**：低分 / 候选接近不触发询问（属于模型不确定）
4. **来源是否为回放**：`INQUIRY_ALLOW_REPLAY=0`（默认）时回放图不触发

## 技术细节

### QMA7981 驱动

- **I2C 地址**：0x12
- **芯片 ID**：0x90（实际读取值，可能与数据手册不同）
- **数据格式**：14 位有符号数，左对齐
- **量程**：±2g / ±4g / ±8g / ±16g 可选
- **采样率**：128Hz（默认）

### 数据传输协议

- **协议**：HTTP POST
- **格式**：JSON
- **频率**：每 10 个样本上传一次（可配置）
- **时间戳**：观测时的开机微秒（`esp_timer_get_time()`，不是 Unix 时间）；第 10 周另带 `observed_uptime_ms` / `send_uptime_ms`，由服务端换算数据年龄

### 数据库设计

```sql
CREATE TABLE imu_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,      -- 观测时开机微秒（非 Unix 时间）
    ax REAL NOT NULL,             -- X 轴加速度
    ay REAL NOT NULL,             -- Y 轴加速度
    az REAL NOT NULL,             -- Z 轴加速度
    request_id TEXT,              -- 远程采集任务关联
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,  -- 服务器接收时间（UTC 文本）
    observed_at REAL,             -- 第 10 周：观测时刻（epoch 秒，服务端换算）
    received_at REAL,             -- 第 10 周：接收时刻（epoch 秒）
    observed_uptime_ms INTEGER,   -- 第 10 周：设备观测开机毫秒
    send_uptime_ms INTEGER,       -- 第 10 周：设备发送前开机毫秒
    boot_id TEXT,                 -- 第 10 周：本次开机标识
    age_basis TEXT,               -- 第 10 周：device_transit / unknown
    provenance TEXT NOT NULL DEFAULT 'device'
);

CREATE TABLE device_status (
    device_id TEXT PRIMARY KEY,
    last_seen_at REAL,            -- 最近一次请求（2s 轮询/上传）
    boot_id TEXT,
    uptime_ms INTEGER,
    imu_attempt TEXT,             -- ok / fail / busy / unknown
    imu_error TEXT,               -- read / timeout / camera_busy ...
    imu_fail_count INTEGER DEFAULT 0,
    updated_at REAL
);
```

## 扩展方向

- [ ] 添加更多传感器（陀螺仪、磁力计、气压计）
- [ ] 实现数据融合算法（卡尔曼滤波、互补滤波）
- [ ] 添加机器学习模型（动作识别、异常检测）
- [ ] 支持 MQTT 协议
- [ ] 添加用户认证和权限管理
- [ ] 实现 OTA 固件更新
- [ ] 添加数据导出功能（CSV、JSON）
- [ ] 支持多设备同时连接

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！

## 联系方式

如有问题，请通过 GitHub Issues 联系。

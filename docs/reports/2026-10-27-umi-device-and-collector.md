# UMI 设备验证与数据采集框架工作报告

- 日期：2026-10-27
- 目标机器：`zy@23.254.219.130:7022`（Ubuntu 22.04，x86_64，内核 `6.1.59-rt16`）
- 工作目录：`/home/zy/workspace/supre_umi`
- 范围：设备复验、设备状态工具、Hysteria 代理、LeRobot v3.0 同步采集框架、借鉴 FOMI 的标定/同步/差分软件

## 结论摘要

| 项目 | 结果 |
| --- | --- |
| AS5600 夹爪编码器 | 可读（`0x36`，现总线 `i2c-3`），但静止时存在约 **50.0 Hz** 角度抖动 |
| CH341 USB-I2C | 驱动正常，适配器名 `ch34x-mphsi-i2c` |
| Vive Tracker | `LHR-69F5265A` 位姿有效（`Running_OK`），基站 `LHB-D62AE5D6` 在线 |
| USB 相机 | `/dev/video0`，640×480，约 20–33 fps |
| 采集框架 | LeRobot v3.0，自包含 writer，三路同步 + QC，已实测落盘并反读校验 |
| 夹爪标定 | 分段二次 + 迟滞模型工具已完成，合成数据验证通过；真实标定待人工扫描 |
| 双编码器差分 | 软件就绪（`DualAs5600` / `wrap_diff`），硬件只有一片 AS5600，未启用 |

## 1. 环境与访问

- 通过 SSH 密码登录后安装本机公钥，后续免密。
- 系统 Python 3.10.12；`cv2 4.5.4` 来自 apt（依赖 numpy 1.x）。
- OpenVR 原只在 `/home/zy/tools/streamvr/.venv`；本次把 `openvr` 也装入系统 `--user`，使单进程可同时使用 cv2、openvr、pyarrow。
- 已把用户 `zy` 加入 `i2c` 组（`sudo usermod -aG i2c zy`）；重新登录后永久生效，当前会话可用 `sg i2c -c`。

## 2. 设备复验

### 2.1 AS5600 / CH341

- CH341 `1a86:5512` 存在，驱动 `ch34x_mphsi_master` 已加载。
- 适配器现在是 **`i2c-3`**（旧文档记录的 `i2c-13` 现为显卡 DP AUX 总线）。工具按适配器名
  `ch34x-mphsi-i2c` 自动发现，不写死总线号。
- 只读寄存器：`STATUS=0x67`（检测到磁铁）、`CONF=0x00`、`ZPOS=0x0000`、`MPOS=0x0000`（均未编程）、
  `AGC≈98`、`MAGNITUDE≈2120`（稳定）。
- **新发现**：夹爪静止时 RAW ANGLE 与 ANGLE 同步以 **50.00 Hz** 周期摆动，跨度约 **1161 计数**
  （约 102°），而 AGC 与磁场幅值稳定。用标准 `i2ctransfer` 复读结果一致，排除读取错误。
  `gripper_calibrate.py analyze` 以实际采样率（约 363 Hz）做 FFT，主频 50.00 Hz。
- 结论：单次瞬时读数不能当宽度；需时间平均（已实现环形均值），根因（工频干扰/机械振动/充磁几何）
  尚未确认，双编码器差分是建议优先验证的方向。

### 2.2 Vive Tracker / SteamVR

- 早先复验：`connected=true` 但 `eTrackingResult=Calibrating_OutOfRange`，无有效位姿。
- 重启 SteamVR 后：`LHR-69F5265A` `conn=Y pose=Y Running_OK`，基站 `LHB-D62AE5D6` 在线，
  电量 100%，位置约 `(+0.185, -1.206, -3.616) m`。
- 采集器退出时不调用 `openvr.shutdown()`，避免 null 头显配置下 vrserver 随之退出。

### 2.3 相机

- `/dev/video0`（HD USB Camera），640×480 MJPG，实测约 20–33 fps（随负载波动）。

## 3. 交付工具（`umi_devices/`）

| 文件 | 作用 |
| --- | --- |
| `umi_status.py` | 三路状态汇总（文本/JSON），设备就绪返回 0 |
| `gripper_as5600.py` | AS5600 读取、流式采样、两点标定、`DualAs5600` 差分、`wrap_diff` |
| `gripper_calib.py` | 分段二次 + 迟滞标定模型（约束最小二乘）、运行时映射、抖动分析 |
| `gripper_calibrate.py` | 标定 CLI：`record` / `fit` / `analyze` / `selftest` |
| `tracker_openvr.py` | OpenVR 枚举、位姿、电量、跟踪结果 |
| `camera_v4l2.py` | V4L2 列举与帧率检查 |
| `lerobot_v30.py` | 自包含 LeRobot v3.0 writer（pandas/pyarrow/ffmpeg） |
| `umi_collect.py` | 三路同步采集主程序（最近邻对齐 + QC） |

## 4. Hysteria 代理与依赖

- 远端 `/home/zy/tools/hysteria` 已有 Hysteria v2.6.5 与 `hiddify_config.yaml`（服务端 `23.254.219.130:36099`），
  HTTP 代理 `0.0.0.0:8080`、SOCKS5 `0.0.0.0:1080`，实测可访问 `https://pypi.org`。
- 经代理安装到 `--user`：`pyarrow 25.0.1`、`pandas 2.3.3`、`openvr 2.12.1401`、`datasets 5.1.0`。
- **numpy 固定 1.26.4**：numpy 2.x 会破坏 apt 版 `cv2 4.5.4`。
- 为对齐格式，下载并解包了 `lerobot 0.4.4` 源码（未安装 torch），按其 `CODEBASE_VERSION="v3.0"` 的
  落盘结构逐字段实现 writer。

## 5. LeRobot v3.0 采集框架

### 5.1 架构

```text
CameraStream  (连续取帧, 时间戳缓冲)  ─┐
TrackerStream (OpenVR ~30Hz, 缓冲)    ─┼─> 固定 fps 主循环 ──> EpisodeRecorder ──> meta/data/videos
GripperStream (AS5600, 缓冲+环形均值) ─┘   每 tick 取最近样本      JPEG 暂存 + 数值行
```

- 每路保留带时间戳的短缓冲；主循环在每个 tick 取**时间上最近的样本**，并记录偏差；
  偏差 > `--max-sample-age`（默认 50 ms）记为缺失。
- 同一帧三路共享 `timestamp = frame_index / fps`。
- 夹爪宽度对最近 16 个读数做环形均值（约 0.3 s），抑制 50 Hz 抖动。
- 视频：帧暂存为 JPEG，episode 结束时用 ffmpeg 编为 H.264/yuv420p。

### 5.2 数据集字段

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `observation.images.camera0` | video H×W×3 | 相机画面 |
| `observation.state` | float32[1] | 夹爪宽度 mm |
| `observation.pose` | float32[7] | Tracker `[x,y,z,qx,qy,qz,qw]`，米 |
| `observation.valid` | float32[1] | 位姿有效性 1/0 |
| `action` | float32[8] | 当前 `pose(7)+宽度` |

外加标准列 `timestamp/frame_index/episode_index/index/task_index`。

### 5.3 落盘结构（v3.0，每 episode 一个文件）

```text
<out>/meta/info.json
<out>/meta/stats.json
<out>/meta/tasks.parquet
<out>/meta/episodes/chunk-000/file-000.parquet
<out>/data/chunk-000/file-000.parquet
<out>/videos/observation.images.camera0/chunk-000/file-000.mp4
<out>/qc/episodes.jsonl                     # 额外：QC 汇总
<out>/qc/episode-000000.jsonl               # 可选：逐帧对齐诊断
```

`meta/episodes` 含 data/video 的 chunk/file 索引、`dataset_from/to_index`、视频 `from/to_timestamp`、
以及每个特征的 `stats/*`；`meta/stats.json` 为跨 episode 加权聚合。

### 5.4 QC

- 每 episode 记录相机/Tracker/夹爪缺失数、位姿失效率、最大相机时差、是否通过。
- `--qc-per-frame` 输出逐帧偏差；`--drop-bad-episodes` 配合 `--max-missing-ratio`、
  `--max-invalid-pose-ratio` 丢弃不合格 episode。

## 6. 借鉴 FOMI 的软件升级

参考 [FOMI (arXiv 2610.19839)](https://arxiv.org/abs/2610.19839) 的采集系统（力反馈机构属硬件改造，未采用）。

1. **夹爪分段标定**：`f(x,σ)=p_k(x)+c_σ`，分段二次 + 方向相关偏置（迟滞），段间值连续；
   连续性约束下用零空间 + numpy `lstsq` 求解；运行时 5 样本滑窗推断方向；
   采集器用 `--gripper-calib-file` 加载。
2. **同步与 QC**：时间戳缓冲 + 最近样本对齐 + 偏差记录 + 坏 episode 丢弃（见 5.4）。
3. **双编码器差分（软件就绪）**：`DualAs5600` 读第二片 AS5600（必须另一块 CH341），
   输出 `wrap_diff(enc1,enc2)`；`--gripper-bus2` 启用；单编码器自动降级。
   当前硬件只有一片，未启用。

## 7. 验证记录

| 项目 | 结果 |
| --- | --- |
| 无硬件自检（writer） | 90 帧合成 episode，结构校验 OK |
| 反读校验 | HF `datasets` 按 `info.json` features 读取成功，`list<float32>`→`Sequence(7)` 正常，四元数范数 1.0 |
| 真实采集（相机+夹爪+Tracker，3 s/30 fps） | 90 帧，`valid` 全 1；视频 90 帧/3.000 s；QC 通过（相机缺失 0、位姿无效 0、最大相机时差 32 ms） |
| 标定模型接入 | 60 帧正常落盘，宽度按模型映射 |
| 标定拟合器 | 合成数据 test RMSE 0.016 mm；线性基线对比正常 |
| 抖动分析 | 主频 50.00 Hz，实际采样约 363 Hz，跨度约 1161 计数 |
| `umi_status` | 三路均 OK（Tracker `Running_OK`，相机约 33 fps） |

## 8. 文件与文档

- 代码：`umi_devices/` 下 8 个 Python 文件（见第 3 节）。
- 文档更新：
  - `umi_devices/README.md`：采集、标定、同步/QC、双编码器用法。
  - `docs/design/umi-data-collector.md`：采集器实现（2026-10-27）、FOMI 借鉴升级、抖动分析结论。
  - `docs/devices/umi-gripper-as5600.md`：2026-10-27 复验（总线变化、50 Hz 抖动）。
  - `docs/devices/vive-tracker-steamvr-openvr.md`：2026-10-27 复验与“位姿已有效”记录。

## 9. 已知问题与后续

- **50 Hz 抖动根因未确认**；环形均值只是缓解。建议验证双编码器差分（需第二片 AS5600 + 第二块 CH341）。
- **真实宽度标定未完成**：需人工把夹爪放到已知宽度并跑 `gripper_calibrate.py record/fit`；
  未标定时 `observation.state` 是理论换算 `计数×80/4096`，不可当精确宽度。
- `action` 目前是绝对 `pose+宽度`；若训练管线要相邻帧相对位姿，需在导出阶段计算。
- 采集依赖 SteamVR 常驻；相机约 20–33 fps，`--max-sample-age` 默认 50 ms 较合适。
- 长 episode 会在临时目录占用与视频等量的 JPEG 空间，编码在 episode 结束时进行。

## 10. 命令速查

```bash
cd /home/zy/workspace/supre_umi/umi_devices

# 状态
sg i2c -c "python3 umi_status.py"
python3 umi_status.py --json

# 采集（交互：s 开始 / e 结束 / q 退出）
sg i2c -c "python3 umi_collect.py --out ~/datasets/umi_demo --fps 30 --task pick"

# 脚本化采集
sg i2c -c "python3 umi_collect.py --out ~/datasets/umi_demo --episodes 5 --duration 10 --task pick"

# 标定
sg i2c -c "python3 gripper_calibrate.py record --out calib.csv \
    --levels 0,10,20,30,40,50,60,70,80 --trips 3"
python3 gripper_calibrate.py fit --input calib.csv --out gripper_calib.json
sg i2c -c "python3 umi_collect.py --out ~/datasets/umi_demo --gripper-calib-file gripper_calib.json"

# 抖动分析
sg i2c -c "python3 gripper_calibrate.py analyze --seconds 2"

# 校验与自检
python3 umi_collect.py --validate-only ~/datasets/umi_demo
python3 umi_collect.py --out /tmp/umi_selftest --selftest
```

## 11. 追加：LeRobot v2.1 支持（同日）

为兼容旧版训练管线，新增 v2.1 输出。

- 代码：`umi_devices/lerobot_v21.py`（`LeRobotV21Writer` / `EpisodeRecorderV21`）；
  `umi_collect.py` 增加 `--format {v3.0,v2.1}`，默认 v3.0。
- 复用：统计计算、parquet 列 schema、JPEG→MP4 编码与 QC 全部与 v3.0 共用。
- v2.1 布局：每 episode 一个 parquet/mp4；`meta/tasks.jsonl`、`episodes.jsonl`、
  `episodes_stats.jsonl`、`stats.json`；`info.json` 含 `total_chunks`、`total_videos`、每特征 `fps`。
- 校验：`--validate-only` 依据 `info.json` 的 `codebase_version` 自动选择 v2.1/v3.0 校验。
- 验证：
  - 自检 90 帧，结构校验 OK；HF `datasets` 反读成功。
  - 真实 2 episode × 2 s：每集 60 帧，全局 `index` 连续（0–59、60–119），QC 全部通过，
    `validate` OK，两段视频与两个数据文件均存在。
- 与 lerobot 的 `convert_dataset_v21_to_v30.py` 读取约定一致，可在有 lerobot 环境时升级为 v3.0。

```bash
sg i2c -c "python3 umi_collect.py --out ~/datasets/umi_v21 --format v2.1 \
    --episodes 5 --duration 10 --task pick"
python3 umi_collect.py --validate-only ~/datasets/umi_v21
```

## 12. 追加：`start_web.sh` 相机不可用 / SteamVR 未启动（2026-11-04）

### 现象

启动 `umi_devices/start_web.sh` 后控制台报“相机不可用”，且 SteamVR（`vrserver`）没有预先运行，
Tracker 状态为空。

### 排查

- 机器重启后 V4L2 编号变化：`/dev/video0` 消失，采集口变为 **`/dev/video1`**，`/dev/video2`
  是元数据节点。`camera_v4l2.py` 能正确识别 `/dev/video1`，权限（ACL `user:zy:rw`）正常。
- 问题在于 `umi_web.py` 的默认/持久化配置把相机写死为 `/dev/video0`
  （`~/.config/umi-web/config.json`），`CameraMonitor` 打开失败即报“相机不可用”。
- `umi_web.py` 只调用 `tracker_openvr.init()`，从不启动 SteamVR；重启后 `vrserver` 未运行，
  因此 Tracker 初始化失败。

### 修复（`umi_web.py`）

1. **相机自动发现**：新增 `resolve_camera()`。配置的节点仍是有效采集设备则沿用，否则用
   `camera_v4l2.cameras()` 的第一个采集口，并把新节点写回配置。首次打开与自动重连路径都使用。
2. **SteamVR 自动启动**：新增 `steamvr_running()`、`launch_steamvr()`、`ensure_steamvr()`。
   `vrserver` 未运行时，以 `DISPLAY=:0`、`XAUTHORITY=~/.Xauthority`、
   `XDG_RUNTIME_DIR=/run/user/<uid>`、DBus 环境后台执行 `vrmonitor.sh`，最多等待 45 s
   （`UMI_STEAMVR_TIMEOUT` 可调）；失败时错误信息给出日志路径
   `~/.cache/umi-web/steamvr.log`。自动重连路径只异步尝试启动一次，避免阻塞。
3. 新增配置项 `auto_start_steamvr`（默认 `true`），纳入设置校验；持久化配置会自动补默认值。

### 验证

- `./start_web.sh` 启动日志：
  `相机节点 /dev/video0 不可用，改用 /dev/video1` → `相机 /dev/video1 已连接 (640x480)`；
  配置写回 `"/camera": "/dev/video1"`。
- SteamVR 被自动拉起，`vrserver` 运行；`/api/health` 返回 `{"ok": true}`，
  `/api/state` 中 `device_errors` 为空，相机约 31–33 fps、夹爪约 336 Hz 正常。
- 已验证后停止测试用 Web 服务，SteamVR 保持运行。

### 遗留（硬件侧，非软件问题）

OpenVR 当前只枚举到虚拟 HMD，没有 `GenericTracker` 或 `TrackingReference`。Watchman Dongle
`28de:2101` 存在且 `vrserver` 已加载，说明软件链路正常；Tracker 未连上属于设备状态
（Tracker 未开机、未与接收器连接或超出基站范围）。Tracker 上电并进入基站视野后应自动出现，
无需重启服务。原 `umi_web.py` 已备份为 `umi_web.py.bak.<时间戳>`。

## 13. 追加：Tracker 不被枚举的根因与修复（2026-11-04）

### 现象

相机与 SteamVR 自动启动修复后，Web 控制台的 Tracker 仍为空；OpenVR 只枚举到虚拟 HMD。

### 排查

- `vrserver` 在运行，但 `/api/state` 的 tracker `connected=false`、`serial=null`。
- Watchman Dongle `28de:2101` 存在（USB 序列号 **487B2520A5**，旧文档记录为 EB7A99AAFC）。
- Tracker 当前是 **USB 直连**：`28de:2300 Valve LHR`、序列号 **LHR-69F5265A**，
  对应 `/dev/hidraw3..5`（`HID_NAME=Valve LHR`，`HID_UNIQ=LHR-69F5265A`）。
- SteamVR pairing 页面日志显示：

  ```text
  bHasHMD 0 bHasVRMonitor 1
  Creating limited VRHTML object (No HMD present)
  local steamvr <invalid> / remote steam <invalid>
  Global ID Offset: 658400000 (vrlink server: false)
  ```

- `vrserver.txt` 被 `vrlink: ERROR: Null VTE_CLIENT_SESSION_ID!` 持续刷屏（约 5 万条）。
- **Steam 客户端没有运行**（只有 `vrmonitor` / `vrserver` 进程）。

### 根因

SteamVR 的 Tracker 无线/USB 链路（`vrlink`）由 **Steam 客户端**服务。只运行
`vrmonitor.sh` 会拉起 `vrserver`，但 `vrlink server` 保持 false，Tracker 不会被枚举。
启动 Steam 客户端后，Tracker 立即出现在 OpenVR 设备列表中。

### 修复（`umi_web.py`）

- 新增 `steam_client_running()`、`launch_steam()`；自动启动顺序改为 **Steam → SteamVR**。
- `ensure_steamvr()`（阻塞版）先确保 Steam 运行，再启动 `vrmonitor.sh`。
- 服务内新增非阻塞的 `_drive_steam_startup()`：Steam 未运行时只启动 Steam；Steam 就绪后
  再启动 SteamVR；Tracker 监视器等到 `vrserver` 就绪后由自动重连创建（30 s 节流）。

### 验证

- 停掉 `vrserver`、保留 Steam，再启动 `./start_web.sh`：
  日志出现 `SteamVR 未运行，正在启动`，约 15 s 后 `Tracker 已自动重连`。
- `/api/state`：`tracker.ok=true`、`connected=true`、`pose_valid=true`、
  `tracking_result=Running_OK`、`serial=LHR-69F5265A`。
- OpenVR 枚举：HMD + `GenericTracker LHR-69F5265A` + `TrackingReference LHB-D62AE5D6`，
  三者均 `valid=1`、`result=200`；位置约 `(0.135, -1.209, -3.676) m`，电量 100%。
- 验证后停止测试用 Web 服务，Steam/SteamVR 保持运行。

### 备注

- 当前 Tracker 通过 USB 直连，因此不依赖无线配对；若改为无线使用，需要确认 Tracker 与
  当前接收器（序列号 487B2520A5）已配对，或换回旧接收器（EB7A99AAFC）。
- 启动顺序很重要：**先 Steam，后 SteamVR**。只启动 SteamVR 时 Tracker 不会出现。

## 14. 追加：网页夹爪标定向导（2026-11-04）

### 入口

控制台“夹爪宽度”卡片右上角新增 **标定** 按钮，打开标定向导。标定期间会显示实时宽度和原始角。

### 两种模式

1. **快速两点（推荐先做）**
   - 输入全闭宽度（默认 0 mm）和全开宽度（默认 80 mm，建议用卡尺实测）。
   - 先完全夹紧记录“全闭”，再完全张开记录“全开”。
   - 生成 `linear_two_point` 线性模型：以全闭计数为零点，按两点换算量程。
2. **多点分段（含迟滞）**
   - 输入宽度级别（如 `0,20,40,60,80`）、重复趟数、每点采样数。
   - 每趟先“张开”由闭到开扫一遍，再“夹紧”由开到闭扫一遍。
   - 复用 `gripper_calib.fit`：分段二次 + 方向相关偏置，输出训练/留出 RMSE 和线性基线对比。

### 后端 API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/calibration` | 当前标定会话状态 |
| POST | `/api/calibration/start` | `{mode, levels, trips, samples, closed_mm, open_mm}` |
| POST | `/api/calibration/record` | 采样约 0.6 s 并记录当前点 |
| POST | `/api/calibration/undo` | 撤销上一点 |
| POST | `/api/calibration/fit` | `{name}` 拟合并保存 JSON |
| POST | `/api/calibration/apply` | `{path}` 应用标定并重连夹爪 |
| POST | `/api/calibration/cancel` | 取消标定 |

- 记录值取 `GripperMonitor` 最近约 0.6 s 的原始角做环形均值，抑制 50 Hz 抖动。
- 标定文件保存在 `umi_devices/<name>.json`，`/api/calibrations` 会列出，设置页可直接选择。
- 应用时走 `update_config({"calibration_file": ...})`：校验模型、持久化配置、重连夹爪。
- 录制中、数据集打开期间禁止标定。

### 验证

- `start(quick)` → `record` ×2（原始角 2370 / 2387）→ `fit` 生成 `test_quick.json` →
  `apply` 后 `/api/state` 的 `calibration` 变为 `test_quick.json`、宽度按新模型显示 →
  `/api/settings {calibration_file:""}` 恢复“理论值”。
- `start(full, 0,20,40,60,80, trips=2)` → 总点数 20 → `record`/`undo`/`cancel` 均正常。
- 页面包含 `calibrationModal`，`app.js` 含 `openCalibration` 且括号结构校验通过。
- 测试文件已删除；实际标定需用户在界面上按提示移动夹爪完成。

## 15. 追加：libsurvive 静止漂移测试（2026-11-04）

### 条件

- 夹爪静止；Tracker `LHR-69F5265A` 通过 USB 连接（名称 `T20`）。
- 停止 SteamVR/控制台，运行 `~/tools/streamvr/libsurvive_tracker/read_tracker` 采集 **63.46 s**，
  共 15,854 帧，约 **249.8 Hz**。
- libsurvive 启动时反复提示：`Two or more lighthouses are on channel 0; tracking is most likely
  going to fail.`

### 结果

| 指标 | libsurvive（63.5 s） | SteamVR/OpenVR（10 s，对照） |
| --- | --- | --- |
| 位置标准差 (mm) | 0.92 / 2.69 / 0.41 | 0.07 / 0.45 / 1.33 |
| 位置峰峰值 (mm) | **23.2 / 70.5 / 13.9** | 0.38 / 2.18 / 6.27 |
| 线性漂移 (mm/min) | -1.24 / +3.29 / +0.09 | — |
| 首尾 10 s 漂移 (mm) | -1.46 / +3.92 / +0.11 | 0.02 / -0.20 / -0.64 |
| 1 s 窗口均值的范围 (mm) | 5.15 / **15.27** / 2.11 | — |
| 姿态相对首帧最大/标准差 | **17.42° / 5.87°** | 0.06° / 0.01° |
| 首尾 10 s 姿态漂移 | **15.59°** | 约 0.06° |
| 单步最大跳变 (mm) | 5.18（p99 0.35） | — |

### 结论

- libsurvive 短期抖动约 1–3 mm，但存在明显的**慢漂移和姿态漂移**：一分钟内 Y 方向峰峰值约
  70 mm、1 秒均值范围约 15 mm、姿态漂移约 15.6°，还伴随偶发 5 mm 跳变。
- 同期 SteamVR/OpenVR 的位置抖动在亚毫米级、姿态抖动 0.06°，稳定性高一个数量级以上。
- 漂移很可能与日志中的“两个基站在通道 0”有关：基站通道/OOTX 歧义会让解算出的基站位姿漂移，
  单基站时更明显。也可能是 poser/OOTX 配置问题，需要用正确通道和双基站几何再验证。
- 因此**暂不建议用 libsurvive 采集训练数据**；保持 SteamVR/OpenVR 作为默认后端。
  若确实需要无头方案，先解决基站通道冲突，并重新做静止漂移测试（目标：<1 mm、<0.5°/min）。

### 恢复

测试后重新绑定 `usbhid`（libsurvive 会解绑内核驱动），重启 SteamVR 与控制台；
Tracker 恢复 `Running_OK`，夹爪仍使用已应用的 `gripper_calib_20261104_111024.json`。

## 16. 追加：单基站修正后 OpenVR vs libsurvive 同时长对比（2026-11-04）

### 条件

- 夹爪静止，同一 Tracker `LHR-69F5265A`，无线接收器 `EB7A99AAFC`。
- 两台基站原先都在 `sobChannel=0`；调整后仅 `LHB-A03E404D` 参与跟踪（`LHB-D62AE5D6` 未连接），
  冲突消失，Tracker 为 `Running_OK`。
- 两个后端各采集约 60 s：OpenVR 6,864 帧（114.4 Hz），libsurvive 10,667 帧（172.1 Hz）。

### 结果

| 指标 | OpenVR | libsurvive |
| --- | --- | --- |
| 位置标准差 (mm) | **0.10 / 0.30 / 0.68** | 0.01 / 2.23 / 0.93 |
| 位置峰峰值 (mm) | **1.70 / 2.72 / 5.98** | 0.13 / **16.57** / 6.94 |
| 线性漂移 (mm/min) | 0.07 / -0.29 / -0.52 | 0.01 / **-6.66** / -2.79 |
| 首尾 10 s 漂移 (mm) | 0.07 / -0.24 / -0.42 | -0.00 / -4.94 / -2.11 |
| 姿态最大/标准差 (°) | **0.07 / 0.011** | 1.95 / 0.381 |
| 姿态漂移 (°) | **0.01** | 0.75 |
| 1 s 窗口标准差 (mm) | 0.05 / 0.18 / 0.40 | 0.01 / 2.21 / 0.92 |
| 1 s 窗口范围 (mm) | **0.33 / 0.81 / 1.85** | 0.05 / 7.36 / 3.23 |
| 单步最大/p99 (mm) | 3.27 / 0.73 | **1.11 / 0.043** |

### 与通道冲突时的 libsurvive 对比

| libsurvive | 双站同通道 0（第 15 节） | 单站 A03E404D（本节） |
| --- | --- | --- |
| 位置峰峰值 (mm) | 23.2 / 70.5 / 13.9 | 0.13 / 16.57 / 6.94 |
| 姿态漂移 (°) | 15.59 | 0.75 |

修正基站冲突后 libsurvive 改善显著（Y 峰峰值 70 mm → 16.6 mm，姿态漂移 15.6° → 0.75°），
但仍明显差于 OpenVR。

### 结论

- **采集默认继续使用 OpenVR/SteamVR**：位置抖动亚毫米、姿态抖动 0.01°，比 libsurvive
  稳定约 5–10 倍（位置）和 30 倍以上（姿态）。
- libsurvive 已可用于无头场景的粗略定位，但仍有约 2 mm 的 Y 轴抖动、约 7 mm/min 的慢漂移，
  对精细操作/训练数据还不够。
- 若必须无头，建议：两台基站分别设为不同通道并保证几何/OOTX 稳定，再尝试不同 poser，
  然后重复本节的静止漂移测试；目标 <1 mm、<0.5°/min。

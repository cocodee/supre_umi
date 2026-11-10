# 自制 UMI 夹爪数据采集器设计

> 讨论记录：2026-10-16。本文记录已确定的装配与采集方案，以及实施前需要验证的条件。设备实测细节见 [`../devices/`](../devices/) 下的各设备文档。

## 目标与物理布局

目标是用自制 UMI 夹爪采集人工示教数据：随夹爪运动的 RGB 图像、夹爪操作点的 6DoF 位姿，以及连续的夹爪开口宽度。采集设备连接到远程服务器，由服务器统一记录。处理后的观测结构面向 UMI 风格的策略训练；[原版 UMI](https://umi-gripper.github.io/umi.pdf)使用同步图像、末端位姿与夹爪宽度作为策略观测，并使用相对位姿轨迹表示动作。

本设计按以下装配布局执行：

| 位置 | 组件 | 连接与作用 |
| --- | --- | --- |
| 随夹爪运动 | USB 摄像头 | 刚性安装，朝向操作区域；通过 USB 向服务器传图像。 |
| 随夹爪运动 | VIVE Tracker 3.0 | 刚性安装，提供夹爪运动的跟踪位姿；通过无线链路连接服务器上的 Watchman Dongle。 |
| 夹爪机构 | AS5600 与磁铁 | 检测机构角度，经过机械标定后换算夹爪宽度；通过 I²C 接 CH341 USB 转接器，再连接服务器。 |
| 工作区域 | 一个 Lighthouse 基站 | 固定安装，为 Tracker 提供跟踪参考；基站不安装在夹爪上。 |
| 远程服务器 | Watchman Dongle、CH341、摄像头 USB 接口、SteamVR/OpenVR | 同机采集并保存三路原始数据。 |

服务器与操作者可以远程交互，但摄像头和 CH341 的 USB 线缆仍需从夹爪接到服务器或合适的 USB 延长设备。装配时要对线缆做应力释放，检查完整操作动作中的牵拉、缠绕，以及线缆或手部对 Tracker 的遮挡。

## 推荐方案与实施顺序

### 第一阶段：固定工位原始采集器

先在单基站覆盖的固定工作区完成可重复录制。服务器运行一个采集服务，分别从 V4L2 摄像头、SteamVR/OpenVR 和 CH341 I²C 适配器读取数据，记录原始样本、时间戳与有效状态。每次演示作为独立 episode 保存，提供开始、停止和预览入口。原始数据保持可重处理，避免将一次性的同步或标定计算写死在采集时。

建议的初始目标是相机约 30 fps、Tracker 位姿约 30 Hz、AS5600 约 50 Hz；这些是采集程序的目标采样率，必须通过长时间试录确认实际稳定性。当前只实际验证了摄像头 640 × 480 取帧；其他相机分辨率虽由驱动声明支持，尚需实测。

### 第二阶段：同步和 UMI 风格数据导出

以每帧图像的采集时间为对齐基准，从原始位姿和宽度流生成同步观测，计算夹爪操作点位姿及相对轨迹，再导出训练所需字段。推荐至少包含 `camera0_rgb`、末端位姿、夹爪宽度和每个样本的有效状态。具体数组形状、坐标约定和 episode 边界应在选择训练代码后与其数据加载器核对。

这套硬件用 OpenVR 取位姿、AS5600 取宽度。原版 UMI 的公开流水线从 GoPro 视频及 SLAM 结果生成数据，因输入来源不同，需要单独编写导出适配层，不能把原始记录直接当作原版 GoPro/SLAM 输入。[UMI 官方仓库](https://github.com/real-stanford/universal_manipulation_interface)。

### 第三阶段：跨场景扩展（按需要）

当前方案优先覆盖固定工位。若以后移动基站或跨房间采集，需要为每次布设记录新的坐标系、重新核对跟踪区域，并处理不同 session 的坐标关系。跨场景定位方案应在固定工位的时间同步、标定和数据质量得到验证后再确定。

## 采集数据与时间同步

```text
摄像头帧 ──┐
OpenVR 位姿 ├── 同一服务器的单调时钟时间戳 ── 原始 episode ── 离线同步 ── 训练数据
AS5600 读数 ┘
```

三路采集均记录服务器单调时钟时间，不以文件写入时间代替采集时间。摄像头优先保留 V4L2 帧时间戳；若采集库只能得到帧出队时间，应明确标记该时间戳语义并实测相机延迟。OpenVR 调用 `getDeviceToAbsoluteTrackingPose(..., 0, ...)` 获取调用时刻的位姿，记录调用时间及 `bDeviceIsConnected`、`bPoseIsValid`；`0` 表示不向未来预测。[OpenVR 接口说明](https://github.com/ValveSoftware/openvr/wiki/IVRSystem::GetDeviceToAbsoluteTrackingPose)。AS5600 每次 I²C 读取同时保存原始 12 位计数、磁铁状态和读取时间。

处理时将位姿与宽度对齐到图像时间。仅对相邻且有效的位姿做位置插值和旋转插值；对宽度做受有效区间约束的插值。位姿失效、时间倒退、明显丢帧和超出允许时间间隔的样本应标记或剔除，不能跨较长失踪区间补造轨迹。相机、OpenVR 和 USB 转 I²C 的端到端延迟需分别测量；原版 UMI 也强调基于实测延迟同步不同观测流。[UMI 论文](https://umi-gripper.github.io/umi.pdf)。

建议原始 episode 至少保存：

```text
session_metadata.json       # 设备序列号、软件版本、坐标约定、标定版本
episodes/000001/
  camera/                  # 视频或逐帧图像，另存逐帧时间戳
  tracker.jsonl            # 原始 OpenVR 位姿、序列号、有效状态、时间戳
  gripper.jsonl            # AS5600 原始计数、磁铁状态、时间戳
  events.jsonl             # 录制开始/停止、操作者事件、错误
```

具体视频容器可以在实现时选择，但必须能追溯到每一帧的采集时间。处理后的数据应保留原始 episode 的引用，以便重新标定和重新同步。

## 几何与宽度标定

1. **相机内参**：标定镜头内参和畸变，并保持采集与训练/部署时的图像裁剪、分辨率约定一致。
2. **Tracker 到夹爪操作点**：标定刚体变换 `T_tracker_tcp`。OpenVR 给出 `T_world_tracker(t)` 后，按统一坐标约定计算 `T_world_tcp(t) = T_world_tracker(t) · T_tracker_tcp`。Tracker 索引用于运行时枚举；持久识别使用序列号。
3. **相机到 Tracker**：相机和 Tracker 均刚性安装时，标定固定外参 `T_tracker_camera`，记录重新拆装后需要复标的条件。
4. **AS5600 到夹爪宽度**：记录闭合与已知开口的端点计数，确定开合方向和跨越 `4095→0` 的展开规则；检查中间位置的线性误差，必要时使用多点标定。`80 mm / 4096 ≈ 0.02 mm/计数` 只是在全行程对应完整一圈时的理论量化步距，不是已验证的宽度精度。
5. **坐标系**：每个 session 记录 SteamVR 的 tracking universe 和任何重置/重新布置事件。优先在数据处理中形成相对末端轨迹，减少对全局原点的依赖；绝对坐标仍应保存在原始记录中。

## 录制前与录制中质量检查

当前设备文档的检查结果表明：摄像头可输出 640 × 480 图像；CH341 总线上的 AS5600 地址 `0x36` 可读，磁铁检测位有效；Tracker 在历史测试中曾输出有效位姿，但最近一次实时检查时显示未连接、位姿无效。SteamVR 缓存中的两个基站序列号属于历史记录，不能据此推断现场有两个基站。

因此，正式录制的启动条件是：

- 现场确认唯一使用的基站已固定、通电且覆盖整个操作动作，Tracker 开机、配对，并持续产生有效位姿。单基站配置需要重点测试手部、夹爪和线缆遮挡；HTC 要求 Tracker 位于至少一个基站的视野内。[VIVE 排障说明](https://www.vive.com/us/support/camtrack/category_howto/troubleshooting-vive-tracker.html)。
- 相机刚性安装在夹爪上，画面能覆盖示教任务所需的接触与操作区域；记录曝光、清晰度、掉帧和实际帧时间。
- AS5600 完成全行程宽度标定，检查磁铁状态、回程重复性及角度零点环绕。
- 完成一次覆盖真实操作动作的连续试录，检查三路时间戳、延迟、位姿失效区间和线缆牵拉，再确定可接受的数据质量阈值。

录制界面应持续显示相机预览、Tracker 连接及位姿有效状态、当前夹爪宽度和各路采样/丢失统计。位姿无效时应显式提示，并在数据中保留失效标记。

## 相关设备文档

- [USB 摄像头](../devices/hd-usb-camera-32e4-9530.md)
- [Vive Tracker、基站与 SteamVR/OpenVR](../devices/vive-tracker-steamvr-openvr.md)
- [AS5600 夹爪宽度传感器](../devices/umi-gripper-as5600.md)

## 2026-10-27 复验更新

设备状态与工具在 [`../umi_devices/`](../../umi_devices/)（`umi_status.py`）。相对本文早先的检查：

- **Tracker**：`LHR-69F5265A` 本次已 `connected`、电量 100%，但 `eTrackingResult=Calibrating_OutOfRange`
  且未枚举到基站，仍无有效位姿。阻碍已从“Tracker 未连接”变为“基站不可见/超范围”。基站与
  Tracker 视野就绪前，暂不满足录制启动条件。
- **AS5600**：CH341 现为 `i2c-3`（旧记录 `i2c-13`）。静止时原始角与滤波角同步以约 50 Hz 摆动、
  跨度约 96°，而磁场幅值与 AGC 稳定，配置寄存器为默认值；来源未确认。宽度标定与同步方案需先
  解决该抖动，采集端至少要时间平均并记录抖动幅度。
- **摄像头**：`/dev/video0` 仍在，640×480 可出帧，实测约 20–25 fps（读循环含处理开销）。

详细测量见两份设备文档的“2026-10-27 复验”小节。

## 采集器实现（2026-10-27）

第一阶段采集器已实现并验证，代码在 [`../../umi_devices/`](../../umi_devices/)：

| 文件 | 作用 |
| --- | --- |
| `umi_collect.py` | 三路同步采集主程序，输出 LeRobot v3.0 数据集 |
| `lerobot_v30.py` | 自包含 v3.0 writer（pandas/pyarrow/ffmpeg，无 torch 依赖） |
| `gripper_as5600.py` / `tracker_openvr.py` / `camera_v4l2.py` | 单设备读取 |

### 架构

```text
CameraStream  (连续取帧, 线程)  ─┐
TrackerStream (OpenVR ~30Hz)    ─┼─> 固定 fps 主循环 ──> EpisodeRecorder ──> LeRobot v3.0
GripperStream (AS5600 ~50Hz,    ─┘    每个 tick 取        JPEG 暂存 + 数值行     data/meta/videos
              环形均值)              各路最新值
```

- 同一帧的三路值共享一个 `timestamp = frame_index / fps`，实现按构造的时间对齐，符合本文
  “统一单调时钟时间戳”的要求。原始流各自的采样时刻不另存。
- 宽度先做环形均值（默认 16 个样本），以抑制静止时约 50 Hz、约 1088 计数的角度抖动。
- 退出时不再显式 `openvr.shutdown()`，并以 `os._exit()` 结束，避免 OpenCV/pyarrow 原生线程
  残留导致进程不退出，也避免 null 头显配置下连带关闭 vrserver。

### 数据字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `observation.images.camera0` | video H×W×3 | 相机，H.264/yuv420p，与数据同帧率 |
| `observation.state` | float32[1] | 夹爪宽度 mm（标定前为理论值） |
| `observation.pose` | float32[7] | Tracker `[x,y,z,qx,qy,qz,qw]`，米 |
| `observation.valid` | float32[1] | 位姿有效性 1/0 |
| `action` | float32[8] | 当前 `pose(7)+宽度` |

外加 LeRobot 标准列 `timestamp/frame_index/episode_index/index/task_index`。`meta/episodes`
按 v3.0 写入 `data/*`、`videos/*` 的 chunk/file 索引、`dataset_from/to_index`、视频
`from/to_timestamp`，以及每个特征的 `stats/*`（min/max/mean/std/±count/q01..q99），
`meta/stats.json` 为跨 episode 的加权聚合。

### 验证结果（2026-10-27）

- 无硬件自检：90 帧合成 episode，结构校验通过。
- 真实采集（相机+夹爪，3 s，30 fps）：90 帧，`timestamp` 0→2.967 s，视频 90 帧/3.000 s；
  宽度经环形均值后跨度约 4.6 mm（未均值时原始角跨度约 1088 计数）。
- 真实采集（含 Tracker，3 s）：90 帧 `observation.valid` 全为 1；记录后 vrserver 仍存活。
- 兼容性：用 HuggingFace `datasets` 按 `info.json` 的 features 反读 parquet 成功，
  `list<float32>`→`Sequence(length=7)` 转换正常，四元数范数约 1。

### 仍需完成

- 夹爪全行程标定与 50 Hz 抖动根因排查，之后才能把 `observation.state` 当作可信宽度。
- 多 episode、长时间运行的磁盘/延迟统计；相机曝光与帧时间记录。
- 若训练管线要求特定 action 语义（如相邻帧相对位姿），需在导出阶段另行计算。
## 借鉴 FOMI 的软件升级（2026-10-27）

参考 [FOMI (arXiv 2610.19839)](https://arxiv.org/abs/2610.19839) 的采集系统，落地了三项软件改进
（其力反馈剪刀机构属硬件改造，本项目未采用）。

### 1. 夹爪分段标定（`gripper_calib.py` / `gripper_calibrate.py`）

- 模型：`f(x,σ)=p_k(x)+c_σ`，分段二次 + 方向相关偏置（迟滞），段间值连续。
- 拟合：连续性约束下的最小二乘，用约束矩阵零空间 + numpy `lstsq` 求解，不依赖 scipy。
- 数据：开/合两个方向、多个已知宽度、重复多趟，每点取环形均值以抑制抖动。
- 评估：held-out RMSE，并与线性基线对比（输出在 `metrics`）。
- 运行时：`GripperCalibration` 按 5 样本滑窗推断开/合方向，解绕到标定参考附近后映射。
- 采集器通过 `--gripper-calib-file` 使用。

### 2. 同步与 QC（`umi_collect.py`）

- 各流改为带时间戳的缓冲；主循环按 tick 取**最近样本**，记录偏差。
- 参考时间线仍是固定 `--fps`；偏差 > `--max-sample-age` 记为缺失。
- 每 episode 输出 `<out>/qc/episodes.jsonl`（缺失率、位姿失效率、最大相机时差、是否通过），
  `--qc-per-frame` 输出逐帧诊断；`--drop-bad-episodes` 可按阈值丢弃。
- 实测 3 s/30 fps：90 帧，相机缺失 0、位姿无效 0、最大相机时差 32 ms。

### 3. 双编码器差分（软件就绪，硬件受限）

- `gripper_as5600.DualAs5600` 读取第二片 AS5600（必须挂在另一块 CH341 适配器上），
  输出 `wrap_diff(enc1, enc2)` 作为宽度信号；`gripper_calib.wrap_diff` 提供最短弧解绕。
- 采集器 `--gripper-bus2` 启用；共模抑制可望缓解 50 Hz 抖动与背隙，但需在差分信号上重新标定。
- 当前硬件只有一片 AS5600，自动降级为单编码器。

### 抖动分析结论

`gripper_calibrate.py analyze` 以实际采样率（约 363 Hz）做 FFT，静止时主频 **50.00 Hz**、
跨度约 1161 计数。这支持“工频干扰/共模扰动”的假设，也说明双编码器差分是值得优先验证的
硬件方向；在只有单编码器时，环形均值 + 分段标定是目前可行的软件缓解手段。

### 尚未采用

- FOMI 的力反馈剪刀机构（需重新设计夹爪）。
- tracker→末端手眼标定与遥操作参考系锚定（等确定要部署到真机时再做）。
- 相对位姿 action 导出（当前 `action` 仍是绝对 pose+宽度，已在设计文档注明）。
### v2.1 输出（2026-10-27 追加）

新增 `lerobot_v21.py`（`LeRobotV21Writer` / `EpisodeRecorderV21`），复用 v3.0 的统计计算、
parquet 列 schema、JPEG→MP4 编码，只改变路径与元数据：

- 每 episode 一个 `data/chunk-000/episode_XXXXXX.parquet` 与
  `videos/chunk-000/<key>/episode_XXXXXX.mp4`。
- `meta/tasks.jsonl`、`meta/episodes.jsonl`、`meta/episodes_stats.jsonl`、`meta/stats.json`。
- `meta/info.json`：`codebase_version="v2.1"`，含 `total_chunks`、`total_videos`，
  非视频特征带 `fps`，视频特征带 `info`。
- `umi_collect.py --format {v3.0,v2.1}`；`--validate-only` 依据 `codebase_version` 自动分派校验。

实测：2 个 episode × 2 s，每集 60 帧，全局 `index` 连续（0–59、60–119），
HF `datasets` 反读 parquet 成功，`validate` OK，QC 通过。v2.1 目录结构符合 lerobot
`convert_dataset_v21_to_v30.py` 读取的字段与路径约定。

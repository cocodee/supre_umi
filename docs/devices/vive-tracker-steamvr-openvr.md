# Vive Tracker 定位设备与 SteamVR/OpenVR 接入

> 检查时间：2026-10-16（目标机器本地时间，UTC+08:00）。本文区分当前设备状态、历史验证结果和待确认信息。目标机器为 `xwjg-M9-PRO`，系统内核为 `6.1.59-rt16`。

## 定位链路

系统计划使用一个 Lighthouse 基站与一个 Vive Tracker 获取位姿。机器通过 Valve Watchman Dongle 接收 Tracker 数据，由 SteamVR 的 Lighthouse 驱动完成设备识别和跟踪，再由 OpenVR Python 接口读取位姿：

```text
Lighthouse 基站 ──光学扫掠──> Vive Tracker 3.0
                                     │
                                无线连接
                                     ↓
主机 USB ──> Watchman Dongle ──> SteamVR Lighthouse 驱动 ──> OpenVR ──> 应用
```

当前主机的 USB 设备清单中没有单独列出基站，因此 USB 清单不能用于判断基站是否通电或正在参与定位。用户计划的物理配置为**一个基站**；SteamVR 本机缓存中保留了**两个历史基站条目**，不能据此断言当前连接了两个实体基站。

## 设备清单

| 组件 | 从机器获取的标识 | 当前状态与含义 |
| --- | --- | --- |
| Watchman Dongle | USB VID:PID `28de:2101`；USB 序列号 `EB7A99AAFC`；产品名 `Valve Software Watchman Dongle` | 当前出现在 `lsusb`；USB 接口 `1-7:1.0` 已绑定 `usbhid`，对应 `/dev/hidraw0`；用户 `zy` 对节点有读写权限。仅此项不能证明 Tracker 已开机、配对或正在被追踪。 |
| Vive Tracker | OpenVR 序列号 `LHR-69F5265A`；型号属性 `VIVE Tracker 3.0 MV`；制造商属性 `HTC`；注册类型 `htc/vive_trackerLHR-69F5265A` | OpenVR 当前仍能枚举到设备记录，类别为 `GenericTracker`，但 `bDeviceIsConnected=false`、`bPoseIsValid=false`，当前没有可用位姿。 |
| 基站记录 A | OpenVR 序列号 `LHB-A03E404D`；型号属性 `Valve SR Imp`；制造商属性 `Valve Corporation` | SteamVR 历史缓存最近记录于 2026-10-13 14:03:50；当前 `TrackingReference` 记录未连接、位姿无效。 |
| 基站记录 B | OpenVR 序列号 `LHB-D62AE5D6`；型号属性 `Valve SR Imp`；制造商属性 `Valve Corporation` | SteamVR 历史缓存最近记录于 2026-10-13 15:41:05；当前 `TrackingReference` 记录未连接、位姿无效。 |
| 虚拟头显 | OpenVR 序列号 `Null Serial Number`；跟踪系统 `null` | 当前作为 HMD 被枚举且位姿有效，坐标为原点；它是软件虚拟设备，不能作为 Tracker 定位成功的证据。 |

基站两个缓存序列号分别对应 Lighthouse 数据库中的十六进制序列号 `0xA03E404D` 和 `0xD62AE5D6`。这两条记录可能来自不同时间接入的基站；**当前实际安装的是哪一台、是否通电及是否可见，尚未由本次远程检查确认**。OpenVR 给出的 `Valve SR Imp` 是设备属性文本；本文不据此推断基站的商业型号或代际。

### Watchman Dongle 的主机连接

- USB 总线位置：Bus 001 Device 026，主机端口路径 `1-7:1.0`。
- USB 版本描述符：2.00；当前连接在 12 Mbit/s USB 链路上。
- HID 原始设备：`/dev/hidraw0`；驱动链为 `usbhid` → `hid-generic`。
- `/dev/hidraw0` 的 ACL 明确授予 `zy` 读写权限。`hidraw0` 和 `1-7:1.0` 都可能在重新插拔或换端口后改变，运行时应按 VID:PID 与实际设备路径重新核对。

## SteamVR 与 OpenVR 软件环境

| 项目 | 当前机器上的状态 |
| --- | --- |
| SteamVR | Steam 应用 ID `250820`，安装清单 `StateFlags=4`，build ID `25330290`；用户配置记录的版本提示为 `2.17.10`。`vrserver` 和 `vrmonitor` 当前正在运行。 |
| SteamVR 安装目录 | `/home/zy/.local/share/Steam/steamapps/common/SteamVR` |
| OpenVR Python 包 | `/home/zy/tools/streamvr/.venv` 中的 `openvr 2.12.1401` |
| 位姿读取脚本 | `/home/zy/tools/streamvr/read_tracker_pose.py` |
| 用户级 SteamVR 配置 | `/home/zy/.local/share/Steam/config/steamvr.vrsettings` |

机器没有物理头显。用户级配置设置 `activateMultipleDrivers=true`、`forcedDriver="null"`、`requireHmd=false` 和 `driver_null.enable=true`，以便通过虚拟 HMD 初始化 OpenVR，同时允许 Lighthouse 驱动工作。虚拟 HMD 的有效位姿与 Tracker 是否被追踪是两个独立状态。

## 位姿接口与历史验证

现有脚本调用 `getDeviceToAbsoluteTrackingPose`，只输出类别为 `GenericTracker` 且 `bDeviceIsConnected`、`bPoseIsValid` 同时为真的设备。默认使用 `TrackingUniverseStanding` 坐标空间，以 JSON Lines 输出：

| 字段 | 含义 |
| --- | --- |
| `time_unix_s` | Python 读取时的 Unix 时间戳；并非硬件测量时间戳。 |
| `serial` | Tracker 序列号；适合用作设备身份标识。 |
| `index` | 当前 OpenVR 设备索引；可能随 SteamVR 会话变化，不宜作为持久身份。 |
| `universe` | 坐标空间，默认 `standing`，也可指定 `raw`。 |
| `position_m` | 位姿矩阵平移列的三个值，单位米。 |
| `rotation_matrix` | 位姿矩阵左上角的 3 × 3 旋转部分。 |

2026-10-13 的历史测试中，`LHR-69F5265A` 曾成功输出有效位姿。留存的 `openvr-tracker-stream.jsonl` 包含 **146 条**记录，时间跨度约 **4.91 秒**，测试脚本当时以 `--hz 30` 轮询。约 30 条/秒是该次脚本的输出速率，**不能等同于传感器或 SteamVR 跟踪系统的硬件更新率**。历史坐标受当时的房间校准和设备摆放影响，不应写作当前固定坐标。

本次 2026-10-16 的 OpenVR 实时查询结果如下：

| OpenVR 索引 | 类别 | 序列号 | 已连接 | 位姿有效 |
| ---: | --- | --- | --- | --- |
| 0 | HMD（虚拟） | `Null Serial Number` | 是 | 是 |
| 1 | GenericTracker | `LHR-69F5265A` | 否 | 否 |
| 2 | TrackingReference | `LHB-A03E404D` | 否 | 否 |
| 3 | TrackingReference | `LHB-D62AE5D6` | 否 | 否 |

因此，**本次检查不能确认当前可连续获取 Tracker 位姿**。需要 Tracker 开机且完成与接收器的连接，并让实际使用的基站正常工作、Tracker 位于可跟踪区域，再重新验证。两条基站记录是历史缓存，不应按索引数量计算现场基站数。

## 验证与排查命令

```bash
# 核对接收器、HID 驱动和权限
lsusb -d 28de:2101
readlink -f /sys/class/hidraw/hidraw0/device/driver
getfacl /dev/hidraw0

# 有效位姿出现后输出一条；30 秒内没有位姿则超时退出
cd /home/zy/tools/streamvr
timeout 30s .venv/bin/python read_tracker_pose.py --once

# 查看 SteamVR 服务日志
tail -n 100 /home/zy/.local/share/Steam/logs/vrserver.txt
```

若接收器在 `lsusb` 中存在，但位姿仍无效，应分别检查 Tracker 电源与配对状态、基站供电及可见范围、`usbhid` 绑定情况，并核对 SteamVR 是否加载 Lighthouse 驱动。`read_tracker_pose.py --once` 在没有有效位姿时会一直等待，所以上例使用 `timeout` 限定检查时间。

## 信息来源与边界

- 当前硬件与权限：`lsusb`、`lsusb -t`、`lsusb -d 28de:2101 -v`、`udevadm`、`readlink`、`getfacl`。
- 当前 OpenVR 设备状态：在运行中的 SteamVR 上调用 OpenVR 设备枚举和 `getDeviceToAbsoluteTrackingPose`。
- 历史基站记录：`/home/zy/.local/share/Steam/config/lighthouse/lighthousedb.json`。
- 历史有效位姿：`/home/zy/tools/streamvr/openvr-tracker-stream.jsonl`；安装与排障记录见同目录 `STEAMVR_INSTALL_NOTES.md`。
- 未从本次检查确认：现场基站的物理数量和对应序列号、基站实时工作状态、Tracker 当前电量、当前可用位姿、绝对精度、端到端时延、遮挡时表现。

## 2026-10-27 复验

复核时机器本地时间 UTC+08:00。本次用
`/home/zy/tools/streamvr/.venv/bin/python` 启动 SteamVR 后实时查询 OpenVR。

### 当前状态较旧记录有进展

- SteamVR `vrmonitor`/`vrserver` 已运行（版本 `2.17.10`）。
- Watchman Dongle `28de:2101` 仍在 `lsusb` 中。
- **Vive Tracker `LHR-69F5265A` 本次 `bDeviceIsConnected=true`**（旧记录为 `false`），
  即已开机并与接收器连接；电量 `100%`、正在充电、固件读数 `1592875850`。
- 但 `bPoseIsValid=false`，`eTrackingResult = Calibrating_OutOfRange`（枚举值 101）。
- 本次实时枚举只有虚拟 HMD（`Null Serial Number`，位姿有效，位于原点）与上述
  `GenericTracker`，**没有枚举到 `TrackingReference`（基站）**。

`Calibrating_OutOfRange` 与未枚举到基站一致：Tracker 已连上接收器，但当前没有可用的
基站观测，因此仍拿不到有效位姿。这与旧文“Tracker 未连接”不同，当前阻碍已经从
“Tracker 未开机/未配对”变为“**基站不可见/超出跟踪范围**”。

### 复验结论

- Tracker 本身在线（连接、电量、固件可读），无需再排查配对与供电。
- 需要确认实际使用的基站已通电、朝向覆盖工作区，且 Tracker 位于其视野内；然后重新查询，
  直到 `bPoseIsValid=true`。
- 两个历史基站序列号 `LHB-A03E404D`、`LHB-D62AE5D6` 仍属缓存记录；本次没有证据表明
  现场有哪个基站在工作。

### 工具

新增 `umi_devices/tracker_openvr.py`，在原 `read_tracker_pose.py` 只输出有效位姿的基础上，
额外报告每个设备的连接状态、`eTrackingResult`、电量、充电状态和型号，便于判断“连上但
无法定位”这类中间状态。原 `read_tracker_pose.py` 不修改。

```bash
cd /home/zy/workspace/supre_umi/umi_devices
/home/zy/tools/streamvr/.venv/bin/python tracker_openvr.py
/home/zy/tools/streamvr/.venv/bin/python tracker_openvr.py --json --count 1
```

## 2026-10-27 后续复验：位姿已有效

同日稍晚重启 SteamVR 后重新查询，之前“已连接但无位姿”的状态已消失：

| 索引 | 类别 | 序列号 | 已连接 | 位姿有效 | 跟踪结果 | 位置 (m) |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | GenericTracker | `LHR-69F5265A` | 是 | 是 | `Running_OK` | `(+0.184, -1.206, -3.616)` |
| 2 | TrackingReference | `LHB-D62AE5D6` | 是 | 是 | `Running_OK` | 原点 |

即现场有一个基站 `LHB-D62AE5D6` 已参与跟踪，Tracker 能输出有效位姿。之前缓存中的另一台
`LHB-A03E404D` 本次未出现。位置值对应本次基站/Tracker 摆放，换布设后应重测。

采集器据此验证了带 Tracker 的 3 秒录制：90 帧 `observation.valid` 全为 1。注意：短命
OpenVR 客户端退出时可能连带使 vrserver 退出（null 头显配置下），采集器已改为不显式调用
`openvr.shutdown()`。

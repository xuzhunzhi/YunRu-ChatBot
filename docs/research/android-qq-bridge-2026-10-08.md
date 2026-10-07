# 安卓免 root 跑 QQ 接入 · 调研

只读调研。凡是标「实测」的，都有可点开的一手来源（官方脚本 / release / issue / PR / 官方 wiki）；标「推断」的是我的推理，没有来源支撑。

## 一句话结论（能 / 不能 / 有条件能 + 条件）

**路 1·安卓不刷机（Termux + proot-distro，免 root）：能，而且是 NapCat 官方写明的部署方式。**

- 条件：arm64 安卓机 + Termux。跑官方 `install.termux.sh`，它先 `pkg install proot-distro screen`，
  再 `proot-distro install debian`，然后在容器里执行官方 `install.sh --docker n --cli n` —— 全程不需要 root。
- 装的是什么：`install.sh` 下载 `NapCat.Shell.zip`（NapCat 无头版），并把**腾讯官方 Linux QQ 的 arm64 包**
  解压到 `~/Napcat/opt/QQ`（固定 3.2.32-52194，按 `arch` 自动选 `arm64.deb` / `aarch64.rpm`）。
- 启动必须带虚拟显示：`xvfb-run -a ~/Napcat/opt/QQ/qq --no-sandbox`。
  也就是说，它**不需要桌面**，但**需要 xvfb**（官方脚本把 xvfb 列为依赖并这样拉起）。
- 已知坑：arm64 + proot 下 Worker 曾以退出码 11 崩（issue 1635），较新版本已由 PR 1984 修，可设 `NAPCAT_FORCE_NODE_PROCESS=1` 兜底；
  安卓后台会杀进程，必须按 MIUI 清单做保活。

**路 2·刷成真 Linux（postmarketOS）：三个机型里只有 K20 Pro 有移植，且已归档无维护 —— 不推荐。**

- Redmi K20 Pro / Mi 9T Pro（raphael）：pmaports 里在 `device/archived/`，内核 APKBUILD 首行写着 `# Archived: unmaintained`；
  v25.12 包索引里 `device-xiaomi-raphael` 的 maintainer 显示 None。
- 小米 10S（thyme）、MIX 4（odin）：**没查到** postmarketOS 设备包或移植页面（只查到第三方 ROM / 设备树）。
- 额外一层麻烦（**推断**）：postmarketOS 是 Alpine 系、musl libc，而 NapCat + Linux QQ 是 glibc 二进制，理论上还得再套 glibc 容器；没查到有人实测。

**哪条更稳**：路 1 明显更省事、可回退（退出 Termux 就是普通手机）；路 2 要刷机、要赌没人维护的下游内核。代价方面路 1 是保活 + 偶发崩溃，路 2 是变砖风险 + 生态缺失。

## 各方案对照（方案 · 需要什么 · 免 root 吗 · 实测/推断 · 依据链接）

| 方案 | 需要什么 | 免 root | 实测/推断 | 依据 |
| --- | --- | --- | --- | --- |
| Termux + proot-distro Debian（官方 `install.termux.sh`） | Termux、proot-distro、约 1.5 GB 空间、xvfb、screen/tmux | 免 root | 实测（有官方脚本 + 多份 issue） | [Shell 文档](https://napneko.github.io/guide/boot/Shell)、[install.termux.sh](https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.termux.sh) |
| 自建 proot/chroot（tmoe 等）后跑官方 Linux 安装命令 | 同上，容器自己建 | 免 root（chroot 需 root） | 实测 | [NapCat-Installer README](https://github.com/NapNeko/NapCat-Installer)、[issue 1715](https://github.com/NapNeko/NapCatQQ/issues/1715) |
| NapCat AppImage（自带 QQ） | xvfb + fuse；有 arm64 构建 | 是 | 实测（release 说明） | [NapCatAppImageBuild v4.18.33](https://github.com/NapNeko/NapCatAppImageBuild/releases/tag/v4.18.33) |
| NapCat Docker（Linux/Arm64） | 手机侧要有能跑 Docker 的内核环境 | 否（一般要 root/特权） | 实测（README 列 Arm64） | [NapCat-Docker](https://github.com/NapNeko/NapCat-Docker) |
| Termux 原生（bionic，不走 proot） | 无 | 是 | 实测失败 | [Termux wiki](https://wiki.termux.com/wiki/Differences_from_Linux)、[issue 1203](https://github.com/NapNeko/NapCatQQ/issues/1203)、[issue 1261](https://github.com/NapNeko/NapCatQQ/issues/1261) |
| 刷 postmarketOS 真 Linux | 解锁 + 刷机；raphael 才有设备包 | 与 root 无关 | raphael 归档：实测；musl 需 glibc 容器：推断 | [archived raphael 内核](https://gitlab.postmarketos.org/postmarketOS/pmaports/-/blob/main/device/archived/linux-xiaomi-raphael/APKBUILD)、[包索引](https://pkgs.nura.eco/package/v25.12/postmarketos/aarch64/device-xiaomi-raphael) |
| 退路：Lagrange.Core（自己实现协议，不挂 QQNT） | .NET 运行时；有 linux-arm64 自包含包 | 是 | 实测（有 arm64 资产） | [nightly release](https://github.com/LagrangeDev/Lagrange.Core/releases/tag/nightly) |

## 关键事实（NapCat 支持架构 · Linux QQ 架构 · Termux/proot 的限制）

**NapCat 的发布形态与架构**

- release 资产：`NapCat.Shell.zip`（无头版，多平台通用）、`NapCat.Shell.Windows.OneKey.zip`、`NapCat.Shell.Windows.Node.zip`、`NapCat.Framework.zip`。
  文档里 Windows 一键包注明「仅适用 Windows.AMD64，已内置 QQ」，Shell 版则要求「确保 QQ 版本安装且最新」。
  依据：[v4.18.33 release](https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.33)、[Shell 文档](https://napneko.github.io/guide/boot/Shell)。
- **arm64 / aarch64 有支持**：`install.sh` 里有 arm64 分支（`arm64.deb` / `aarch64.rpm`）；
  官方 AppImage 发布页同时给 amd64 和 arm64 两个资产；Docker 仓库 README 写 `Linux/Arm64`；
  NapCat 的原生模块也存在 `MoeHoo.linux.arm64.node`（由 [issue 2025](https://github.com/NapNeko/NapCatQQ/issues/2025) 的排查正文列出）。
- **它自带的/拉取的客户端不是第三方客户端**，是**腾讯官方 Linux QQ（QQNT）**：
  `install.sh` 从 `qqdl.gtimg.cn`（腾讯官方 CDN）下载，文件名形如 `QQ_3.2.32_260812_arm64_01.deb`；
  AppImage 资产名也是 `QQ-52194_NapCat-v4.18.33-arm64.AppImage`。
  依据：[install.sh](https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.sh)、[AppImage release](https://github.com/NapNeko/NapCatAppImageBuild/releases/tag/v4.18.33)。

**Linux QQ（腾讯官方）的架构**：官方同时提供 amd64 / arm64 / loongarch64 / mips64el 的 Linux 包，
arm64 有 deb、rpm、AppImage 三种；当前版本号 3.2.32（deb/rpm 构建日期 2026-08-12）。
依据：NapCat release 正文里给的腾讯官方 CDN 链接（含 `LinuxArm64 DEB / RPM`），以及 [Rodert/qq-versions 镜像 release](https://github.com/Rodert/qq-versions/releases)（自动化抓取官方下载配置，逐项列出 `linux-arm64-deb/rpm/appimage` 与 SHA256）。

**Termux 与 proot 的限制**

- Termux 用 Android 的 bionic libc、非 FHS 布局，**桌面 Linux 的原生二进制直接跑不了**（动态链接器路径与 libc ABI 都不匹配）；
  Termux wiki 明确给出绕过办法就是 PRoot。依据：[Termux wiki · Differences from Linux](https://wiki.termux.com/wiki/Differences_from_Linux)。
- 因此官方 Termux 脚本走的是 proot-distro 的 Debian（glibc），**proot 不需要 root**。依据：[install.termux.sh](https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.termux.sh)。
- 「无头仍需要什么」：需要 **xvfb**（虚拟 X 显示），官方安装脚本把它装成依赖，启动命令也是 `xvfb-run`；
  日志里出现的 dbus 连不上、`/proc/sys/fs/inotify/max_user_watches` 读不到、`NETLINK` 绑定失败都只是噪声，进程仍能起来（issue 1715、1777 的日志即此形态）。

## 已知的坑与失败报告

1. **arm64 + proot 下 Worker SIGSEGV（退出码 11）**：issue 1635 在 `linux arm64 6.1.118-android14` + Termux 上稳定复现；
   PR 1984「修复 WebUI 白屏与 arm64/proot 下 Worker SIGSEGV」于 2026-08-10 合并，办法是走 Node 进程并注入 `--no-sandbox`，可用 `NAPCAT_FORCE_NODE_PROCESS=1` 强制。之后 PR 2045 又收窄了注入条件。依据：[issue 1635](https://github.com/NapNeko/NapCatQQ/issues/1635)、[PR 1984](https://github.com/NapNeko/NapCatQQ/pull/1984)、[PR 2045](https://github.com/NapNeko/NapCatQQ/pull/2045)。
2. **Termux 原生路线的失败**：udocker/Termux 里跑 QQ 因缺 `/dev/shm` 直接 FATAL 退出（issue 1203）；
   原生 Termux 下 `npm install` 卡在 `node-pty` 编译报 `undefined variable android_ndk_path`（issue 1261）。
3. **proot 容器里的进程管理**：issue 1715 报告 screen 在断开连接后丢会话、NapCat 变孤儿进程，作者建议改用 tmux（在 proot-distro 下更稳），并说「在 Android 上部署 napcat 还是比较常见的现象」。
4. **安卓后台被杀（这一节的保活要求是硬要求）**：有实测博客记录「proot-distro 跑 ubuntu，手机一熄屏 Termux 就被 `kill -9`」，
   给出的解法是：关闭电池优化 / 允许完全后台、在最近任务里锁定 Termux、`termux-wake-lock` 拿唤醒锁、
   必要时用 adb 把 `max_phantom_processes` 调大。依据：[zeerd 博客](https://blog.zeerd.com/color-os-termux/)、[Termux wiki · wake-lock](https://wiki.termux.dev/wiki/Termux-wake-lock)。
   MIUI 侧还要额外做：允许自启动、省电策略设为「无限制」、在安全中心里锁定应用、必要时关掉 MIUI 优化。
   依据：[Don't kill my app · Xiaomi](https://dontkillmyapp.com/xiaomi)。
   「无障碍服务」这条路：**没查到**任何来源说它对 Termux 保活有效 —— 不写进结论。
5. **风控 / 掉线（与手机无关的普遍风险）**：issue 1728「最新版本的 NapCat/QQ 登录会被风控检测导致频繁掉线」，
   表现为频繁被 `KickedOffline`，作者临时方案是降回旧版 QQ + 旧版 NapCat；该 issue 已被关闭为 not planned。
   依据：[issue 1728](https://github.com/NapNeko/NapCatQQ/issues/1728)。
6. **退路 Lagrange.Core 的现状**：仓库 2026-09-07 仍有推送、约 2992 star、42 个 open issue；发布只有 `nightly`（2025-08-02），
   里面有 `Lagrange.OneBot_linux-arm64_net9.0_SelfContained.tar.gz`（下载量 4527）。
   已知问题：armhf（32 位）设备上无法连接腾讯登录服务器（issue 444，已关闭）。依据：[仓库](https://github.com/LagrangeDev/Lagrange.Core)、[nightly](https://github.com/LagrangeDev/Lagrange.Core/releases/tag/nightly)、[issue 444](https://github.com/LagrangeDev/Lagrange.Core/issues/444)。

## 我的建议（按「省事程度 / 风险」排）

1. **首选：路 1 官方 Termux 脚本。** 一条命令装完，免 root，官方文档背书；先按原样跑通，再考虑换 tmux 托管。
   上生产前必做保活清单（第 4 条），否则熄屏就被杀。
2. **跑通后立刻验证两条**：一是 arm64/proot 的 Worker 是否还崩（崩就升 NapCat 并设 `NAPCAT_FORCE_NODE_PROCESS=1`）；
   二是连续挂 24 小时看会不会被风控踢下线（issue 1728 那种），这跟手机无关，换设备也躲不掉。
3. **退路：Lagrange.Core 的 arm64 自包含包。** 不依赖 QQNT，环境最轻；代价是协议自实现，风控/掉线风险要自己扛，且只有 nightly 构建。
4. **不建议为这件事刷 postmarketOS**：三者里只有 raphael 有设备包且已归档无维护，thyme/odin 没有移植；
   加上 musl 与 glibc 的隔阂，收益远小于风险。

## 没查到的 / 不确证的

- **没查到** postmarketOS 对小米 10S（thyme）、MIX 4（odin）的设备包或移植页面；也没有这三个机型「当常驻服务器跑」的实测报告。
- **没查到**「真 Linux 手机跑无头客户端」与「Windows 桌面版」在风控/掉线频率上的任何对比数据 —— 两边都可能被 issue 1728 那类风控影响，但没有证据说哪边更好。
- **没查到**无障碍服务对 Termux 类应用保活有效的实测；也没查到 MIUI 上「能稳定活几天」的定量记录，只有上面的配置清单与熄屏被杀的实例。
- **不确证**：手机上的可用内存/发热是否够长期挂 NapCat + QQNT（没有找到移动端实测数据）；安卓 12+ 的 phantom process 限制对 proot 的重启影响强度。
- 未实测：本次调研全程只读，没有在任何手机上跑过上述脚本；路 1 的可行性依据是官方脚本 + 他人 issue，不是我在手机上亲手跑通。

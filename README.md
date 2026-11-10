# v2ray-agent

本仓库是 [mack-a/v2ray-agent](https://github.com/mack-a/v2ray-agent) 的 1Panel 兼容分支。中文入口 `install.sh` 保留独立 Nginx 模式，并在选择 Xray-core 后提供 **1Panel OpenResty** 安装模式。英文入口和独立 sing-box 安装流程保持上游行为。

上游功能补丁已同步至 `v3.5.41`（`b407b5e`），保留本 fork 的 1Panel 后端和原有测试。Xray 原生 Hysteria2 沿用独立模式；1Panel 模式的协议范围保持不变。

## 1Panel OpenResty 模式

先在服务器安装并启动 **1Panel v2、其 OpenResty 应用**，创建网站和有效证书，并开启该证书的自动续期。OpenResty 须使用 host 网络并包含 `stream_ssl_preread` 模块。安装器会列出面板实际的网站与证书 ID，供选择；无须开启面板公网 API，也不需要提供 API Key。

```bash
curl -fsSL https://raw.githubusercontent.com/iamsyc/v2ray-agent/master/install.sh -o /root/install.sh
bash /root/install.sh
```

选择“安装”或“任意组合安装” → “Xray-core” → “1Panel OpenResty”。默认组合为 `0,7,12,14`；同时支持上游的 VLESS/VMess WebSocket 和 Trojan 组合。每次端口提示均注明对应协议。手动端口允许 `1–65535`，各节点端口必须互不重复，不能占用网站内部端口和协议内部保留端口。

安装前会明确提示：OpenResty 接管公网 TCP 443，关闭面板内置默认 HTTPS 占位页，面板现有 HTTPS 网站统一使用内部端口（默认 888），Xray 中使用 443 的入口只绑定 `127.0.0.1`。浏览器访问代理域名仍显示该域名的伪装站，HTTP 跳转使用标准 HTTPS 地址，不带内部端口。使用了 HTTP/3、显式 SSL 域名端口 443 或未登记到面板的 443 配置时，需要先在面板处理，安装器不会覆盖这些配置。

网站、站点配置、TCP 配置及证书均沿用面板的实际目录，不假设面板一定安装在 `/opt/1panel`。REALITY 回落目标使用本机网站端口，避免绕回公网入口。订阅目录通过只读父目录挂载提供给 OpenResty，兼容上游原子替换订阅目录的发布方式；HTTP 仅暴露订阅格式和摘要文件名的白名单。

**证书由 1Panel 续期**。证书目录发生变化后，安装器配置的 systemd 任务校验证书域名、有效期和私钥配对，部署到 Xray 的固定路径，并核对 OpenResty 实际提供的证书。证书不变时不会重复重启 Xray；部署失败会保留原 Xray 证书，五分钟后重试。新证书载入需要重启 Xray，现有代理连接会短暂中断。原 vasma 的 ACME 续期任务会移除。

用户与订阅管理继续使用 `vasma`。重新安装会沿用所选模式；需要改变节点端口时再次运行安装流程。1Panel 模式只管理 Xray，sing-box/Hysteria2/TUIC 请使用原独立模式。如已配置绑定入站或用户的 SOCKS5 分流规则，重新安装或切换核心前需先解除这些绑定。核心版本与 Web 模式独立选择，重新安装默认保留现有核心版本；旧客户端可自行选择兼容发行版，例如 `v26.3.27`。本模式不会消除新旧 REALITY 客户端的握手差异。

此分支没有迁移平台或额外的升级、回滚管理器。脚本自身更新从本 fork 获取，后端模块按内嵌 SHA-256 校验后加载。公网 IPv4 地址发生变化后需重新安装以更新 TCP 监听地址。卸载代理时保留面板网站、证书及网站的 443 转发入口。

### 开发验证

```bash
python3 tools/update_onepanel_manifest.py --check
bash -n install.sh
bash -n onepanel/backend.sh
python3 -m unittest discover -s checks -p 'test_onepanel_backend.py' -v
```

修改 `onepanel/` 后运行 `python3 tools/update_onepanel_manifest.py` 更新加载器摘要。`checks/onepanel_e2e.py` 只能在明确标记的临时测试机中运行，会安装真实面板应用、创建测试网站和测试证书；其合成证书用于验证部署事件，不验证真实公网 CA 或 DNS 账号。不要在生产服务器运行该测试脚本。

集成验证环境为 Ubuntu 24.04 ARM64、1Panel v2.4.2、OpenResty 1.27.1.2 和 Xray v26.3.27。测试覆盖真实安装、七种订阅节点的实际传输、五种订阅格式、菜单增删用户、面板证书替换后的自动部署，其他面板 HTTPS 网站共存，以及卸载代理后保留网站和证书。公有 CA 签发与真实 DNS 账号续期不在合成证书测试范围内。

- [感谢 JetBrains 提供的非商业开源软件开发授权](https://www.jetbrains.com/?from=v2ray-agent)


[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Telegram Channel](https://img.shields.io/badge/Telegram-Channel-blue)](https://t.me/v2rayAgentChannel)
[![Telegram Group](https://img.shields.io/badge/Telegram-Group-blue)](https://t.me/technologyshare)
[![Official Website](https://img.shields.io/badge/Website-v2ray--agent.com-blue)](https://www.v2ray-agent.com/)
[![English Version](https://img.shields.io/badge/English-Version-blue)](documents/en/README_EN.md)

基于 Bash 的 Xray-core / sing-box 安装与管理脚本，支持普通主机部署和 Docker Reality 独立部署。

## 功能

*   **多核心支持:** 支持 Xray-core 和 sing-box，可按需安装单核心或组合使用.
*   **多协议支持:** 支持 VLESS（TLS、Reality、XHTTP）、VMess、Trojan、Hysteria2、Tuic、NaiveProxy、AnyTLS，以及独立 SOCKS5 入站.
*   **TLS 与 Reality:** TLS 协议可通过 acme.sh 自动申请和续订证书；Reality 支持无域名部署.
*   **易于管理:** 提供菜单管理用户、端口、配置、核心版本、日志和证书，并支持二维码及客户端节点信息输出.
*   **订阅支持:** 生成和管理订阅链接，支持远程订阅配置.
*   **分流管理:** 提供 WARP/WireGuard（IPv4/IPv6）、IPv6、SOCKS5、DNS、VMess+WS、SNI 反向代理等分流能力，可用于流媒体等场景.
*   **网络策略管理:** 支持 Hysteria2 端口跳跃、域名黑名单和 BT/P2P 下载限制.
*   **Docker Reality:** 无需域名即可通过 Docker 部署 VLESS-Reality，支持 Vision、XHTTP 或同时启用两种模式，并提供交互式和命令行参数模式.
*   **更多内容请访问[官方网站](https://www.v2ray-agent.com/categories/jiao-cheng)、[备用](https://www.592083.com/categories/jiao-cheng)、[X](https://x.com/v2rayagent)**

## 环境要求

*   **脚本版:** Linux、root 权限，支持 CentOS、Debian、Ubuntu 和 Alpine；支持 amd64/x86_64、arm64/aarch64.
*   **Docker Reality:** Linux、root 或 sudo 权限，以及正在运行的 Docker；脚本可在交互模式下提示安装 Docker.

## 快速开始

### 安装脚本版

```
curl -fsSL "https://raw.githubusercontent.com/iamsyc/v2ray-agent/master/install.sh" -o /root/install.sh && chmod 700 /root/install.sh && /root/install.sh
```

### 使用

安装后，运行以下命令可再次打开管理菜单:

```
vasma
```

### 安装Docker版
```
wget -P /root -N --no-check-certificate "https://raw.githubusercontent.com/mack-a/v2ray-agent/master/shell/docker_reality.sh" && chmod 700 /root/docker_reality.sh && /root/docker_reality.sh
```

### 使用 

* [Docker Reality 使用说明](https://www.v2ray-agent.com/archives/019e1b57-92b3-70ab-8919-cdf8c0bb4fe9)
 
安装后，运行以下命令可再次打开管理菜单:

```
vasmad
```

## 文档和指南

*   [SOCKS5 入站与分流出站：简明教程](https://www.v2ray-agent.com/archives/01a1b212-d16c-743c-a4b1-d6150a948bda)

*   [八合一脚本从入门到精通](https://www.v2ray-agent.com/archives/1710141233)
*   [脚本快速搭建教程](https://www.v2ray-agent.com/archives/1682491479771)
*   [脚本使用注意事项](https://www.v2ray-agent.com/archives/1679931532764)
*   [脚本异常处理](https://www.v2ray-agent.com/archives/1684115970026)   
*   [VPS选购攻略](https://www.v2ray-agent.com/archives/1679975663984)
*   [垃圾VPS大救星，hysteria2最新协议一键搭建](https://www.v2ray-agent.com/archives/1697162969693)
*   [RackNerd低价 联通AS4837套餐，年付10美起](https://www.v2ray-agent.com/archives/racknerdtao-can-zheng-li-nian-fu-10mei-yuan)
*   [搬瓦工优质套餐推荐](https://www.v2ray-agent.com/archives/2023nian-ban-wa-gong-ji-fang-tui-jian)
*   [DMIT优质套餐推荐](https://www.v2ray-agent.com/archives/1679159868033)

## 社区与支持

*   **Telegram:** [频道](https://t.me/v2rayAgentChannel) | [群组](https://t.me/technologyshare)
*   **网站:** [官网](https://www.v2ray-agent.com/) | [备用](https://www.592083.xyz/)
*   **反馈:** [提交 issue](https://github.com/mack-a/v2ray-agent/issues)
*   **X:** [链接](https://x.com/v2rayagent)

## 捐赠

感谢您对开源项目的关注和支持。如果您觉得这个项目对您有帮助，欢迎通过以下方式进行捐赠。

*   [购买VPS捐赠](https://www.v2ray-agent.com/categories/vps)
*   [通过虚拟币向我捐赠](https://www.v2ray-agent.com/1679123834836)

## 许可证

本项根据 [AGPL-3.0 许可证](LICENSE) 授权.

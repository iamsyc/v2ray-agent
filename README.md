# v2ray-agent

本仓库是 [mack-a/v2ray-agent](https://github.com/mack-a/v2ray-agent) 的 1Panel 兼容分支。中文入口 `install.sh` 保留独立 Nginx 模式，并在选择 Xray-core 后提供 **1Panel OpenResty** 安装模式。英文入口和独立 sing-box 安装流程保持上游行为。

上游功能补丁已同步至 `v3.5.41`（`b407b5e`）。两种安装模式均支持 Xray 原生 Hysteria2。

## 1Panel OpenResty 模式

先在服务器安装并启动 **1Panel v2 和 OpenResty**，保持面板网站的默认 HTTPS 端口为 `443`。在 1Panel 中为代理域名建站、开启 HTTPS、绑定证书并启用自动续期。OpenResty 须使用 host 网络并包含 `stream_ssl_preread` 模块，系统须支持 nftables。安装器通过面板本机 Unix socket 读取网站和证书。

```bash
curl -fsSL https://raw.githubusercontent.com/iamsyc/v2ray-agent/master/install.sh -o /root/install.sh
bash /root/install.sh
```

选择“安装”或“任意组合安装” → “Xray-core” → “1Panel OpenResty”。默认组合为 `0,6,7,12,14`，也可选 VLESS/VMess WebSocket 和 Trojan。默认端口为 TLS Vision TCP `777`、Hysteria2 UDP `8443`、REALITY Vision TCP `443`、REALITY XHTTP TCP `555`、XHTTP TLS TCP `666`。安装时逐项可改，端口冲突按 TCP 和 UDP 分别检查。Hysteria2 需要支持原生 Hysteria2 的 Xray-core，UDP `443` 留给网站 HTTP/3。

公网 TCP `443` 经独立的 nftables 表送到 OpenResty 的 SNI 分流入口。网站 HTTPS 仍由 1Panel 在 `443` 管理。REALITY Vision 选择 `443` 时，匹配的域名进入 Xray，普通浏览器握手回落到本机网站。分流匹配本机地址，服务器地址变化后无需重新写入 IP。分流保留真实访问来源地址。

订阅默认使用代理站点的域名和 TCP `888`。也可选择另一个已启用 HTTPS 的 1Panel 站点作为订阅域名，端口可单独设定。订阅配置、网站和证书路径从面板实际部署读取。OpenResty 仅读取发布的订阅文件，URL 按格式和摘要文件名匹配。

**证书由 1Panel 续期**。证书目录发生变化后，安装器配置的 systemd 任务校验证书域名、有效期和私钥配对，部署到 Xray 的固定路径，并核对 OpenResty 实际提供的证书。证书不变时不会重复重启 Xray；部署失败会保留原 Xray 证书，五分钟后重试。新证书载入需要重启 Xray，现有代理连接会短暂中断。原 vasma 的 ACME 续期任务会移除。

用户和订阅继续使用 `vasma` 管理。安装参数记录在 `/etc/v2ray-agent/onepanel.json`。有原生 Hysteria2 入站时，账号增删也覆盖该协议。独立 sing-box 和 TUIC 继续走独立模式。核心版本和 Web 模式独立选择，旧版核心可以安装其支持的协议组合。

共享 443 布局用于新装。已有旧版 1Panel 布局继续按其原状态运行。脚本自身更新从本 fork 获取，后端模块按内嵌 SHA-256 校验。卸载共享 443 模式会移除代理分流和订阅，保留面板网站及证书。

### 开发验证

```bash
python3 tools/update_onepanel_manifest.py --check
bash -n install.sh
bash -n onepanel/backend.sh
python3 -m unittest discover -s checks -p 'test_onepanel_backend.py' -v
```

修改 `onepanel/` 后运行 `python3 tools/update_onepanel_manifest.py` 更新加载器摘要。`checks/onepanel_e2e.py` 只能在明确标记的临时测试机中运行，会安装真实面板应用、创建测试网站和测试证书；其合成证书用于验证部署事件，不验证真实公网 CA 或 DNS 账号。不要在生产服务器运行该测试脚本。

集成验证环境为 Ubuntu 24.04 ARM64、1Panel v2.4.2、OpenResty 1.27.1.2 和 Xray v26.11.9。测试覆盖实际安装、五种默认协议的实际传输、五种订阅格式、独立订阅域名和端口、菜单增删用户、面板证书替换后的自动部署、新建面板 HTTPS 网站，以及卸载代理后保留网站和证书。公有 CA 签发与真实 DNS 账号续期不在合成证书测试范围内。

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

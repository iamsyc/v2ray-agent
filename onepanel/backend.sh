#!/usr/bin/env bash
# 1Panel v2 OpenResty installation backend, sourced by the main installer.
# Globals are shared with install.sh, which owns their callers and initialization.
# shellcheck disable=SC2034,SC2154
# No production credentials or per-server IDs are embedded in this module.
onepanelController() {
    python3 /etc/v2ray-agent/onepanel/controller.py "$@"
}

onepanelLoadState() {
    local value
    value=$(python3 -c 'import json; print(json.load(open("/etc/v2ray-agent/onepanel.json"))["domain"])') || return 1
    onepanelDomain=${value}
    onepanelWebPort=$(python3 -c 'import json; print(json.load(open("/etc/v2ray-agent/onepanel.json"))["web_port"])') || return 1
    onepanelSiteRoot=$(python3 -c 'import json; from pathlib import Path; c=json.load(open("/etc/v2ray-agent/onepanel.json")); print(Path(c["website_root"])/"sites"/c["alias"]/"index")') || return 1
    onepanelContainer=$(python3 -c 'import json; print(json.load(open("/etc/v2ray-agent/onepanel.json"))["container"])') || return 1
    domain=${onepanelDomain}
    currentHost=${onepanelDomain}
    nginxStaticPath="${onepanelSiteRoot}/"
    webBackend=onepanel
}

onepanelReadPort() {
    python3 -c 'import json,sys; print(json.load(open("/etc/v2ray-agent/onepanel.json"))["ports"].get(sys.argv[1],""))' "$1"
}

onepanelReadSubscribe() {
    subscribePort=${onepanelWebPort}
    subscribeType=https
    subscribeDomain=${onepanelDomain}
}

onepanelNginx() {
    case "$1" in
    stop) return 0 ;; # Never interrupt all panel websites for a node/user operation.
    start) onepanelController reload ;;
    *) return 2 ;;
    esac
}

onepanelRunning() {
    [[ "$(docker inspect -f '{{.State.Running}}' "${onepanelContainer}" 2>/dev/null)" == true ]]
}

onepanelManagedWebsite() {
    echoContent yellow "网站和证书由 1Panel 管理，网站目录：${onepanelSiteRoot}"
}

onepanelDependencies() {
    [[ "${EUID}" == 0 ]] || { echoContent red '1Panel 模式请使用 root 运行'; return 1; }
    command -v systemctl >/dev/null && command -v docker >/dev/null || {
        echoContent red '请先安装并启动 1Panel v2 和其 OpenResty 应用'; return 1;
    }
    local missing=false tool
    for tool in python3 jq curl openssl wget unzip ip ss crontab lsof qrencode; do
        command -v "${tool}" >/dev/null || missing=true
    done
    if [[ "${missing}" == true ]]; then
        if command -v apt-get >/dev/null; then
            apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3 jq curl openssl wget unzip cron iproute2 lsof qrencode || return 1
        elif command -v dnf >/dev/null; then
            dnf install -y python3 jq curl openssl wget unzip cronie iproute lsof qrencode || return 1
        else
            echoContent red '请先安装 python3、jq、curl、openssl、wget、unzip、cron、iproute、lsof 和 qrencode'; return 1
        fi
    fi
}

onepanelPromptPort() {
    local key=$1 label=$2 default=$3 answer
    answer=$(onepanelReadPort "${key}" 2>/dev/null)
    default=${answer:-${default}}
    read -r -p "${label} [回车 ${default}，允许 1-65535]:" answer
    answer=${answer:-${default}}
    [[ "${answer}" =~ ^[0-9]{1,5}$ ]] && ((10#${answer} >= 1 && 10#${answer} <= 65535)) || {
        echoContent red '端口必须为 1-65535 的整数'; return 1;
    }
    onepanelChosenPorts=$(jq --arg key "${key}" --argjson port "$((10#${answer}))" '. + {($key):$port}' <<<"${onepanelChosenPorts}")
}

onepanelInstall() {
    onepanelDependencies || return 1
    if [[ -f "${onepanelInstallerFile}" && "${onepanelInstallerFile}" != /etc/v2ray-agent/install.sh ]]; then
        install -m 700 "${onepanelInstallerFile}" /etc/v2ray-agent/install.sh || return 1
    fi
    [[ -f /etc/v2ray-agent/install.sh ]] || { echoContent red '请先下载 install.sh 到普通文件，再运行安装'; return 1; }
    ln -sf /etc/v2ray-agent/install.sh /usr/bin/vasma || return 1
    local inventory websiteId sslId webPort confirmation selection coreVersion
    inventory=$(onepanelController inventory) || return 1
    echoContent skyBlue '\n1Panel 网站：'
    jq -r '.websites[] | "\(.id). \(.domain)  当前证书ID: \(.ssl_id // 0)"' <<<"${inventory}"
    echoContent skyBlue '1Panel 证书：'
    jq -r '.certificates[] | "\(.id). \(.primaryDomain)  状态: \(.status)  自动续期: \(.autoRenew)"' <<<"${inventory}"
    echoContent yellow '请先在 1Panel 创建网站及可自动续期的有效证书；本安装器复用其目录和证书。'
    local previousWebsite='' previousSSL=''
    if [[ -f /etc/v2ray-agent/onepanel.json ]]; then
        previousWebsite=$(jq -r .website_id /etc/v2ray-agent/onepanel.json)
        previousSSL=$(jq -r .ssl_id /etc/v2ray-agent/onepanel.json)
    fi
    read -r -p "网站 ID [${previousWebsite}]:" websiteId
    read -r -p "证书 ID [${previousSSL}]:" sslId
    websiteId=${websiteId:-${previousWebsite}}
    sslId=${sslId:-${previousSSL}}
    read -r -p 'OpenResty 网站/订阅内部 HTTPS 端口 [回车 888]:' webPort
    webPort=${webPort:-888}
    [[ "${websiteId}" =~ ^[0-9]+$ && "${sslId}" =~ ^[0-9]+$ && "${webPort}" =~ ^[0-9]{1,5}$ ]] || return 1
    echoContent yellow "将由 OpenResty 接管公网 TCP 443，将现有 HTTPS 网站移至 ${webPort}；Xray 443 仅监听本机。"
    echoContent yellow '面板内置的默认 HTTPS 占位页将关闭，已登记的网站保留。'
    echoContent yellow '需要暂停现有 Xray；HTTP/3 或显式域名端口 443 请先在面板调整。'
    read -r -p '确认使用 1Panel OpenResty 模式安装？[y/n]:' confirmation
    [[ "${confirmation}" == y ]] || return 1
    onepanelController select --website-id "${websiteId}" --ssl-id "${sslId}" --web-port "${webPort}" >/dev/null || return 1
    onepanelLoadState || return 1

    echoContent yellow '协议编号：0 TLS+Vision，1 VLESS+WS，3 VMess+WS，4 Trojan，7 REALITY+Vision，12 REALITY+XHTTP，14 TLS+XHTTP'
    read -r -p '选择协议 [回车 0,7,12,14]:' selection
    selection=${selection:-0,7,12,14}
    [[ "${selection}" =~ ^(0|1|3|4|7|12|14)(,(0|1|3|4|7|12|14))*$ ]] || {
        echoContent red '请选择列出的 Xray 协议编号，使用英文逗号分隔'; return 1;
    }
    selectCustomInstallType=$(normalizeXrayInstallSelection "${selection}")
    onepanelChosenPorts='{}'
    if [[ "${selectCustomInstallType}" == *,0,* ]]; then
        local tlsDefault=443
        [[ "${selectCustomInstallType}" == *,7,* ]] && tlsDefault=777
        onepanelPromptPort 0 'TLS + Vision 公网 TCP 端口' "${tlsDefault}" || return 1
    fi
    [[ "${selectCustomInstallType}" != *,7,* ]] || onepanelPromptPort 7 'REALITY + Vision 公网 TCP 端口' 443 || return 1
    [[ "${selectCustomInstallType}" != *,12,* ]] || onepanelPromptPort 12 'REALITY + XHTTP 公网 TCP 端口' 555 || return 1
    [[ "${selectCustomInstallType}" != *,14,* ]] || onepanelPromptPort 14 'TLS + XHTTP 公网 TCP 端口' 666 || return 1
    onepanelController set-ports --ports "${onepanelChosenPorts}" || return 1

    if [[ -x /etc/v2ray-agent/xray/xray ]]; then
        coreVersion="v$(/etc/v2ray-agent/xray/xray version | awk 'NR==1 {print $2}')"
    else
        coreVersion=latest
    fi
    echoContent yellow '核心版本可输入 latest 或发行标签，例如 v26.3.27。1Panel 兼容模式不会改变客户端握手要求。'
    read -r -p "Xray-core 版本 [回车 ${coreVersion}]:" version
    version=${version:-${coreVersion}}
    if [[ "${version}" == latest ]]; then
        version=$(curl -fsSL https://api.github.com/repos/XTLS/Xray-core/releases/latest | jq -er .tag_name) || return 1
    fi
    [[ "${version}" =~ ^v[0-9][A-Za-z0-9._-]*$ ]] || return 1
    mkdirTools
    onepanelController prepare || return 1
    installCoreRelease xray "${version}" || return 1
    updateGeoSite false /etc/v2ray-agent/xray/ || return 1
    onepanelController seed-certificate || return 1
    coreInstallType=1
    selectCoreType=1
    configPath=/etc/v2ray-agent/xray/conf/
    ctlPath=/etc/v2ray-agent/xray/xray
    port=$(onepanelReadPort 0)
    customPort=${port}
    if [[ -n "${currentClients}" || -n "${currentRealityPrivateKey}" ]]; then
        lastInstallationConfig=true
        echoContent green '保留现有用户和 REALITY 密钥'
    fi
    randomPathFunction 1
    initXrayConfig custom 2 || return 1
    installXrayService 3
    handleXray start transaction || return 1
    systemctl is-active --quiet xray || return 1
    onepanelController watch || return 1
    local nodePort
    for nodePort in 80 443 "${onepanelWebPort}" $(jq -r '.[]' <<<"${onepanelChosenPorts}"); do
        allowPort "${nodePort}"
    done
    checkWgetShowProgress
    subscribe false || return 1
    echoContent green "1Panel OpenResty 模式安装完成：https://${onepanelDomain}/"
    echoContent yellow '网站与证书请在 1Panel 管理；用户、节点和订阅继续使用 vasma。'
}

onepanelUninstall() {
    local answer
    read -r -p '卸载代理，保留 1Panel 网站和证书？[y/n]:' answer
    [[ "${answer}" == y ]] || return 0
    onepanelController detach || return 1
    systemctl stop xray
    systemctl disable xray
    rm -f /etc/systemd/system/xray.service /usr/bin/vasma /usr/sbin/vasma
    systemctl daemon-reload
    rm -rf /etc/v2ray-agent
    echoContent green '代理已卸载，1Panel 网站、证书和网站 443 入口保留。'
}

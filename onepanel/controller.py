#!/usr/bin/env python3
"""1Panel v2 backend. All panel writes use its local agent API, never SQLite.

This module deliberately has no migration, release-manager or rollback CLI.
The small commands below are installation hooks used by install.sh.
"""
import argparse
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time

AGENT = Path('/etc/v2ray-agent')
STATE = AGENT / 'onepanel.json'
SOCKET = '/etc/1panel/agent.sock'
FORMATS = 'clashMeta|default|clashMetaProfiles|sing-box|sing-box_profiles'
SAFE_NAME = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]*$')
INTERNAL_PORTS = {31296, 31297, 31299, 31300, 31302, 45987, 45988}
DOMAIN = re.compile(r'^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9-]{2,63}$')


def run(*args, data=None, timeout=180):
    p = subprocess.run(list(map(str, args)), input=data, capture_output=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f'{args[0]} failed: {p.stderr.decode(errors="replace")[-600:]}')
    return p.stdout.decode()


def write(path, content, mode=0o600):
    """Publish one file atomically, including when replacing an old symlink."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as f:
            f.write(content.encode() if isinstance(content, str) else content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=180)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Panel:
    def __init__(self, path=SOCKET):
        self.path = path

    def call(self, path, body=None):
        c = UnixHTTP(self.path)
        try:
            c.request('GET' if body is None else 'POST', '/api/v2/' + path,
                      body=None if body is None else json.dumps(body),
                      headers={'Content-Type': 'application/json'})
            response = c.getresponse()
            raw = response.read(16 * 1024 * 1024)
            result = json.loads(raw)
            if response.status != 200 or result.get('code') != 200:
                raise RuntimeError(f'1Panel {path}: {result.get("message", response.status)}')
            return result.get('data')
        finally:
            c.close()


def items(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return value.get('items') or value.get('list') or []
    return []


def validate_state(c):
    if c.get('schema') != 1 or c.get('backend') != 'onepanel':
        raise ValueError('Unsupported 1Panel installation state')
    for key in ('alias', 'container'):
        if not SAFE_NAME.fullmatch(c[key]) or c[key] in ('.', '..'):
            raise ValueError(f'Invalid {key}')
    if not DOMAIN.fullmatch(c['domain']):
        raise ValueError('Select a website with a DNS hostname, not an IP or wildcard')
    for key in ('website_id', 'ssl_id', 'app_id'):
        if type(c[key]) is not int or c[key] < 1:
            raise ValueError(f'Invalid {key}')
    if type(c['web_port']) is not int or not 1 <= c['web_port'] <= 65535 or c['web_port'] in {80, 443} | INTERNAL_PORTS:
        raise ValueError('Website port is invalid or reserved by an internal listener')
    address = ipaddress.ip_address(c['listen_ipv4'])
    if address.version != 4 or address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError('Public listener must use an assigned non-loopback IPv4 address')
    for key in ('website_root', 'compose_path'):
        p = Path(c[key])
        if not p.is_absolute() or '..' in p.parts or any(x in str(p) for x in '\n\r;{}"$'):
            raise ValueError(f'Invalid {key}')
    if c['web_port'] in c.get('ports', {}).values():
        raise ValueError('A proxy node port collides with the website port')
    values = list(c.get('ports', {}).values())
    if len(values) != len(set(values)) or any(type(p) is not int or not 1 <= p <= 65535 or p == 80 for p in values):
        raise ValueError('Node ports must be distinct valid TCP ports, excluding website HTTP 80')
    if set(values) & INTERNAL_PORTS:
        raise ValueError('Node port is reserved by an internal protocol/fallback listener')
    return c


def load(path=STATE):
    return validate_state(json.loads(Path(path).read_text()))


def site_path(c):
    return Path(c['website_root']) / 'sites' / c['alias']


def site_config(c):
    return Path(c['website_root']) / 'conf.d' / (c['alias'] + '.conf')


def container_path(c, path):
    return '/www/' + Path(path).relative_to(c['website_root']).as_posix()


def stream_config(c, front):
    ipaddress.ip_address(c['listen_ipv4'])
    upstream = '127.0.0.1:443' if front else f'127.0.0.1:{c["web_port"]}'
    ipv6 = '\n    listen [::]:443 ipv6only=on;' if c.get('ipv6') else ''
    return f'''# Managed by v2ray-agent 1Panel backend. No TLS termination here.
map $ssl_preread_server_name $v2ray_agent_backend {{
    {c['domain']} {upstream};
    default 127.0.0.1:{c['web_port']};
}}
server {{
    listen {c['listen_ipv4']}:443;{ipv6}
    ssl_preread on;
    proxy_pass $v2ray_agent_backend;
    proxy_connect_timeout 5s;
    proxy_timeout 24h;
    proxy_socket_keepalive on;
    proxy_half_close on;
}}
'''


def subscription_location():
    return f'''location ~ "^/s/({FORMATS})/([a-f0-9]{{32}})$" {{
    default_type 'text/plain; charset=utf-8';
    alias /srv/v2ray-agent/subscribe/$1/$2;
}}
'''


def fallback_config(c):
    root = container_path(c, site_path(c) / 'index')
    location = subscription_location()
    return f'''# Managed by v2ray-agent 1Panel backend; TLS/Vision fallback.
server {{
    listen 127.0.0.1:31300 proxy_protocol;
    server_name {c['domain']};
    set_real_ip_from 127.0.0.1;
    real_ip_header proxy_protocol;
    root {root};
    {location}
}}
server {{
    listen 127.0.0.1:31302 proxy_protocol;
    http2 on;
    server_name {c['domain']};
    set_real_ip_from 127.0.0.1;
    real_ip_header proxy_protocol;
    root {root};
    {location}
}}
'''


def check_certificate(cert, key, domain):
    run('openssl', 'x509', '-checkend', '86400', '-noout', data=cert)
    result = run('openssl', 'x509', '-checkhost', domain, '-noout', data=cert)
    if 'does NOT match' in result:
        raise ValueError('The selected certificate does not cover the website hostname')
    if run('openssl', 'x509', '-pubkey', '-noout', data=cert) != run('openssl', 'pkey', '-pubout', data=key):
        raise ValueError('Certificate and private key do not match')


def assigned_ipv4():
    routes = json.loads(run('ip', '-j', '-4', 'route', 'show', 'default'))
    if not routes:
        raise ValueError('No IPv4 default route')
    addresses = json.loads(run('ip', '-j', '-4', 'address', 'show', 'dev', routes[0]['dev']))
    for interface in addresses:
        for address in interface.get('addr_info', []):
            if address.get('scope') == 'global':
                return address['local']
    raise ValueError('No IPv4 address on the default-route interface')


class Backend:
    def __init__(self, panel=None):
        self.panel = panel or Panel()

    def websites(self):
        result = self.panel.call('websites/search', {'page': 1, 'pageSize': 1000, 'name': '', 'websiteGroupId': 0,
                                                   'orderBy': 'primary_domain', 'order': 'ascending'})
        if isinstance(result, dict) and result.get('total', 0) > 1000:
            raise ValueError('This installer supports at most 1000 registered websites')
        return items(result)

    def inventory(self):
        if not Path(self.panel.path).is_socket():
            raise ValueError('1Panel v2 local agent socket is unavailable; start 1Panel first')
        names = run('docker', 'ps', '-a', '--format', '{{.Names}}').splitlines()
        containers = json.loads(run('docker', 'inspect', *names)) if names else []
        candidates = [x for x in containers if 'openresty' in x['Config']['Image'].lower()
                      and x['Config'].get('Labels', {}).get('createdBy') == 'Apps']
        if len(candidates) != 1:
            raise ValueError('Exactly one 1Panel-managed OpenResty container is required')
        d = candidates[0]
        if d['HostConfig']['NetworkMode'] != 'host':
            raise ValueError('1Panel OpenResty must use host networking')
        app_list = items(self.panel.call('apps/installed/search', {'page': 1, 'pageSize': 1000, 'name': '', 'all': True}))
        matches = [x for x in app_list if (x.get('containerName') or x.get('container')) == d['Name'].lstrip('/')]
        if len(matches) != 1:
            raise ValueError('Cannot match OpenResty to its 1Panel installed-app record')
        mounts = {x['Destination'].rstrip('/'): x['Source'] for x in d['Mounts']}
        if '/www' not in mounts:
            raise ValueError('OpenResty does not have the standard /www website mount')
        labels = d['Config']['Labels']
        compose = labels.get('com.docker.compose.project.config_files', '')
        if ',' in compose or not Path(compose).is_file():
            raise ValueError('A single existing OpenResty compose file is required')
        # Native module support is checked without requiring the container to be running.
        archive = subprocess.run(['docker', 'cp', d['Name'].lstrip('/') + ':/usr/local/openresty/nginx/sbin/nginx', '-'],
                                 capture_output=True, check=True).stdout
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            binary = tar.extractfile(tar.getmembers()[0]).read()
        if b'--with-stream_ssl_preread_module' not in binary:
            raise ValueError('OpenResty is missing stream_ssl_preread support')
        return {'app_id': int(matches[0]['id']), 'container': d['Name'].lstrip('/'),
                'website_root': mounts['/www'], 'compose_path': compose,
                'cpu_quota': d['HostConfig'].get('NanoCpus', 0) / 1e9,
                'memory_mb': d['HostConfig'].get('Memory', 0) / 1048576,
                'restart_policy': d['HostConfig'].get('RestartPolicy', {}).get('Name') or 'unless-stopped',
                'listen_ipv4': assigned_ipv4(),
                'ipv6': Path('/proc/sys/net/ipv6/conf/all/disable_ipv6').read_text().strip() == '0',
                'running': d['State']['Running']}

    def select(self, website_id, ssl_id, web_port):
        c = self.inventory()
        w = self.panel.call(f'websites/{website_id}')
        cert = self.panel.call(f'websites/ssl/{ssl_id}')
        c.update(schema=1, backend='onepanel', website_id=website_id, ssl_id=ssl_id,
                 alias=w['alias'], domain=w['primaryDomain'], web_port=web_port, ports={})
        validate_state(c)
        if not cert.get('autoRenew') or cert.get('status') != 'ready':
            raise ValueError('Select a ready 1Panel certificate with automatic renewal enabled')
        if cert.get('provider') not in ('dnsAccount', 'http'):
            raise ValueError('Select a certificate managed by a 1Panel ACME DNS or HTTP account')
        check_certificate(cert['pem'].encode(), cert['privateKey'].encode(), c['domain'])
        if not site_config(c).is_file():
            raise ValueError('The selected website has no generated OpenResty configuration')
        if STATE.exists():
            previous = load()
            if previous['website_id'] == website_id and previous['domain'] == c['domain']:
                c['ports'] = previous.get('ports', {})
        write(STATE, json.dumps(c, indent=2) + '\n')
        return c

    def set_ports(self, c, ports):
        if not isinstance(ports, dict) or any(k not in ('0', '7', '12', '14') for k in ports):
            raise ValueError('Invalid protocol-port selection')
        c = dict(c, ports=ports)
        validate_state(c)
        # Allow replacement of this installer's own Xray listeners, never unrelated services.
        p = subprocess.run(['systemctl', 'show', 'xray', '-p', 'MainPID', '--value'], capture_output=True, text=True)
        own_pid = p.stdout.strip()
        panel_pids = set()
        if c.get('running'):
            rows = run('docker', 'top', c['container'], '-eo', 'pid').splitlines()[1:]
            panel_pids = {x.strip() for x in rows if x.strip().isdigit()}
        own_pids = {own_pid} - {'', '0'}
        legacy_pids = set()
        if Path('/etc/nginx/conf.d/alone.conf').exists():
            result = subprocess.run(['systemctl', 'show', 'nginx', '-p', 'ControlGroup', '--value'], capture_output=True, text=True)
            group = result.stdout.strip()
            if group.startswith('/') and '..' not in group:
                procs = Path('/sys/fs/cgroup') / group.lstrip('/') / 'cgroup.procs'
                if procs.exists():
                    legacy_pids = set(procs.read_text().split())
        allowed = {port: own_pids for port in ports.values()}
        allowed[443] = own_pids | panel_pids | legacy_pids
        allowed[c['web_port']] = panel_pids | legacy_pids
        internal = INTERNAL_PORTS
        for port in internal:
            allowed[port] = own_pids | legacy_pids
            if (Path(c['website_root']) / 'conf.d/v2ray-agent-fallback.conf').exists() and port in (31300, 31302):
                allowed[port] |= panel_pids
        for port, permitted in allowed.items():
            listeners = run('ss', '-H', '-lntp', f'sport = :{port}')
            for line in listeners.splitlines():
                pids = re.findall(r'pid=(\d+)', line)
                if not pids or not set(pids).issubset(permitted):
                    raise ValueError(f'TCP port {port} is occupied by another service')
        write(STATE, json.dumps(c, indent=2) + '\n')

    def reload(self, c):
        run('docker', 'exec', c['container'], 'openresty', '-t')
        run('docker', 'exec', c['container'], 'openresty', '-s', 'reload')

    def https(self, website_id, cert_id, web_port):
        current = self.panel.call(f'websites/{website_id}/https')
        body = {'websiteId': website_id, 'enable': True, 'websiteSSLId': cert_id, 'type': 'existed',
                'httpConfig': 'HTTPAlso', 'SSLProtocol': current.get('SSLProtocol') or ['TLSv1.3', 'TLSv1.2'],
                'algorithm': current.get('algorithm') or 'HIGH:!aNULL:!MD5', 'hsts': current.get('hsts', False),
                'hstsIncludeSubDomains': current.get('hstsIncludeSubDomains', False),
                'httpsPorts': [web_port], 'http3': False}
        self.panel.call(f'websites/{website_id}/https', body)

    def prepare(self, c):
        """Normalize the panel's actual HTTPS default before creating the TCP front."""
        cohort = []
        for w in self.websites():
            if w.get('protocol', '').lower() == 'https' or w['id'] == c['website_id']:
                if not SAFE_NAME.fullmatch(w['alias']):
                    raise ValueError('Unsupported website alias')
                domains = self.panel.call(f'websites/domains/{w["id"]}')
                if any(x['port'] == 443 and x.get('ssl') for x in domains):
                    raise ValueError('Remove explicit SSL domain-port 443 entries in 1Panel first; its default HTTPS listener will be moved automatically')
                p = Path(c['website_root']) / 'conf.d' / (w['alias'] + '.conf')
                text = p.read_text()
                if re.search(r'listen[^;]*\bquic\b', text):
                    raise ValueError('Disable website HTTP/3 in 1Panel before enabling the shared TCP 443 mode')
                w['certificate_id'] = c['ssl_id'] if w['id'] == c['website_id'] else self.panel.call(
                    f'websites/{w["id"]}')['webSiteSSLId']
                if not w['certificate_id']:
                    raise ValueError('An existing HTTPS website has no associated certificate')
                cohort.append((w, p))
        if not cohort:
            raise ValueError('No managed website found')
        known = {p for _, p in cohort}
        for p in (Path(c['website_root']) / 'conf.d').glob('*.conf'):
            if p not in known and re.search(r'listen\s+(?:\[::\]:)?443\b', p.read_text()):
                raise ValueError('An unmanaged website config owns TCP 443; register or adjust it in 1Panel first')
        for p in (Path(c['website_root']) / 'stream.d').glob('*.conf'):
            if p.name != 'v2ray-agent-443.conf' and re.search(r'listen\s+[^;]*\b443\b', p.read_text()):
                raise ValueError('Another TCP stream config owns port 443')
        # The app's placeholder HTTPS listener is enabled by default in fresh 1Panel.
        # Disable it through the native control, keeping registered sites intact.
        if self.panel.call('openresty/https').get('https'):
            self.panel.call('openresty/https', {'operate': 'disable', 'sslRejectHandshake': True})
        # A stopped legacy agent nginx is not needed by this backend.
        if subprocess.run(['systemctl', 'is-active', '--quiet', 'nginx']).returncode == 0:
            if not Path('/etc/nginx/conf.d/alone.conf').exists():
                raise ValueError('An unrelated standalone nginx is running; resolve its listeners first')
            run('systemctl', 'stop', 'nginx')
            run('systemctl', 'disable', 'nginx')
        run('systemctl', 'stop', 'xray') if Path('/etc/systemd/system/xray.service').exists() else None
        # Replacing existing 443 web listeners before rebuilding keeps the new TCP front free.
        for w, p in cohort:
            text = p.read_text()
            text = re.sub(r'(?m)^(\s*listen\s+)(?:\[::\]:)?443(\s+ssl\b)', rf'\g<1>{c["web_port"]}\g<2>', text)
            # De-duplicate the IPv4/IPv6-to-IPv4 conversion above.
            lines, seen = [], set()
            for line in text.splitlines(True):
                if 'listen ' in line:
                    if line.strip() in seen:
                        continue
                    seen.add(line.strip())
                lines.append(line)
            text = ''.join(lines)
            write(p, text, 0o644)
        # Keep the whole parent mount: subscription generation atomically renames the child directory.
        compose = Path(c['compose_path']).read_text()
        mount = '/etc/v2ray-agent:/srv/v2ray-agent:ro'
        if mount not in compose:
            pattern = re.compile(r'(?m)^(\s*)volumes:\s*$')
            match = pattern.search(compose)
            if not match:
                raise ValueError('OpenResty compose has no volumes section')
            compose = compose[:match.end()] + '\n' + match[1] + '    - ' + mount + compose[match.end():]
        # The panel validates new ports before applying them. Move a pre-existing website
        # listener aside just long enough to register its new app default.
        current_app = next(x for x in items(self.panel.call('apps/installed/search',
                           {'page': 1, 'pageSize': 1000, 'all': True})) if x['id'] == c['app_id'])
        current_port = current_app.get('httpsPort', 443)
        temp_port = None
        if current_port != c['web_port']:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                temp_port = sock.getsockname()[1]
            for _, p in cohort:
                write(p, re.sub(r'(listen\s+)' + str(c['web_port']) + r'(\s+ssl)', rf'\g<1>{temp_port}\g<2>', p.read_text()), 0o644)
            if c.get('running'):
                self.reload(c)
                time.sleep(1)
        self.panel.call('apps/installed/params/update', {
            'installId': c['app_id'], 'params': {'PANEL_APP_PORT_HTTPS': c['web_port']},
            'advanced': True, 'editCompose': True, 'dockerCompose': compose,
            'hostMode': True, 'containerName': c['container'], 'cpuQuota': c.get('cpu_quota', 0), 'memoryLimit': c.get('memory_mb', 0),
            'memoryUnit': 'MB', 'pullImage': False, 'restartPolicy': c.get('restart_policy', 'unless-stopped')})
        # Native app rebuild is asynchronous. Wait for its replacement container,
        # mounted subscription directory and nginx config before touching sites.
        time.sleep(2)
        deadline = time.monotonic() + 90
        while True:
            try:
                app = next(x for x in items(self.panel.call('apps/installed/search',
                           {'page': 1, 'pageSize': 1000, 'all': True})) if x['id'] == c['app_id'])
                d = json.loads(run('docker', 'inspect', c['container']))[0]
                ready = app['status'] == 'Running' and d['State']['Running'] and any(
                    m['Destination'] == '/srv/v2ray-agent' and not m['RW'] for m in d['Mounts'])
                if ready:
                    run('docker', 'exec', c['container'], 'openresty', '-t')
                    break
            except (RuntimeError, KeyError, subprocess.SubprocessError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError('1Panel OpenResty rebuild did not become ready; check its app task log')
            time.sleep(1)
        for w, p in cohort:
            if temp_port:
                write(p, p.read_text().replace(f'listen {temp_port} ssl', f'listen {c["web_port"]} ssl'), 0o644)
            self.https(w['id'], w['certificate_id'], c['web_port'])
            redirect_dir = Path(c['website_root']) / 'sites' / w['alias'] / 'redirect'
            redirect_dir.mkdir(parents=True, exist_ok=True)
            redirect = redirect_dir / 'v2ray-agent-public-https.conf'
            write(redirect, 'if ($scheme = http) { return 301 https://$host$request_uri; }\nerror_page 497 https://$host$request_uri;\n', 0o644)
            include = f'include /www/sites/{w["alias"]}/redirect/v2ray-agent-public-https.conf;'
            text = p.read_text()
            text = re.sub(r'(?m)^\s*' + re.escape(include) + r'\s*$', '', text)
            text = text.replace('server {', 'server {\n    ' + include, 1)
            self.panel.call('websites/nginx/update', {'id': w['id'], 'content': text})
        self.configure_site(c)
        self.configure_stream(c, front=False)
        self.reload(c)

    def configure_site(self, c):
        include = site_path(c) / 'v2ray-agent-subscription.conf'
        write(include, subscription_location(), 0o644)
        p = site_config(c)
        directive = 'include ' + container_path(c, include) + ';'
        text = p.read_text()
        if directive not in text:
            text = text.replace('server {', 'server {\n    ' + directive, 1)
            self.panel.call('websites/nginx/update', {'id': c['website_id'], 'content': text})
        write(Path(c['website_root']) / 'conf.d' / 'v2ray-agent-fallback.conf', fallback_config(c), 0o644)

    def configure_stream(self, c, front):
        write(Path(c['website_root']) / 'stream.d' / 'v2ray-agent-443.conf', stream_config(c, front), 0o644)

    def normalize_xray(self, c):
        front = False
        for path in (AGENT / 'xray/conf').glob('*.json'):
            data = json.loads(path.read_text())
            changed = False
            for i in data.get('inbounds', []):
                if i.get('port') == 443:
                    i['listen'] = '127.0.0.1'
                    front = True
                    changed = True
                st = i.get('streamSettings', {})
                if st.get('security') == 'reality':
                    st['realitySettings']['target'] = f'127.0.0.1:{c["web_port"]}'
                    changed = True
            # The upstream Reality gate used a tag that did not match its inbound.
            if path.name == '07_VLESS_vision_reality_inbounds.json':
                for rule in data.get('routing', {}).get('rules', []):
                    if rule.get('inboundTag') == ['dokodemo-in']:
                        rule['inboundTag'] = ['dokodemo-in-VLESSReality']
                        changed = True
            if changed:
                write(path, json.dumps(data, indent=2) + '\n')
        run(AGENT / 'xray/xray', 'run', '-test', '-confdir', AGENT / 'xray/conf')
        self.configure_stream(c, front)
        self.reload(c)

    def deploy_certificate(self, c, restart=True):
        """Verify the pair, publish it, and verify the actual OpenResty TLS certificate."""
        (AGENT / 'tls').mkdir(parents=True, exist_ok=True)
        with open(AGENT / 'onepanel-certificate.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            source = site_path(c) / 'ssl'
            cert = (source / 'fullchain.pem').read_bytes()
            key = (source / 'privkey.pem').read_bytes()
            check_certificate(cert, key, c['domain'])
            leaf = ssl.PEM_cert_to_DER_cert(cert.decode().split('-----END CERTIFICATE-----')[0] + '-----END CERTIFICATE-----\n')
            fingerprint = hashlib.sha256(cert + key).hexdigest()
            a, b = (AGENT / 'tls' / (c['domain'] + ext) for ext in ('.crt', '.key'))
            stamp = AGENT / 'onepanel-certificate.sha256'

            def loaded():
                try:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    # Trust the exact authenticated-panel leaf, not an arbitrary self-signed peer.
                    with socket.create_connection(('127.0.0.1', c['web_port']), timeout=3) as conn:
                        with context.wrap_socket(conn, server_hostname=c['domain']) as tls:
                            return tls.getpeercert(binary_form=True) == leaf
                except (OSError, ssl.SSLError):
                    return False

            same = (a.exists() and b.exists() and a.read_bytes() == cert and b.read_bytes() == key)
            if same and stamp.exists() and stamp.read_text().strip() == fingerprint and loaded():
                print('1Panel certificate unchanged')
                return
            old = (a.read_bytes() if a.exists() else None, b.read_bytes() if b.exists() else None)
            restart_attempted = False
            try:
                write(a, cert)
                write(b, key)
                if restart:
                    run(AGENT / 'xray/xray', 'run', '-test', '-confdir', AGENT / 'xray/conf')
                self.reload(c)
                for _ in range(10):
                    if loaded():
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('OpenResty did not load the expected 1Panel certificate')
                if restart:
                    restart_attempted = True
                    run('systemctl', 'restart', 'xray')
                    run('systemctl', 'is-active', '--quiet', 'xray')
                write(stamp, fingerprint + '\n')
            except Exception:
                for path, original in zip((a, b), old):
                    write(path, original) if original is not None else path.unlink(missing_ok=True)
                if restart_attempted and all(x is not None for x in old):
                    # A failed restart may have stopped the old process. Try its known pair.
                    subprocess.run(['systemctl', 'restart', 'xray'], capture_output=True, timeout=60)
                raise
            print('1Panel certificate deployed')

    def install_watcher(self, c):
        directory = Path('/etc/systemd/system')
        helper = AGENT / 'onepanel/controller.py'
        write(directory / 'v2ray-agent-onepanel-cert.service', f'''[Unit]
Description=Deploy 1Panel certificate to Xray
After=docker.service xray.service
[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/python3 {helper} deploy
''', 0o644)
        write(directory / 'v2ray-agent-onepanel-cert.path', f'''[Unit]
Description=Watch 1Panel certificate deployment
[Path]
PathChanged={site_path(c) / 'ssl'}
Unit=v2ray-agent-onepanel-cert.service
[Install]
WantedBy=multi-user.target
''', 0o644)
        write(directory / 'v2ray-agent-onepanel-cert.timer', '''[Unit]
Description=Retry 1Panel certificate deployment
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=v2ray-agent-onepanel-cert.service
[Install]
WantedBy=timers.target
''', 0o644)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', 'v2ray-agent-onepanel-cert.path', 'v2ray-agent-onepanel-cert.timer')
        old = subprocess.run(['crontab', '-l'], capture_output=True, text=True).stdout
        cron = '\n'.join(line for line in old.splitlines() if '/etc/v2ray-agent/install.sh RenewTLS' not in line) + '\n'
        run('crontab', '-', data=cron.encode())

    def detach(self, c):
        subprocess.run(['systemctl', 'disable', '--now', 'v2ray-agent-onepanel-cert.path', 'v2ray-agent-onepanel-cert.timer'], check=True)
        subprocess.run(['systemctl', 'stop', 'v2ray-agent-onepanel-cert.service'], check=True)
        for suffix in ('path', 'timer', 'service'):
            Path('/etc/systemd/system/v2ray-agent-onepanel-cert.' + suffix).unlink(missing_ok=True)
        run('systemctl', 'daemon-reload')
        p = site_config(c)
        directive = 'include ' + container_path(c, site_path(c) / 'v2ray-agent-subscription.conf') + ';'
        text = '\n'.join(line for line in p.read_text().splitlines() if line.strip() != directive) + '\n'
        self.panel.call('websites/nginx/update', {'id': c['website_id'], 'content': text})
        (site_path(c) / 'v2ray-agent-subscription.conf').unlink(missing_ok=True)
        (Path(c['website_root']) / 'conf.d/v2ray-agent-fallback.conf').unlink(missing_ok=True)
        self.configure_stream(c, front=False)
        self.reload(c)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inventory', 'select', 'set-ports', 'prepare', 'normalize', 'seed-certificate', 'deploy', 'watch', 'reload', 'detach'])
    parser.add_argument('--website-id', type=int)
    parser.add_argument('--ssl-id', type=int)
    parser.add_argument('--web-port', type=int, default=888)
    parser.add_argument('--ports')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run as root on the 1Panel server')
    os.umask(0o077)
    backend = Backend()
    if args.command == 'inventory':
        info = backend.inventory()
        info['websites'] = [{'id': w['id'], 'domain': w['primaryDomain'],
                             'ssl_id': backend.panel.call(f'websites/{w["id"]}').get('webSiteSSLId', 0)}
                            for w in backend.websites()]
        info['certificates'] = [{k: x.get(k) for k in ('id', 'primaryDomain', 'status', 'autoRenew', 'expireDate')}
                                for x in items(backend.panel.call('websites/ssl/list', {}))]
        print(json.dumps(info, ensure_ascii=False))
        return
    if args.command == 'select':
        if not args.website_id or not args.ssl_id:
            parser.error('--website-id and --ssl-id are required')
        print(json.dumps(backend.select(args.website_id, args.ssl_id, args.web_port)))
        return
    c = load()
    if args.command == 'set-ports':
        backend.set_ports(c, json.loads(args.ports))
    elif args.command == 'prepare':
        backend.prepare(c)
    elif args.command == 'normalize':
        backend.normalize_xray(c)
    elif args.command == 'seed-certificate':
        backend.deploy_certificate(c, restart=False)
    elif args.command == 'deploy':
        backend.deploy_certificate(c)
    elif args.command == 'watch':
        backend.install_watcher(c)
    elif args.command == 'reload':
        backend.reload(c)
    elif args.command == 'detach':
        backend.detach(c)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print(f'1Panel: {exc}', file=sys.stderr)
        raise SystemExit(1)

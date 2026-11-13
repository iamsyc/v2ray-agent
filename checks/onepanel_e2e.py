#!/usr/bin/env python3
"""Destructive integration fixture for a disposable Linux test machine ONLY.

Creates a real 1Panel OpenResty app, website and synthetic certificate. The
synthetic ACME record tests deployment events, not a public CA/DNS provider.
Never point this at an existing server. Hostname and sandbox marker are required.
"""
import argparse
import importlib.util
import base64
import hashlib
from urllib.parse import urlsplit, parse_qs, unquote, urlencode
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('panel', ROOT / 'onepanel/controller.py')
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)
DOMAIN = 'proxy.example.test'
FIXTURE = Path('/root/onepanel-e2e')


def guard():
    if not socket.gethostname().startswith('v2ray-agent-1panel-test') or not Path('/run/v2ray-agent-test-sandbox').is_file():
        raise SystemExit('Only run inside the explicitly marked disposable test machine')
    if os.geteuid() != 0:
        raise SystemExit('Run as root inside the disposable machine')
    os.umask(0o077)
    FIXTURE.mkdir(exist_ok=True)


def certificate(name):
    key, cert = FIXTURE / (name + '.key'), FIXTURE / (name + '.pem')
    ctl.run('openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
            '-nodes', '-keyout', key, '-out', cert, '-days', '90', '-subj', '/CN=' + DOMAIN,
            '-addext', 'subjectAltName=DNS:' + DOMAIN + ',DNS:files.example.test')
    return cert.read_text(), key.read_text()


def setup():
    panel = ctl.Panel()
    app = panel.call('apps/openresty')
    detail = panel.call(f'apps/detail/{app["id"]}/1.27.1.2-5-1-focal/app')
    apps = ctl.items(panel.call('apps/installed/search', {'page': 1, 'pageSize': 100, 'name': '', 'all': True}))
    if not apps:
        panel.call('apps/install', {
            'appDetailId': detail['id'], 'name': 'openresty',
            'params': {'PANEL_APP_PORT_HTTP': 80, 'PANEL_APP_PORT_HTTPS': 443, 'WEBSITE_DIR': '/opt/1panel/www'},
            'advanced': True, 'containerName': 'openresty_e2e', 'hostMode': True,
            'cpuQuota': 0, 'memoryLimit': 0, 'memoryUnit': 'MB', 'restartPolicy': 'unless-stopped'})
    if apps and apps[0].get('status') == 'InstallErr':
        panel.call('apps/installed/op', {'installId': apps[0]['id'], 'operate': 'rebuild', 'pullImage': False})
    deadline = time.monotonic() + 360
    while True:
        apps = ctl.items(panel.call('apps/installed/search', {'page': 1, 'pageSize': 100, 'name': '', 'all': True}))
        if apps and apps[0].get('status') in ('Running', 'running'):
            break
        if apps and apps[0].get('status') in ('Error', 'error'):
            raise RuntimeError('OpenResty app installation failed: ' + str(apps[0].get('message')))
        if time.monotonic() > deadline:
            raise RuntimeError('OpenResty app installation timed out')
        time.sleep(2)
    print('Real 1Panel OpenResty app running', flush=True)
    cert, key = certificate('initial')
    panel.call('websites/ssl/upload', {'type': 'paste', 'certificate': cert, 'privateKey': key, 'description': 'Disposable integration fixture'})
    certificates = ctl.items(panel.call('websites/ssl/list', {}))
    selected = next(x for x in certificates if x['primaryDomain'] == DOMAIN)
    cert_id = selected['id']
    # No real provider credentials are used: this explicitly synthetic record
    # exercises the API contract and successful certificate-deployment events.
    panel.call('websites/ssl/update', {'id': cert_id, 'autoRenew': True, 'description': 'Synthetic test certificate, no public CA',
               'primaryDomain': DOMAIN, 'otherDomains': '', 'provider': 'http', 'acmeAccountId': 0,
               'dnsAccountId': 0, 'keyType': 'EC256', 'apply': False, 'pushDir': False, 'dir': '',
               'disableCNAME': False, 'skipDNS': False, 'nameserver1': '', 'nameserver2': '', 'execShell': False, 'shell': ''})
    websites = ctl.Backend(panel).websites()
    if not websites:
        panel.call('websites', {'appType': 'installed', 'type': 'static', 'alias': DOMAIN, 'domains': [{'domain': DOMAIN, 'port': 80, 'ssl': False}],
                   'webSiteGroupID': 1, 'enableSSL': False, 'websiteSSLID': cert_id})
    website = next(w for w in ctl.Backend(panel).websites() if w['primaryDomain'] == DOMAIN)
    ctl.Backend(panel).https(website['id'], cert_id, 443)
    (Path('/opt/1panel/www/sites') / DOMAIN / 'index/index.html').write_text('<!doctype html><title>1Panel fixture</title>onepanel-e2e-website\n')
    address = ctl.assigned_ipv4()
    hosts = Path('/etc/hosts')
    hosts.write_text(hosts.read_text() + f'\n{address} {DOMAIN}\n')
    write = {'website_id': website['id'], 'ssl_id': cert_id, 'address': address}
    (FIXTURE / 'fixture.json').write_text(json.dumps(write))
    print('Native website/certificate fixture ready', json.dumps(write), flush=True)


def install():
    f = json.loads((FIXTURE / 'fixture.json').read_text())
    protocols = os.environ.get('ONEPANEL_E2E_PROTOCOLS', '0,6,7,12,14')
    xhttp_port = os.environ.get('ONEPANEL_E2E_XHTTP_PORT', '999')
    answers = ['2', '1', '2', str(f['website_id']), str(f['ssl_id']),
               os.environ.get('ONEPANEL_E2E_SUBSCRIPTION_WEBSITE', ''), os.environ.get('ONEPANEL_E2E_SUBSCRIPTION_PORT', '888'), 'y', protocols, '777']
    if '6' in protocols.split(','):
        answers.append('8443')
    answers += ['443', '555', xhttp_port, os.environ.get('ONEPANEL_E2E_XRAY', 'v26.11.9'), '', '', 'fixture', '']
    if '6' in protocols.split(','):
        answers += ['100', '100']
    answers += ['']
    with open(FIXTURE / 'install.log', 'w') as log:
        p = subprocess.run(['bash', os.environ.get('ONEPANEL_E2E_INSTALLER', str(ROOT / 'install.sh'))], input='\n'.join(answers) + '\n', text=True,
                           stdout=log, stderr=subprocess.STDOUT, timeout=600, preexec_fn=lambda: os.umask(0o022))
    if p.returncode:
        raise RuntimeError(f'Installer exited {p.returncode}; inspect sandbox-only install.log')
    if not Path('/etc/v2ray-agent/onepanel.json').exists():
        raise RuntimeError('Installer did not create a 1Panel installation state')
    ctl.run('systemctl', 'is-active', '--quiet', 'xray')
    state = ctl.load()
    if state['schema'] == 2:
        address = f'https://{state["subscription_domain"]}:{state["subscription_port"]}/s/default/'
        assert address in (FIXTURE / 'install.log').read_text(), 'Installer published a different subscription address'
    print('Fresh installer completed', flush=True)


def verify():
    c = ctl.load()
    panel = ctl.Panel()
    ctl.run('docker', 'exec', c['container'], 'openresty', '-t')
    ctl.run('/etc/v2ray-agent/xray/xray', 'run', '-test', '-confdir', '/etc/v2ray-agent/xray/conf')
    data = json.loads(Path('/etc/v2ray-agent/xray/conf/07_VLESS_vision_reality_inbounds.json').read_text())
    assert data['inbounds'][0]['listen'] == '127.0.0.1'
    assert data['inbounds'][0]['port'] == (ctl.REALITY_GATE_PORT if c['schema'] == 2 else 443)
    assert data['inbounds'][1]['streamSettings']['realitySettings']['target'] == f'127.0.0.1:{c["web_port"]}'
    trust = ctl.site_path(c) / 'ssl/fullchain.pem'
    public = ctl.run('curl', '--cacert', trust, '-sS', '-w', '\n%{http_code}', 'https://' + DOMAIN + '/')
    assert 'onepanel-e2e-website' in public and public.endswith('200'), public[-100:]
    redirect = ctl.run('curl', '-sS', '-o', '/dev/null', '-w', '%{redirect_url}', 'http://' + DOMAIN + '/')
    assert redirect == 'https://' + DOMAIN + '/', redirect
    for fmt in ('default', 'clashMeta', 'clashMetaProfiles', 'sing-box', 'sing-box_profiles'):
        files = list((Path('/etc/v2ray-agent/subscribe') / fmt).iterdir())
        assert any(p.is_file() for p in files), fmt + ' has no published files'
        for path in files:
            if not path.is_file():
                continue
            body = subscription_get(c, fmt, path.name).stdout
            assert body == path.read_bytes(), fmt
    install_info = next(x for x in ctl.items(panel.call('apps/installed/search', {'page': 1, 'pageSize': 1000, 'all': True})) if x['id'] == c['app_id'])
    assert install_info['httpsPort'] == c['web_port'], install_info['httpsPort']
    print('Actual website, canonical redirect, five subscription formats and panel port metadata verified', flush=True)


def subscription_get(c, fmt, digest, status_only=False):
    domain = c.get('subscription_domain', DOMAIN)
    port = c.get('subscription_port', c['web_port'])
    source = ctl.subscription_site_path(c) if c['schema'] == 2 else ctl.site_path(c)
    args = ['curl', '--cacert', str(source / 'ssl/fullchain.pem'), '--resolve', f'{domain}:{port}:{c["listen_ipv4"]}', '-sS']
    if status_only:
        args += ['-o', '/dev/null', '-w', '%{http_code}']
    return subprocess.run(args + [f'https://{domain}:{port}/s/{fmt}/{digest}'], capture_output=True, check=True)


def users():
    c = ctl.load()
    conf = Path('/etc/v2ray-agent/xray/conf')
    def all_clients():
        result = []
        for path in sorted(conf.glob('*inbounds.json')):
            for inbound in json.loads(path.read_text()).get('inbounds', []):
                settings = inbound.get('settings', {})
                clients = settings.get('clients', settings.get('users'))
                if clients is not None:
                    result.append(clients)
        assert len(result) >= (5 if '6' in c['ports'] else 4)
        return result
    before = all_clients()
    assert all(len(x) == 1 for x in before)
    added = str(uuid.uuid4())
    def invoke(name, answers):
        with open(FIXTURE / (name + '.log'), 'w') as log:
            r = subprocess.run(['bash', str(ROOT / 'install.sh')], input='\n'.join(answers) + '\n', text=True,
                               stdout=log, stderr=subprocess.STDOUT, timeout=120, preexec_fn=lambda: os.umask(0o022))
        assert r.returncode == 0, name + ' menu failed'
        ctl.run('systemctl', 'is-active', '--quiet', 'xray')
    original_files = {p.name for p in Path('/etc/v2ray-agent/subscribe/default').iterdir() if p.is_file()}
    invoke('user-add', ['7', '4', '1', added, 'fixturesecond', ''])
    def credential(u):
        return u.get('id', u.get('password', u.get('auth')))
    assert all(len(x) == 2 and any(credential(u) == added for u in x) for x in all_clients())
    new_files = {p.name for p in Path('/etc/v2ray-agent/subscribe/default').iterdir() if p.is_file()} - original_files
    assert len(new_files) == 1
    verify()
    invoke('user-remove', ['7', '5', '2', ''])
    identity = lambda groups: [[(credential(u), u.get('flow')) for u in group] for group in groups]
    assert identity(all_clients()) == identity(before)
    for name in new_files:
        assert not (Path('/etc/v2ray-agent/subscribe/default') / name).exists()
        code = subscription_get(c, 'default', name, status_only=True).stdout
        assert code == b'404', code
    verify()
    print('Actual menu add/remove updated all protocols and all published subscriptions', flush=True)


def proxy():
    """Dial every emitted VLESS node with a real Xray client, then transfer bytes."""
    c = ctl.load()
    trust = ctl.site_path(c) / 'ssl/fullchain.pem'
    certificate = trust.read_text().split('-----END CERTIFICATE-----', 1)[0] + '-----END CERTIFICATE-----\n'
    pinned = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest()
    folder = Path('/etc/v2ray-agent/subscribe/default')
    links = []
    for path in folder.iterdir():
        if path.is_file():
            links.extend(base64.b64decode(path.read_bytes()).decode().splitlines())
    links = list(dict.fromkeys(x for x in links if x.startswith(('vless://', 'vmess://', 'trojan://', 'hysteria2://'))))
    assert len(links) >= (5 if '6' in c['ports'] else 4), 'Missing subscribed nodes'
    # Current Xray freedom defaults block private destination addresses.
    for index, link in enumerate(links):
        vmess = {}
        if link.startswith('vmess://'):
            vmess = json.loads(base64.b64decode(link[8:]).decode())
            link = 'vmess://' + vmess['id'] + '@' + vmess['add'] + ':' + str(vmess['port']) + '?' + urlencode({
                'type': vmess.get('net', 'ws'), 'security': vmess.get('tls', 'tls'),
                'sni': vmess.get('sni') or vmess.get('host') or c['domain'], 'path': vmess.get('path', '/')})
        u = urlsplit(link)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        hysteria = u.scheme == 'hysteria2'
        network, security = ('hysteria', 'tls') if hysteria else (q.get('type', 'tcp'), q.get('security', 'tls'))
        stream = {'network': network, 'security': security}
        if security == 'reality':
            stream['realitySettings'] = {'serverName': q['sni'], 'fingerprint': q.get('fp', 'chrome'),
                                         'publicKey': q['pbk'], 'shortId': q.get('sid', ''), 'spiderX': q.get('spx', '/')}
            if q.get('pqv'):
                stream['realitySettings']['mldsa65Verify'] = q['pqv']
        else:
            stream['tlsSettings'] = {'serverName': q.get('sni', c['domain']), 'allowInsecure': False}
            if not os.environ.get('ONEPANEL_E2E_XRAY') or os.environ['ONEPANEL_E2E_XRAY'] != 'v26.3.27':
                stream['tlsSettings']['pinnedPeerCertSha256'] = pinned
            if q.get('alpn'):
                stream['tlsSettings']['alpn'] = q['alpn'].split(',')
            if q.get('fp'):
                stream['tlsSettings']['fingerprint'] = q['fp']
        if network == 'ws':
            stream['wsSettings'] = {'path': q.get('path', '/'), 'headers': {'Host': q.get('host', c['domain'])}}
        if network == 'xhttp':
            stream['xhttpSettings'] = {'path': q.get('path', '/'), 'host': q.get('host', c['domain']),
                                       'mode': q.get('mode', 'auto')}
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        user = {'id': unquote(u.username), 'encryption': 'none'}
        if q.get('flow'):
            user['flow'] = q['flow']
        if hysteria:
            assert (q.get('upmbps'), q.get('downmbps')) == ('100', '100')
            stream['hysteriaSettings'] = {'version': 2, 'auth': unquote(u.username)}
            settings = {'version': 2, 'address': u.hostname, 'port': u.port}
        elif u.scheme == 'trojan':
            settings = {'servers': [{'address': u.hostname, 'port': u.port, 'password': unquote(u.username)}]}
        else:
            if u.scheme == 'vmess':
                user = {'id': unquote(u.username), 'security': vmess.get('scy', 'auto'), 'alterId': int(vmess.get('aid', 0))}
            settings = {'vnext': [{'address': u.hostname, 'port': u.port, 'users': [user]}]}
        config = {'log': {'loglevel': 'warning'},
                  'inbounds': [{'listen': '127.0.0.1', 'port': port, 'protocol': 'socks', 'settings': {'udp': False}}],
                  'outbounds': [{'protocol': 'hysteria' if hysteria else u.scheme, 'settings': settings,
                                 'streamSettings': stream}]}
        config_path = FIXTURE / f'client-{index}.json'
        config_path.write_text(json.dumps(config))
        env = dict(os.environ, SSL_CERT_FILE=str(trust))
        with open(FIXTURE / f'client-{index}.log', 'w') as log:
            client = subprocess.Popen(['/etc/v2ray-agent/xray/xray', 'run', '-c', str(config_path)], stdout=log, stderr=log, env=env)
            try:
                time.sleep(1)
                status = ctl.run('curl', '-fsS', '--max-time', '20', '--noproxy', '', '--socks5-hostname',
                                 f'127.0.0.1:{port}', '-o', '/dev/null', '-w', '%{http_code}',
                                 'https://1.1.1.1/cdn-cgi/trace')
                assert status == '200', status
            finally:
                client.terminate()
                client.wait(timeout=10)
        transport = 'UDP' if hysteria else 'TCP'
        print(f'Actual subscribed node passed: {u.scheme}/{security}/{network} {transport} {u.port}', flush=True)


def companion():
    panel = ctl.Panel()
    c = ctl.load()
    other = os.environ.get('ONEPANEL_E2E_COMPANION', 'files.example.test')
    if not any(w['primaryDomain'] == other for w in ctl.Backend(panel).websites()):
        panel.call('websites', {'appType': 'installed', 'type': 'static', 'alias': other,
                   'domains': [{'domain': other, 'port': 80, 'ssl': False}], 'webSiteGroupID': 1, 'enableSSL': False})
    w = next(w for w in ctl.Backend(panel).websites() if w['primaryDomain'] == other)
    ssl_id = c['ssl_id']
    if other != 'files.example.test':
        key, cert = FIXTURE / 'companion.key', FIXTURE / 'companion.pem'
        ctl.run('openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                '-nodes', '-keyout', key, '-out', cert, '-days', '90', '-subj', '/CN=' + other,
                '-addext', 'subjectAltName=DNS:' + other)
        panel.call('websites/ssl/upload', {'type': 'paste', 'certificate': cert.read_text(), 'privateKey': key.read_text()})
        ssl_id = next(x['id'] for x in ctl.items(panel.call('websites/ssl/list', {})) if x['primaryDomain'] == other)
    ctl.Backend(panel).https(w['id'], ssl_id, c['web_port'])
    index = Path(c['website_root']) / 'sites' / other / 'index/index.html'
    index.write_text('onepanel-companion-website')
    index.chmod(0o644)
    for attempt in range(10):
        trust = Path(c['website_root']) / 'sites' / w['alias'] / 'ssl/fullchain.pem'
        try:
            if ctl.run('curl', '--cacert', trust, '--resolve', f'{other}:443:{c["listen_ipv4"]}',
                       '-fsS', 'https://' + other + '/') == 'onepanel-companion-website':
                break
        except RuntimeError:
            pass
        time.sleep(.5)
    else:
        raise AssertionError('Companion HTTPS site did not become available')
    print('Another native 1Panel HTTPS site coexists on public 443', flush=True)


def uninstall():
    c = ctl.load()
    with open(FIXTURE / 'uninstall.log', 'w') as log:
        result = subprocess.run(['bash', str(ROOT / 'install.sh')], input='20\ny\n', text=True,
                                stdout=log, stderr=subprocess.STDOUT, timeout=90)
    assert result.returncode == 0
    assert not Path('/etc/v2ray-agent').exists()
    assert subprocess.run(['systemctl', 'is-active', '--quiet', 'xray']).returncode != 0
    assert ctl.Panel().call(f'websites/{c["website_id"]}')['webSiteSSLId'] == c['ssl_id']
    trust = ctl.site_path(c) / 'ssl/fullchain.pem'
    for attempt in range(10):
        try:
            body = ctl.run('curl', '--cacert', trust, '-fsS', 'https://' + DOMAIN + '/')
        except RuntimeError:
            body = ''
        if 'onepanel-e2e-website' in body:
            break
        time.sleep(.5)
    else:
        raise AssertionError('Website did not survive proxy uninstall')
    print('Actual uninstall preserved panel websites, certificate and public HTTPS 443', flush=True)


def rotate():
    c = ctl.load()
    cert, key = certificate('rotated')
    panel = ctl.Panel()
    previous_pid = ctl.run('systemctl', 'show', 'xray', '-p', 'MainPID', '--value').strip()
    panel.call('websites/ssl/upload', {'type': 'paste', 'sslID': c['ssl_id'], 'certificate': cert, 'privateKey': key,
                                     'description': 'Synthetic rotated integration certificate'})
    ctl.Backend(panel).https(c['website_id'], c['ssl_id'], c['web_port'])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current_pid = ctl.run('systemctl', 'show', 'xray', '-p', 'MainPID', '--value').strip()
        if current_pid not in ('0', previous_pid) and (Path('/etc/v2ray-agent/tls') / (DOMAIN + '.crt')).read_text() == cert:
            break
        time.sleep(1)
    else:
        raise RuntimeError('Certificate path watcher did not deploy the replacement')
    print('Real panel certificate replacement triggered automatic Xray deployment', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('step', choices=['setup', 'install', 'verify', 'users', 'proxy', 'rotate', 'companion', 'uninstall'])
    args = parser.parse_args()
    guard()
    globals()[args.step]()

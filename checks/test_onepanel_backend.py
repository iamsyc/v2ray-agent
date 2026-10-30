#!/usr/bin/env python3
"""Rootless contract and failure-path tests; no installed services are modified."""
import copy
import importlib.util
import json
from pathlib import Path
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('onepanel_controller', ROOT / 'onepanel/controller.py')
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)


def context(root):
    return dict(schema=1, backend='onepanel', website_id=1, ssl_id=2, app_id=3,
                domain='proxy.example.test', alias='proxy.example.test', container='openresty_test',
                website_root=str(root / 'www'), compose_path=str(root / 'docker-compose.yml'),
                listen_ipv4='192.0.2.10', ipv6=True, web_port=888,
                ports={'0': 777, '7': 443, '12': 555, '14': 666})


def shell_function(name):
    s = (ROOT / 'install.sh').read_text()
    m = re.search(r'^' + re.escape(name) + r'\(\)\s*([({])', s, re.M)
    closing = '\n}' if m[1] == '{' else '\n)'
    return s[m.start():s.index(closing, m.end()) + 2] + '\n'


class StateTests(unittest.TestCase):
    def test_valid_and_rejected_state(self):
        with tempfile.TemporaryDirectory() as t:
            c = context(Path(t))
            self.assertEqual(ctl.validate_state(c), c)
            for key, bad in [('alias', '../private'), ('domain', 'host;id.test'),
                             ('website_id', 0), ('ssl_id', True), ('web_port', 443),
                             ('listen_ipv4', '127.0.0.1'), ('listen_ipv4', '0.0.0.0'), ('website_root', '/x/../etc')]:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    ctl.validate_state(dict(c, **{key: bad}))
            for ports in [{'0': 888}, {'0': 777, '7': 777}, {'0': 80}, {'0': '777'}, {'14': 45988}, {'0': 31300}]:
                with self.subTest(ports=ports), self.assertRaises(ValueError):
                    ctl.validate_state(dict(c, ports=ports))

    def test_templates_do_not_terminate_reality_tls_or_publish_private_files(self):
        c = context(Path('/tmp/example'))
        text = ctl.stream_config(c, True)
        self.assertIn('listen 192.0.2.10:443;', text)
        self.assertIn('listen [::]:443 ipv6only=on;', text)
        self.assertIn('proxy.example.test 127.0.0.1:443;', text)
        self.assertNotIn('443 ssl', text)
        self.assertNotIn('proxy_ssl on', text)
        self.assertIn('proxy_timeout 24h;', text)
        self.assertIn('proxy.example.test 127.0.0.1:888;', ctl.stream_config(c, False))
        sub = ctl.subscription_location()
        self.assertIn('([a-f0-9]{32})$', sub)
        self.assertNotIn('(.*)', sub)
        self.assertIn('/srv/v2ray-agent/subscribe/', sub)
        self.assertNotIn('/xray/', sub)
        self.assertIn('proxy_protocol', ctl.fallback_config(c))

    def test_every_asset_matches_the_bootstrap_digest(self):
        p = subprocess.run(['python3', str(ROOT / 'tools/update_onepanel_manifest.py'), '--check'], capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode())

    def test_standalone_hooks_do_not_require_a_panel(self):
        code = shell_function('unInstallSubscribe') + '\nnginxConfigPath="$TESTROOT/"; webBackend=standalone; unInstallSubscribe'
        with tempfile.TemporaryDirectory() as t:
            import os
            f = Path(t) / 'subscribe.conf'
            f.write_text('original standalone subscription')
            r = subprocess.run(['bash', '-c', code], env=dict(os.environ, TESTROOT=t), capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            self.assertFalse(f.exists())
            f.write_text('panel website must survive user/subscription operations')
            r = subprocess.run(['bash', '-c', code.replace('webBackend=standalone', 'webBackend=onepanel')],
                               env=dict(os.environ, TESTROOT=t), capture_output=True)
            self.assertEqual(r.returncode, 0)
            self.assertTrue(f.exists())

    def test_fork_updater_cannot_silently_restore_the_upstream_installer(self):
        text = shell_function('updateV2RayAgent')
        self.assertIn('raw.githubusercontent.com/iamsyc/v2ray-agent/master/install.sh', text)
        self.assertNotIn('raw.githubusercontent.com/mack-a/v2ray-agent/master/install.sh', text)

    def test_manual_xhttp_tls_port_is_not_limited_to_the_random_range(self):
        code = shell_function('isValidXHTTPTLSPort')
        for port, expected in [('999', 0), ('443', 0), ('65535', 0), ('0', 1), ('65536', 1), ('1;id', 1), ('999999999999999999999', 1)]:
            p = subprocess.run(['bash', '-c', code + 'isValidXHTTPTLSPort "$1"', '--', port], capture_output=True)
            self.assertEqual(p.returncode, expected, port)


class ConfigTests(unittest.TestCase):
    def test_normalize_preserves_users_ports_and_protocols(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            c = context(root)
            agent = root / 'agent'
            conf = agent / 'xray/conf'
            conf.mkdir(parents=True)
            configs = {
                '02_VLESS_TCP_inbounds.json': {'inbounds': [{'port': 777, 'settings': {'clients': [{'id': 'unchanged-user'}]}}]},
                '07_VLESS_vision_reality_inbounds.json': {'inbounds': [
                    {'port': 443, 'tag': 'dokodemo-in-VLESSReality'},
                    {'listen': '127.0.0.1', 'port': 45987, 'settings': {'clients': [{'id': 'unchanged-user'}]},
                     'streamSettings': {'security': 'reality', 'realitySettings': {'target': 'proxy.example.test:888', 'privateKey': 'unchanged-test-key'}}}],
                    'routing': {'rules': [{'inboundTag': ['dokodemo-in'], 'outboundTag': 'blackhole_out'}]}},
                '12_VLESS_XHTTP_inbounds.json': {'inbounds': [{'port': 555, 'streamSettings': {'security': 'reality', 'realitySettings': {'target': 'wrong-public-443:443'}}}]}}
            for name, data in configs.items():
                (conf / name).write_text(json.dumps(data))
            b = ctl.Backend()
            with patch.object(ctl, 'AGENT', agent), patch.object(ctl, 'run', return_value=''), patch.object(b, 'reload'):
                b.normalize_xray(c)
            reality = json.loads((conf / '07_VLESS_vision_reality_inbounds.json').read_text())
            self.assertEqual(reality['inbounds'][0]['listen'], '127.0.0.1')
            self.assertEqual(reality['inbounds'][0]['port'], 443)
            self.assertEqual(reality['inbounds'][1]['settings']['clients'][0]['id'], 'unchanged-user')
            self.assertEqual(reality['inbounds'][1]['streamSettings']['realitySettings']['privateKey'], 'unchanged-test-key')
            self.assertEqual(reality['inbounds'][1]['streamSettings']['realitySettings']['target'], '127.0.0.1:888')
            self.assertEqual(reality['routing']['rules'][0]['inboundTag'], ['dokodemo-in-VLESSReality'])
            self.assertEqual(json.loads((conf / '02_VLESS_TCP_inbounds.json').read_text())['inbounds'][0]['port'], 777)
            self.assertIn('proxy.example.test 127.0.0.1:443', (root / 'www/stream.d/v2ray-agent-443.conf').read_text())


class CertificateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        for name, domain in [('one', 'proxy.example.test'), ('two', 'proxy.example.test'), ('wrong', 'wrong.example.test')]:
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                            '-nodes', '-keyout', str(cls.root / (name + '.key')), '-out', str(cls.root / (name + '.pem')),
                            '-days', '3', '-subj', '/CN=' + domain, '-addext', 'subjectAltName=DNS:' + domain],
                           check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def material(self, name):
        return ((self.root / (name + '.pem')).read_bytes(), (self.root / (name + '.key')).read_bytes())

    def test_rejects_wrong_hostname_and_mismatched_key(self):
        one, two, wrong = (self.material(n) for n in ('one', 'two', 'wrong'))
        ctl.check_certificate(*one, 'proxy.example.test')
        with self.assertRaises(ValueError):
            ctl.check_certificate(one[0], two[1], 'proxy.example.test')
        with self.assertRaises((ValueError, RuntimeError)):
            ctl.check_certificate(*wrong, 'proxy.example.test')

    def test_rotation_is_published_once_and_invalid_pair_cannot_replace_it(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            c = context(root)
            agent = root / 'agent'
            source = ctl.site_path(c) / 'ssl'
            source.mkdir(parents=True)
            cert, key = self.material('one')
            (source / 'fullchain.pem').write_bytes(cert)
            (source / 'privkey.pem').write_bytes(key)
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(.1)
            c['web_port'] = listener.getsockname()[1]
            stop = threading.Event()
            served_directory = [source]

            def serve():
                while not stop.is_set():
                    try:
                        conn, _ = listener.accept()
                    except socket.timeout:
                        continue
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    folder = served_directory[0]
                    context.load_cert_chain(folder / 'fullchain.pem', folder / 'privkey.pem')
                    try:
                        with context.wrap_socket(conn, server_side=True):
                            pass
                    except ssl.SSLError:
                        conn.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            calls = []
            original = ctl.run

            def command(*a, **kw):
                if str(a[0]) == 'openssl':
                    return original(*a, **kw)
                calls.append(a)
                return ''

            b = ctl.Backend()
            try:
                with patch.object(ctl, 'AGENT', agent), patch.object(ctl, 'run', side_effect=command), patch.object(b, 'reload'):
                    b.deploy_certificate(c)
                    b.deploy_certificate(c)
                    self.assertEqual(sum(a[:3] == ('systemctl', 'restart', 'xray') for a in calls), 1)
                    next_cert, next_key = self.material('two')
                    (source / 'fullchain.pem').write_bytes(next_cert)
                    (source / 'privkey.pem').write_bytes(next_key)
                    b.deploy_certificate(c)
                    self.assertEqual(sum(a[:3] == ('systemctl', 'restart', 'xray') for a in calls), 2)
                    frozen = root / 'stale-server'
                    frozen.mkdir()
                    (frozen / 'fullchain.pem').write_bytes(next_cert)
                    (frozen / 'privkey.pem').write_bytes(next_key)
                    served_directory[0] = frozen
                    (source / 'fullchain.pem').write_bytes(cert)
                    (source / 'privkey.pem').write_bytes(key)
                    with patch.object(ctl.time, 'sleep'), self.assertRaises(RuntimeError):
                        b.deploy_certificate(c)
                    self.assertEqual((agent / 'tls/proxy.example.test.crt').read_bytes(), next_cert)
                    self.assertEqual(sum(a[:3] == ('systemctl', 'restart', 'xray') for a in calls), 2)
                    self.assertEqual((agent / 'tls/proxy.example.test.crt').read_bytes(), next_cert)
                    # Reject before publishing or probing the inconsistent source pair.
                    (source / 'fullchain.pem').write_bytes(next_cert)
                    (source / 'privkey.pem').write_bytes(key)
                    with self.assertRaises(ValueError):
                        b.deploy_certificate(c)
                    self.assertEqual((agent / 'tls/proxy.example.test.key').read_bytes(), next_key)
                    self.assertEqual(sum(a[:3] == ('systemctl', 'restart', 'xray') for a in calls), 2)
            finally:
                stop.set()
                thread.join(timeout=2)
                listener.close()


if __name__ == '__main__':
    unittest.main()

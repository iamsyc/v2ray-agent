"""Exercise native Hysteria2 accounts alongside existing VLESS accounts."""
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def function(source, name):
    start = re.search(r'^' + name + r'\(\)\s*([({])', source, re.M)
    end = '\n)' if start.group(1) == '(' else '\n}'
    return source[start.start():source.index(end, start.end()) + 2] + '\n'


class UpstreamAccounts(unittest.TestCase):
    def test_hysteria_accounts_are_listed_and_deletion_is_transactional(self):
        for script in ('install.sh', 'shell/install_en.sh'):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as tmp:
                source = (ROOT / script).read_text()
                directory = Path(tmp)
                hys = directory / '06_hysteria2_inbounds.json'
                vless = directory / '02_VLESS_TCP_inbounds.json'
                original = {'inbounds': [{'settings': {'users': [{'auth': 'keep', 'email': 'alice-xray_hysteria2'},
                                                                  {'auth': 'remove', 'email': 'bob-xray_hysteria2'}]}}]}
                hys.write_text(json.dumps(original))
                vless.write_text(json.dumps({'inbounds': [{'settings': {'clients': [{'id': 'remove', 'email': 'bob-VLESS'}]}}]}))
                code = ''.join(function(source, n) for n in ('accountUserKey', 'collectAccountUsers', 'restoreXrayUserConfigs', 'socks5OutboundPaths', 'socks5OutboundCheckUserRemoval', 'deleteXrayUserByUUID'))
                code += '\nconfigPath="$1/"; socks5OutboundRoot="$1"; singBoxConfigPath=; reloadCore() { return 0; }; echoContent() { :; };\n'
                p = subprocess.run(['bash', '-c', code + 'collectAccountUsers', '--', tmp], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual([u['key'] for u in json.loads(p.stdout)], ['alice', 'bob'])
                before = {p: p.read_bytes() for p in (hys, vless)}
                p = subprocess.run(['bash', '-c', code + 'ctlPath=/bin/false; ! deleteXrayUserByUUID remove', '--', tmp], capture_output=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual({p: p.read_bytes() for p in before}, before)
                p = subprocess.run(['bash', '-c', code + 'ctlPath=/bin/true; deleteXrayUserByUUID remove', '--', tmp], capture_output=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual([u['auth'] for u in json.loads(hys.read_text())['inbounds'][0]['settings']['users']], ['keep'])
                self.assertEqual(json.loads(vless.read_text())['inbounds'][0]['settings']['clients'], [])


    def test_core_cleanup_only_touches_managed_units(self):
        for script in ('install.sh', 'shell/install_en.sh'):
            for executable in ('/usr/bin/sing-box', '/usr/bin/env ; SAVED=path=/etc/v2ray-agent/sing-box/sing-box',
                               '/etc/v2ray-agent/sing-box/sing-box'):
                with self.subTest(script=script, executable=executable):
                    code = function((ROOT / script).read_text(), 'removeCoreService')
                    code += r"""
release=ubuntu
systemctl() {
    case "$1:$3" in
        show:LoadState) printf 'loaded\n' ;;
        show:ExecStart) printf '{ path=%s ; argv[]=test ; }\n' "$TEST_EXECUTABLE" ;;
        is-active:*) return 1 ;;
        *) printf '%s\n' "$1" ;;
    esac
}
pgrep() { return 1; }
rm() { printf 'remove\n'; }
removeCoreService sing-box
"""
                    result = subprocess.run(['bash', '-c', 'TEST_EXECUTABLE=$1\n' + code, '--', executable],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    expected = ['stop', 'disable', 'remove', 'daemon-reload'] if executable.startswith('/etc/v2ray-agent/') else []
                    self.assertEqual(result.stdout.splitlines(), expected)


if __name__ == '__main__':
    unittest.main()

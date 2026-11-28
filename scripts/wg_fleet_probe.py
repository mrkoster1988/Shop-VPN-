#!/usr/bin/env python3
"""External WireGuard probe; never creates interfaces or changes host routes.

Run on an independent Linux monitoring host with a dedicated, preconfigured
network namespace and one synthetic peer. See docs/WG-FLEET-PROBE.md. Secrets
are read from protected files and never written to the report or diagnostics.
"""
import argparse
import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


MAX_CONFIG_BYTES = 65536
SAFE_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
ENV = {'PATH': SAFE_PATH, 'LANG': 'C', 'LC_ALL': 'C'}


class ProbeFailure(Exception):
    pass


def _protected_read(path, *, maximum=MAX_CONFIG_BYTES):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as file:
            metadata = os.fstat(file.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                    or metadata.st_uid != os.geteuid()):
                raise ProbeFailure('UnsafeLocalFile')
            raw = file.read(maximum + 1)
            if len(raw) > maximum:
                raise ProbeFailure('LocalFileTooLarge')
            return raw
    except (OSError, ValueError):
        raise ProbeFailure('LocalFileUnavailable') from None


def _safe_url(value, *, report=False):
    if not isinstance(value, str) or len(value) > 2048:
        raise ProbeFailure('InvalidHttpsUrl')
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ProbeFailure('InvalidHttpsUrl') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.query or any(ch.isspace() for ch in value)
            or (port is not None and not 1 <= port <= 65535)):
        raise ProbeFailure('InvalidHttpsUrl')
    if report and parsed.path != '/api/admin/wg-fleet/probes/report':
        raise ProbeFailure('InvalidReportEndpoint')
    return parsed


def load_config(path):
    try:
        data = json.loads(_protected_read(path))
    except (ValueError, UnicodeError):
        raise ProbeFailure('InvalidConfig') from None
    required = {'schema_version', 'server_id', 'server_version', 'generation', 'probe_id',
                'namespace', 'interface', 'server_public_key', 'dns_server', 'https_url',
                'https_status', 'bandwidth_capacity_bps', 'metrics_ssh', 'report_url',
                'token_file', 'state_file'}
    if (not isinstance(data, dict) or set(data) != required
            or type(data['schema_version']) is not int or data['schema_version'] != 1):
        raise ProbeFailure('InvalidConfigFields')
    for field in ('server_id', 'probe_id'):
        if not isinstance(data[field], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', data[field]):
            raise ProbeFailure('InvalidIdentity')
    for field in ('server_version', 'generation', 'bandwidth_capacity_bps'):
        if type(data[field]) is not int or not 1 <= data[field] <= 2**63 - 1:
            raise ProbeFailure('InvalidConfigInteger')
    if (not isinstance(data['namespace'], str)
            or not re.fullmatch(r'pushok-probe-[a-zA-Z0-9_-]{1,40}', data['namespace'])):
        raise ProbeFailure('DedicatedNamespaceRequired')
    if (not isinstance(data['interface'], str) or data['interface'] == 'lo'
            or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.-]{0,14}', data['interface'])):
        raise ProbeFailure('InvalidProbeInterface')
    try:
        raw_key = base64.b64decode(data['server_public_key'], validate=True)
        if len(raw_key) != 32 or base64.b64encode(raw_key).decode() != data['server_public_key']:
            raise ValueError()
        ipaddress.IPv4Address(data['dns_server'])
    except (TypeError, ValueError):
        raise ProbeFailure('InvalidProbeNetwork') from None
    _safe_url(data['https_url'])
    _safe_url(data['report_url'], report=True)
    if type(data['https_status']) is not int or data['https_status'] not in (200, 204):
        raise ProbeFailure('InvalidProbeHttpStatus')
    ssh = data['metrics_ssh']
    if (not isinstance(ssh, dict)
            or set(ssh) != {'host', 'port', 'user', 'private_key_file', 'known_hosts_file', 'interface'}):
        raise ProbeFailure('InvalidMetricsConfig')
    if (not isinstance(ssh['host'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', ssh['host'])
            or not isinstance(ssh['user'], str)
            or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', ssh['user'])
            or type(ssh['port']) is not int or not 1 <= ssh['port'] <= 65535
            or not isinstance(ssh['interface'], str)
            or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.:-]{0,14}', ssh['interface'])):
        raise ProbeFailure('InvalidMetricsConfig')
    for value in (data['token_file'], data['state_file'], ssh['private_key_file'], ssh['known_hosts_file']):
        if not isinstance(value, str) or not Path(value).is_absolute() or '\x00' in value:
            raise ProbeFailure('AbsoluteLocalPathRequired')
    return data


def _binary(name):
    result = shutil.which(name, path=SAFE_PATH)
    if not result:
        raise ProbeFailure('ProbeDependencyMissing')
    return result


def _run(argv, *, timeout=5, stdin=None):
    try:
        result = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                                timeout=timeout, check=False, env=ENV)
    except (OSError, subprocess.SubprocessError):
        raise ProbeFailure('ProbeCommandFailed') from None
    if result.returncode or len(result.stdout) > MAX_CONFIG_BYTES:
        raise ProbeFailure('ProbeCommandFailed')
    return result.stdout.strip()


def _in_namespace(config, arguments, *, timeout=5):
    return _run([_binary('ip'), 'netns', 'exec', config['namespace'], *arguments], timeout=timeout)


def verify_probe_interface(config):
    try:
        links = json.loads(_run([_binary('ip'), '-n', config['namespace'], '-d', '-j',
                                'link', 'show', 'dev', config['interface']]))
    except (ValueError, ProbeFailure):
        raise ProbeFailure('namespace_unavailable') from None
    if (len(links) != 1 or links[0].get('linkinfo', {}).get('info_kind') != 'wireguard'
            or 'UP' not in links[0].get('flags', [])):
        raise ProbeFailure('interface_mismatch')
    try:
        peers = _in_namespace(config, [_binary('wg'), 'show', config['interface'], 'peers']).splitlines()
    except ProbeFailure:
        raise ProbeFailure('interface_mismatch') from None
    if peers != [config['server_public_key']]:
        raise ProbeFailure('peer_mismatch')


def verify_route(config, address):
    try:
        ipaddress.IPv4Address(address)
        rows = json.loads(_run([_binary('ip'), '-n', config['namespace'], '-j',
                               'route', 'get', address]))
    except (ValueError, ProbeFailure):
        raise ProbeFailure('route_mismatch') from None
    if len(rows) != 1 or rows[0].get('dev') != config['interface'] or rows[0].get('type') in ('local', 'blackhole', 'unreachable'):
        raise ProbeFailure('route_mismatch')


def wg_counters(config):
    def read(field):
        output = _in_namespace(config, [_binary('wg'), 'show', config['interface'], field])
        rows = [line.split() for line in output.splitlines()]
        match = [row[1:] for row in rows if row and row[0] == config['server_public_key']]
        if len(match) != 1:
            raise ProbeFailure('peer_mismatch')
        try:
            values = [int(value) for value in match[0]]
            if any(value < 0 for value in values):
                raise ValueError()
            return values
        except ValueError:
            raise ProbeFailure('probe_failed') from None
    handshake, transfer = read('latest-handshakes'), read('transfer')
    if len(handshake) != 1 or len(transfer) != 2:
        raise ProbeFailure('probe_failed')
    return {'handshake_at': handshake[0], 'rx': transfer[0], 'tx': transfer[1]}


def _skip_dns_name(message, offset):
    while True:
        if offset >= len(message):
            raise ProbeFailure('dns_failed')
        length = message[offset]
        if length & 0xc0 == 0xc0:
            if offset + 1 >= len(message):
                raise ProbeFailure('dns_failed')
            return offset + 2
        if length & 0xc0 or length > 63:
            raise ProbeFailure('dns_failed')
        offset += 1
        if length == 0:
            return offset
        offset += length


def dns_query(server, interface, hostname):
    """Runs INSIDE the dedicated namespace; one bounded IPv4 DNS query."""
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', hostname)
            or any(not part or len(part) > 63 for part in hostname.split('.'))):
        raise ProbeFailure('dns_failed')
    transaction = secrets.randbelow(65536)
    name = b''.join(bytes([len(part)]) + part.encode('ascii') for part in hostname.split('.')) + b'\0'
    question = name + struct.pack('!HH', 1, 1)
    packet = struct.pack('!6H', transaction, 0x0100, 1, 0, 0, 0) + question
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b'\0')
            client.settimeout(5)
            client.connect((server, 53))
            client.send(packet)
            response = client.recv(4096)
        if len(response) < 12:
            raise ValueError()
        ident, flags, questions, answers, _, _ = struct.unpack('!6H', response[:12])
        if ident != transaction or not flags & 0x8000 or flags & 0x020f or questions != 1 or not answers:
            raise ValueError()
        end_question = _skip_dns_name(response, 12) + 4
        if response[12:end_question] != question:
            raise ValueError()
        offset, addresses = end_question, []
        for _ in range(min(answers, 64)):
            offset = _skip_dns_name(response, offset)
            kind, klass, _, length = struct.unpack('!HHIH', response[offset:offset + 10])
            offset += 10
            value = response[offset:offset + length]
            if len(value) != length:
                raise ValueError()
            if kind == 1 and klass == 1 and length == 4:
                addresses.append(str(ipaddress.IPv4Address(value)))
            offset += length
        if not addresses:
            raise ValueError()
        return list(dict.fromkeys(addresses))
    except (OSError, ValueError, struct.error):
        raise ProbeFailure('dns_failed') from None


def probe_traffic(config):
    result = {'handshake_at': 0, 'rx_delta_bytes': 0, 'tx_delta_bytes': 0,
              'dns_ok': False, 'https_ok': False, 'failure_code': None}
    before = None
    try:
        verify_probe_interface(config)
        verify_route(config, config['dns_server'])
        before = wg_counters(config)
        parsed = _safe_url(config['https_url'])
        try:
            addresses = json.loads(_in_namespace(config, [sys.executable, str(Path(__file__).resolve()),
                '--internal-dns', config['dns_server'], config['interface'], parsed.hostname], timeout=7))
            if not isinstance(addresses, list) or not addresses:
                raise ValueError()
            ipaddress.IPv4Address(addresses[0])
        except (ProbeFailure, ValueError):
            raise ProbeFailure('dns_failed') from None
        result['dns_ok'] = True
        address = addresses[0]
        verify_route(config, address)
        url = config['https_url'] + '?pushok_probe_nonce=' + secrets.token_hex(12)
        port = parsed.port or 443
        with tempfile.TemporaryDirectory(prefix='pushok-probe-') as directory:
            try:
                status = _in_namespace(config, [_binary('curl'), '-q', '--silent', '--show-error',
                    '--noproxy', '*', '--proto', '=https', '--tlsv1.2',
                    '--interface', config['interface'], '--connect-timeout', '5', '--max-time', '12',
                    '--max-filesize', '65536', '--header', 'Cache-Control: no-cache',
                    '--resolve', f'{parsed.hostname}:{port}:{address}',
                    '--output', str(Path(directory) / 'body'), '--write-out', '%{http_code}', url], timeout=14)
            except ProbeFailure:
                raise ProbeFailure('https_failed') from None
            if status != str(config['https_status']):
                raise ProbeFailure('https_failed')
        result['https_ok'] = True
    except ProbeFailure as failure:
        result['failure_code'] = str(failure) if str(failure) in {
            'namespace_unavailable', 'interface_mismatch', 'peer_mismatch',
            'route_mismatch', 'dns_failed', 'https_failed'} else 'probe_failed'
    finally:
        if before is not None:
            try:
                after = wg_counters(config)
                result.update(handshake_at=after['handshake_at'],
                    rx_delta_bytes=max(0, after['rx'] - before['rx']),
                    tx_delta_bytes=max(0, after['tx'] - before['tx']))
                if after['rx'] < before['rx'] or after['tx'] < before['tx']:
                    result['failure_code'] = result['failure_code'] or 'traffic_missing'
            except ProbeFailure:
                result['failure_code'] = result['failure_code'] or 'probe_failed'
    if not result['failure_code']:
        if not result['handshake_at']:
            result['failure_code'] = 'handshake_missing'
        elif int(time.time()) - result['handshake_at'] > 180:
            result['failure_code'] = 'handshake_stale'
        elif not result['rx_delta_bytes'] or not result['tx_delta_bytes']:
            result['failure_code'] = 'traffic_missing'
    return result


METRICS_SCRIPT = r'''
import json, pathlib, time
def read():
    cpu = [int(value) for value in pathlib.Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    base = pathlib.Path('/sys/class/net') / interface / 'statistics'
    return cpu, int((base/'rx_bytes').read_text()), int((base/'tx_bytes').read_text()), time.monotonic()
before = read()
time.sleep(1)
after = read()
delta = [b-a for a,b in zip(before[0],after[0])]
total = sum(delta)
elapsed = after[3]-before[3]
if total <= 0 or elapsed <= 0 or any(v < 0 for v in delta) or after[1] < before[1] or after[2] < before[2]:
    raise SystemExit(2)
print(json.dumps({'cpu_percent': round(100*(total-delta[3]-delta[4])/total, 6),
    'rx_bps': int(8*(after[1]-before[1])/elapsed), 'tx_bps': int(8*(after[2]-before[2])/elapsed)}))
'''


def collect_metrics(config):
    """Read-only fixed SSH command, pinned host keys, no configurable shell code."""
    ssh = config['metrics_ssh']
    try:
        _protected_read(ssh['private_key_file'])  # Validate permissions; never log its bytes.
        if not Path(ssh['known_hosts_file']).is_file():
            raise ProbeFailure('metrics_failed')
        script = 'interface = ' + repr(ssh['interface']) + '\n' + METRICS_SCRIPT
        output = _run([_binary('ssh'), '-T', '-F', '/dev/null', '-oBatchMode=yes',
            '-oIdentitiesOnly=yes', '-oStrictHostKeyChecking=yes', '-oForwardAgent=no',
            '-oClearAllForwardings=yes', '-oConnectTimeout=5', '-oConnectionAttempts=1',
            '-oUserKnownHostsFile=' + ssh['known_hosts_file'], '-i', ssh['private_key_file'],
            '-p', str(ssh['port']), ssh['user'] + '@' + ssh['host'], '/usr/bin/python3', '-'],
            stdin=script, timeout=10)
        data = json.loads(output)
        if (not isinstance(data, dict) or set(data) != {'cpu_percent', 'rx_bps', 'tx_bps'}
                or type(data['cpu_percent']) not in (float, int) or not 0 <= data['cpu_percent'] <= 100
                or any(type(data[field]) is not int or not 0 <= data[field] < 2**63 for field in ('rx_bps', 'tx_bps'))):
            raise ValueError()
        return {**data, 'bandwidth_capacity_bps': config['bandwidth_capacity_bps']}
    except (OSError, ValueError, ProbeFailure):
        raise ProbeFailure('metrics_failed') from None


def _write_json(path, data):
    target = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.pushok-probe-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(data, file, separators=(',', ':'), allow_nan=False)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def next_sequence(config):
    """Caller holds the separate lock until submission has finished."""
    path = Path(config['state_file'])
    # Descriptive/state changes may rebase server_version without changing
    # this probe's identity. Its sequence must continue across such changes.
    binding = [config[field] for field in ('server_id', 'generation', 'probe_id')]
    previous = None
    if path.exists() or path.is_symlink():
        try:
            previous = json.loads(_protected_read(path))
            if (not isinstance(previous, dict) or set(previous) != {'binding', 'sequence'}
                    or type(previous['sequence']) is not int or not 1 <= previous['sequence'] < 2**63 - 1):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise ProbeFailure('InvalidSequenceState') from None
    sequence = previous['sequence'] + 1 if previous and previous['binding'] == binding else 1
    _write_json(path, {'binding': binding, 'sequence': sequence})
    return sequence


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _authorized_request(config, *, report=None):
    token = _protected_read(config['token_file'], maximum=4096).decode('ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,512}', token):
        raise ProbeFailure('InvalidProbeToken')
    payload = json.dumps(report, separators=(',', ':'), allow_nan=False).encode() if report is not None else None
    url = config['report_url'] if report is not None else config['report_url'].removesuffix('/report') + '/assignment'
    request = urllib.request.Request(url, data=payload, method='POST' if report is not None else 'GET',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=12) as response:
            if not 200 <= response.status < 300:
                raise ProbeFailure('ProbeReportRejected')
            body = response.read(MAX_CONFIG_BYTES + 1)
            if len(body) > MAX_CONFIG_BYTES:
                raise ProbeFailure('ProbeReportRejected')
            return body
    except (OSError, urllib.error.URLError):
        raise ProbeFailure('ProbeReportRejected') from None


def refresh_assignment(config):
    """Follow metadata versions, never silently enroll a different WG identity."""
    try:
        assignment = json.loads(_authorized_request(config))
    except (ValueError, UnicodeError):
        raise ProbeFailure('InvalidProbeAssignment') from None
    if (not isinstance(assignment, dict)
            or set(assignment) != {'server_id', 'server_version', 'probe_id', 'generation'}
            or any(assignment[field] != config[field] for field in ('server_id', 'probe_id', 'generation'))
            or type(assignment['server_version']) is not int
            or not config['server_version'] <= assignment['server_version'] <= 2**63 - 1):
        raise ProbeFailure('ProbeEnrollmentChanged')
    config['server_version'] = assignment['server_version']


def submit(config, report):
    _authorized_request(config, report=report)


def collect(config, sequence):
    started = int(time.time())
    traffic = probe_traffic(config)
    try:
        metrics = collect_metrics(config)
    except ProbeFailure:
        metrics = dict.fromkeys(('cpu_percent', 'rx_bps', 'tx_bps', 'bandwidth_capacity_bps'))
        traffic['failure_code'] = traffic['failure_code'] or 'metrics_failed'
    finished = int(time.time())
    if finished - started > 60 or finished < started:
        raise ProbeFailure('ProbeDurationExceeded')
    return {'schema_version': 1,
            **{key: config[key] for key in ('server_id', 'server_version', 'generation', 'probe_id')},
            'sequence': sequence, 'started_at': started, 'finished_at': finished, **traffic, **metrics}


def main():
    if len(sys.argv) == 5 and sys.argv[1] == '--internal-dns':
        try:
            print(json.dumps(dns_query(sys.argv[2], sys.argv[3], sys.argv[4])))
            return 0
        except Exception:
            return 2  # No resolver, peer, or credential details in subprocess errors.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--report-file', help='Optional protected local JSON; contains no secrets')
    parser.add_argument('--no-submit', action='store_true', help='Run checks and retain sequence without API submission')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if not args.no_submit:
            refresh_assignment(config)
        lock_path = config['state_file'] + '.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            metadata = os.fstat(lock.fileno())
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077 or not stat.S_ISREG(metadata.st_mode):
                raise ProbeFailure('UnsafeSequenceLock')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProbeFailure('ProbeAlreadyRunning') from None
            report = collect(config, next_sequence(config))
            if args.report_file:
                _write_json(args.report_file, report)
            if not args.no_submit:
                submit(config, report)
            print(json.dumps({'submitted': not args.no_submit, 'sequence': report['sequence'],
                              'healthy': report['failure_code'] is None,
                              'failure_code': report['failure_code']}))
            return 0 if report['failure_code'] is None else 1
    except ProbeFailure as failure:
        print(json.dumps({'error': str(failure)}), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({'error': 'ProbeFailed'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

"""IPv4 DNS resolution, ICMP probing and bounded LAN discovery."""
import ipaddress
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

PRIVATE = tuple(ipaddress.ip_network(x) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))

def resolve_ipv4(address):
    return socket.gethostbyname(address)

def parse_ping(output, windows):
    # Windows may return 0 for destination-unreachable messages; require TTL.
    for line in output.splitlines():
        if not re.search(r'\bttl\s*[= ]\s*\d+', line, re.I):
            continue
        match = re.search(r'([=<])\s*(\d+(?:[.,]\d+)?)\s*(?:ms|мс)\b', line, re.I)
        if match:
            return True, float(match[2].replace(',', '.')), match[1] == '<'
        return True, None, False
    return False, None, False

def probe(address, fast=False):
    try:
        ip = resolve_ipv4(address)
    except OSError:
        return None, False, None, False
    windows = sys.platform.startswith('win')
    cmd = ['ping','-4','-n','1','-w', '600' if fast else '1500',ip] if windows else ['ping','-4','-c','1','-W','1' if fast else '2',ip]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=3.5, check=False)
        # TTL and numeric RTT are ASCII; support Windows Cyrillic milliseconds.
        enc = 'oem' if windows else 'utf-8'
        output = r.stdout.decode(enc, errors='replace')
        ok, ms, less = parse_ping(output, windows)
        return ip, ok, ms, less
    except (OSError, subprocess.TimeoutExpired):
        return ip, False, None, False

def validate_network(raw):
    net = ipaddress.ip_network(raw, strict=False)
    if net.version != 4 or net.prefixlen < 24 or not any(net.subnet_of(p) for p in PRIVATE):
        raise ValueError('Дозволено приватні IPv4-підмережі /24–/32 (до 256 адрес).')
    return net

def discover(raw):
    net = validate_network(raw)
    def one(ip):
        address, ok, ms, less = probe(str(ip), fast=True)
        return {'address':address,'latency_ms':ms,'less_than':less} if ok else None
    with ThreadPoolExecutor(max_workers=24) as pool:
        return [row for row in pool.map(one, net.hosts()) if row is not None]

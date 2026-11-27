"""Read lightweight host metrics over the saved SSH connection without touching terminals."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
import subprocess
import threading
import time

from settings import ROOT
from ssh_login import PasswordTicket
from terminal import ssh_args


POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix='server-monitor')
CACHE_TTL = 12
_CACHE = {}
_CACHE_LOCK = threading.Lock()

# Keep the remote probe self-contained: Bash and /proc are available on the Linux
# hosts already supported by SSH Workbench. No package installation or sudo needed.
PROBE = r'''set -eu
cpu_snapshot() { awk '$1=="cpu" {total=0; for(i=2;i<=NF;i++) total+=$i; idle=$5+$6; printf "%.0f %.0f\n",total,idle; exit}' /proc/stat; }
net_snapshot() { awk 'NR>2 {sub(":","",$1); if($1!="lo") {rx+=$2; tx+=$10}} END {printf "%.0f %.0f\n",rx,tx}' /proc/net/dev; }
io_snapshot() {
  for file in /sys/block/*/stat; do
    [ -r "$file" ] || continue
    awk '{printf "%.0f %.0f\n",$3,$7}' "$file"
  done | awk '{read+=$1; write+=$2} END {printf "%.0f %.0f\n",read,write}'
}
read -r CPU0_TOTAL CPU0_IDLE <<< "$(cpu_snapshot)"
read -r NET0_RX NET0_TX <<< "$(net_snapshot)"
read -r IO0_READ IO0_WRITE <<< "$(io_snapshot)"
sleep 0.4
read -r CPU1_TOTAL CPU1_IDLE <<< "$(cpu_snapshot)"
read -r NET1_RX NET1_TX <<< "$(net_snapshot)"
read -r IO1_READ IO1_WRITE <<< "$(io_snapshot)"
printf 'cpu\t%s\t%s\t%s\t%s\n' "$CPU0_TOTAL" "$CPU0_IDLE" "$CPU1_TOTAL" "$CPU1_IDLE"
printf 'network\t%s\t%s\t%s\t%s\n' "$NET0_RX" "$NET0_TX" "$NET1_RX" "$NET1_TX"
printf 'diskio\t%s\t%s\t%s\t%s\n' "$IO0_READ" "$IO0_WRITE" "$IO1_READ" "$IO1_WRITE"
awk '
  /^MemTotal:/ {total=$2} /^MemAvailable:/ {available=$2}
  /^MemFree:/ {free=$2} /^Buffers:/ {buffers=$2} /^Cached:/ {cached=$2}
  /^SwapTotal:/ {swap_total=$2} /^SwapFree:/ {swap_free=$2}
  END {
    if (!available) available=free+buffers+cached
    printf "memory\t%.0f\t%.0f\n",(total-available)*1024,total*1024
    printf "swap\t%.0f\t%.0f\n",(swap_total-swap_free)*1024,swap_total*1024
  }
' /proc/meminfo
awk '{printf "load\t%s\t%s\t%s\n",$1,$2,$3}' /proc/loadavg
awk '{printf "uptime\t%.0f\n",$1}' /proc/uptime
printf 'cores\t%s\n' "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
printf 'kernel\t%s\n' "$(uname -r)"
OS=$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -1 | tr -d '"')
printf 'os\t%s\n' "${OS:-$(uname -s)}"
printf 'processes\t%s\n' "$(ps -e --no-headers 2>/dev/null | wc -l | tr -d ' ')"
if command -v ss >/dev/null 2>&1; then
  printf 'tcp\t%s\n' "$(ss -Htan 2>/dev/null | wc -l | tr -d ' ')"
else
  printf 'tcp\t%s\n' "$(awk 'NR>1' /proc/net/tcp /proc/net/tcp6 2>/dev/null | wc -l | tr -d ' ')"
fi
df -P -B1 / /var /home /opt /data 2>/dev/null | awk 'NR>1 && !seen[$6]++ {gsub(/%/,"",$5); printf "disk\t%s\t%s\t%s\t%s\n",$6,$3,$2,$5}'
'''


def _number(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_probe(output, sampled_at=None):
    """Parse the fixed tab-separated probe response into safe numeric metrics."""
    raw = {}
    disks = []
    for line in output.splitlines():
        fields = line.split('\t')
        if not fields:
            continue
        key = fields[0]
        if key == 'disk' and len(fields) == 5:
            disks.append({
                'mount': fields[1], 'used': int(_number(fields[2])), 'total': int(_number(fields[3])),
                'percent': max(0, min(100, int(_number(fields[4])))),
            })
        elif key == 'load' and len(fields) == 4:
            raw['load'] = fields[1:4]
        elif key in ('cpu', 'network', 'diskio', 'memory', 'swap'):
            raw[key] = fields[1:]
        elif len(fields) >= 2:
            raw[key] = fields[1]
    if not all(key in raw for key in ('cpu', 'network', 'diskio', 'memory', 'swap', 'load')):
        raise ValueError('监控数据不完整')

    cpu0, idle0, cpu1, idle1 = map(_number, raw['cpu'])
    total_delta, idle_delta = cpu1-cpu0, idle1-idle0
    cpu_percent = 0 if total_delta <= 0 else max(0, min(100, (total_delta-idle_delta)*100/total_delta))
    rx0, tx0, rx1, tx1 = map(_number, raw['network'])
    read0, write0, read1, write1 = map(_number, raw['diskio'])
    mem_used, mem_total = map(_number, raw['memory'])
    swap_used, swap_total = map(_number, raw['swap'])
    interval = 0.4

    return {
        'status': 'online',
        'sampledAt': sampled_at or datetime.now().astimezone().isoformat(timespec='seconds'),
        'cpuPercent': round(cpu_percent, 1), 'cpuCores': max(1, int(_number(raw.get('cores'), 1))),
        'memoryUsed': int(mem_used), 'memoryTotal': int(mem_total),
        'memoryPercent': round(mem_used*100/mem_total, 1) if mem_total else 0,
        'swapUsed': int(swap_used), 'swapTotal': int(swap_total),
        'swapPercent': round(swap_used*100/swap_total, 1) if swap_total else 0,
        'load': [round(_number(value), 2) for value in raw['load']],
        'disks': disks,
        'networkRxBytes': int(rx1), 'networkTxBytes': int(tx1),
        'networkRxRate': max(0, int((rx1-rx0)/interval)),
        'networkTxRate': max(0, int((tx1-tx0)/interval)),
        'diskReadRate': max(0, int((read1-read0)*512/interval)),
        'diskWriteRate': max(0, int((write1-write0)*512/interval)),
        'uptimeSeconds': int(_number(raw.get('uptime'))),
        'os': raw.get('os', 'Linux'), 'kernel': raw.get('kernel', ''),
        'processes': int(_number(raw.get('processes'))), 'tcpConnections': int(_number(raw.get('tcp'))),
    }


def _run_probe(connection, password):
    ticket = PasswordTicket(connection['user'], password) if password else None
    try:
        if connection.get('kind') == 'local':
            args = ['bash', '-s']
            env = os.environ.copy()
        else:
            args = ssh_args(connection, False)
            args[2:2] = ['-o', 'BatchMode=no' if ticket else 'BatchMode=yes',
                         '-o', 'PreferredAuthentications=publickey,password' if ticket else 'PreferredAuthentications=publickey',
                         '-o', 'PasswordAuthentication=yes' if ticket else 'PasswordAuthentication=no',
                         '-o', 'NumberOfPasswordPrompts=1']
            env = os.environ.copy()
            if ticket:
                env.update(DISPLAY=env.get('DISPLAY') or ':0', SSH_ASKPASS=str(ROOT/'ssh-askpass.py'),
                           SSH_ASKPASS_REQUIRE='force', WORKBENCH_SSH_USER=connection['user'],
                           WORKBENCH_SSH_TICKET=ticket.path)
        result = subprocess.run(args if connection.get('kind') == 'local' else [*args, 'bash -s'],
                                input=PROBE, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=9, check=True, env=env)
        return parse_probe(result.stdout)
    finally:
        if ticket:
            ticket.close()


def _collect_server(store, server, connection):
    try:
        private_connection, password = store.login_connection(connection['id'])
        metrics = _run_probe(private_connection, password)
        return {**server, 'connection': connection['id'], 'user': private_connection['user'], **metrics}
    except (OSError, ValueError, subprocess.SubprocessError, KeyError):
        return {**server, 'connection': connection['id'], 'user': connection.get('user', ''),
                'status': 'offline', 'sampledAt': datetime.now().astimezone().isoformat(timespec='seconds'),
                'error': 'SSH 暂时无法连接，或当前账号无法读取系统指标。'}


def snapshot(store):
    """Return cached metrics for each saved server, probing stale servers concurrently."""
    state = store.current()
    connections = {item['id']: item for item in state.get('connections', [])}
    now = time.monotonic()
    results, pending = {}, {}
    for server in state.get('servers', []):
        base = {'id': server['id'], 'name': server['name'], 'host': server['host'], 'port': server.get('port')}
        if server.get('kind') == 'nested':
            results[server['id']] = {**base, 'status': 'unsupported', 'error': '跳板机内层连接暂不支持独立监控。'}
            continue
        candidates = [connections[key] for key in server.get('connections', []) if key in connections
                      and connections[key].get('kind') != 'nested']
        if not candidates:
            results[server['id']] = {**base, 'status': 'unsupported', 'error': '没有可用于采集的 SSH 连接。'}
            continue
        candidates.sort(key=lambda item: not item.get('passwordSet', False))
        connection = candidates[0]
        with _CACHE_LOCK:
            cached = _CACHE.get(server['id'])
        if cached and now-cached[0] < CACHE_TTL:
            results[server['id']] = cached[1]
        else:
            pending[server['id']] = (base, connection, POOL.submit(_collect_server, store, base, connection))

    for identifier, (_, _, future) in pending.items():
        try:
            result = future.result()
        except Exception:
            result = {'id': identifier, 'status': 'offline', 'error': '监控采集失败。'}
        with _CACHE_LOCK:
            _CACHE[identifier] = (time.monotonic(), result)
        results[identifier] = result

    return {
        'servers': [results[server['id']] for server in state.get('servers', [])],
        'generatedAt': datetime.now().astimezone().isoformat(timespec='seconds'),
        'refreshAfter': CACHE_TTL,
    }

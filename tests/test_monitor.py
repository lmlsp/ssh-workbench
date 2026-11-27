import unittest
from unittest.mock import patch

import monitor
from monitor import parse_probe


class MonitorParserTests(unittest.TestCase):
    def test_parses_host_metrics_and_rates(self):
        output = '\n'.join([
            'cpu\t100\t70\t120\t80',
            'network\t1000\t2000\t1400\t2600',
            'diskio\t10\t20\t12\t23',
            'memory\t500\t1000',
            'swap\t0\t0',
            'load\t0.10\t0.20\t0.30',
            'uptime\t3600',
            'cores\t4',
            'kernel\t6.8.0-test',
            'os\tUbuntu 24.04 LTS',
            'processes\t123',
            'tcp\t42',
            'disk\t/\t50\t100\t50',
            'disk\t/data\t90\t100\t90',
        ])
        result = parse_probe(output, sampled_at='2026-11-26T16:00:00+08:00')
        self.assertEqual(result['status'], 'online')
        self.assertEqual(result['cpuPercent'], 50)
        self.assertEqual(result['cpuCores'], 4)
        self.assertEqual(result['memoryPercent'], 50)
        self.assertEqual(result['swapPercent'], 0)
        self.assertEqual(result['load'], [0.1, 0.2, 0.3])
        self.assertEqual(result['networkRxRate'], 1000)
        self.assertEqual(result['networkTxRate'], 1500)
        self.assertEqual(result['diskReadRate'], 2560)
        self.assertEqual(result['diskWriteRate'], 3840)
        self.assertEqual(result['disks'][1]['mount'], '/data')
        self.assertEqual(result['uptimeSeconds'], 3600)
        self.assertEqual(result['os'], 'Ubuntu 24.04 LTS')
        self.assertEqual(result['tcpConnections'], 42)

    def test_rejects_incomplete_probe_output(self):
        with self.assertRaisesRegex(ValueError, '监控数据不完整'):
            parse_probe('cpu\t1\t1\t2\t2')

    def test_handles_zero_memory_and_cpu_deltas(self):
        output = '\n'.join([
            'cpu\t10\t10\t10\t10', 'network\t10\t10\t10\t10', 'diskio\t10\t10\t10\t10',
            'memory\t0\t0', 'swap\t0\t0', 'load\t0\t0\t0', 'uptime\t0', 'cores\t1',
            'kernel\tk', 'os\tLinux', 'processes\t0', 'tcp\t0',
        ])
        result = parse_probe(output)
        self.assertEqual(result['cpuPercent'], 0)
        self.assertEqual(result['memoryPercent'], 0)
        self.assertEqual(result['swapPercent'], 0)


class MonitorSnapshotTests(unittest.TestCase):
    def test_nested_connections_are_reported_without_direct_ssh_probe(self):
        class Store:
            def current(self):
                return {
                    'servers': [
                        {'id': 'direct', 'name': 'App', 'host': 'app.example', 'port': 22, 'connections': ['app-login']},
                        {'id': 'nested', 'name': 'Jump target', 'host': 'db.example', 'port': 22,
                         'kind': 'nested', 'connections': ['nested-login']},
                    ],
                    'connections': [
                        {'id': 'app-login', 'server': 'direct', 'user': 'deploy', 'passwordSet': False},
                        {'id': 'nested-login', 'server': 'nested', 'user': 'db', 'kind': 'nested'},
                    ],
                }

            def login_connection(self, identifier):
                return ({'id': identifier, 'user': 'deploy', 'host': 'app.example', 'port': 22}, None)

        with patch.dict(monitor._CACHE, {}, clear=True), patch.object(
            monitor, '_collect_server', return_value={'id': 'direct', 'name': 'App', 'host': 'app.example',
                                                      'port': 22, 'status': 'online'}
        ) as collect:
            result = monitor.snapshot(Store())
        self.assertEqual([item['status'] for item in result['servers']], ['online', 'unsupported'])
        self.assertIn('跳板机', result['servers'][1]['error'])
        collect.assert_called_once()


if __name__ == '__main__':
    unittest.main()

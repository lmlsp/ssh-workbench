import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import auth
import catalog


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.addCleanup(patch.stopall)
        patch.object(catalog, 'STATE', root / 'state.json').start()
        patch.object(catalog, 'KEY', root / 'workbench.key').start()
        patch.object(auth, 'AUTH', root / 'admin.json').start()
        self.store = catalog.Store()

    def connection(self, name, host):
        return self.store.save('connections', {'name': name, 'host': host, 'port': 22, 'user': 'operator'})

    def command(self, connection, name, script='printf "%s" {{value}}', **values):
        return self.store.save('commands', dict(name=name, connection=connection['id'], user='appuser',
                                                directory='/srv/example', script=script, **values))

    def test_admin_password_is_hashed_and_checked(self):
        value = 'demo-login-value-for-test'
        auth.set_password(value)
        self.assertTrue(auth.verify('admin', value))
        self.assertFalse(auth.verify('admin', value + 'x'))
        self.assertNotIn(value, auth.AUTH.read_text())
        self.assertEqual(auth.AUTH.stat().st_mode & 0o777, 0o600)

    def test_switch_password_cleared_if_connection_target_changes(self):
        connection = self.connection('App node', 'app.example')
        value = 'fixture-only-password'
        command = self.command(connection, 'Inspect', password=value)
        self.assertTrue(command['passwordSet'])
        self.assertNotIn('secret', command)
        self.assertEqual(self.store.password(command['id']), value)
        self.assertNotIn(value, catalog.STATE.read_text())
        self.assertEqual(catalog.KEY.stat().st_mode & 0o777, 0o600)
        self.store.save('connections', dict(connection, host='replacement.example'))
        self.assertIsNone(self.store.password(command['id']))

    def test_workflow_reads_latest_command_and_keeps_run_snapshot(self):
        node = self.connection('App', 'app.example')
        first = self.command(node, 'Inspect')
        second = self.command(node, 'Check', script='pwd')
        flow = self.store.save('flows', {'name': 'Inspect and check', 'connection': node['id'], 'steps': [first['id'], second['id']]})
        old = self.store.plan('flow', flow['id'], {'value': 'first'})
        self.store.save('commands', dict(first, script='printf "edited: %s" {{value}}'))
        new = self.store.plan('flow', flow['id'], {'value': 'second'})
        self.assertEqual(old[2][0]['script'], 'printf "%s" first')
        self.assertEqual(new[2][0]['script'], 'printf "edited: %s" second')

    def test_workflow_cannot_mix_connections(self):
        first = self.connection('App 1', 'one.example')
        second = self.connection('App 2', 'two.example')
        a, b = self.command(first, 'Inspect'), self.command(second, 'Check')
        with self.assertRaises(ValueError):
            self.store.save('flows', {'name': 'Mixed', 'connection': first['id'], 'steps': [a['id'], b['id']]})

    def test_server_groups_accounts_by_host_and_ssh_port(self):
        first = self.connection('App', 'APP.EXAMPLE')
        second = self.store.save('connections', dict(name='Alternate account', host='app.example.', port=22, user='appuser'))
        other = self.store.save('connections', dict(name='Alternate port', host='app.example', port=2222, user='operator'))
        model = self.store.current()
        self.assertEqual([server['connections'] for server in model['servers']], [[first['id'], second['id']], [other['id']]])

    def test_command_deletion_checks_flows_and_flow_deletion_keeps_commands(self):
        node = self.connection('App', 'app.example')
        a = self.command(node, 'Inspect', script='pwd')
        b = self.command(node, 'Check', script='id')
        flow = self.store.save('flows', {'name': 'Inspect and check', 'connection': node['id'], 'steps': [a['id'], b['id']]})
        preview = self.store.deletion('commands', a['id'])
        self.assertEqual(preview['references'], [flow['name']])
        with self.assertRaisesRegex(ValueError, flow['name']):
            self.store.deletion('commands', a['id'], preview['fingerprint'])
        preview = self.store.deletion('flows', flow['id'])
        self.store.deletion('flows', flow['id'], preview['fingerprint'])
        self.assertEqual(len(self.store.current()['commands']), 2)
        preview = self.store.deletion('commands', a['id'])
        self.store.deletion('commands', a['id'], preview['fingerprint'])
        self.assertEqual([row['id'] for row in self.store.current()['commands']], [b['id']])

    def test_server_deletion_is_atomic_scoped_and_rechecks_preview_and_busy_state(self):
        first = self.connection('App', 'app.example')
        second = self.connection('Another account', 'app.example')
        other = self.connection('Separate', 'separate.example')
        self.command(first, 'A', script='pwd')
        command = self.command(second, 'B', script='id')
        self.store.save('flows', {'name': 'Check twice', 'connection': second['id'], 'steps': [command['id']]*2})
        self.store.save('apps', {'name': 'Legacy', 'connection': second['id'], 'sources': [{'name':'Log','path':'/srv/example/app.log'}]})
        self.command(other, 'Keep', script='pwd')
        target = catalog.server_id(first)
        preview = self.store.deletion('servers', target)
        self.assertEqual({kind:len(rows) for kind,rows in preview['items'].items()}, {'connections':2, 'commands':2, 'flows':1, 'apps':1})
        with self.assertRaisesRegex(ValueError, '运行'):
            self.store.deletion('servers', target, preview['fingerprint'], [second['id']])
        self.command(first, 'New dependency', script='pwd')
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.store.deletion('servers', target, preview['fingerprint'])
        preview = self.store.deletion('servers', target)
        self.store.deletion('servers', target, preview['fingerprint'])
        state = self.store.current()
        self.assertEqual([c['id'] for c in state['connections']], [other['id']])
        self.assertEqual([c['name'] for c in state['commands']], ['Keep'])
        self.assertFalse(state['flows'] or state['apps'])


class ParameterTests(unittest.TestCase):
    def test_parameter_does_not_add_shell_commands(self):
        argument = "https://example.test/file.jar?a=1; printf 'unexpected'"
        script = catalog.render('printf "%s" {{value}}', {'value': argument})
        result = subprocess.run(['bash', '-c', script], stdout=subprocess.PIPE, env={'PATH': os.environ['PATH']}, check=True)
        self.assertEqual(result.stdout.decode(), argument)

    def test_placeholder_must_be_a_complete_value(self):
        for script in ('printf "%s" "{{value}}"', 'printf "%s" pre{{value}}'):
            with self.subTest(script=script), self.assertRaises(ValueError):
                catalog.render(script, {'value': 'example'})

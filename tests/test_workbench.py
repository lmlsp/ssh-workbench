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

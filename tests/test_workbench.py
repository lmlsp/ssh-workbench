import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import auth
import catalog
from run_log import RunLog
from terminal import CANCEL_DISPATCH, DISPATCH, Terminal, pack, routed_endpoint
import workbench


class ListenerTests(unittest.TestCase):
    def test_local_default_can_use_http(self):
        self.assertEqual(workbench.listener_options('127.0.0.1', '', '', ''), (None, ()))

    def test_lan_listener_requires_explicit_scope_and_transport(self):
        with self.assertRaisesRegex(ValueError, 'LAN access'):
            workbench.listener_options('192.0.2.10', '', '', '192.0.2.0/24')
        with self.assertRaisesRegex(ValueError, 'LAN access'):
            workbench.listener_options('192.0.2.10', 'example.pem', 'example.key', '')
        with self.assertRaisesRegex(ValueError, 'specific IPv4'):
            workbench.listener_options('0.0.0.0', '', '', '')

    def test_explicit_lan_http_requires_an_allowlist(self):
        tls, networks = workbench.listener_options('192.0.2.10', '', '', '192.0.2.0/24', allow_http=True)
        self.assertIsNone(tls)
        self.assertEqual([str(network) for network in networks], ['192.0.2.0/24'])
        with self.assertRaisesRegex(ValueError, 'WORKBENCH_ALLOWED_NETWORKS'):
            workbench.listener_options('192.0.2.10', '', '', '', allow_http=True)


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

    def test_saved_ssh_login_password_is_private_and_bound_to_endpoint(self):
        connection = self.store.save('connections', {'name':'Node', 'host':'app.example', 'port':22,
                                                      'user':'operator', 'password':'test-login-value'})
        self.assertTrue(connection['passwordSet'])
        self.assertNotIn('secret', connection)
        self.assertNotIn('test-login-value', catalog.STATE.read_text())
        self.assertEqual(self.store.login_connection(connection['id'])[1], 'test-login-value')
        renamed = self.store.save('connections', dict(connection, name='Renamed node'))
        self.assertTrue(renamed['passwordSet'])
        migrated = self.store.save('connections', dict(renamed, port=2222))
        self.assertFalse(migrated['passwordSet'])
        self.assertIsNone(self.store.login_connection(connection['id'])[1])

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

    def test_grouped_flow_binds_each_selected_groups_parameters_in_order(self):
        node = self.connection('App', 'one.example')
        common = self.command(node, 'Stop', 'printf stop')
        download = self.command(node, 'Download', 'printf %s {{link}}')
        finish = self.command(node, 'Start', 'printf start')
        flow = self.store.save('flows', {'name': 'Grouped', 'connection': node['id'],
                                         'steps': [common['id'], download['id'], download['id'], finish['id']],
                                         'stepGroups': ['', 'A', 'B', '']})
        with self.assertRaisesRegex(ValueError, '至少选择一个'):
            self.store.plan('flow', flow['id'], {})
        _, _, steps, _ = self.store.plan('flow', flow['id'], {}, ['B'], {'B': {'link': 'build-b'}})
        self.assertEqual([step['script'] for step in steps], ['printf stop', 'printf %s build-b', 'printf start'])
        _, _, steps, _ = self.store.plan('flow', flow['id'], {}, ['B', 'A'], {'A': {'link': 'build-a'}, 'B': {'link': 'build-b'}})
        self.assertEqual([step['script'] for step in steps], ['printf stop', 'printf %s build-a', 'printf %s build-b', 'printf start'])
        prior_client = {key: value for key, value in flow.items() if key != 'stepGroups'}
        self.assertEqual(self.store.save('flows', dict(prior_client, name='Renamed'))['stepGroups'], flow['stepGroups'])

    def test_flow_revision_rejects_a_change_to_a_previewed_command(self):
        node = self.connection('App', 'one.example')
        task = self.command(node, 'Inspect', 'pwd')
        flow = self.store.save('flows', {'name': 'Check', 'connection': node['id'], 'steps': [task['id']] * 2})
        revision = self.store.current()['flows'][0]['revision']
        self.store.plan('flow', flow['id'], {}, revision=revision)
        self.store.save('commands', dict(task, script='id'))
        with self.assertRaisesRegex(ValueError, '已有修改'):
            self.store.plan('flow', flow['id'], {}, revision=revision)

    def test_step_waits_follow_the_selected_steps_and_preview_does_not_save(self):
        node = self.connection('App', 'one.example')
        first = self.command(node, 'Prepare', 'true')
        skipped = self.command(node, 'Wrong group', 'true')
        last = self.command(node, 'Last step', 'true')
        flow = self.store.save('flows', {'name': 'Wait', 'connection': node['id'],
                              'steps': [first['id'], skipped['id'], last['id']],
                              'stepGroups': ['', 'Admin', 'Portal'], 'stepWaits': [5, 11, 13]})
        _, _, steps, _ = self.store.plan('flow', flow['id'], {}, ['Portal'], waits=[7, 12, 15])
        self.assertEqual([(step['name'], step['waitSeconds']) for step in steps], [('Prepare',7), ('Last step',0)])
        self.assertEqual(self.store.current()['flows'][0]['stepWaits'], [5,11,13])
        for malformed in ([1, -1, 1], [1, True, 1], [1, 2.4, 1], [1, 2]):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(ValueError, '等待时间'):
                self.store.plan('flow', flow['id'], {}, ['Portal'], waits=malformed)

    def test_ssh_uses_legacy_saved_value_and_manual_login_requires_unique_password(self):
        node = self.connection('Jump', 'jump.example')
        command = self.command(node, 'Connect', 'ssh -p 2222 operator@dest.example', password='fixture-one')
        _, _, steps, switch_password = self.store.plan('command', command['id'], {})
        self.assertIsNone(switch_password)
        self.assertEqual(steps[0]['response'], 'fixture-one')
        self.assertEqual(self.store.ssh_password(node['id'], 'appuser', 'dest.example', 2222, 'operator'), 'fixture-one')
        self.assertIsNone(self.store.ssh_password(node['id'], 'appuser', 'dest.example', 22, 'operator'))
        self.store.save('commands', dict(command, name='Edited'))
        self.assertEqual(self.store.password(command['id']), 'fixture-one')
        self.command(node, 'Duplicate', 'ssh operator@dest.example -p 2222', password='fixture-two')
        self.assertIsNone(self.store.ssh_password(node['id'], 'appuser', 'dest.example', 2222, 'operator'))
        self.store.save('commands', dict(command, script='ssh -p 2222 operator@new.example'))
        self.assertIsNone(self.store.password(command['id']))

    def test_custom_manual_prompt_never_uses_a_saved_response(self):
        node = self.connection('App', 'one.example')
        command = self.command(node, 'Prompt', 'read -r -p "Continue?" answer', password='fixture-response',
                               followup={'prompt': 'Continue?', 'mode': 'manual'})
        _, _, steps, switch_password = self.store.plan('command', command['id'], {})
        self.assertIsNone(switch_password)
        self.assertIsNone(steps[0]['response'])
        self.assertEqual(steps[0]['followup']['prompt'], 'Continue?')

    def test_nested_connections_save_by_login_path(self):
        first = self.connection('First jump', 'one.example')
        second = self.connection('Second jump', 'two.example')
        hop = pack(['Host.EXAMPLE.', '2222', 'operator']).decode()
        endpoint = routed_endpoint(first, hop)
        self.assertEqual(endpoint, routed_endpoint(first, pack(['host.example', '2222', 'operator']).decode()))
        self.assertNotEqual(endpoint['id'], routed_endpoint(second, hop)['id'])
        self.assertNotEqual(catalog.server_id(endpoint), catalog.server_id(routed_endpoint(second, hop)))
        for name in ('First command', 'Second command'):
            self.store.save('commands', {'name': name, 'connection': endpoint['id'], 'user': 'appuser',
                                         'directory': '/srv/app', 'script': 'pwd'}, nested=endpoint)
        state = self.store.current()
        self.assertEqual([row['id'] for row in state['connections'] if row.get('kind') == 'nested'], [endpoint['id']])
        self.assertEqual({row['connection'] for row in state['commands']}, {endpoint['id']})

    def test_builtin_local_connection_is_opt_in_and_supports_persistent_commands(self):
        identifier = catalog.LOCAL_CONNECTION_ID
        with patch.object(catalog, 'LOCAL_TERMINAL_ENABLED', False):
            self.assertRaises(ValueError, self.store.login_connection, identifier)
        with patch.object(catalog, 'LOCAL_TERMINAL_ENABLED', True):
            connection, password = self.store.login_connection(identifier)
            self.assertEqual(connection['kind'], 'local')
            self.assertIsNone(password)
            self.assertEqual(self.store.current()['servers'][0]['id'], identifier)
            saved = self.store.save('commands', {'name':'Inspect local', 'connection':identifier,
                                                 'user':connection['user'], 'directory':'/tmp', 'script':'pwd'})
            name, selected, steps, _ = self.store.plan('command', saved['id'], {})
            self.assertEqual(name, 'Inspect local')
            self.assertEqual(selected['id'], identifier)
            self.assertEqual(steps[0]['script'], 'pwd')
            self.assertRaises(ValueError, self.store.deletion, 'connections', identifier)
            self.assertNotIn('"connections": [{', catalog.STATE.read_text())

    def test_switch_shortcut_uses_literal_target_and_clears_password_when_target_changes(self):
        node = self.connection('App', 'app.example')
        switch = self.command(node, 'Switch account', script='su - "deploy"', password='example-only-password')
        self.assertEqual(switch['switchTo'], 'deploy')
        plan = self.store.plan('command', switch['id'], {})
        self.assertEqual(plan[2][0]['switchTo'], 'deploy')
        self.assertEqual(plan[2][0]['user'], 'appuser')
        ordinary = self.command(node, 'Check', script='pwd')
        with self.assertRaisesRegex(ValueError, '单独执行'):
            self.store.save('flows', {'name': 'Invalid switch', 'connection': node['id'], 'steps': [switch['id'], ordinary['id']]})
        changed = self.store.save('commands', dict(switch, script='su - maintenance'))
        self.assertEqual(changed['switchTo'], 'maintenance')
        self.assertFalse(changed['passwordSet'])
        self.assertIsNone(catalog.switch_target('su - deploy\nls'))

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


class SelectedTerminalTests(unittest.TestCase):
    def setUp(self):
        self.connection = {'id':'saved-connection', 'name':'Application node', 'host':'app.example', 'port':22, 'user':'operator'}
        self.steps = [{'script':'pwd', 'directory':'/srv/app', 'user':'operator'}]
        self.terminal = SimpleNamespace(id='selected', connection=dict(self.connection), closed=False, ready=True,
                                        context={'user':'operator'}, run=None, condition=threading.RLock(), begin=Mock())
        self.terminal.endpoint = self.terminal.connection
        self.terms={'selected':self.terminal}
        self.addCleanup(patch.stopall)
        patch.object(workbench, 'TERMS', self.terms).start()
        patch.object(workbench.STORE, 'plan', return_value=('Inspect', self.connection, self.steps, None)).start()
        self.new_terminal = patch.object(workbench, 'Terminal').start()

    def test_run_keeps_selected_terminal(self):
        result=workbench.execute_on_terminal({'kind':'flow','id':'example','terminal':'selected'})
        self.assertEqual(result,{'id':'selected'})
        self.terminal.begin.assert_called_once_with('Inspect',self.steps,None,'flow')
        self.new_terminal.assert_not_called()
        self.assertEqual(list(self.terms),['selected'])

    def test_paused_run_can_only_be_replaced_with_its_id_after_prompt_returns(self):
        self.terminal.control = Mock()
        self.terminal.run = {'id': 'paused-run', 'name': 'Previous', 'phase': 'paused',
                             'steps': [{'name': 'Waiting'}], 'index': 0, 'connection': self.connection['id']}
        request = {'kind': 'flow', 'id': 'example', 'terminal': 'selected'}
        with self.assertRaisesRegex(ValueError, 'Previous'):
            workbench.execute_on_terminal(dict(request, replaceRun='different-run'))
        self.terminal.ready = False
        with self.assertRaisesRegex(ValueError, 'Previous'):
            workbench.execute_on_terminal(dict(request, replaceRun='paused-run'))
        self.terminal.control.assert_not_called()
        self.terminal.ready = True
        workbench.execute_on_terminal(dict(request, replaceRun='paused-run'))
        self.terminal.control.assert_called_once_with('leave')
        self.terminal.begin.assert_called_once_with('Inspect', self.steps, None, 'flow')

    def test_busy_terminal_or_changed_connection_rejected(self):
        request={'kind':'command','id':'example','terminal':'selected'}
        self.terminal.ready=False
        with self.assertRaisesRegex(ValueError,'等待输入'):
            workbench.execute_on_terminal(request)
        self.terminal.ready=True
        self.terminal.connection['host']='old.example'
        with self.assertRaisesRegex(ValueError,'重新连接'):
            workbench.execute_on_terminal(request)
        self.terminal.begin.assert_not_called()
        self.new_terminal.assert_not_called()

    def test_run_without_selection_must_not_open_connection(self):
        with self.assertRaisesRegex(ValueError,'先选中'):
            workbench.execute_on_terminal({'kind':'flow','id':'example'})
        self.new_terminal.assert_not_called()

    def test_run_cannot_target_outer_connection_while_logged_into_nested_host(self):
        hop = pack(['app.example', '22', 'operator']).decode()
        self.terminal.endpoint = routed_endpoint(self.connection, hop)
        with self.assertRaisesRegex(ValueError, '请选择'):
            workbench.execute_on_terminal({'kind':'command','id':'example','terminal':'selected'})
        self.terminal.begin.assert_not_called()


class PromptTests(unittest.TestCase):
    def test_password_response_requires_exact_ssh_host_and_is_sent_once(self):
        term = Terminal.__new__(Terminal)
        term.write = Mock()
        term.changed = Mock()
        term.prepare_input('ssh', 'fixture-response', login='operator', host='target.example')
        term.prompt = b"\r\noperator@other.example's password: "
        term.respond_to_prompt()
        term.write.assert_not_called()
        term.prompt = b"\r\noperator@target.example's password: "
        term.respond_to_prompt()
        term.write.assert_called_once_with(b'fixture-response\r')
        term.prompt = b"\r\noperator@target.example's password: "
        term.respond_to_prompt()
        term.write.assert_called_once()
        self.assertEqual(term.waiting_for_input, '等待输入密码')


class TerminalDispatchTests(unittest.TestCase):
    def setUp(self):
        self.term = Terminal.__new__(Terminal)
        self.term.condition = threading.RLock()
        self.term.connection = self.term.endpoint = {'id': 'example'}
        self.term.ready = False
        self.term.login_ticket = self.term.interaction = self.term.on_run = None
        self.term.wait_timer = None
        self.term.waiting_for_input = ''
        self.term.run = {'phase': 'dispatch', 'connection': 'example', 'ticket': 'step-ticket',
                         'payloadSent': False, 'index': 0, 'steps': [{'status': 'pending'}]}
        self.term.write = Mock()
        self.term.changed = Mock()

    def test_stop_while_shell_requests_payload_sends_cancel_count(self):
        self.term.control('end')
        self.assertEqual(self.term.run['phase'], 'stopping')
        self.term.write.assert_not_called()
        self.term.marker(['', 'D'])
        self.term.write.assert_called_once_with(b'0\n')
        self.term.marker(['', 'P', 'operator', '/srv/example'])
        self.assertEqual(self.term.run['phase'], 'ended')
        self.assertTrue(self.term.ready)

    def test_stop_after_payload_clears_the_unsubmitted_line(self):
        self.term.run['payloadSent'] = True
        self.term.control('end')
        self.term.marker(['', 'A', 'step-ticket'])
        self.term.write.assert_called_once_with(CANCEL_DISPATCH)
        self.term.marker(['', 'P', 'operator', '/srv/example'])
        self.assertEqual(self.term.run['phase'], 'ended')

    def test_incomplete_command_is_interrupted_and_failed(self):
        self.term.run['phase'] = 'running'
        self.term.marker(['', 'I', 'step-ticket'])
        self.term.write.assert_called_once_with(b'\x03')
        self.assertEqual(self.term.run['steps'][0], {'status': 'failed', 'exitCode': 2})
        self.term.marker(['', 'R', 'step-ticket', '130'])
        self.term.marker(['', 'P', 'operator', '/srv/example'])
        self.assertEqual(self.term.run['phase'], 'paused')
        self.assertTrue(self.term.ready)

    def test_exiting_a_live_command_does_not_write_any_terminal_input(self):
        self.term.run['phase'] = 'running'
        self.term.run['steps'][0]['status'] = 'running'
        self.term.interaction = {'kind': 'custom', 'response': 'fixture'}
        self.term.control('leave')
        self.term.write.assert_not_called()
        self.assertEqual(self.term.run['phase'], 'exited')
        self.assertEqual(self.term.run['steps'][0]['status'], 'detached')
        self.assertNotIn('response', self.term.interaction)

    def test_pause_suspends_a_countdown_and_old_timer_cannot_launch_a_step(self):
        self.term.closed = False
        self.term.ready = True
        self.term.context = {'user': 'operator'}
        self.term.wait_timer = timer = Mock()
        run = self.term.run
        run.update(phase='waiting', name='Workflow', user='operator', switchTo=None,
                   takeover=False, waitDeadline=109, waitRemaining=5,
                   steps=[{'name':'Next', 'status':'pending'}])
        with patch('terminal.time.monotonic', return_value=104):
            self.term.control('pause')
        timer.cancel.assert_called_once_with()
        self.assertEqual(run['waitRemaining'], 5)
        with patch('terminal.time.monotonic', return_value=120), patch('terminal.threading.Timer', return_value=Mock()):
            self.term.control('resume')
        self.assertEqual(run['waitDeadline'], 125)
        self.term.wait_finished(run, 109)
        self.term.write.assert_not_called()
        self.term.wait_finished(run, 125)
        self.term.write.assert_called_once_with(DISPATCH)

    def test_ctrl_c_keeps_the_step_at_recovery_even_if_the_command_returns_zero(self):
        self.term.run.update(phase='running', takeover=False)
        self.term.run['steps'][0]['status'] = 'running'
        self.term.input('\x03')
        self.term.marker(['', 'R', 'step-ticket', '0'])
        self.term.marker(['', 'P', 'operator', '/srv/example'])
        self.assertEqual(self.term.run['phase'], 'paused')
        self.assertEqual(self.term.run['index'], 0)
        self.assertEqual(self.term.run['steps'][0]['status'], 'interrupted')
        with self.assertRaisesRegex(ValueError, '重试'):
            self.term.control('resume')


class ShellHistoryTests(unittest.TestCase):
    def test_first_command_is_recorded_with_read_only_history_timestamp_format(self):
        actor = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / '.bashrc').write_text("readonly HISTTIMEFORMAT='%F %T '\n")
            account = SimpleNamespace(pw_name=actor, pw_dir=home)
            with patch('terminal.LOCAL_TERMINAL_ENABLED', True), patch('terminal.pwd.getpwuid', return_value=account):
                terminal = Terminal({'kind': 'local', 'id': 'fixture'})
            try:
                with terminal.condition:
                    self.assertTrue(terminal.condition.wait_for(lambda: terminal.ready, timeout=5))
                    command = 'printf "%s\\n" first-command'
                    terminal.input(command + '\r')
                    self.assertTrue(terminal.condition.wait_for(lambda: terminal.ready, timeout=5))
                    self.assertEqual([entry['script'] for entry in terminal.history], [command])
            finally:
                terminal.close()


class RunLogTests(unittest.TestCase):
    def test_earlier_page_keeps_immutable_order_after_updates(self):
        with tempfile.TemporaryDirectory() as data:
            path = Path(data) / 'runs.sqlite3'
            log = RunLog(path)
            self.assertEqual(log.page(), {'runs': [], 'before': None})
            for index in range(32):
                log.save({'id': str(index), 'phase': 'running'})
            log.save({'id': '0', 'phase': 'succeeded'})
            latest = RunLog(path).page()
            older = RunLog(path).page(latest['before'])
            self.assertEqual([entry['id'] for entry in latest['runs']], [str(index) for index in range(31, 1, -1)])
            self.assertEqual([entry['id'] for entry in older['runs']], ['1', '0'])
            self.assertEqual(older['runs'][1]['phase'], 'succeeded')
            self.assertIsNone(older['before'])

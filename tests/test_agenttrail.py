"""Contrat du relais Map : confidentialite, perimetre, transport et sources."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentwatch import cli
from agentwatch.collector.store import EventStore
from agentwatch.connectors import agenttrail as A
from agentwatch.core.schema import empty_event


class AgenttrailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / 'home'
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.outside = Path(self.temp.name) / 'outside'
        self.outside.mkdir()
        self.store = EventStore(self.home)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def event(self, **values):
        row = empty_event()
        row.update(client='codex', session_id='parent', phase='start', call_id='call-1',
                   tool_name='Read', cwd=str(self.project), project_dir=str(self.project),
                   target='README.md', target_kind='path')
        row.update(values)
        return row

    def args(self, **values):
        defaults = dict(project_dir=str(self.project), port=5330, interval=2.0,
                        recent_seconds=120.0, dry_run=False, follow=False)
        defaults.update(values)
        return argparse.Namespace(**defaults)

    def test_projection_does_not_leak_private_fields(self):
        row = self.event(params={'command':'PRIVATE'}, evidence={'text':'PRIVATE'},
                         error_summary='PRIVATE', usage={'tokens':123}, input_fingerprint='PRIVATE')
        item, = A.map_events(row, self.project)
        self.assertEqual((item['agent'], item['hook_event_name']), ('codex', 'PreToolUse'))
        self.assertEqual(item['tool_input'], {'pattern':'target=README.md'})
        self.assertEqual(set(item), {'agent','cwd','session_id','hook_event_name','tool_name','tool_input'})
        self.assertNotIn('PRIVATE', json.dumps(item))

    def test_reads_and_tool_completions_never_claim_file_writes(self):
        for tool, status in (('Read','success'), ('Write','success'), ('Write','error'), ('Read',None)):
            item, = A.map_events(self.event(phase='end',tool_name=tool,status=status),self.project)
            self.assertEqual(item['hook_event_name'], 'PostToolUse')
            self.assertNotIn('file_path', item['tool_input'])
            self.assertNotIn('notebook_path', item['tool_input'])
            self.assertIn('result=' + (status or 'unknown'), item['tool_input']['pattern'])

    def test_child_identity_is_separate_and_turn_stop_can_resume(self):
        parent, = A.map_events(self.event(),self.project)
        child, = A.map_events(self.event(client='claude-code',agent_id='child',phase='subagent_stop'),self.project)
        self.assertEqual((child['agent'],child['hook_event_name']), ('claude','Stop'))
        self.assertNotEqual(parent['session_id'], child['session_id'])
        self.assertEqual(A.map_events(self.event(phase='subagent_stop'),self.project), [])
        self.assertEqual(A.map_events(self.event(agent_id='main'),self.project)[0]['session_id'],parent['session_id'])
        stopped, = A.map_events(self.event(phase='turn_end'),self.project)
        resumed, = A.map_events(self.event(phase='turn_start'),self.project)
        self.assertEqual(stopped['session_id'],resumed['session_id'])
        self.assertEqual(resumed['hook_event_name'],'SessionStart')

    def test_missing_or_unrelated_identity_is_rejected(self):
        for values in ({'session_id':None},{'client':'cursor'},{'client':[]},{'phase':{}},{'event_id':{}},
                       {'schema_version':'99'}, {'cwd':'relative','target':None},
                       {'cwd':str(self.outside),'project_dir':str(self.outside)},
                       {'phase':'message'}, {'phase':'usage'}, {'tool_name':None}):
            self.assertEqual(A.map_events(self.event(**values),self.project), [], values)

    def test_project_can_be_scoped_by_an_explicit_file_target(self):
        item, = A.map_events(self.event(cwd=str(self.outside),target=str(self.project/'README.md')),self.project)
        self.assertEqual(item['cwd'],str(self.project))
        self.assertEqual(item['tool_input']['pattern'],'target=README.md')
        self.assertEqual(A.map_events(self.event(cwd=str(self.outside),target_kind='command',target='PRIVATE'),self.project),[])

    def test_outside_paths_and_command_targets_are_excluded(self):
        for values in ({'target':'../outside.txt'}, {'target_kind':'command','target':'PRIVATE'}):
            item, = A.map_events(self.event(**values),self.project)
            self.assertEqual(item['tool_input'],{'pattern':'AgentWatch: tool-start'})

    def test_normalized_paths_anchor_targets_to_source_project(self):
        with patch.object(A.Path,'home',return_value=Path(self.temp.name)):
            item, = A.map_events(self.event(cwd='~/project/src',project_dir='~/project',target='src/module.py'),self.project)
        self.assertEqual(item['cwd'],str(self.project/'src'))
        self.assertEqual(item['tool_input']['pattern'],'target=src/module.py')
        item, = A.map_events(self.event(cwd=str(self.project/'src'),target='README.md'),self.project)
        self.assertEqual(item['tool_input']['pattern'],'target=README.md')

    def test_status_does_not_turn_unknown_or_failed_into_success(self):
        for status in ('error','denied','timeout','interrupted','success',None,'unknown'):
            item, = A.map_events(self.event(phase='end',status=status),self.project)
            self.assertTrue(item['tool_input']['pattern'].startswith('result='+ (status or 'unknown')))
        item, = A.map_events(self.event(phase='failure',status=None),self.project)
        self.assertTrue(item['tool_input']['pattern'].startswith('result=error'))

    def test_session_identity_is_stable_and_provider_scoped(self):
        row = self.event()
        first = A.map_events(row,self.project)[0]
        self.assertEqual(A.map_events(dict(row,event_id='different'),self.project)[0]['session_id'],first['session_id'])
        self.assertNotEqual(A.map_events(dict(row,client='claude-code'),self.project)[0]['session_id'],first['session_id'])
        self.assertNotEqual(A.map_events(dict(row,agent_id='child'),self.project)[0]['session_id'],first['session_id'])
        parent = A.map_events(dict(row,session_id='a:b'),self.project)[0]['session_id']
        child = A.map_events(dict(row,session_id='a',agent_id='b'),self.project)[0]['session_id']
        self.assertNotEqual(parent,child)
        long = 'a' * 200
        self.assertNotEqual(A.map_events(dict(row,session_id=long+'b'),self.project)[0]['session_id'],
                            A.map_events(dict(row,session_id=long+'c'),self.project)[0]['session_id'])

    def test_tail_deduplicates_compaction_and_preserves_sources(self):
        now = time.time_ns()
        old = self.event(received_time_ns=now-300_000_000_000)
        fresh = self.event(received_time_ns=now)
        paths = [Path(self.store.write_event(row)) for row in (old,fresh)]
        before = {str(p):p.read_bytes() for p in paths}
        tail = A.EventTail(self.home,now-1_000_000_000)
        self.assertEqual([r['event_id'] for r in tail.poll(now)],[fresh['event_id']])
        segment = self.home/'segments'/'codex'/'parent'/'segment-test.jsonl'
        segment.parent.mkdir(parents=True)
        segment.write_text(json.dumps(old)+'\n'+json.dumps(fresh)+'\n',encoding='utf-8')
        self.assertEqual(tail.poll(now),[])
        self.assertEqual(before,{str(p):p.read_bytes() for p in paths})

    def test_tail_handles_malformed_future_and_new_rows(self):
        now = time.time_ns()
        self.store.write_event(self.event(received_time_ns=now+70_000_000_000))
        file = self.home/'spool'/'codex'/'parent'/f'{now:020d}-bad.json'
        file.write_text('{',encoding='utf-8')
        tail = A.EventTail(self.home,now-1_000_000_000)
        self.assertEqual(tail.poll(now),[])
        self.assertTrue(tail.warnings)
        row = self.event(received_time_ns=now+1)
        self.store.write_event(row)
        self.assertEqual([r['event_id'] for r in tail.poll(now+2)],[row['event_id']])
        self.assertEqual(tail.poll(now+2),[])

    def test_cli_dry_run_contacts_no_service_and_keeps_sources(self):
        file = Path(self.store.write_event(self.event()))
        before = file.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(A.MapClient,'connect',side_effect=AssertionError('network')):
            code = cli.main(['--home',str(self.home),'agenttrail','--project-dir',str(self.project),
                             '--dry-run','--recent-seconds','120'])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.getvalue())['hook_event_name'],'PreToolUse')
        self.assertEqual(file.read_bytes(),before)

    def test_invalid_port_and_timing_are_rejected(self):
        for interval, recent in ((0.1,0),(2,-1),(2,121),(float('nan'),0),(2,float('inf'))):
            with self.assertRaises(ValueError):
                A.run_bridge(self.args(interval=interval,recent_seconds=recent,dry_run=True),self.home)
        for port in (True,80,65536,'example.com'):
            with self.assertRaises(ValueError):
                A.MapClient(port,self.project)

    def server(self, *, wrong_project=False, wrong_model=False, redirect=False):
        calls = []
        identity = {'repoPath':str(self.outside if wrong_project else self.project)}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                calls.append((self.path,None))
                if redirect:
                    self.send_response(302);self.send_header('Location','http://127.0.0.1:1/leak');self.end_headers();return
                self.respond(identity if self.path=='/whoami' else {'runs':[],'hasPlan':False} if not wrong_model else {'app':'unrelated'})
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path,payload))
                self.send_response(200);self.end_headers()
            def respond(self,value):
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps(value).encode())
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        identity['port'] = server.server_port
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        return calls,server,identity

    def test_http_checks_board_before_post_and_rechecks_reassigned_port(self):
        calls,server,identity = self.server()
        client = A.MapClient(server.server_port,self.project)
        self.assertEqual(client.connect(),f'http://127.0.0.1:{server.server_port}')
        self.assertTrue(client.send(A.map_events(self.event(),self.project)[0]))
        self.assertEqual([r[0] for r in calls],['/whoami','/model','/hook'])
        identity['repoPath'] = str(self.outside)
        with self.assertRaises(A.ConnectorError): client.connect()
        with self.assertRaises(A.ConnectorError): client.send({})
        self.assertEqual(sum(route=='/hook' for route,_ in calls),1)

    def test_wrong_board_and_redirect_receive_no_events(self):
        for options in ({'wrong_project':True},{'wrong_model':True},{'redirect':True}):
            calls,server,_ = self.server(**options)
            with self.assertRaises(A.ConnectorError): A.MapClient(server.server_port,self.project).connect()
            self.assertFalse(any(route=='/hook' for route,_ in calls))

    @unittest.skipUnless(os.name == 'nt','les chemins Windows sont insensibles a la casse')
    def test_windows_cwd_uses_the_exact_board_spelling_without_mutation(self):
        calls,server,identity = self.server()
        identity['repoPath'] = str(self.project).upper()
        client = A.MapClient(server.server_port,self.project)
        client.connect()
        event = A.map_events(self.event(),self.project)[0]
        original = dict(event)
        client.send(event)
        self.assertEqual(calls[-1][1]['cwd'],identity['repoPath'])
        self.assertEqual(event,original)

    def test_follow_retains_events_while_board_is_offline(self):
        self.store.write_event(self.event())
        delivered = []
        with patch.object(A.MapClient,'connect',side_effect=[A.ConnectorError('offline'),'http://127.0.0.1:5330']), \
             patch.object(A.MapClient,'send',side_effect=lambda ev:delivered.append(ev) or True), \
             patch.object(A.time,'sleep',side_effect=[None,KeyboardInterrupt]):
            self.assertEqual(A.run_bridge(self.args(follow=True),self.home,lambda _:None,lambda _:None),0)
        self.assertEqual(len(delivered),1)

    def test_ambiguous_post_is_not_replayed_as_a_duplicate_tool(self):
        self.store.write_event(self.event())
        output = []
        with patch.object(A.MapClient,'connect',return_value='http://127.0.0.1:5330'), \
             patch.object(A.MapClient,'send',side_effect=A.DeliveryUncertain('response lost')) as send, \
             patch.object(A.time,'sleep',side_effect=[None,KeyboardInterrupt]):
            self.assertEqual(A.run_bridge(self.args(follow=True),self.home,lambda _:None,output.append),0)
        self.assertEqual(send.call_count,1)
        self.assertTrue(any('non rejoue' in line for line in output))

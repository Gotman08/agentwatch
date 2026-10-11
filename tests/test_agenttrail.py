"""Contrat du relais local, y compris transport HTTP et conservation des sources."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
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
        self.state = Path(self.temp.name) / 'kitchen'
        self.state.mkdir()
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

    def test_projection_does_not_leak_private_fields(self):
        row = self.event(params={'command':'PRIVATE'}, evidence={'text':'PRIVATE'},
                         error_summary='PRIVATE', usage={'tokens':123}, input_fingerprint='PRIVATE')
        item, = A.kitchen_events(row, self.project)
        self.assertEqual((item['provider'], item['sessionId'], item['kind']), ('codex', 'parent', 'tool-start'))
        self.assertEqual(item['file'], str(self.project / 'README.md'))
        self.assertFalse(any(key in item for key in ('params', 'usage', 'evidence', 'error_summary', 'tasks')))
        self.assertNotIn('PRIVATE', json.dumps(item))

    def test_child_identity_and_subagent_stop_are_separate_from_parent_end(self):
        item, = A.kitchen_events(self.event(client='claude-code', agent_id='child', phase='subagent_stop'), self.project)
        self.assertEqual((item['provider'],item['sessionId'],item['parentId'],item['kind']), ('claude','child','parent','turn-end'))
        self.assertEqual(A.kitchen_events(self.event(phase='subagent_stop'),self.project), [])
        self.assertEqual(A.kitchen_events(self.event(agent_id='main'),self.project)[0]['sessionId'], 'parent')

    def test_missing_or_unrelated_identity_is_rejected(self):
        for values in ({'session_id':None},{'client':'cursor'},{'client':[]},{'phase':{}},{'event_id':{}},{'schema_version':'99'},
                       {'cwd':'relative'},{'cwd':str(self.state)},{'phase':'message'},{'phase':'usage'}):
            self.assertEqual(A.kitchen_events(self.event(**values), self.project), [])

    def test_file_outside_project_and_command_targets_are_excluded(self):
        for values in ({'target':'../outside.txt'}, {'target_kind':'command','target':'PRIVATE'}):
            self.assertNotIn('file', A.kitchen_events(self.event(**values),self.project)[0])

    def test_normalized_home_paths_and_project_relative_targets_are_resolved(self):
        with patch.object(A.Path,'home',return_value=Path(self.temp.name)):
            item,=A.kitchen_events(self.event(cwd='~/project/src',project_dir='~/project',target='src/module.py'),self.project)
        self.assertEqual(item['cwd'],str(self.project/'src'))
        self.assertEqual(item['file'],str(self.project/'src/module.py'))
        item,=A.kitchen_events(self.event(cwd=str(self.project/'src'),target='README.md'),self.project)
        self.assertEqual(item['file'],str(self.project/'README.md'))

    def test_status_mapping_does_not_call_unknown_a_success(self):
        for status in ('error','denied','timeout'):
            self.assertTrue(A.kitchen_events(self.event(phase='end',status=status),self.project)[0]['error'])
        for status,kind in ((None,'unknown'),('unknown','unknown'),('interrupted','interrupted')):
            items=A.kitchen_events(self.event(phase='end',status=status),self.project)
            self.assertEqual([i['kind'] for i in items], ['tool-end',kind])
            self.assertNotEqual(items[0]['id'],items[1]['id'])
        self.assertEqual(len(A.kitchen_events(self.event(phase='end',status='success'),self.project)),1)

    def test_identity_is_stable_and_provider_scoped(self):
        row=self.event()
        self.assertEqual(A.kitchen_events(row,self.project),A.kitchen_events(row,self.project))
        other=dict(row,client='claude-code')
        self.assertNotEqual(A.kitchen_events(row,self.project)[0]['id'],A.kitchen_events(other,self.project)[0]['id'])

    def test_tail_ignores_history_and_deduplicates_compaction_without_writes(self):
        now=time.time_ns()
        old=self.event(received_time_ns=now-300_000_000_000)
        fresh=self.event(received_time_ns=now)
        old_path=Path(self.store.write_event(old))
        fresh_path=Path(self.store.write_event(fresh))
        before={str(p):p.read_bytes() for p in (old_path,fresh_path)}
        tail=A.EventTail(self.home,now-1_000_000_000)
        self.assertEqual([r['event_id'] for r in tail.poll(now)], [fresh['event_id']])
        segment=self.home/'segments'/'codex'/'parent'/'segment-test.jsonl'
        segment.parent.mkdir(parents=True)
        segment.write_text(json.dumps(old)+'\n'+json.dumps(fresh)+'\n',encoding='utf-8')
        self.assertEqual(tail.poll(now), [])
        self.assertEqual(before,{str(p):p.read_bytes() for p in (old_path,fresh_path)})

    def test_tail_handles_malformed_and_future_rows(self):
        now=time.time_ns()
        self.store.write_event(self.event(received_time_ns=now+70_000_000_000))
        file=self.home/'spool'/'codex'/'parent'/f'{now:020d}-bad.json'
        file.write_text('{',encoding='utf-8')
        tail=A.EventTail(self.home,now-1_000_000_000)
        self.assertEqual(tail.poll(now), [])
        self.assertTrue(tail.warnings)

    def test_tail_sees_new_events_after_first_empty_poll(self):
        now=time.time_ns()
        tail=A.EventTail(self.home,now)
        self.assertEqual(tail.poll(now), [])
        row=self.event(received_time_ns=now+1)
        self.store.write_event(row)
        self.assertEqual([r['event_id'] for r in tail.poll(now+2)], [row['event_id']])
        self.assertEqual(tail.poll(now+2), [])

    def test_cli_dry_run_contacts_no_service_and_keeps_sources(self):
        row=self.event()
        file=Path(self.store.write_event(row));before=file.read_bytes()
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch.object(A.KitchenClient,'connect',side_effect=AssertionError('network')):
            code=cli.main(['--home',str(self.home),'agenttrail','--project-dir',str(self.project),
                           '--dry-run','--recent-seconds','120'])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.getvalue())['kind'],'tool-start')
        self.assertEqual(file.read_bytes(),before)

    def test_invalid_timing_options_are_rejected(self):
        for values in ((0.1,0),(2,-1),(2,121),(float('nan'),0),(2,float('inf'))):
            args=argparse.Namespace(project_dir=str(self.project),state_dir=str(self.state),
                                    interval=values[0],recent_seconds=values[1],dry_run=True,follow=False)
            with self.assertRaises(ValueError):A.run_bridge(args,self.home)

    def server(self, *, wrong_app=False, redirect=False):
        calls=[]
        project=str(self.project)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                calls.append((self.path,self.headers.get('Authorization'),None))
                if redirect:
                    self.send_response(302);self.send_header('Location','http://127.0.0.1:1/leak');self.end_headers();return
                self.respond({'app':'unrelated' if wrong_app else 'agenttrail-kitchen','version':2})
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path,self.headers.get('Authorization'),payload))
                self.respond({'app':'agenttrail-kitchen','projects':[project]} if self.path=='/api/attach' else {'accepted':True})
            def respond(self,value):
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(value).encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        (self.state/'server.json').write_text(json.dumps({'port':server.server_port,'hookToken':'a'*48}),encoding='utf-8')
        return calls,server

    def test_http_authentication_attach_and_rotation(self):
        calls,server=self.server()
        client=A.KitchenClient(self.state,self.project)
        self.assertEqual(client.connect(),f'http://127.0.0.1:{server.server_port}')
        client.send(A.kitchen_events(self.event(),self.project)[0])
        self.assertEqual([r[0] for r in calls],['/api/state','/api/attach','/api/hook'])
        self.assertIsNone(calls[0][1]);self.assertEqual(calls[1][1],'Bearer '+'a'*48)
        (self.state/'server.json').write_text(json.dumps({'port':server.server_port,'hookToken':'b'*48}),encoding='utf-8')
        client.connect()
        self.assertEqual(calls[-1][1],'Bearer '+'b'*48)

    def test_wrong_service_and_redirect_receive_no_bearer_token(self):
        for options in ({'wrong_app':True},{'redirect':True}):
            calls,_=self.server(**options)
            with self.assertRaises(A.ConnectorError):A.KitchenClient(self.state,self.project).connect()
            self.assertEqual(len(calls),1);self.assertIsNone(calls[0][1])

    def test_invalid_registration_never_becomes_an_arbitrary_remote_url(self):
        for value in ({'port':True,'hookToken':'a'*48},{'port':80,'hookToken':'a'*48},
                      {'port':4780,'hookToken':'PRIVATE'},{'port':'example.com','hookToken':'a'*48}):
            (self.state/'server.json').write_text(json.dumps(value),encoding='utf-8')
            with self.assertRaises(A.ConnectorError) as raised:A.KitchenClient(self.state,self.project).connect()
            self.assertNotIn('PRIVATE',str(raised.exception))

    def test_follow_retries_failed_delivery_with_same_identity(self):
        self.store.write_event(self.event())
        args=argparse.Namespace(project_dir=str(self.project),state_dir=str(self.state),
                                interval=2.0,recent_seconds=120.0,dry_run=False,follow=True)
        seen=[]
        def send(event):
            seen.append(event['id'])
            if len(seen)==1:raise A.ConnectorError('offline')
            return True
        with patch.object(A.KitchenClient,'connect',return_value='http://127.0.0.1:4780'),\
             patch.object(A.KitchenClient,'send',side_effect=send),\
             patch.object(A.time,'sleep',side_effect=[None,KeyboardInterrupt]):
            self.assertEqual(A.run_bridge(args,self.home,lambda _:None,lambda _:None),0)
        self.assertEqual(len(seen),2);self.assertEqual(seen[0],seen[1])

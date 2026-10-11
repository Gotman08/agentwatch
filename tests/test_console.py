"""Console : isolation, reprise, vraie correlation, temps et contexte de consultation."""
from __future__ import annotations

import hashlib
import http.cookiejar
import io
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agentwatch.collector.store import EventStore
from agentwatch.console import analysis, chat
from agentwatch.console.ingest import Ingestor
from agentwatch.console.maps import MapProcess
from agentwatch.console.sources import capture
from agentwatch.console.history import trends, comparison, HistoryJobs, snapshots, load_snapshot
from agentwatch.console.server import Console, Server
from agentwatch.console.storage import Project, Registry, encode, session_key, console_lock
from agentwatch.core.schema import empty_event


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.a = self.root / 'a'
        self.b = self.root / 'b'
        self.a.mkdir(); self.b.mkdir()
        self.state = self.root / 'state'
        self.registry = Registry(self.state)
        self.pa, _ = self.registry.add(str(self.a))
        self.pb, _ = self.registry.add(str(self.b))
        self.source = self.root / 'source'
        self.store = EventStore(self.source)
        self.reader = Ingestor(self.registry, self.source, self.state)
        self.ns = time.time_ns() - 30_000_000_000

    def tearDown(self):
        self.temp.cleanup()

    def event(self, **values):
        e = empty_event()
        e.update(client='codex',session_id='session',phase='start',call_id='call',tool_name='mcp__example__lookup',
                 tool_category='mcp',mcp_server='example',mcp_tool='lookup',cwd=str(self.a),project_dir=str(self.a),
                 params={'id':'record'},received_time_ns=self.ns)
        e.update(values)
        e['received_time'] = datetime.fromtimestamp(e['received_time_ns']/1e9,timezone.utc).isoformat()
        return e

    def put(self, **values):
        e = self.event(**values); self.store.write_event(e); return e

    def analysed(self, p=None):
        p = p or self.pa
        for sid in analysis.pending(p,100):
            analysis.analyse(p,sid)


class StorageRoutingTests(Fixture):
    def test_temporarily_unavailable_roots_remain_registered_when_adding_a_project(self):
        missing=str(self.root/'unmounted')
        entries=json.loads(self.registry.path.read_text('utf-8'));entries.append({'root':missing})
        self.registry.path.write_text(encode(entries),'utf-8')
        recovered=Registry(self.state);self.assertTrue(recovered.errors)
        extra=self.root/'extra';extra.mkdir();recovered.add(str(extra))
        self.assertIn({'root':missing},json.loads(recovered.path.read_text('utf-8')))

    def test_failed_registry_save_does_not_publish_a_project_without_its_observer(self):
        extra=self.root/'extra';extra.mkdir();before=self.registry.path.read_bytes()
        with patch('agentwatch.console.storage.os.replace',side_effect=PermissionError('readonly')):
            with self.assertRaises(PermissionError): self.registry.add(str(extra))
        self.assertEqual(len(self.registry.all()),2)
        self.assertEqual(self.registry.path.read_bytes(),before)

    def test_live_arrival_is_read_while_older_segment_is_backfilled(self):
        for n in range(8):
            self.put(call_id=str(n), received_time_ns=self.ns+n)
        self.store.compact_session('codex',self.store.session_dir('codex','session').name)
        self.reader.step(1)
        self.assertIsNotNone(self.reader.files)
        self.put(session_id='new-live',cwd=str(self.b),project_dir=str(self.b))
        self.reader.step(1,live=True)
        self.assertEqual(self.pb.info()['events'],1)
        self.assertIsNotNone(self.reader.files)
        for _ in range(8): self.reader.step(1)
        self.assertEqual(self.pa.info()['events'],8)

    def test_console_owner_lock_is_exclusive_and_released(self):
        with console_lock(self.state):
            with self.assertRaises(ValueError):
                with console_lock(self.state): pass
        with console_lock(self.state): pass

    def test_unfollow_keeps_history_and_readding_recovers_it(self):
        self.pa.append(self.event(),'cwd')
        app=Console(self.state,self.source)
        app.remove(self.pa.id)
        self.assertTrue((self.pa.home/'history.sqlite3').is_file())
        self.assertNotIn(self.pa.id,[p['id'] for p in app.projects()])
        recovered=app.add(str(self.a))
        self.assertEqual(recovered.info()['events'],1)
        app.stop()

    def test_parallel_roots_and_completion_without_cwd(self):
        self.put(call_id='a')
        self.put(call_id='b',cwd=str(self.b),project_dir=str(self.b),received_time_ns=self.ns+100)
        self.put(call_id='a',phase='end',cwd=None,project_dir=None,status='success',received_time_ns=self.ns+2_000_000_000)
        self.put(call_id='b',phase='end',cwd=None,project_dir=None,status='failure',received_time_ns=self.ns+3_000_000_000)
        self.reader.step(); self.analysed(self.pa); self.analysed(self.pb)
        ca=analysis.calls_page(self.pa,{})['items'][0]; cb=analysis.calls_page(self.pb,{})['items'][0]
        self.assertEqual((ca['call_id'],ca['status'],ca['duration_ms']),('a','success',2000))
        self.assertEqual((cb['call_id'],cb['status']),('b','failure'))
        self.assertEqual((self.pa.info()['events'],self.pb.info()['events']),(2,2))

    def test_explicit_workdir_and_mcp_session_inference_keeps_provenance(self):
        self.put(cwd=str(self.root),project_dir=str(self.root),params={'workdir':str(self.a)})
        self.put(call_id='mcp',cwd=str(self.root),project_dir=str(self.root),received_time_ns=self.ns+1)
        self.reader.step()
        with self.pa.db() as db:
            rows=db.execute('SELECT route,data FROM events ORDER BY at_ns').fetchall()
        self.assertEqual([r[0] for r in rows],['workdir','inferred_session_workdir'])
        self.assertEqual(json.loads(rows[1][1])['cwd'],str(self.root))

    def test_out_of_order_backfill_uses_the_workdir_at_the_event_time(self):
        with self.reader.db() as db:
            self.reader._route(self.event(params={'workdir':str(self.b)},received_time_ns=self.ns+10),db)
            self.reader._route(self.event(params={'workdir':str(self.a)},received_time_ns=self.ns),db)
            old,basis=self.reader._route(self.event(cwd=str(self.root),project_dir=str(self.root),received_time_ns=self.ns+5),db)
            new,_=self.reader._route(self.event(cwd=str(self.root),project_dir=str(self.root),received_time_ns=self.ns+15),db)
        self.assertEqual((old.id,new.id,basis),(self.pa.id,self.pb.id,'inferred_session_workdir'))

    def test_live_completion_before_its_start_stays_retryable(self):
        self.put(phase='end',status='success',cwd=str(self.root),project_dir=str(self.root),received_time_ns=self.ns+10)
        self.reader.step(live=True)
        self.assertEqual(self.pa.info()['events'],0)
        self.put(params={'workdir':str(self.a)},received_time_ns=self.ns)
        self.reader.step()
        self.assertEqual(self.pa.info()['events'],2)

    def test_nesting_chooses_the_registered_working_root(self):
        nested=self.a/'nested';nested.mkdir();p,_=self.registry.add(str(nested));self.reader.reset()
        self.put(cwd=str(nested),project_dir=str(self.a));self.reader.step()
        self.assertEqual((self.pa.info()['events'],p.info()['events']),(0,1))

    def test_compaction_restart_and_new_project_do_not_duplicate_or_mutate_sources(self):
        event=self.put();self.reader.step();self.store.compact_session('codex',EventStore(self.source).session_dir('codex','session').name)
        paths=list((self.source/'segments').rglob('*.jsonl'));before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        restarted=Ingestor(Registry(self.state),self.source,self.state);restarted.step();restarted.reset();restarted.step()
        self.assertEqual(self.pa.info()['events'],1)
        self.assertEqual(before,{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

    def test_budget_resumes_inside_large_segment(self):
        for n in range(7): self.put(call_id=str(n),received_time_ns=self.ns+n)
        self.store.compact_session('codex',self.store.session_dir('codex','session').name)
        self.reader.step(2);self.reader.step(2);self.reader.step(2);self.reader.step(2)
        self.assertEqual(self.pa.info()['events'],7)

    def test_partial_jsonl_does_not_block_other_projects(self):
        folder=self.source/'segments'/'codex'/'partial';folder.mkdir(parents=True)
        path=folder/'segment-1.jsonl';raw=encode(self.event(session_id='partial')).encode();path.write_bytes(raw[:50])
        self.put(cwd=str(self.b),project_dir=str(self.b));self.reader.step()
        self.assertEqual(self.pb.info()['events'],1)
        path.write_bytes(raw+b'\n');self.reader.step();self.assertEqual(self.pa.info()['events'],1)

    def test_outside_project_is_not_attached_to_selected_project(self):
        self.put(cwd=str(self.root),project_dir=str(self.root));self.reader.step()
        self.assertEqual(self.pa.info()['events'],0);self.assertEqual(self.reader.snapshot()['unassigned'],1)

    def test_two_concurrent_writers_deduplicate_sqlite(self):
        event=self.event()
        threads=[threading.Thread(target=lambda:self.pa.append(event,'cwd')) for _ in range(8)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(self.pa.info()['events'],1)


class AnalysisTests(Fixture):
    def test_historical_result_is_recoverable_after_job_manager_restart(self):
        self.pa.append(self.event(),'cwd')
        jobs=HistoryJobs();job=jobs.start(self.pa,'trends',{'days':0});jobs.stop()
        recovered=load_snapshot(self.pa,job['id'])
        self.assertEqual(recovered['status'],'complete')
        self.assertEqual(snapshots(self.pa)[0]['id'],job['id'])
        with self.assertRaises(KeyError):load_snapshot(self.pb,job['id'])
    def test_real_error_statuses_are_counted_and_grouped(self):
        for n,status in enumerate(['error','timeout','denied']):
            self.pa.append(self.event(call_id=str(n),phase='end',status=status),'cwd')
        self.analysed();self.assertEqual(self.pa.info()['failures'],3)
        self.assertEqual(analysis.calls_page(self.pa,{'status':'failure'})['total'],3)

    def test_trends_and_comparison_use_only_this_roots_observations(self):
        for sid in ['first','second']:
            self.pa.append(self.event(session_id=sid),'cwd')
        self.pb.append(self.event(session_id='other',cwd=str(self.b),target='PRIVATE-B'),'cwd')
        result=trends(self.pa,{'days':0})
        self.assertNotIn('PRIVATE-B',encode(result));self.assertEqual(result['console_scope']['sessions_retained'],2)
        result=comparison(self.pa,{'before':session_key('codex','first'),'after':session_key('codex','second')})
        self.assertEqual(result['console_scope']['root'],str(self.a))

    def test_parallel_time_is_union_and_sum_and_crosses_bucket_boundaries(self):
        for cid,lo,hi in [('one',0,3),('two',1,4)]:
            self.pa.append(self.event(call_id=cid,received_time_ns=self.ns+lo*1_000_000_000),'cwd')
            self.pa.append(self.event(call_id=cid,phase='end',status='success',received_time_ns=self.ns+hi*1_000_000_000),'call_start')
        self.analysed()
        d=analysis.timeline(self.pa,{'since':str(self.ns),'until':str(self.ns+5_000_000_000),'seconds':'1'})
        self.assertEqual(sum(b['active_ms'] for b in d['buckets']),4000)
        self.assertEqual(sum(b['tool_ms'] for b in d['buckets']),6000)
        self.assertEqual([b['tool_ms'] for b in d['buckets']],[1000,2000,2000,1000,0])

    def test_open_call_is_not_a_measured_busy_interval(self):
        self.pa.append(self.event(),'cwd');self.analysed()
        d=analysis.timeline(self.pa,{'since':str(self.ns),'until':str(self.ns+10_000_000_000),'seconds':'1'})
        self.assertEqual(sum(b['active_ms'] for b in d['buckets']),0)
        self.assertEqual(sum(b['unknown_duration'] for b in d['buckets']),1)

    def test_stable_pagination_with_equal_timestamps(self):
        for n in range(7):self.pa.append(self.event(call_id=str(n)),'cwd')
        self.analysed();seen=[];q={'limit':'2'}
        while True:
            p=analysis.calls_page(self.pa,q);seen.extend(c['id'] for c in p['items'])
            if not p['cursor']:break
            q['cursor']=p['cursor']
        self.assertEqual(len(seen),7);self.assertEqual(len(set(seen)),7)

    def test_report_keeps_mcp_and_durable_exports(self):
        self.pa.append(self.event(),'cwd');self.analysed();sid=session_key('codex','session')
        call=analysis.calls_page(self.pa,{})['items'][0]
        self.assertEqual((call['mcp_server'],call['mcp_tool']),('example','lookup'))
        report=self.pa.report(sid)['report'];self.assertIn('observed_dependency_graph',report)
        self.assertTrue((self.pa.home/'reports'/f'{sid}.md').is_file())
        self.assertEqual(report['project_scope']['routing_bases'],{'cwd':1})


class ContextTests(Fixture):
    def test_call_and_event_context_respects_mcp_result_and_text_filters(self):
        for cid,server,status in [('excluded','example','success'),('selected','other','error')]:
            self.pa.append(self.event(call_id=cid,mcp_server=server,target=cid),'cwd')
            self.pa.append(self.event(call_id=cid,mcp_server=server,target=cid,phase='end',status=status,received_time_ns=self.ns+1_000_000_000),'cwd')
        self.analysed()
        ctx=chat.prepare_context(self.pa,{'sections':['calls','events'],'mcp':'other','status':'failure','search':'selected'})
        self.assertEqual([c['call_id'] for c in ctx['bundle']['sections']['calls']['items']],['selected'])
        self.assertEqual({e['call_id'] for e in ctx['bundle']['sections']['events']['items']},{'selected'})

    def test_budget_keeps_recent_calls_instead_of_omitting_the_entire_section(self):
        for n in range(20):
            self.pa.append(self.event(call_id=str(n),params={'description':'x'*1000},received_time_ns=self.ns+n),'cwd')
        self.analysed()
        ctx=chat.prepare_context(self.pa,{'sections':['calls'],'budget_chars':8000})
        selected=ctx['bundle']['sections']['calls']['items']
        self.assertGreater(len(selected),0);self.assertLess(len(selected),20)
        self.assertEqual(selected[0]['call_id'],'19')
        self.assertTrue(ctx['bundle']['omissions']);self.assertLessEqual(ctx['chars'],8000)

    def test_native_output_is_verified_masked_and_persisted_without_source_changes(self):
        native=self.root/'native';native.mkdir();self.source.mkdir(exist_ok=True)
        (self.source/'config.json').write_text(encode({'health':{'codex_sessions_dir':str(native)}}),'utf-8')
        name='rollout-2026-10-11T00-00-00-session.jsonl';path=native/name
        record={'type':'response_item','payload':{'type':'function_call_output','call_id':'call',
                  'output':'password=secretvalue123\n{"api_key":"jsonsecret123"}\n'+('output '*500)}}
        path.write_text(encode(record)+'\n','utf-8');before=path.read_bytes()
        source={'source':{'file':name,'offset':0,'line':1},'source_identity':{'record_type':'response_item','item_id':'call'}}
        self.pa.append(self.event(),'cwd');self.pa.append(self.event(phase='end',status='success',evidence=source,received_time_ns=self.ns+1000000000),'call_start')
        self.analysed();cid=analysis.calls_page(self.pa,{})['items'][0]['id']
        output=capture(self.pa,cid,self.source,1000)
        self.assertFalse(output['complete']);self.assertNotIn('secretvalue123',output['text']);self.assertNotIn('jsonsecret123',output['text'])
        self.assertEqual(before,path.read_bytes())
        path.write_text(encode({'type':'response_item','payload':{'call_id':'different','output':'WRONG'}}),'utf-8')
        with self.assertRaises(ValueError):capture(self.pa,cid,self.source)
        context=chat.prepare_context(self.pa,{'sections':['outputs']})
        self.assertNotIn('WRONG',encode(context));self.assertIn('output',encode(context))

    def test_context_has_only_one_project_and_snapshot_is_immutable(self):
        self.pa.append(self.event(target='only-A'),'cwd');self.pb.append(self.event(target='SECRET-B',cwd=str(self.b)),'cwd')
        self.analysed(self.pa);self.analysed(self.pb)
        ctx=chat.prepare_context(self.pa,{'sections':['calls'],'call_limit':1})
        self.assertNotIn('SECRET-B',encode(ctx));self.assertEqual(ctx['bundle']['project']['id'],self.pa.id)
        self.pa.append(self.event(call_id='new',target='after-preview'),'cwd');self.analysed()
        with self.pa.db() as db: stored=db.execute('SELECT data,digest FROM contexts WHERE id=?',(ctx['id'],)).fetchone()
        self.assertNotIn('after-preview',stored[0]);self.assertEqual(stored[1],ctx['digest'])

    def test_budget_marks_omissions_and_redacts_plan_secrets(self):
        (self.a/'PLAN.md').write_text('# A\npassword=supersecret123\n'+'x'*12000,'utf-8')
        ctx=chat.prepare_context(self.pa,{'sections':['summary','plan'],'budget_chars':8000})
        self.assertLessEqual(ctx['chars'],8000);self.assertTrue(ctx['bundle']['omissions'])
        self.assertNotIn('supersecret123',encode(ctx))
        ctx=chat.prepare_context(self.pa,{'sections':['plan'],'budget_chars':24000})
        self.assertNotIn('supersecret123',encode(ctx));self.assertIn('<secret:',encode(ctx))

    def test_cli_adapters_do_not_load_working_project_or_enable_writes(self):
        with patch('agentwatch.console.chat.shutil.which',return_value='provider.exe'):
            c=chat.command('codex',self.state,'chosen');a=chat.command('claude',self.state,None)
        self.assertIn('read-only',c);self.assertIn('--ignore-user-config',c);self.assertIn('hooks',c)
        self.assertIn('mcp_servers={}',c);self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',c)
        self.assertIn('--safe-mode',a);self.assertEqual(a[a.index('--tools')+1],'')

    def test_codex_and_claude_packets_are_real_text_or_errors(self):
        self.assertEqual(chat.parse_reply('codex',{'type':'item.completed','item':{'type':'agent_message','text':'answer'}})[0],'answer')
        self.assertEqual(chat.parse_reply('claude',{'type':'assistant','message':{'content':[{'type':'text','text':'answer'}]}})[0],'answer')
        self.assertTrue(chat.parse_reply('claude',{'type':'result','is_error':True,'result':'login required'})[2])

    def test_provider_failure_persists_and_cannot_reuse_another_projects_context(self):
        manager=chat.ChatManager(self.state);ctx=chat.prepare_context(self.pa,{'sections':['summary']})
        with patch('agentwatch.console.chat.shutil.which',return_value='missing.exe'):
            with self.assertRaises(ValueError):
                manager.start(self.pb,{'context_id':ctx['id'],'message':'question'})
            result=manager.start(self.pa,{'context_id':ctx['id'],'message':'question'})
            manager.jobs[result['message']]['thread'].join(5)
        messages=self.pa.conversation(result['conversation']);self.assertEqual(messages[-1]['status'],'failed')
        self.assertIsNotNone(messages[-1]['error']);manager.stop()


class HttpTests(Fixture):
    def setUp(self):
        super().setUp();self.app=Console(self.state,self.source);self.server=Server(self.app,0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.jar=http.cookiejar.CookieJar();self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.open(self.url+'/').read();self.csrf=json.load(self.opener.open(self.url+'/api/bootstrap'))['csrf']

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.app.stop();self.thread.join();super().tearDown()

    def request(self,path,data=None,**headers):
        if data is not None:headers.setdefault('Content-Type','application/json')
        request=urllib.request.Request(self.url+path,data=encode(data).encode() if data is not None else None,headers=headers)
        return self.opener.open(request)

    def test_mutations_require_csrf_and_reject_foreign_origin(self):
        path=f'/api/projects/{self.pa.id}/context'
        for headers in ({},{'X-AgentWatch-CSRF':self.csrf,'Origin':'http://evil.example'}):
            with self.assertRaises(urllib.error.HTTPError) as exc:self.request(path,{'sections':[]},**headers)
            self.assertEqual(exc.exception.code,403)
        with self.request(path,{'sections':[]},**{'X-AgentWatch-CSRF':self.csrf}) as response:
            self.assertEqual(json.load(response)['bundle']['project']['id'],self.pa.id)

    def test_read_api_requires_local_session_and_unknown_call_is_scoped(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:urllib.request.urlopen(self.url+'/api/projects')
        self.assertEqual(exc.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError) as exc:self.request(f'/api/projects/{self.pa.id}/calls/not-in-this-project')
        self.assertEqual(exc.exception.code,404)

    def test_static_script_and_project_bootstrap_are_served(self):
        with self.request('/app.js') as response:self.assertIn(b'prepareContext',response.read())
        d=json.load(self.request('/api/bootstrap'));self.assertEqual(len(d['projects']),2)

    def test_shutdown_requires_local_token_and_returns_before_stopping(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:self.request('/api/shutdown',{})
        self.assertEqual(exc.exception.code,403)
        response=json.load(self.request('/api/shutdown',{},**{'X-AgentWatch-CSRF':self.csrf}))
        self.assertTrue(response['stopping'])
        self.thread.join(3);self.assertFalse(self.thread.is_alive())

    def test_native_feedback_marks_and_clear_are_persisted(self):
        path=f'/api/projects/{self.pa.id}/feedback'
        for mark in ['relevant','false-positive','clear']:
            self.request(path,{'id':'finding-id','mark':mark},**{'X-AgentWatch-CSRF':self.csrf}).read()
            from agentwatch.reports.feedback import load_feedback
            feedback=load_feedback(self.pa.home)
            self.assertEqual('finding-id' in feedback,mark!='clear')

    def test_past_message_context_cannot_be_read_from_another_root(self):
        ctx=chat.prepare_context(self.pa,{'sections':['summary']})
        with self.pa.db() as db:
            db.execute("INSERT INTO conversations VALUES(?,?,?)",('conversation',self.ns,'question'))
            db.execute("INSERT INTO messages(id,conversation,role,content,status,created_ns,context,digest,provider) VALUES(?,?,?,?,?,?,?,?,?)",
                       ('message','conversation','user','question','complete',self.ns,encode(ctx['bundle']),ctx['digest'],'codex'))
        response=json.load(self.request(f'/api/projects/{self.pa.id}/message-context/message'))
        self.assertEqual(response['digest'],ctx['digest'])
        with self.assertRaises(urllib.error.HTTPError) as exc:self.request(f'/api/projects/{self.pb.id}/message-context/message')
        self.assertEqual(exc.exception.code,404)


@unittest.skipUnless(shutil.which('node'),'Node.js requis pour verifier le vrai observateur Map')
class NativeMapTests(Fixture):
    def test_file_change_bursts_are_durable_beyond_the_preview_and_resume(self):
        observer=MapProcess(self.pa);observer.start()
        try:
            for n in range(25): (self.a/f'burst-{n}.txt').write_text(str(n))
            time.sleep(.35)
            observer.capture_changes()
            with self.pa.db() as db:
                paths={r[0] for r in db.execute('SELECT path FROM changes')}
            self.assertEqual(paths,{f'burst-{n}.txt' for n in range(25)})
            self.assertEqual(observer.capture_changes(),0)
            observer.stop();observer.start()
            (self.a/'after-restart.txt').write_text('new');time.sleep(.2)
            observer.capture_changes()
            with self.pa.db() as db:
                self.assertEqual(db.execute("SELECT COUNT(DISTINCT path) FROM changes").fetchone()[0],26)
        finally: observer.stop()

    def test_two_real_watchers_follow_different_roots_simultaneously(self):
        a,b=MapProcess(self.pa),MapProcess(self.pb);a.start();b.start()
        try:
            self.assertNotEqual(a.port,b.port)
            (self.a/'only-a.txt').write_text('a');(self.b/'only-b.txt').write_text('b');time.sleep(.25)
            a.cache_at=b.cache_at=0
            self.assertEqual(a.model()['activity']['file'],'only-a.txt')
            self.assertEqual(b.model()['activity']['file'],'only-b.txt')
            self.assertEqual(a.project.id,self.pa.id);self.assertEqual(b.project.id,self.pb.id)
        finally:a.stop();b.stop()

    def test_map_observes_real_files_but_ignores_its_storage_and_disables_setup(self):
        observer=MapProcess(self.pa);observer.start()
        try:
            self.assertTrue(observer.info()['running'],observer.error)
            before=observer.model()['activity'];(self.pa.home/'report.json').write_text('{}')
            time.sleep(.15);observer.cache_at=0;self.assertEqual(observer.model()['activity'],before)
            (self.a/'real.txt').write_text('real change');time.sleep(.2);observer.cache_at=0
            self.assertEqual(observer.model()['activity']['file'],'real.txt')
            self.assertFalse((self.a/'PLAN.md').exists());self.assertFalse((self.a/'AGENTS.md').exists())
            req=urllib.request.Request(f'http://127.0.0.1:{observer.port}/setup-board',data=b'{}',headers={'X-AgentWatch-Internal':observer.token})
            with self.assertRaises(urllib.error.HTTPError) as exc:urllib.request.urlopen(req)
            self.assertEqual(exc.exception.code,403)
        finally:observer.stop()
        self.assertFalse(observer.info()['running'])

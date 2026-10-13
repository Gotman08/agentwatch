"""Contrat avec le vrai serveur Map officiel, sur un depot et un home temporaires."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agentwatch.collector.store import EventStore
from agentwatch.connectors.agenttrail import EventTail, MapClient, map_events
from agentwatch.core.schema import empty_event


@unittest.skipUnless(os.environ.get('AGENTTRAIL_MAP_PACKAGE') and shutil.which('node'),
                     'definir AGENTTRAIL_MAP_PACKAGE vers la source officielle Map recente')
class MapIntegrationTests(unittest.TestCase):
    def test_real_map_receives_sessions_and_observes_real_writes(self):
        package = Path(os.environ['AGENTTRAIL_MAP_PACKAGE']).resolve()
        self.assertTrue((package/'bin/agenttrail.mjs').is_file())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root/'project with spaces';project.mkdir()
            readme = project/'README.md';readme.write_text('fixture',encoding='utf-8')
            plan = project/'PLAN.md'
            plan.write_text('# Fixture\n## Observer les fichiers {#files}\nfiles: [README.md]\n- [ ] Verifier les fichiers {#check}\n',encoding='utf-8')
            state = root/'state';home = root/'agentwatch'
            with (root/'server.log').open('wb') as log:
                launcher = Path(__file__).resolve().parent.parent/'examples/agenttrail_map.mjs'
                proc = subprocess.Popen([shutil.which('node'),str(launcher),'--package',str(package),
                                         '--project',str(project),'--state-dir',str(state),'--port','15330'],
                                        stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic()+15
                    registry = state/'server.json'
                    while not registry.is_file() and proc.poll() is None and time.monotonic()<deadline:
                        time.sleep(0.05)
                    self.assertIsNone(proc.poll(),'Map did not start')
                    registration = json.loads(registry.read_text(encoding='utf-8'))
                    client = MapClient(registration['port'],project);client.connect()
                    tail = EventTail(home,time.time_ns());store = EventStore(home)
                    source_paths = []
                    def deliver(**values):
                        row = empty_event()
                        row.update(client='codex',session_id='parent',cwd=str(project),project_dir=str(project),phase='session_start')
                        row.update(values)
                        source_paths.append(Path(store.write_event(row)))
                        for event in tail.poll():
                            for item in map_events(event,project): self.assertTrue(client.send(item))
                    deliver()
                    deliver(client='claude-code',agent_id='child',phase='subagent_start')
                    deliver(phase='start',tool_name='Read',target='README.md',target_kind='path',params={'command':'PRIVATE'})
                    deliver(phase='end',tool_name='Read',target='README.md',target_kind='path',status='unknown')
                    current = client._http('/model')
                    self.assertEqual({r['agent'] for r in current['runs']},{'codex','claude'})
                    parent = next(r for r in current['runs'] if r['agent']=='codex')
                    self.assertIsNone(parent['currentTool'])
                    self.assertIn('result=unknown',parent['recentTools'][0]['detail'])
                    self.assertIsNone(current['activity'],'a read was falsely displayed as a write')
                    self.assertIsNone(current['plan'][0]['touchedAt'])
                    deliver(phase='end',tool_name='Write',target='README.md',target_kind='path',status='error')
                    current = client._http('/model')
                    self.assertIn('result=error',next(r for r in current['runs'] if r['agent']=='codex')['recentTools'][0]['detail'])
                    self.assertIsNone(current['activity'],'a failed write was falsely displayed as a write')
                    before_sources = {str(p):p.read_bytes() for p in source_paths}
                    readme.write_text('real file change',encoding='utf-8')
                    deadline = time.monotonic()+5
                    while time.monotonic()<deadline:
                        current = client._http('/model')
                        if current['activity'] and current['activity']['file']=='README.md': break
                        time.sleep(0.05)
                    self.assertEqual(current['activity']['file'],'README.md')
                    self.assertIsNotNone(current['plan'][0]['touchedAt'])
                    self.assertEqual(current['plan'][0]['status'],'pending')
                    deliver(phase='turn_end')
                    deliver(client='claude-code',agent_id='child',phase='subagent_stop')
                    snapshot = client._http('/model')
                    self.assertEqual(len(snapshot['runs']),2)
                    self.assertTrue(all(r['ended'] for r in snapshot['runs']))
                    self.assertTrue(all(r['todos']==[] for r in snapshot['runs']))
                    self.assertNotIn('PRIVATE',json.dumps(snapshot))
                    self.assertEqual(before_sources,{p:Path(p).read_bytes() for p in before_sources})
                    self.assertFalse((project/'.claude').exists())
                    self.assertFalse((project/'AGENTS.md').exists())
                    self.assertFalse((project/'CLAUDE.md').exists())
                    self.assertEqual(set(project.iterdir()),{readme,plan})
                finally:
                    if proc.poll() is None:
                        proc.stdin.write(b'stop\n');proc.stdin.flush()
                        try: proc.wait(timeout=10)
                        except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=5)
                    proc.stdin.close()
                self.assertEqual(proc.returncode,0,'Map did not shut down cleanly')
                self.assertTrue(list((state/'observer-home'/'.agenttrail').glob('*.json')))

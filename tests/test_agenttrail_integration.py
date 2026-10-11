"""Contrat HTTP avec la vraie Kitchen; opt-in, sans collecte de journaux personnels."""
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
from agentwatch.connectors.agenttrail import EventTail, KitchenClient, kitchen_events
from agentwatch.core.schema import empty_event


@unittest.skipUnless(os.environ.get('AGENTTRAIL_KITCHEN_PACKAGE') and shutil.which('node'),
                     'definir AGENTTRAIL_KITCHEN_PACKAGE pour le test de la vraie Kitchen')
class KitchenIntegrationTests(unittest.TestCase):
    def test_real_kitchen_receives_agentwatch_events_without_inventing_tasks(self):
        package=Path(os.environ['AGENTTRAIL_KITCHEN_PACKAGE']).resolve()
        self.assertTrue((package/'src/server.mjs').is_file())
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);project=root/'project with spaces';project.mkdir()
            (project/'README.md').write_text('fixture',encoding='utf-8')
            state=root/'state';home=root/'agentwatch'
            log=(root/'server.log').open('wb')
            launcher=Path(__file__).resolve().parent.parent/'examples'/'agenttrail_kitchen.mjs'
            proc=subprocess.Popen([shutil.which('node'),str(launcher),'--package',str(package),
                                   '--project',str(project),'--state-dir',str(state),'--port','14780'],
                                  stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT)
            try:
                deadline=time.monotonic()+15
                while not (state/'server.json').is_file() and proc.poll() is None and time.monotonic()<deadline:
                    time.sleep(0.05)
                self.assertIsNone(proc.poll(),'Kitchen did not start')
                client=KitchenClient(state,project);client.connect()
                before=client._http(client.registration[0],'/api/state')
                self.assertEqual(before['executors'],[]);self.assertFalse(before['observing'])
                tail=EventTail(home,time.time_ns());store=EventStore(home)
                def deliver(**values):
                    row=empty_event();row.update(client='codex',session_id='parent',cwd=str(project),phase='session_start')
                    row.update(values);store.write_event(row)
                    delivered=0
                    for event in tail.poll():
                        for item in kitchen_events(event,project):
                            self.assertTrue(client.send(item));delivered+=1
                    return delivered
                deliver()
                deliver(agent_id='child',phase='subagent_start')
                deliver(phase='start',call_id='read',tool_name='Read',target='README.md',target_kind='path',params={'command':'PRIVATE'})
                deliver(phase='end',call_id='read',tool_name='Read',status='unknown')
                current=client._http(client.registration[0],'/api/state')
                parent=next(s for s in current['executors'] if s['sessionId']=='parent')
                self.assertEqual((parent['state'],parent['activeToolCount']),('unknown',0))
                deliver(phase='start',call_id='write',tool_name='Write',target='README.md',target_kind='path')
                deliver(phase='end',call_id='write',tool_name='Write',status='error')
                current=client._http(client.registration[0],'/api/state')
                self.assertEqual(next(s for s in current['executors'] if s['sessionId']=='parent')['state'],'error')
                deliver(phase='turn_end')
                deliver(agent_id='child',phase='subagent_stop')
                snapshot=client._http(client.registration[0],'/api/state')
                self.assertEqual(len(snapshot['executors']),2)
                child=next(s for s in snapshot['executors'] if s['sessionId']=='child')
                self.assertEqual((child['parentId'],child['state'],child['ended']),('codex:parent','complete',False))
                self.assertEqual(snapshot['orders'],[])
                self.assertNotIn('PRIVATE',json.dumps(snapshot))
                self.assertEqual((project/'README.md').read_text(encoding='utf-8'),'fixture')
                self.assertFalse((project/'PLAN.md').exists());self.assertFalse((project/'.claude').exists())
            finally:
                if proc.poll() is None:
                    proc.stdin.write(b'stop\n');proc.stdin.flush()
                    try:proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                proc.stdin.close();log.close()
            self.assertEqual(proc.returncode,0,'Kitchen did not shut down cleanly')

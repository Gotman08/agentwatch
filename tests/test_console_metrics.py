"""Quantiles, perimetre et union de temps avec des oracles independants."""
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentwatch.console import analysis, chat, mcp
from agentwatch.console.storage import Project, encode


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        (base/'a').mkdir(); (base/'b').mkdir()
        self.a, self.b = Project(base/'a'), Project(base/'b')
        self.ns = 1000000000000

    def tearDown(self):
        self.temp.cleanup()

    def call(self, name, duration, *, tool='lookup', status='success', size=0,
             reliable=True, start=0, session='s', project=None, timeline_reliable=True):
        p = project or self.a
        at = self.ns + start*1000000000
        data = {'id':name,'client':'codex','session':session,'call_id':name,'tool_name':tool,
                'mcp_server':'example','mcp_tool':tool,'start_ns':at,
                'end_ns':at+int(duration*1000000) if duration is not None else None,
                'duration_ms':duration,'duration_reliable':reliable,'timeline_reliable':timeline_reliable,
                'status':status,'output_size_bytes':size,'output_truncated':name=='7'}
        with p.db() as db:
            db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?)',
                       (name,session,at,data['end_ns'],tool,'codex',status,'example',encode(data)))
        return data

    def test_exact_server_quantiles_do_not_average_tool_percentiles(self):
        for i in range(20): self.call(str(i),10*i,tool='a' if i%2==0 else 'b',size=i)
        self.call('open',None,status='open',size=None)
        self.call('unreliable',100000,status='error',reliable=False,size=-1)
        result=mcp.overview(self.a,{})
        total=result['total']
        self.assertEqual((total['calls'],total['measured_calls'],total['unknown_duration_calls']),(22,20,2))
        self.assertEqual((total['median_ms'],total['p95_ms'],total['max_ms'],total['total_ms']),(95,180,190,1900))
        self.assertEqual((total['output_observed_calls'],total['output_bytes'],total['unknown_output_calls']),(20,190,2))
        self.assertEqual(total['truncated_calls'],1)
        self.assertAlmostEqual(total['failure_rate_pct'],100/21)
        server=result['servers'][0]
        self.assertEqual((server['median_ms'],server['p95_ms']),(95,180))
        tool_stats={row['tool']:row for row in result['tools']}
        self.assertEqual(tool_stats['a']['p95_ms'],180)
        self.assertEqual(tool_stats['b']['p95_ms'],190)

    def test_tool_dates_and_project_scope_remain_exact(self):
        self.call('a',5,tool='a',start=1)
        self.call('b',15,tool='b',start=2)
        self.call('foreign',1000,tool='a',start=1,project=self.b)
        q={'tool':'a','since':str(self.ns),'until':str(self.ns+2000000000)}
        data=mcp.overview(self.a,q)
        self.assertEqual(data['total']['calls'],1)
        self.assertEqual(data['total']['p95_ms'],5)
        self.assertEqual(analysis.calls_page(self.a,q)['items'][0]['id'],'a')
        self.assertEqual(data['selection'],q)

    def test_absent_measurements_are_not_zero_or_a_success_rate(self):
        empty=mcp.overview(self.a,{})['total']
        self.assertIsNone(empty['total_ms']); self.assertIsNone(empty['output_bytes'])
        self.call('open',None,status='open',size=None)
        unknown=mcp.overview(self.a,{})['total']
        for key in ['median_ms','p95_ms','total_ms','output_bytes','failure_rate_pct']:
            self.assertIsNone(unknown[key],key)
        self.assertEqual(unknown['unknown_duration_calls'],1)

    def test_odd_quantiles_and_zero_duration_are_measured(self):
        for i,n in enumerate([0,10,20]): self.call(str(i),n)
        data=mcp.overview(self.a,{})['total']
        self.assertEqual((data['measured_calls'],data['median_ms'],data['p95_ms']),(3,10,20))

    def test_timeline_streams_without_decoding_raw_parameters_and_keeps_union_exact(self):
        rng=random.Random(7)
        intervals=[]
        for i in range(90):
            lo=rng.randrange(-10,60); duration=rng.randrange(1,15)
            self.call(str(i),duration*1000,start=lo)
            intervals.append(set(range(lo,lo+duration)))
        self.call('unknown',None,start=1,status='open')
        self.call('bad-time',2000,start=2,timeline_reliable=False)
        self.call('bad-duration',500000,start=3,reliable=False)
        with patch('agentwatch.console.analysis.json.loads',side_effect=AssertionError('raw decoding')):
            result=analysis.timeline(self.a,{'since':str(self.ns),'until':str(self.ns+60000000000),'seconds':'10'})
        for i,bucket in enumerate(result['buckets']):
            cells=set(range(i*10,(i+1)*10))
            self.assertEqual(bucket['active_ms'],len(set().union(*intervals)&cells)*1000)
            self.assertEqual(bucket['tool_ms'],sum(len(cells&interval) for interval in intervals)*1000)
        self.assertEqual(sum(b['unknown_duration'] for b in result['buckets']),2)
        self.assertEqual(sum(b['unplaced_time'] for b in result['buckets']),1)

    def test_mcp_chat_context_has_the_selected_tool_metrics_only(self):
        self.call('one',10,tool='a'); self.call('two',100,tool='b')
        context=chat.prepare_context(self.a,{'sections':['mcp'],'tool':'a','page':'mcp'})['bundle']
        selected=context['sections']['mcp']
        self.assertEqual(context['selection'],{'tool':'a'})
        self.assertEqual(selected['total']['calls'],1)
        self.assertEqual(selected['tools'][0]['tool'],'a')
        self.assertEqual(selected['total']['p95_ms'],10)

    def test_undated_calls_are_retained_without_pushing_bounds_to_1970(self):
        self.call('undated',None,start=-1000,status='unknown',size=None)
        self.call('dated',10,start=1)
        result=mcp.overview(self.a,{})
        self.assertEqual(result['total']['calls'],2)
        self.assertEqual(result['total']['undated_calls'],1)
        self.assertEqual(result['total']['first_ns'],self.ns+1000000000)
        self.assertEqual(analysis.summary(self.a)['first_ns'],self.ns+1000000000)
        timed=analysis.timeline(self.a,{'seconds':'1'})
        self.assertEqual(timed['calls'],1)
        self.assertEqual(timed['undated_calls'],1)
        self.assertEqual(timed['bounds'][0],self.ns+1000000000)


if __name__=='__main__': unittest.main()

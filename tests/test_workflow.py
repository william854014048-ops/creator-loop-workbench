import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.workflow import Workflow, WorkflowError
from app.storage import Conflict
from app.model import Settings, ModelClient


class MockModel:
    def __init__(self):
        self.calls = []
        self.fail = False

    def complete(self, task, system, inputs):
        self.calls.append((task, system, copy.deepcopy(inputs)))
        if self.fail:
            raise RuntimeError('secret key must never surface')
        return json.dumps({
            'angles': {'angles': [{'id': 'a', 'title': '方向', 'audience': '创作者', 'change': '改变', 'example': '例', 'reason': '理由'}], 'questions': []},
            'package': {'titles': ['标题'], 'cover': '封面', 'opening': '开头', 'note': '备注'},
            'plan': {'report': '内容建议'},
            'review': {'report': '检查', 'edits': [{'original': '重复', 'replacement': '具体', 'reason': '清楚', 'risk': 'low'}]},
            'value': {'report': '价值', 'hypotheses': ['受众会认同']},
            'blind-score': {'dimensions': {k: {'score': 3, 'reason': '原稿', 'confidence': 'low'} for k in ['ER','HP','QL','NA','AB','SR','SAT']}},
            'cheat-predict': {'report': '实验判断', 'bucket': '基础盘', 'probabilities': [{'bucket':k,'probability':p} for k,p in [('底部',30),('基础盘',40),('命中',20),('小爆',8),('大爆',2)]], 'center': 300, 'hypotheses': ['开头有效'], 'counterfactuals': ['更换开头']},
            'retro': {'report': '复盘', 'comparisons': [], 'observations': [{'text': '开头更具体', 'evidence': '评论', 'next_action': '下一条用场景'}]},
        }[task], ensure_ascii=False)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.model = MockModel()
        self.flow = Workflow(self.tmp.name, self.model)
        self.w = self.flow.create({'title': '测试'})

    def patch(self, **patch):
        self.w = self.flow.patch(self.w['id'], self.w['revision'], patch)

    def act(self, action, **params):
        self.w = self.flow.action(self.w['id'], action, {'revision': self.w['revision'], **params})

    def ready(self):
        self.patch(material='我想讲的真实经历', spoken='这是重复的话', final={'script':'最终原稿', 'title':'实际标题', 'cover':'实际封面', 'opening':'实际开头', 'platform':'抖音', 'account':'我的账号', 'duration_seconds':60})
        self.act('confirm-final')

    def test_forecasts_receive_no_history_and_zero_metadata(self):
        self.ready()
        self.act('judge-value')
        self.act('judge-cheat')
        for task, system, payload in self.model.calls:
            self.assertNotIn('material', payload)
            self.assertNotIn('metrics', payload)
            self.assertNotIn('retros', payload)
            self.assertNotIn('other_judgment', payload)
            self.assertNotIn('account', payload['final'])
        b = self.w['judgments']['cheat'][0]
        self.assertEqual(b['result']['composite'], 6)
        self.assertEqual(b['provenance']['calibration_samples'], 0)
        self.assertIsNone(b['provenance']['baseline_plays'])
        self.assertEqual(set(self.model.calls[1][2]), {'final', 'rules'})

    def test_lock_publish_metrics_retro_and_observation(self):
        self.ready()
        for kind in ['value', 'cheat']:
            self.act('judge-' + kind)
            self.act('lock-judgment', kind=kind, judgment_id=self.w['judgments'][kind][-1]['id'])
        frozen = copy.deepcopy(self.w['judgments'])
        with self.assertRaises(WorkflowError):
            self.patch(judgments={'value': []})
        self.act('publish', platform='抖音', account='我的账号', url='', published_at='2026-11-01T12:00', window_hours=72)
        pub = self.w['publications'][-1]
        self.act('metrics', publication_id=pub['id'], observed_at='2026-11-02T12:00', plays=0, likes='', comments_count=0, shares=None, favorites=0, comments=[], comments_unavailable_reason='还没有评论')
        m = self.w['metrics'][-1]
        self.assertEqual(m['plays'], 0)
        self.assertIsNone(m['likes'])
        self.assertEqual(m['elapsed_hours'], 24)
        self.assertTrue(m['early'])
        self.assertTrue(m['observed_at'].endswith('+08:00'))
        self.act('retro', metrics_id=m['id'])
        self.assertEqual(len(self.flow.observations()), 1)
        self.assertEqual(self.w['judgments'], frozen)
        with self.assertRaises(WorkflowError):
            self.act('judge-value')
        with self.assertRaises(WorkflowError):
            self.act('judge-cheat')

    def test_merge_exact_intervals_and_stale(self):
        self.ready()
        self.act('save-spoken')
        self.act('review')
        r = self.w['reviews'][-1]
        self.act('merge', review_id=r['id'], accepted_ids=[r['edits'][0]['id']])
        self.assertEqual(self.w['clean_script'], '这是具体的话')
        self.patch(spoken='新口述')
        with self.assertRaises(WorkflowError):
            self.act('merge', review_id=r['id'], accepted_ids=[])
        self.act('judge-value')
        self.patch(final={**self.w['final'], 'title':'改标题'})
        self.assertFalse(self.w['final']['confirmed'])
        self.assertTrue(self.w['judgments']['value'][0]['stale'])

    def test_automatic_names_manual_override_and_legacy_names(self):
        work = self.flow.create({})
        self.assertTrue(work['title'].startswith('新作品 '))
        self.assertEqual(work['title_mode'], 'auto')
        self.w = work
        self.patch(material='素材第一句。后面的一句。')
        self.assertEqual(self.w['title'], '素材第一句')
        self.assertIn('素材首句', self.w['title_source'])
        self.act('angles')
        self.act('choose-angle', angle_id='a')
        self.assertEqual(self.w['title'], '方向')
        self.assertEqual(next(item for item in self.flow.list() if item['id'] == self.w['id'])['title'], '方向')
        self.patch(title='我的名称', material='更新素材')
        self.assertEqual(self.w['title'], '我的名称')
        self.assertEqual(self.w['title_mode'], 'manual')
        self.patch(title=' ')
        self.assertEqual(self.w['title'], '方向')
        legacy = self.flow.store.update(self.w['id'], self.w['revision'],
            lambda item: (item.update(title='未命名作品', stage=1), item.pop('title_mode', None)))
        before = self.flow.store.path(legacy['id']).read_bytes()
        self.assertEqual(self.flow.get(legacy['id'])['title'], '方向')
        self.assertEqual(self.flow.get(legacy['id'])['stage'], 2)
        self.assertEqual(self.flow.store.path(legacy['id']).read_bytes(), before)

    def test_plan_staleness_and_legacy_plan_compatibility(self):
        self.patch(material='素材')
        self.act('angles')
        self.act('choose-angle', angle_id='a')
        self.act('plan')
        self.assertFalse(self.w['plan_stale'])
        self.patch(material='修改后的素材')
        self.assertTrue(self.w['plan_stale'])
        self.act('plan')
        self.assertFalse(self.w['plan_stale'])
        self.w = self.flow.store.update(self.w['id'], self.w['revision'], lambda item: item['plan'].pop('source_hash'))
        self.assertFalse(self.flow.get(self.w['id'])['plan_stale'])
        self.patch(material='旧方案之后的新素材')
        self.assertTrue(self.w['plan_stale'])

    def test_review_saves_spoken_once_and_reports_current_source(self):
        self.patch(spoken='这是重复的话')
        self.act('review')
        self.assertTrue(self.w['review_current'])
        self.assertEqual([item['text'] for item in self.w['spoken_versions']], ['这是重复的话'])
        self.act('review')
        self.assertEqual(len(self.w['spoken_versions']), 1)
        self.patch(spoken='这是新的重复口述')
        self.assertFalse(self.w['review_current'])
        self.act('review')
        self.assertEqual(len(self.w['spoken_versions']), 2)
        self.assertTrue(self.w['review_current'])

    def test_delete_published_work_keeps_snapshots_and_observation_references(self):
        self.test_lock_publish_metrics_retro_and_observation()
        observation = self.flow.observations()[0]
        next_work = self.flow.create({'applied_observation': observation})
        snapshots = {path: path.read_bytes() for path in (self.flow.store.root / 'snapshots').rglob('*.json')}
        self.flow.delete(self.w['id'], {'revision': self.w['revision']})
        self.assertEqual([item['id'] for item in self.flow.list()], [next_work['id']])
        self.assertEqual(self.flow.observations(), [])
        self.assertEqual(self.flow.get(next_work['id'])['applied_observation']['text'], observation['text'])
        continued = self.flow.patch(next_work['id'], next_work['revision'], {'applied_observation': {**next_work['applied_observation'], 'application': '继续保留这一项应用'}})
        self.assertEqual(continued['applied_observation']['application'], '继续保留这一项应用')
        self.assertEqual({path: path.read_bytes() for path in snapshots}, snapshots)

    def test_deleted_work_rejects_slow_model_commit(self):
        self.patch(material='要处理的素材')
        complete = self.model.complete
        def delete_during_call(task, system, inputs):
            self.flow.delete(self.w['id'], {'revision': self.w['revision']})
            return complete(task, system, inputs)
        self.model.complete = delete_during_call
        from app.storage import StorageError
        with self.assertRaises(StorageError) as error:
            self.act('angles')
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.flow.list(), [])

    def test_bad_review_and_model_failure_preserve_input(self):
        self.patch(spoken='重复重复')
        with self.assertRaises(WorkflowError):
            self.act('review')
        self.assertEqual(self.flow.get(self.w['id'])['reviews'], [])
        self.model.fail = True
        with self.assertRaises(WorkflowError) as err:
            self.act('angles')
        self.assertNotIn('secret', str(err.exception))
        self.assertEqual(self.flow.get(self.w['id'])['spoken'], '重复重复')

    def test_slow_model_revision_conflict_keeps_newer_content(self):
        self.patch(material='最初素材')
        revision=self.w['revision']
        complete=self.model.complete
        def simultaneous(task,system,inputs):
            self.flow.patch(self.w['id'],revision,{'material':'用户刚更新的素材'})
            return complete(task,system,inputs)
        self.model.complete=simultaneous
        with self.assertRaises(Conflict):
            self.act('angles')
        saved=self.flow.get(self.w['id'])
        self.assertEqual(saved['material'],'用户刚更新的素材')
        self.assertEqual(saved['angles'],[])

    def test_lock_reopen_and_tampered_snapshot_retained(self):
        self.ready();self.act('judge-value')
        self.act('lock-judgment',kind='value',judgment_id=self.w['judgments']['value'][0]['id'])
        reopened=Workflow(self.tmp.name,self.model).get(self.w['id'])
        self.assertEqual(reopened['judgments'],self.w['judgments'])
        path=next((Path(self.tmp.name)/'snapshots'/self.w['id']).glob('value-*.json'))
        original=path.read_bytes()
        altered=json.loads(original);altered['result']['report']='被改过'
        path.write_text(json.dumps(altered),encoding='utf-8')
        from app.storage import StorageError
        with self.assertRaises(StorageError):self.flow.get(self.w['id'])
        self.assertEqual(json.loads(path.read_text('utf-8'))['result']['report'],'被改过')

    def test_early_vs_full_window_and_comment_reason(self):
        self.ready()
        for kind in ('value','cheat'):
            self.act('judge-'+kind)
            self.act('lock-judgment',kind=kind,judgment_id=self.w['judgments'][kind][-1]['id'])
        self.act('publish',platform='抖音',account='我的账号',published_at='2026-10-01T00:00:00+08:00',window_hours=72)
        self.act('metrics',publication_id=self.w['publications'][0]['id'],observed_at='2026-10-04T00:00:00+08:00',plays=0)
        metric=self.w['metrics'][-1]
        self.assertFalse(metric['early']);self.assertTrue(metric['eligible_same_window'])
        self.assertEqual(metric['window_hours'],72)
        with self.assertRaises(WorkflowError):self.act('retro',metrics_id=metric['id'])
        self.assertEqual(self.w['retros'],[])

    def test_overlapping_edits_and_invalid_probabilities_rejected(self):
        self.ready()
        def bad(task,system,inputs):
            if task=='review':return {'report':'检查','edits':[{'original':'这是重复','replacement':'','reason':'改','risk':'low'},{'original':'重复的话','replacement':'','reason':'改','risk':'low'}]}
            if task=='cheat-predict':return {'report':'报告','bucket':'基础盘','center':1,'hypotheses':[],'counterfactuals':[],'probabilities':[{'bucket':'基础盘','probability':99}]}
            return MockModel().complete(task,system,inputs)
        self.model.complete=bad
        with self.assertRaises(WorkflowError):self.act('review')
        with self.assertRaises(WorkflowError):self.act('judge-cheat')
        saved=self.flow.get(self.w['id'])
        self.assertEqual(saved['reviews'],[]);self.assertEqual(saved['judgments']['cheat'],[])

    def test_no_sneaked_model_metadata_or_other_judgment(self):
        self.ready()
        complete=self.model.complete
        def dishonest(task,system,inputs):
            result=json.loads(complete(task,system,inputs))
            if task in ('value','cheat-predict'):
                result.update(baseline_plays=999,calibration_samples=40,rubric_version='v2')
            return result
        self.model.complete=dishonest
        self.act('judge-cheat');self.act('judge-value')
        for kind in ('cheat','value'):
            result=self.w['judgments'][kind][0]['result']
            self.assertIsNone(result['baseline_plays']);self.assertEqual(result['calibration_samples'],0)
            self.assertEqual(result['rubric_version'],'v0')
        value_payload=self.model.calls[-1][2]
        self.assertNotIn('blind_score',value_payload)
        self.assertNotIn('bucket_priors',self.model.calls[0][2]['rules'])

    def test_actions_preserve_explicit_navigation_stage(self):
        self.ready();self.patch(stage=0)
        self.act('angles');self.assertEqual(self.w['stage'],0)
        self.act('choose-angle',angle_id='a');self.assertEqual(self.w['stage'],0)
        self.act('plan');self.assertEqual(self.w['stage'],0)
        self.act('review');self.assertEqual(self.w['stage'],0)
        r=self.w['reviews'][-1]
        self.act('merge',review_id=r['id'],accepted_ids=[]);self.assertEqual(self.w['stage'],0)
        self.act('confirm-final');self.assertEqual(self.w['stage'],0)
        for kind in ('value','cheat'):
            self.act('judge-'+kind)
            self.act('lock-judgment',kind=kind,judgment_id=self.w['judgments'][kind][-1]['id'])
        self.act('publish',platform='抖音',account='我的账号',published_at='2026-10-01T00:00',window_hours=72)
        self.assertEqual(self.w['stage'],0)
        self.act('metrics',publication_id=self.w['publications'][0]['id'],observed_at='2026-10-04T01:00',plays=0,comments_unavailable_reason='无评论')
        self.assertEqual(self.w['stage'],0)
        self.assertTrue(self.w['metrics'][-1]['eligible_same_window'])
        self.act('retro',metrics_id=self.w['metrics'][-1]['id']);self.assertEqual(self.w['stage'],0)

    def test_late_metrics_are_not_same_window(self):
        self.ready()
        for kind in ('value','cheat'):
            self.act('judge-'+kind)
            self.act('lock-judgment',kind=kind,judgment_id=self.w['judgments'][kind][-1]['id'])
        self.act('publish',platform='抖音',account='我的账号',published_at='2026-10-01T00:00',window_hours=24)
        self.act('metrics',publication_id=self.w['publications'][0]['id'],observed_at='2026-10-08T00:00',plays=0)
        metric=self.w['metrics'][-1]
        self.assertFalse(metric['eligible_same_window']);self.assertTrue(metric['late'])
        self.assertFalse(metric['early']);self.assertEqual(metric['tolerance_hours'],1.2)

    def test_blind_system_contains_generic_rule_only(self):
        self.ready();self.act('judge-cheat')
        system=self.model.calls[0][1]
        self.assertIn('综合分公式',system)
        self.assertNotIn('先验概率',system)
        self.assertNotIn('480',system)
        self.assertNotIn('Phase 0',system)

    def test_locked_snapshot_retry_after_work_write_failure(self):
        self.ready();self.act('judge-value')
        judgment=self.w['judgments']['value'][0]
        from app.storage import atomic_json
        def fail_work(path,value):
            if path.parent.name=='works':raise OSError('simulated disk failure')
            return atomic_json(path,value)
        with patch('app.storage.atomic_json',side_effect=fail_work):
            with self.assertRaises(OSError):self.act('lock-judgment',kind='value',judgment_id=judgment['id'])
        frozen_path=next((Path(self.tmp.name)/'snapshots'/self.w['id']).glob('value-*.json'))
        frozen=json.loads(frozen_path.read_text('utf-8'))
        count=len(self.model.calls)
        with patch('app.workflow.now',return_value='2026-11-04T23:59:59+08:00'):
            self.act('lock-judgment',kind='value',judgment_id=judgment['id'])
        self.assertEqual(self.w['judgments']['value'][0]['locked_at'],frozen['locked_at'])
        self.assertEqual(self.w['judgments']['value'][0]['result'],judgment['result'])
        self.assertEqual(len(self.model.calls),count)

    def test_clean_script_manual_save(self):
        self.patch(clean_script='用户手改合并稿')
        self.assertEqual(self.w['clean_script'],'用户手改合并稿')

    def test_prediction_bucket_center_and_hypothesis_schema(self):
        self.ready()
        complete=self.model.complete
        for error in ('missing_bucket','wrong_center','bad_hypothesis'):
            def invalid(task,system,inputs):
                result=json.loads(complete(task,system,inputs))
                if task=='cheat-predict':
                    if error=='missing_bucket':result['probabilities']=[{'bucket':'基础盘','probability':100}]
                    elif error=='wrong_center':result['center']=50000
                    else:result['hypotheses']=[{}]
                return result
            self.model.complete=invalid
            with self.assertRaises(WorkflowError):self.act('judge-cheat')
        self.assertEqual(self.flow.get(self.w['id'])['judgments']['cheat'],[])

    def test_comment_likes_reject_fractional_count(self):
        self.ready()
        for kind in ('value','cheat'):
            self.act('judge-'+kind)
            self.act('lock-judgment',kind=kind,judgment_id=self.w['judgments'][kind][-1]['id'])
        self.act('publish',platform='抖音',account='我的账号',published_at='2026-10-01T00:00',window_hours=72)
        with self.assertRaises(WorkflowError):
            self.act('metrics',publication_id=self.w['publications'][0]['id'],observed_at='2026-10-04T00:00',comments=[{'text':'评论','likes':1.5}])
        self.assertEqual(self.flow.get(self.w['id'])['metrics'],[])

    def test_retro_rejects_bad_comparisons_without_committing(self):
        self.ready()
        for kind in ('value', 'cheat'):
            self.act('judge-' + kind)
            self.act('lock-judgment', kind=kind, judgment_id=self.w['judgments'][kind][-1]['id'])
        self.act('publish', platform='抖音', account='我的账号', published_at='2026-10-01T00:00', window_hours=72)
        self.act('metrics', publication_id=self.w['publications'][0]['id'], observed_at='2026-10-04T00:00', plays=100, comments_unavailable_reason='暂无评论')
        metric_id = self.w['metrics'][-1]['id']
        revision = self.w['revision']
        complete = self.model.complete
        valid = [{'kind': kind, 'hypothesis': '原判断', 'verdict': '待验证', 'evidence': '同窗记录'} for kind in ('value', 'cheat')]
        for comparisons in ([None], [7], [{'kind': 'other', 'hypothesis': 'a', 'verdict': 'b', 'evidence': 'c'}],
                            [{'kind': 'value', 'hypothesis': 'a', 'verdict': 'b'}]):
            def response(task, system, inputs):
                if task == 'retro':
                    return json.dumps({'report': '复盘', 'comparisons': comparisons, 'observations': []}, ensure_ascii=False)
                return complete(task, system, inputs)
            self.model.complete = response
            with self.assertRaises(WorkflowError):
                self.act('retro', metrics_id=metric_id)
            saved = self.flow.get(self.w['id'])
            self.assertEqual(saved['revision'], revision)
            self.assertEqual(saved['retros'], [])
        self.model.complete = lambda task, system, inputs: json.dumps({'report': '复盘', 'comparisons': valid, 'observations': []}, ensure_ascii=False) if task == 'retro' else complete(task, system, inputs)
        self.act('retro', metrics_id=metric_id)
        self.assertEqual(self.w['retros'][-1]['comparisons'], valid)

    def test_request_provenance_uses_sent_settings_for_each_call(self):
        settings = Settings(self.tmp.name)
        settings.update({'provider': 'chat', 'base_url': 'https://chat.example/v1', 'model': 'request-model', 'effort': 'low', 'key': 'first-key'})
        flow = Workflow(self.tmp.name, ModelClient(settings))
        work = flow.create({'title': '来源测试'})
        work = flow.patch(work['id'], work['revision'], {'material': '素材'})
        tasks = ['angles', 'angles', 'blind-score', 'cheat-predict']
        sent = []
        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return self.body
        def opened(request, **kwargs):
            index = len(sent)
            sent.append((request.full_url, json.loads(request.data)))
            task = tasks[index]
            if index == 0:
                settings.update({'provider': 'responses', 'base_url': 'https://response.example/v1', 'model': 'changed-during-request', 'effort': 'high', 'key': 'second-key'})
            if index == 2:
                settings.update({'provider': 'chat', 'base_url': 'https://next.example/v1', 'model': 'prediction-model', 'effort': 'off', 'key': 'third-key'})
            content = MockModel().complete(task, '', {})
            body = {'choices': [{'message': {'content': content}}]} if request.full_url.endswith('/chat/completions') else {'output': [{'content': [{'type': 'output_text', 'text': content}]}]}
            return Response(json.dumps(body, ensure_ascii=False).encode('utf-8'))
        with patch('urllib.request.urlopen', side_effect=opened):
            work = flow.action(work['id'], 'angles', {'revision': work['revision']})
            first = work['method_runs'][-1]['provenance']
            work = flow.action(work['id'], 'angles', {'revision': work['revision']})
            second = work['method_runs'][-1]['provenance']
            final = {'script': '实际正文', 'title': '标题', 'cover': '封面', 'opening': '开头', 'platform': '抖音', 'account': '账号', 'duration_seconds': 60}
            work = flow.patch(work['id'], work['revision'], {'final': final})
            work = flow.action(work['id'], 'confirm-final', {'revision': work['revision']})
            work = flow.action(work['id'], 'judge-cheat', {'revision': work['revision']})
        self.assertEqual((first['provider'], first['base_url'], first['model'], first['effort']), ('chat', 'https://chat.example/v1', 'request-model', 'low'))
        self.assertEqual((second['provider'], second['model'], second['effort']), ('responses', 'changed-during-request', 'high'))
        cheat = work['judgments']['cheat'][-1]['provenance']
        self.assertEqual((cheat['blind_score_provenance']['provider'], cheat['blind_score_provenance']['model']), ('responses', 'changed-during-request'))
        self.assertEqual((cheat['provider'], cheat['base_url'], cheat['model'], cheat['effort']), ('chat', 'https://next.example/v1', 'prediction-model', 'off'))
        self.assertEqual(sent[0][1]['model'], 'request-model')
        self.assertEqual(sent[1][1]['model'], 'changed-during-request')
        self.assertEqual(sent[2][1]['model'], 'changed-during-request')
        self.assertEqual(sent[3][1]['model'], 'prediction-model')

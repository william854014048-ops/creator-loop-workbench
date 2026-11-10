import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.model import Settings, ModelClient, ModelError


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(self.tmp.name)

    def test_provider_keys_are_separate_and_bound_to_address(self):
        self.settings.update({'provider':'chat','base_url':'https://chat.example/v1','model':'m','effort':'low','key':'CHAT-SECRET'})
        self.assertTrue(self.settings.public()['ready'])
        self.settings.update({'provider':'responses','base_url':'https://response.example/v1','model':'r','key':'RESPONSE-SECRET'})
        self.assertEqual(self.settings.credentials()[1], 'RESPONSE-SECRET')
        self.settings.update({'provider':'chat'})
        self.assertEqual(self.settings.credentials()[1], 'CHAT-SECRET')
        self.settings.update({'base_url':'https://else.example/v1'})
        self.assertFalse(self.settings.public()['ready'])
        self.assertNotIn('SECRET', json.dumps(self.settings.public()))
        self.assertNotIn('SECRET', (Path(self.tmp.name)/'settings.json').read_text('utf-8'))

    def test_new_stateless_requests_and_safe_errors(self):
        self.settings.update({'provider':'chat','base_url':'https://chat.example/v1','model':'m','key':'secret'})
        captured=[]
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,*args): return b'{"choices":[{"message":{"content":"{}"}}]}'
        def opened(req, **kwargs):
            captured.append(json.loads(req.data))
            return Response()
        with patch('urllib.request.urlopen', side_effect=opened):
            client=ModelClient(self.settings)
            client.complete('value','rules',{'final':{'script':'text'}})
            client.complete('blind-score','other',{'final':{'script':'text'}})
        self.assertEqual(len(captured[1]['messages']),2)
        self.assertNotIn('conversation', captured[1])
        self.assertNotIn('previous_response_id', captured[1])
        with patch('urllib.request.urlopen', side_effect=RuntimeError('secret')):
            with self.assertRaises(ModelError) as err:
                client.complete('value','rules',{})
        self.assertNotIn('secret', str(err.exception))

    def test_responses_are_fresh_and_authorization_not_redirected(self):
        self.settings.update({'provider':'responses','base_url':'https://response.example/v1','model':'r','key':'secret'})
        requests=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"output":[{"content":[{"type":"output_text","text":"{}"}]}]}'
        def opened(req,**kwargs):requests.append(req);return Response()
        with patch('urllib.request.urlopen',side_effect=opened):
            client=ModelClient(self.settings)
            client.complete('blind-score','scoring',{})
            client.complete('cheat-predict','prediction',{})
        payload=json.loads(requests[1].data)
        self.assertEqual(len(payload['input']),1)
        self.assertFalse(payload['store'])
        self.assertNotIn('previous_response_id',payload)
        self.assertNotIn('conversation',payload)
        self.assertNotIn('Authorization',requests[1].headers)
        self.assertEqual(requests[1].unredirected_hdrs['Authorization'],'Bearer secret')

    def test_profile_read_does_not_switch_active_provider(self):
        self.settings.update({'provider':'responses','model':'saved-r'})
        self.settings.update({'provider':'chat','model':'saved-c'})
        self.assertEqual(self.settings.public('responses')['model'],'saved-r')
        self.assertEqual(self.settings.public()['provider'],'chat')

    def test_unsaved_connection_candidate_does_not_persist(self):
        self.settings.update({'provider':'chat','model':'saved'})
        before=self.settings.path.read_bytes()
        with patch.object(ModelClient,'complete',return_value='{}') as complete:
            ModelClient(self.settings).test({'provider':'responses','model':'unsaved','base_url':'https://test.example/v1','key':'temporary-secret'})
        self.assertEqual(complete.call_count,1)
        self.assertEqual(self.settings.path.read_bytes(),before)
        self.assertEqual(self.settings.public()['model'],'saved')

    def test_remote_http_rejected_and_local_allowed(self):
        with self.assertRaises(ModelError):self.settings.update({'base_url':'http://remote.example/v1'})
        self.settings.update({'base_url':'http://127.0.0.1:8000/v1'})

    def test_effort_translation_and_deepseek_token_field(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"choices":[{"message":{"content":"{}"}}],"output":[{"content":[{"type":"output_text","text":"{}"}]}]}'
        captured=[]
        def opened(req,**kwargs):captured.append(json.loads(req.data));return Response()
        self.settings.update({'provider':'chat','model':'deepseek-chat','base_url':'https://api.deepseek.com/v1','effort':'default','key':'secret'})
        with patch('urllib.request.urlopen',side_effect=opened):
            client=ModelClient(self.settings);client.complete('value','s',{})
            self.assertNotIn('reasoning_effort',captured[-1]);self.assertIn('max_tokens',captured[-1])
            self.settings.update({'effort':'off'});client.complete('value','s',{})
            self.assertEqual(captured[-1]['thinking'],{'type':'disabled'})
            self.settings.update({'provider':'responses','model':'r','effort':'off','key':'secret'})
            client.complete('value','s',{});self.assertEqual(captured[-1]['reasoning'],{'effort':'none'})
            self.settings.update({'effort':'default'});client.complete('value','s',{})
            self.assertNotIn('reasoning',captured[-1])
        with self.assertRaises(ModelError):self.settings.update({'effort':'unvalidated'})

    def test_official_openai_chat_uses_completion_token_limit(self):
        self.settings.update({'provider':'chat','base_url':'https://api.openai.com/v1','model':'gpt-test','key':'secret'})
        captured=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"choices":[{"message":{"content":"{}"}}]}'
        def opened(req,**kwargs):captured.append(json.loads(req.data));return Response()
        with patch('urllib.request.urlopen',side_effect=opened):
            ModelClient(self.settings).complete('value','s',{})
        self.assertEqual(captured[0]['max_completion_tokens'],32768)
        self.assertNotIn('max_tokens',captured[0])


    def test_named_profiles_independent_and_preview(self):
        one=self.settings.create({'name':'主力','provider':'chat','model':'m','key':'first'})
        two=self.settings.create({'name':'备用','provider':'chat','model':'m','key':'second'})
        self.assertNotEqual(one['profile_id'],two['profile_id'])
        self.settings.activate(one['profile_id'])
        self.assertEqual(self.settings.credentials()[1],'first')
        self.settings.update({'name':'主力重命名','max_output_tokens':48000})
        reopened=Settings(self.tmp.name)
        self.assertEqual(reopened.public()['name'],'主力重命名')
        self.assertEqual(reopened.credentials()[1],'first')
        before=reopened.path.read_bytes()
        preview=reopened.preview({'new_profile':True,'name':'未保存','provider':'chat','model':'m'})
        self.assertFalse(preview.public()['ready'])
        self.assertEqual(reopened.path.read_bytes(),before)
        self.assertEqual(reopened.public(profile_id=two['profile_id'])['name'],'备用')
        self.assertEqual(reopened.public()['profile_id'],one['profile_id'])

    def test_profile_validation_leaves_disk_unchanged(self):
        self.settings.create({'name':'测试','provider':'chat'})
        before=self.settings.path.read_bytes()
        for bad in (0,1023,131073,True,1.2,'32768'):
            with self.assertRaises(ModelError):self.settings.update({'max_output_tokens':bad})
        with self.assertRaises(ModelError):self.settings.create({'name':'测试'})
        with self.assertRaises(ModelError):self.settings.activate('missing')
        self.assertEqual(self.settings.path.read_bytes(),before)

    def test_limit_diagnostics_and_budget_provenance(self):
        self.settings.update({'model':'m','key':'secret','max_output_tokens':48000})
        captured=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps({'choices':[{'finish_reason':'length','message':{'content':'PRIVATE'}}],'usage':{'total_tokens':48000,'untrusted':'SECRET'}}).encode()
        def opened(req,**kwargs):captured.append(json.loads(req.data));return Response()
        client=ModelClient(self.settings)
        with patch('urllib.request.urlopen',side_effect=opened):
            with self.assertRaisesRegex(ModelError,'输出预算'):client.complete('value','PRIVATE',{'PRIVATE':'SECRET'})
        self.assertEqual(captured[0]['max_completion_tokens'],48000)
        log=client.log_path.read_text('utf-8')
        self.assertNotIn('PRIVATE',log);self.assertNotIn('SECRET',log)
        row=json.loads(log);self.assertEqual(row['outcome'],'output_limit');self.assertEqual(row['usage']['total_tokens'],48000)
        with patch.object(client,'_complete',return_value='{}'):
            _,provenance=client.complete_with_provenance('value','s',{})
        self.assertEqual(provenance['max_output_tokens'],48000)
        self.assertEqual(provenance['profile_id'],self.settings.public()['profile_id'])

    def test_legacy_migration_preserves_ciphertext_and_blank_slot(self):
        self.settings.update({'model':'saved','key':'legacy-secret'})
        chat=self.settings.data['profiles'][0].copy()
        for field in ('id','name','provider','max_output_tokens'):chat.pop(field)
        legacy={'provider':'chat','chat':chat,'responses':{'base_url':'https://api.openai.com/v1','model':'','effort':''}}
        self.settings.path.write_text(json.dumps(legacy),encoding='utf-8')
        original=self.settings.path.read_bytes()
        migrated=Settings(self.tmp.name)
        self.assertEqual(migrated.path.read_bytes(),original)
        self.assertEqual(migrated.credentials()[1],'legacy-secret')
        self.assertFalse(migrated.public('responses')['ready'])
        self.assertEqual(migrated.data['profiles'][0]['encrypted_key'],chat['encrypted_key'])
        migrated.update({'name':'Migrated'})
        self.assertEqual(migrated.path.with_name('settings.v1.encrypted-backup.json').read_bytes(),original)
        self.assertEqual(Settings(self.tmp.name).credentials()[1],'legacy-secret')

    def test_corrupt_v2_never_overwritten(self):
        for malformed in ({'version':2,'profiles':[]}, {'version':2,'profiles':[{}],'active_profile_id':'missing'}):
            self.settings.path.write_text(json.dumps(malformed),encoding='utf-8')
            before=self.settings.path.read_bytes()
            with self.assertRaises(ModelError):Settings(self.tmp.name)
            self.assertEqual(self.settings.path.read_bytes(),before)

    def test_changing_selected_provider_clears_only_its_key(self):
        first=self.settings.create({'name':'First','model':'m','key':'first'})
        second=self.settings.create({'name':'Second','model':'m','key':'second'})
        result=self.settings.update({'profile_id':first['id'],'provider':'responses'})
        self.assertFalse(result['ready'])
        self.settings.activate(second['id'])
        self.assertEqual(self.settings.credentials()[1],'second')

    def test_connection_probe_uses_independent_small_budget(self):
        self.settings.update({'provider':'chat','base_url':'https://api.deepseek.com/v1','model':'deepseek-chat','effort':'high','key':'secret'})
        before=self.settings.path.read_bytes()
        captured=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}'
        def opened(req,**kwargs):captured.append(json.loads(req.data));return Response()
        with patch('urllib.request.urlopen',side_effect=opened):ModelClient(self.settings).test()
        self.assertEqual(captured[0]['max_tokens'],1024)
        self.assertEqual(captured[0]['thinking'],{'type':'disabled'})
        self.assertEqual(self.settings.path.read_bytes(),before)
        self.assertEqual(self.settings.public()['effort'],'high')

    def test_responses_budget_limit_and_success_provenance_bound_to_sent_config(self):
        original=self.settings.update({'provider':'responses','model':'r','effort':'high','key':'secret'})
        class Response:
            body={'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},'output':[]}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps(self.body).encode()
        response=Response()
        client=ModelClient(self.settings)
        with patch('urllib.request.urlopen',return_value=response) as opened:
            with self.assertRaisesRegex(ModelError,'输出预算'):client.complete('package','s',{})
            self.assertEqual(opened.call_count,1)
            sent=json.loads(opened.call_args.args[0].data)
            self.assertEqual(sent['max_output_tokens'],32768)
            self.assertEqual(sent['reasoning'],{'effort':'high'})
        response.body={'status':'completed','output':[{'content':[{'type':'output_text','text':'{}'}]}]}
        def switched(req,**kwargs):
            self.settings.update({'model':'changed','max_output_tokens':64000})
            return response
        with patch('urllib.request.urlopen',side_effect=switched):
            result,provenance=client.complete_with_provenance('package','s',{})
        self.assertEqual(result,'{}');self.assertEqual(provenance['model'],'r')
        self.assertEqual(provenance['max_output_tokens'],32768)
        self.assertEqual(provenance['profile_id'],original['id'])
        row=json.loads(client.log_path.read_text('utf-8').splitlines()[-1])
        self.assertEqual(row['correlation_id'],provenance['correlation_id'])
        self.assertEqual(row['task'],'package');self.assertEqual(row['status'],'completed')

    def test_timeout_truncation_and_http_errors_logged_without_secrets(self):
        import urllib.error
        self.settings.update({'model':'m','key':'secret'})
        client=ModelClient(self.settings)
        class Truncated:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"choices":'
        cases=[TimeoutError('SECRET'),urllib.error.HTTPError('https://private?key=SECRET',429,'SECRET',{},None),None]
        for error in cases:
            kwargs={'side_effect':error} if error else {'return_value':Truncated()}
            with patch('urllib.request.urlopen',**kwargs) as opened:
                with self.assertRaises(ModelError) as caught:client.complete('review','PRIVATE',{'script':'PRIVATE'})
                self.assertEqual(opened.call_count,1)
                self.assertEqual(opened.call_args.kwargs['timeout'],240)
                self.assertNotIn('SECRET',str(caught.exception))
        text=client.log_path.read_text('utf-8')
        self.assertNotIn('SECRET',text);self.assertNotIn('PRIVATE',text)
        rows=[json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[1]['http_status'],429)

    def test_bounded_logs_and_unwritable_log_does_not_fail_call(self):
        from app.model_log import write_diagnostic
        log=Path(self.tmp.name)/'bounded.jsonl'
        with patch('app.model_log.MAX_BYTES',100):
            for _ in range(20):write_diagnostic(log,{'outcome':'success','usage':{}})
        self.assertLessEqual(log.stat().st_size,100)
        self.assertLessEqual(log.with_name(log.name+'.1').stat().st_size,100)
        blocked=Path(self.tmp.name)/'blocked'
        blocked.write_text('file',encoding='utf-8')
        self.settings.update({'model':'m','key':'secret'})
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}'
        with patch('urllib.request.urlopen',return_value=Response()):
            self.assertEqual(ModelClient(self.settings,blocked/'log.jsonl').complete('value','s',{}),'{}')

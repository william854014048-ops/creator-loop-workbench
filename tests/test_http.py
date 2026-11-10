import json
import http.client
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
from server import create_server


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.server = create_server(port=0, data_root=self.tmp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def req(self, path, body=None, method=None, headers=None):
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(), method=method, headers=headers or {})
        try:
            response = urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            response = exc
        return response.status, json.loads(response.read())

    def test_health_offline_save_and_revision(self):
        self.assertEqual(self.req('/api/health')[1]['application'], 'creator-loop')
        self.assertEqual(self.req('/api/works', {})[0], 403)
        headers = {'X-Creator-Loop':'local'}
        code, w = self.req('/api/works', {'title':'离线'}, headers=headers)
        self.assertEqual(code, 201)
        code, saved = self.req('/api/works/' + w['id'], {'revision':1,'patch':{'material':'原话'}}, method='PUT', headers=headers)
        self.assertEqual(code, 200)
        code, err = self.req('/api/works/' + w['id'], {'revision':1,'patch':{'material':'覆盖'}}, method='PUT', headers=headers)
        self.assertEqual(code, 409)
        self.assertEqual(err['work']['material'], '原话')
        self.assertIn('key_status', self.req('/api/settings')[1])

    def test_host_origin_and_paths(self):
        self.assertEqual(self.req('/api/health', headers={'Host':'evil.test'})[0], 403)
        self.assertEqual(self.req('/api/health', headers={'Origin':'https://evil.test'})[0], 403)
        self.assertEqual(self.req('/api/works/../config')[0], 404)
        self.assertEqual(self.req('/config/settings.json')[0],404)
        self.assertEqual(self.req('/skills/source-manifest.json')[0],404)

    def test_delete_requires_local_header_revision_and_keeps_old_work_hidden(self):
        headers = {'X-Creator-Loop': 'local'}
        _, work = self.req('/api/works', {}, headers=headers)
        path = '/api/works/' + work['id']
        self.assertEqual(self.req(path, {'revision': work['revision']}, method='DELETE')[0], 403)
        self.assertEqual(self.req(path, {}, method='DELETE', headers=headers)[0], 400)
        self.assertEqual(self.req(path, {'revision': 0}, method='DELETE', headers=headers)[0], 409)
        self.assertEqual(self.req(path, {'revision': work['revision']}, method='DELETE', headers={**headers, 'Origin': 'https://example.com'})[0], 403)
        for _ in range(2):
            self.assertEqual(self.req(path, {'revision': work['revision']}, method='DELETE', headers=headers)[0], 200)
        self.assertEqual(self.req(path)[0], 404)
        self.assertEqual(self.req('/api/works')[1]['works'], [])

    def test_version_matches_change_log(self):
        from app import __version__
        self.assertEqual(self.req('/api/health')[1]['version'], __version__)
        code, history = self.req('/api/changelog')
        self.assertEqual(code, 200)
        self.assertEqual(history['version'], __version__)
        self.assertIn('## ' + __version__, history['report'])

    def test_large_invalid_body_and_protected_fields(self):
        headers={'X-Creator-Loop':'local'}
        code,w=self.req('/api/works',{},headers=headers)
        self.assertEqual(code,201)
        code,err=self.req('/api/works/'+w['id'],{'revision':1,'patch':{'publications':[]}},method='PUT',headers=headers)
        self.assertEqual(code,400)
        # A declared oversized request is rejected before allocating/reading its body.
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        connection.putrequest('POST','/api/works')
        connection.putheader('X-Creator-Loop','local')
        connection.putheader('Content-Length',str(2*1024*1024+1))
        connection.endheaders()
        response=connection.getresponse()
        self.assertEqual(response.status,413)
        self.assertIn('error',json.loads(response.read()))
        connection.close()

    def test_settings_profile_query_and_unsaved_connection_body(self):
        headers={'X-Creator-Loop':'local'}
        self.assertEqual(self.req('/api/settings',{'provider':'responses','model':'saved-r'},method='PUT',headers=headers)[0],200)
        self.assertEqual(self.req('/api/settings',{'provider':'chat','model':'saved-c'},method='PUT',headers=headers)[0],200)
        code,profile=self.req('/api/settings?provider=responses')
        self.assertEqual(code,200);self.assertEqual(profile['model'],'saved-r')
        self.assertEqual(self.req('/api/settings')[1]['provider'],'chat')
        before=self.server.settings.path.read_bytes()
        with patch('app.model.ModelClient.complete',autospec=True,return_value='{}') as complete:
            code,result=self.req('/api/settings/test',{'provider':'responses','model':'unsaved-r','key':'temp-secret'},headers=headers)
        self.assertEqual(code,200);self.assertTrue(result['ok'])
        tested=complete.call_args.args[0].settings
        self.assertEqual(tested.public()['model'],'unsaved-r')
        self.assertEqual(tested.credentials()[1],'temp-secret')
        self.assertEqual(self.server.settings.path.read_bytes(),before)

    def test_named_profiles_create_select_query_and_invalid_requests(self):
        headers={'X-Creator-Loop':'local'}
        code,one=self.req('/api/settings/profiles',{'name':'主力','provider':'chat','model':'one','key':'FIRST'},headers=headers)
        self.assertEqual(code,201)
        code,two=self.req('/api/settings/profiles',{'name':'备用','provider':'chat','model':'two','key':'SECOND'},headers=headers)
        self.assertEqual(code,201)
        code,selected=self.req('/api/settings?profile_id='+one['id'])
        self.assertEqual(code,200);self.assertEqual(selected['profile_id'],one['id'])
        self.assertEqual(selected['active_profile_id'],two['id'])
        self.assertNotIn('FIRST',json.dumps(selected));self.assertNotIn('encrypted_key',json.dumps(selected))
        code,active=self.req('/api/settings/activate',{'profile_id':one['id']},headers=headers)
        self.assertEqual(code,200);self.assertEqual(active['active_profile_id'],one['id'])
        self.assertEqual(self.server.settings.credentials()[1],'FIRST')
        before=self.server.settings.path.read_bytes()
        with patch('app.model.ModelClient.complete',return_value='{}') as complete:
            code,_=self.req('/api/settings/test',{'new_profile':True,'name':'预览','provider':'chat','model':'candidate','key':'TEMP'},headers=headers)
        self.assertEqual(code,200);self.assertEqual(complete.call_count,1)
        self.assertEqual(self.server.settings.path.read_bytes(),before)
        self.assertEqual(self.req('/api/settings/profiles',{'name':'主力'},headers=headers)[0],400)
        self.assertEqual(self.req('/api/settings/activate',{'profile_id':'missing'},headers=headers)[0],400)
        self.assertEqual(self.req('/api/settings?profile_id=missing')[0],400)
        self.assertEqual(self.server.settings.path.read_bytes(),before)

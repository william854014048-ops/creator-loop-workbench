"""Local-only creator-loop HTTP server, no third-party runtime packages."""
import argparse
import json
import mimetypes
from pathlib import Path
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
import webbrowser
from app import __version__
from app.model import Settings, ModelClient, ModelError
from app.storage import Conflict, StorageError
from app.workflow import Workflow, WorkflowError

ROOT = Path(__file__).resolve().parent
STATIC = {'/':'index.html','/index.html':'index.html','/style.css':'style.css','/styles.css':'styles.css','/app.js':'app.js'}
MAX_BODY = 2 * 1024 * 1024


def create_server(host='127.0.0.1', port=8780, data_root=None, model=None):
    if host not in ('127.0.0.1','localhost','::1'):
        raise ValueError('服务仅允许本机地址。')
    root = Path(data_root).resolve() if data_root else ROOT/'data'
    settings = Settings(root)
    client = ModelClient(settings, (root / 'runtime' if data_root else ROOT / 'runtime') / 'model-diagnostics.jsonl')
    workflow = Workflow(root, model if model is not None else client)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'CreatorLoop/1'
        def log_message(self, format, *args):
            # No request URL/body, credentials, content or upstream errors in logs.
            pass

        def json_response(self, status, value):
            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(data)

        def validate(self):
            allowed_hosts = {'127.0.0.1','localhost','[::1]'}
            host_header = self.headers.get('Host','')
            expected = {h + ':' + str(self.server.server_port) for h in allowed_hosts}
            if self.server.server_port==80: expected |= allowed_hosts
            if host_header not in expected:
                self.json_response(403,{'error':'此服务只接受本机访问。'})
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in {'http://' + h for h in expected}:
                self.json_response(403,{'error':'访问来源无效，请使用本机页面。'})
                return False
            if self.headers.get('Sec-Fetch-Site')=='cross-site':
                self.json_response(403,{'error':'跨站访问已拒绝。'})
                return False
            if self.command in ('POST','PUT','DELETE') and self.headers.get('X-Creator-Loop')!='local':
                self.json_response(403,{'error':'保存请求缺少本机页面标记，请刷新页面。'})
                return False
            return True

        def body(self):
            if self.headers.get('Transfer-Encoding'):
                raise WorkflowError('请求传输格式不受支持。')
            try:
                size = int(self.headers.get('Content-Length','0'))
            except ValueError:
                raise WorkflowError('请求长度无效。') from None
            if size < 0 or size > MAX_BODY:
                err=WorkflowError('内容超过2MB，请缩短后保存。');err.status=413;raise err
            try:
                value=json.loads(self.rfile.read(size).decode('utf-8'))
            except (ValueError,UnicodeError):
                raise WorkflowError('请求须为 UTF-8 JSON。') from None
            if not isinstance(value,dict): raise WorkflowError('请求须为 JSON 对象。')
            return value

        def route(self):
            if not self.validate(): return
            path=urllib.parse.urlsplit(self.path).path
            try:
                if self.command=='GET' and path=='/api/health':
                    return self.json_response(200,{'ok':True,'application':'creator-loop','version':__version__})
                if self.command=='GET' and path=='/api/changelog':
                    return self.json_response(200,{'version':__version__,'report':(ROOT/'CHANGELOG.md').read_text(encoding='utf-8')})
                if path=='/api/works':
                    if self.command=='GET': return self.json_response(200,{'works':workflow.list()})
                    if self.command=='POST':return self.json_response(201,workflow.create(self.body()))
                match=re.fullmatch(r'/api/works/([0-9a-f]{32})(?:/actions/([a-z-]+))?',path)
                if match:
                    work_id,action=match.groups()
                    if action is not None and self.command=='POST':
                        return self.json_response(200,workflow.action(work_id,action,self.body()))
                    if action is None:
                        if self.command=='GET': return self.json_response(200,workflow.get(work_id))
                        if self.command=='DELETE': return self.json_response(200,workflow.delete(work_id,self.body()))
                        if self.command=='PUT':
                            body=self.body()
                            if set(body)-{'revision','patch'}:raise WorkflowError('作品保存字段无效。')
                            return self.json_response(200,workflow.patch(work_id,body.get('revision'),body.get('patch')))
                if path=='/api/observations' and self.command=='GET':
                    return self.json_response(200,{'observations':workflow.observations()})
                if path=='/api/settings':
                    if self.command=='GET':
                        query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                        provider=query.get('provider',[None])[0]
                        return self.json_response(200,settings.public(provider,query.get('profile_id',[None])[0]))
                    if self.command=='PUT':return self.json_response(200,settings.update(self.body()))
                if path=='/api/settings/profiles' and self.command=='POST':
                    return self.json_response(201,settings.create(self.body()))
                if path=='/api/settings/activate' and self.command=='POST':
                    body=self.body()
                    if set(body)!={'profile_id'}:raise ModelError('请选择要使用的模型配置。')
                    return self.json_response(200,settings.activate(body['profile_id']))
                if path=='/api/settings/test' and self.command=='POST':
                    return self.json_response(200,client.test(self.body()))
                if self.command=='GET' and path in STATIC:
                    target=ROOT/'web'/STATIC[path]
                    if target.is_file():
                        data=target.read_bytes()
                        self.send_response(200)
                        self.send_header('Content-Type',(mimetypes.guess_type(target.name)[0] or 'text/plain')+'; charset=utf-8')
                        self.send_header('Content-Length',str(len(data)))
                        self.send_header('Cache-Control','no-store')
                        self.send_header('X-Content-Type-Options','nosniff')
                        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                        self.end_headers();self.wfile.write(data);return
                self.json_response(404,{'error':'找不到此页面或接口。'})
            except Conflict as exc:
                self.json_response(409,{'error':str(exc),'work':workflow.decorate(exc.work)})
            except (WorkflowError,StorageError,ModelError) as exc:
                self.json_response(getattr(exc,'status',400),{'error':str(exc)})
            except (BrokenPipeError,ConnectionResetError):
                return
            except Exception:
                self.json_response(500,{'error':'操作未完成，原资料已保留，请检查输入后重试。'})

        do_GET=route
        do_POST=route
        do_PUT=route
        do_DELETE=route

    server=ThreadingHTTPServer((host,port),Handler)
    server.application=workflow
    server.settings=settings
    return server


def main():
    parser=argparse.ArgumentParser(description='口播闭环工作台')
    parser.add_argument('--port',type=int,default=8780)
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--open',action='store_true',dest='open_browser')
    args=parser.parse_args()
    with create_server(port=args.port,data_root=args.data_dir) as server:
        url='http://127.0.0.1:'+str(server.server_port)
        print('口播闭环工作台：'+url)
        if args.open_browser:webbrowser.open(url)
        try:server.serve_forever()
        except KeyboardInterrupt:pass


if __name__=='__main__':main()

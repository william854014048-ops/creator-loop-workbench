"""Stateless API transports and Windows user-bound, endpoint-bound credentials."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import threading
import time
import uuid
from .model_log import write_diagnostic
import urllib.error
import urllib.parse
import urllib.request
from .storage import atomic_json, read_json


class ModelError(Exception):
    status = 400


def protect(value, entropy, decrypt=False):
    if os.name != 'nt':
        raise ModelError('保存或读取密钥需要 Windows 用户加密服务。')
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]
    def blob(data):
        buf = ctypes.create_string_buffer(data)
        return Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf
    source, source_buf = blob(value)
    salt, salt_buf = blob(entropy.encode('utf-8'))
    output = Blob()
    crypt = ctypes.windll.crypt32
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(salt), None, None, 1, ctypes.byref(output))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), 'Creator Loop', ctypes.byref(salt), None, None, 1, ctypes.byref(output))
    if not ok:
        raise ModelError('Windows 密钥加密服务无法读取此配置，请重新填写密钥。')
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


class Settings:
    FIELDS = {'provider','base_url','model','effort','key','profile_id','name','max_output_tokens'}

    def __init__(self, root):
        self.path = Path(root).resolve() / 'settings.json'
        self.lock = threading.RLock()
        default = Path(__file__).resolve().parents[1] / 'config' / 'settings.json'
        source = self.path if self.path.exists() else default
        data = read_json(source)
        self.legacy_bytes = None
        if isinstance(data, dict) and data.get('version') == 2:
            self.data = data
        elif isinstance(data, dict) and data.get('provider') in ('chat','responses'):
            try:
                profiles = []
                for provider in ('chat','responses'):
                    c = dict(data[provider])
                    c.update(id='legacy-' + provider, name='Chat Completions' if provider=='chat' else 'Responses', provider=provider, max_output_tokens=32768)
                    profiles.append(c)
                self.data = dict(version=2, active_profile_id='legacy-' + data['provider'], profiles=profiles)
                if self.path.exists(): self.legacy_bytes = source.read_bytes()
            except (KeyError, TypeError, ValueError):
                raise ModelError('模型设置文件格式损坏，请保留文件后检查。') from None
        else:
            raise ModelError('模型设置文件格式损坏，请保留文件后检查。')
        self._validate(self.data)

    @staticmethod
    def _validate(data):
        if not isinstance(data.get('profiles'),list) or not data['profiles']:
            raise ModelError('模型配置列表损坏，请保留文件后检查。')
        ids, names = set(), set()
        for c in data['profiles']:
            if not isinstance(c,dict): raise ModelError('模型配置格式无效。')
            ident, name = c.get('id'), c.get('name')
            if not isinstance(ident,str) or not ident or len(ident)>128 or ident in ids: raise ModelError('模型配置编号无效。')
            if not isinstance(name,str) or not name.strip() or name != name.strip() or len(name)>80 or name.casefold() in names: raise ModelError('配置名称须为唯一的1到80个字符。')
            ids.add(ident); names.add(name.casefold())
            if c.get('provider') not in ('chat','responses'): raise ModelError('请选择 Chat Completions 或 Responses。')
            for field in ('base_url','model','effort'):
                if not isinstance(c.get(field),str) or len(c[field])>2048: raise ModelError('模型设置内容无效。')
            try:
                parts=urllib.parse.urlsplit(c['base_url'])
                parts.port
            except ValueError:
                raise ModelError('服务地址格式无效，请检查主机与端口。') from None
            if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise ModelError('服务地址须为完整的 http 或 https 地址，且不含账号、参数或片段。')
            if parts.scheme=='http' and parts.hostname not in ('localhost','127.0.0.1','::1'): raise ModelError('远程模型服务须使用 HTTPS。本机服务可以使用 HTTP。')
            if c['effort'] not in ('','default','off','none','minimal','low','medium','high','xhigh','max'): raise ModelError('推理强度无效，请重新选择。')
            budget=c.get('max_output_tokens')
            if type(budget) is not int or not 1024<=budget<=131072: raise ModelError('输出预算须为1024到131072之间的整数。')
            for field in ('encrypted_key','key_address'):
                if field in c and not isinstance(c[field],str): raise ModelError('密钥配置格式损坏。')
        if not isinstance(data.get('active_profile_id'),str) or data['active_profile_id'] not in ids: raise ModelError('当前模型配置编号无效。')

    @staticmethod
    def _find(data, profile_id):
        for c in data['profiles']:
            if c['id']==profile_id:return c
        raise ModelError('找不到该模型配置，请刷新后重试。')

    def _persist(self, data):
        self._validate(data)
        if self.legacy_bytes is not None:
            backup=self.path.with_name('settings.v1.encrypted-backup.json')
            if not backup.exists():
                # The original contains DPAPI ciphertext only; preserve its exact bytes.
                with backup.open('xb') as handle:handle.write(self.legacy_bytes)
        atomic_json(self.path,data)
        self.legacy_bytes=None

    def _edit(self, data, patch, creating=False):
        if not isinstance(patch,dict) or set(patch)-self.FIELDS:raise ModelError('模型设置字段无效。')
        if creating:
            if 'profile_id' in patch:raise ModelError('新配置请勿指定已有编号。')
            c=dict(id=uuid.uuid4().hex,name=patch.get('name',''),provider=patch.get('provider','chat'),base_url='https://api.openai.com/v1',model='',effort='',max_output_tokens=32768)
            data['profiles'].append(c)
        else:
            ident=patch.get('profile_id',data['active_profile_id'])
            # Legacy provider-only navigation retains each provider's stored slot.
            if 'profile_id' not in patch and 'provider' in patch:
                active=self._find(data,ident)
                if active['provider']!=patch['provider']:
                    match=next((v for v in data['profiles'] if v['provider']==patch['provider']),None)
                    if match:ident=match['id']
            c=self._find(data,ident)
        old_endpoint=(c['provider'],c['base_url'])
        for field in ('name','provider','base_url','model','effort','max_output_tokens'):
            if field in patch:
                value=patch[field]
                if field!='max_output_tokens':
                    if not isinstance(value,str):raise ModelError('模型设置内容无效。')
                    value=value.strip().rstrip('/') if field=='base_url' else value.strip()
                c[field]=value
        data['active_profile_id']=c['id']
        self._validate(data)
        if old_endpoint!=(c['provider'],c['base_url']) or c.get('key_address')!=c['base_url']:
            c.pop('encrypted_key',None);c.pop('key_address',None)
        if 'key' in patch:
            if not isinstance(patch['key'],str) or len(patch['key'])>16384: raise ModelError('密钥格式无效。')
            key=patch['key'].strip()
            if key:
                c['encrypted_key']=base64.b64encode(protect(key.encode('utf-8'),c['provider']+'|'+c['base_url'])).decode('ascii')
                c['key_address']=c['base_url']
            else:
                c.pop('encrypted_key',None);c.pop('key_address',None)
        return data

    def update(self, patch, persist=True):
        with self.lock:
            data=self._edit(json.loads(json.dumps(self.data)),patch)
            if persist:self._persist(data)
            self.data=data
            return self.public()

    def create(self, patch, persist=True):
        with self.lock:
            data=self._edit(json.loads(json.dumps(self.data)),patch,creating=True)
            if persist:self._persist(data)
            self.data=data
            return self.public()

    def activate(self, profile_id):
        with self.lock:
            data=json.loads(json.dumps(self.data))
            self._find(data,profile_id)
            data['active_profile_id']=profile_id
            self._persist(data)
            self.data=data
            return self.public()

    def preview(self, patch):
        if not isinstance(patch,dict):raise ModelError('模型设置字段无效。')
        patch=dict(patch)
        new=patch.pop('new_profile',False)
        if type(new) is not bool:raise ModelError('新配置标记无效。')
        with self.lock:
            candidate=object.__new__(Settings)
            candidate.path=self.path
            candidate.lock=threading.RLock()
            candidate.legacy_bytes=None
            candidate.data=json.loads(json.dumps(self.data))
        if new:candidate.create(patch,persist=False)
        else:candidate.update(patch,persist=False)
        return candidate

    @staticmethod
    def _public_profile(c):
        key_ok=bool(c.get('encrypted_key') and c.get('key_address')==c['base_url'])
        return dict(id=c['id'],profile_id=c['id'],name=c['name'],provider=c['provider'],base_url=c['base_url'],model=c['model'],effort=c['effort'],max_output_tokens=c['max_output_tokens'],ready=bool(key_ok and c['model']),key_status='configured' if key_ok else 'missing')

    def public(self, provider=None, profile_id=None):
        with self.lock:
            if profile_id is not None:c=self._find(self.data,profile_id)
            elif provider is not None:
                c=next((v for v in self.data['profiles'] if v['provider']==provider),None)
                if c is None:raise ModelError('找不到该服务类型的配置。')
            else:c=self._find(self.data,self.data['active_profile_id'])
            return dict(self._public_profile(c),active_profile_id=self.data['active_profile_id'],profiles=[self._public_profile(v) for v in self.data['profiles']])

    def credentials(self):
        with self.lock:
            public=self.public()
            if not public['ready']:raise ModelError('请先在模型设置中填写服务地址、模型和 API Key。离线写稿与保存仍可使用。')
            try:
                c=self._find(self.data,public['profile_id'])
                key=protect(base64.b64decode(c['encrypted_key']),c['provider']+'|'+c['base_url'],True).decode('utf-8')
            except ModelError:raise
            except Exception:raise ModelError('密钥读取失败，请重新填写 API Key。') from None
            return public,key


class ModelClient:
    def __init__(self, settings, log_path=None):
        self.settings = settings
        self.log_path = Path(log_path) if log_path else settings.path.parent / 'runtime' / 'model-diagnostics.jsonl'

    def complete(self, task, system, inputs):
        config, key = self.settings.credentials()
        return self._complete(task, system, inputs, config, key)

    def complete_with_provenance(self, task, system, inputs):
        config, key = self.settings.credentials()
        config['_correlation_id'] = uuid.uuid4().hex
        result = self._complete(task, system, inputs, config, key)
        return result, dict({name: config[name] for name in ('provider', 'base_url', 'model', 'effort', 'profile_id', 'max_output_tokens')}, profile_name=config['name'], correlation_id=config['_correlation_id'])

    def _complete(self, task, system, inputs, config, key):
        parts=urllib.parse.urlsplit(config['base_url'])
        if parts.scheme!='https' and not (parts.scheme=='http' and parts.hostname in ('localhost','127.0.0.1','::1')):
            raise ModelError('远程模型服务须使用 HTTPS。本机服务可以使用 HTTP。')
        effort=config['effort']
        if effort in ('','default'):effort=None
        elif effort=='off':effort='none'
        user = json.dumps(inputs, ensure_ascii=False, allow_nan=False)
        if config['provider'] == 'chat':
            endpoint = '/chat/completions'
            token_limit='max_completion_tokens' if parts.hostname=='api.openai.com' else 'max_tokens'
            payload = {'model':config['model'], 'messages':[{'role':'system','content':system},{'role':'user','content':user}], token_limit:config['max_output_tokens']}
            if parts.hostname=='api.deepseek.com' and effort:
                if effort=='none':payload['thinking']={'type':'disabled'}
                elif effort in ('low','high','max'):
                    payload['thinking']={'type':'enabled'}
                    payload['reasoning_effort']=effort
                else:raise ModelError('DeepSeek 推理强度请选择默认、关闭、低、高或最高。')
            elif effort:
                payload['reasoning_effort']=effort
        else:
            endpoint = '/responses'
            payload = {'model':config['model'], 'instructions':system, 'input':[{'role':'user','content':[{'type':'input_text','text':user}]}], 'max_output_tokens':config['max_output_tokens'], 'store':False}
            if effort:
                payload['reasoning'] = {'effort':effort}
        request = urllib.request.Request(config['base_url'] + endpoint, json.dumps(payload, ensure_ascii=False).encode('utf-8'), {'Content-Type':'application/json'}, method='POST')
        # Redirects must never forward the credential to a different endpoint.
        request.add_unredirected_header('Authorization', 'Bearer ' + key)
        started=time.monotonic()
        diagnostic=dict(correlation_id=config.get('_correlation_id',uuid.uuid4().hex),task=task if task in ('connection-test','angles','package','plan','review','value','blind-score','cheat-predict','retro') else 'generation',profile_id=config['profile_id'],model=config['model'],provider=config['provider'],hostname=parts.hostname,request_budget=config['max_output_tokens'],usage={},outcome='error')
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                diagnostic['outcome']='response_too_large'
                raise ModelError('模型回复过长，本次结果未保存，请缩短输入后重试。')
            body = json.loads(raw.decode('utf-8'))
            usage=body.get('usage',{})
            if isinstance(usage,dict):
                diagnostic['usage']={k:v for k,v in usage.items() if k in ('prompt_tokens','completion_tokens','total_tokens','input_tokens','output_tokens') and type(v) is int and v>=0}
                for detail in ('completion_tokens_details','output_tokens_details'):
                    values=usage.get(detail,{})
                    if isinstance(values,dict) and type(values.get('reasoning_tokens')) is int:diagnostic['usage']['reasoning_tokens']=values['reasoning_tokens']
            if config['provider'] == 'chat':
                choice = body['choices'][0]
                reason=choice.get('finish_reason')
                diagnostic['finish_reason']=reason if reason in ('stop','length','content_filter','tool_calls','function_call') else 'unknown'
                if reason == 'length':
                    diagnostic['outcome']='output_limit'
                    raise ModelError('模型输出预算已耗尽，本次结果未保存。请在模型设置中增加输出预算，或手动降低推理强度后重试；原输入与旧结果已保留。')
                if reason not in (None,'stop'):raise ModelError('模型未返回完整正文，请检查模型设置后重试。')
                result = choice['message']['content']
            else:
                status=body.get('status')
                diagnostic['status']=status if status in ('completed','incomplete','failed','cancelled','queued','in_progress') else 'unknown'
                if status == 'incomplete':
                    reason=body.get('incomplete_details',{}).get('reason')
                    diagnostic['outcome']='output_limit' if reason=='max_output_tokens' else 'incomplete'
                    if reason=='max_output_tokens':raise ModelError('模型输出预算已耗尽，请增加输出预算，或手动降低推理强度后重试。原输入与旧结果已保留。')
                    raise ModelError('模型输出未完成，请检查模型设置后重试。原输入与旧结果已保留。')
                if status not in (None,'completed'):raise ModelError('模型输出未完成，请稍后手动重试。')
                result = ''.join(item['text'] for block in body.get('output',[]) for item in block.get('content',[]) if item.get('type') == 'output_text')
            if not isinstance(result, str) or not result.strip():
                raise ModelError('模型没有返回可用正文，请检查模型设置。')
            diagnostic['outcome']='success'
            return result
        except ModelError:
            raise
        except urllib.error.HTTPError as exc:
            diagnostic['http_status']=exc.code
            diagnostic['outcome']='http_error'
            if exc.code in (401,403):
                message = '模型服务拒绝访问，请检查 API Key 和模型权限。'
            elif exc.code == 429:
                message = '模型服务请求额度或频率受限，请稍后手动重试。'
            else:
                message = '模型服务返回错误（HTTP ' + str(exc.code) + '），请检查服务地址和模型。'
            raise ModelError(message) from None
        except (TimeoutError, urllib.error.URLError) as exc:
            diagnostic['outcome']='timeout' if isinstance(exc,TimeoutError) or isinstance(getattr(exc,'reason',None),TimeoutError) else 'connection_error'
            raise ModelError('模型连接超时或连接失败，请检查网络后手动重试。原输入与旧结果已保留。') from None
        except (ValueError, UnicodeError, KeyError, IndexError, TypeError):
            diagnostic['outcome']='invalid_response'
            raise ModelError('模型回复格式不完整或无法读取，请手动重试。原输入与旧结果已保留。') from None
        except Exception:
            diagnostic['outcome']='connection_or_parse_error'
            raise ModelError('模型连接或回复读取失败，请检查网络和模型设置。本次输入与旧结果已保留。') from None
        finally:
            diagnostic['elapsed_seconds']=round(time.monotonic()-started,3)
            write_diagnostic(self.log_path,diagnostic)

    def test(self, patch=None):
        if patch:
            return ModelClient(self.settings.preview(patch),self.log_path).test()
        candidate=self.settings.preview({})
        # The connectivity probe does not spend the generation/reasoning budget.
        c=candidate._find(candidate.data,candidate.data['active_profile_id'])
        c['max_output_tokens']=1024
        c['effort']='off' if urllib.parse.urlsplit(c['base_url']).hostname=='api.deepseek.com' else ('low' if c['effort'] not in ('','default','off','none') else c['effort'])
        ModelClient(candidate,self.log_path).complete('connection-test', '请只返回一个 JSON 对象：{"ok":true}。', {})
        return {'ok':True, 'message':'连接成功'}

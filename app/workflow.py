"""Creator workflow. Calls outside lock; every commit rechecks revision."""
import copy
from datetime import datetime
import json
import math
import re
from .storage import Store, Conflict, StorageError, digest, new_id, now, ZONE
from .methods import Methods, ZERO_RULES
from .model import ModelError


class WorkflowError(Exception):
    status = 400


def require(condition, message):
    if not condition:
        raise WorkflowError(message)


def text(value, label, required=False):
    require(isinstance(value, str), label + '须填写文字。')
    require(len(value) <= 200000, label + '过长，请缩短后保存。')
    require(not required or bool(value.strip()), '请填写' + label + '。')
    return value


def timestamp(value, label):
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZONE)
        return parsed.astimezone(ZONE)
    except (TypeError, ValueError, AttributeError):
        raise WorkflowError(label + '格式无效。') from None


def number(value, label, optional=True, maximum=None):
    if optional and value in ('',None):
        return None
    require(not isinstance(value,bool) and isinstance(value,(str,int,float)), label + '须为非负数字。')
    try:
        result = float(value)
    except (TypeError,ValueError):
        raise WorkflowError(label + '须为非负数字。') from None
    require(math.isfinite(result) and result >= 0 and (maximum is None or result <= maximum), label + '数值范围无效。')
    return int(result) if result.is_integer() else result


def text_list(value,label,required=True):
    require(isinstance(value,list) and (not required or bool(value)),label+'须为非空文字列表。')
    for item in value:text(item,label,True)
    return value


class Workflow:
    editable = {'title','stage','material','spoken','clean_script','final','applied_observation'}
    final_keys = {'script','title','cover','opening','platform','account','duration_seconds','confirmed'}
    def __init__(self, data_root, model):
        self.store = Store(data_root)
        self.model = model
        self.methods = Methods()

    def basis(self, work):
        return {k:copy.deepcopy(work['final'].get(k)) for k in sorted(self.final_keys - {'confirmed'})}

    def plan_inputs(self, work):
        return {key: copy.deepcopy(work[key]) for key in ('material', 'angle', 'package')}

    def name(self, work):
        title = work.get('title', '').strip()
        mode = work.get('title_mode', 'manual' if title and title != '未命名作品' else 'auto')
        if mode == 'manual' and title and title != '未命名作品':
            return title, 'manual', '手动命名 · 清空后恢复自动命名'
        candidates = [((work.get('angle') or {}).get('title', ''), '已选角度'),
                      (work.get('final', {}).get('title', ''), '成片标题'),
                      (work.get('material', ''), '素材首句'),
                      (work.get('spoken', ''), '口述首句'),
                      (work.get('clean_script', ''), '正文首句')]
        for value, source in candidates:
            match = re.search(r'[^\r\n。！？!?]+', value)
            if match and match.group().strip():
                title = re.sub(r'\s+', ' ', match.group().strip())
                return title[:40] + ('…' if len(title) > 40 else ''), 'auto', '自动命名 · 来自' + source + ' · 可直接修改'
        stamp = timestamp(work['created_at'], '创建时间').strftime('%m-%d %H:%M')
        return '新作品 ' + stamp + ' · ' + work['id'][:4], 'auto', '自动命名 · 来自新建时间 · 输入素材或选择角度后更新'

    def decorate(self, work):
        work = copy.deepcopy(work)
        work['title'], work['title_mode'], work['title_source'] = self.name(work)
        if work['stage'] == 1:
            work['stage'] = 2
        work['plan_stale'] = bool(work['plan'].get('source_hash') and work['plan']['source_hash'] != digest(self.plan_inputs(work)))
        review = work['reviews'][-1] if work['reviews'] else None
        work['review_current'] = bool(review and review['source_hash'] == digest(work['spoken']))
        basis_hash = digest(self.basis(work))
        work['current_basis_hash'] = basis_hash
        for records in work['judgments'].values():
            for judgment in records:
                judgment['stale'] = judgment['basis_hash'] != basis_hash
        return work

    def get(self, work_id):
        return self.decorate(self.store.get(work_id))

    def list(self):
        return [{key: decorated[key] for key in ('id','title','title_mode','title_source','stage','revision','updated_at')}
                for decorated in (self.decorate(work) for work in self.store.list())]

    def delete(self, work_id, body):
        require(isinstance(body, dict) and set(body) == {'revision'}, '删除作品须提供当前版本号。')
        self.store.delete(work_id, body['revision'])
        return {'deleted': work_id}

    def observation(self, value):
        if value is None:
            return None
        require(isinstance(value,dict) and isinstance(value.get('id'),str), '请选择已有复盘观察。')
        original = next((o for o in self.observations() if o['id'] == value['id']), None)
        require(original is not None, '找不到这项复盘观察，请刷新后重试。')
        return {**original,'application':text(value.get('application',''),'本次应用方式')}

    def create(self, body):
        require(isinstance(body,dict) and not set(body)-{'title','applied_observation'}, '新作品字段无效。')
        return self.decorate(self.store.create(text(body.get('title',''),'作品名称'), self.observation(body.get('applied_observation'))))

    def patch(self, work_id, revision, patch):
        require(isinstance(patch,dict) and not set(patch)-self.editable, '此字段由专门操作保存，普通保存无法修改。')
        def apply(w):
            if w['plan'].get('report') and not w['plan'].get('source_hash'):
                w['plan']['source_hash'] = digest(self.plan_inputs(w))
            for key, value in patch.items():
                if key == 'stage':
                    require(type(value) is int and 0 <= value <= 7, '步骤编号无效。')
                elif key == 'final':
                    require(isinstance(value,dict) and not set(value)-self.final_keys, '实际成片字段无效。')
                    candidate = {**w['final'],**value}
                    for k in self.final_keys-{'duration_seconds','confirmed'}:
                        text(candidate[k], '实际成片')
                    candidate['duration_seconds'] = number(candidate['duration_seconds'], '时长')
                    # Confirmation is granted only by the explicit action.
                    changed = any(candidate.get(k) != w['final'].get(k) for k in self.final_keys-{'confirmed'})
                    candidate['confirmed'] = bool(w['final']['confirmed'] and not changed)
                    value = candidate
                elif key == 'applied_observation':
                    applied = w.get('applied_observation')
                    if isinstance(value, dict) and applied and value.get('id') == applied['id']:
                        value = {**applied, 'application': text(value.get('application', ''), '本次应用方式')}
                    else:
                        value = self.observation(value)
                else:
                    text(value,key)
                if key == 'title':
                    value = value.strip()
                    w['title_mode'] = 'manual' if value and value != '未命名作品' else 'auto'
                w[key] = copy.deepcopy(value)
        return self.decorate(self.store.update(work_id,revision,apply))

    def call(self, task, payload):
        try:
            system, provenance = self.methods.load(task)
            if hasattr(self.model, 'complete_with_provenance'):
                raw, request_config = self.model.complete_with_provenance(task, system, copy.deepcopy(payload))
                provenance = {**provenance, **request_config}
            else:
                raw = self.model.complete(task, system, copy.deepcopy(payload))
                provenance = {**provenance, 'provider': 'injected', 'model': 'injected'}
            if isinstance(raw,str):
                require(len(raw)<=1500000, '模型回复过长，本次结果未保存。')
                result = json.loads(raw)
            else:
                result = copy.deepcopy(raw)
            require(isinstance(result,dict), '模型回复格式无效，需要 JSON 对象。')
            # Reject non-finite numbers before any data is persisted.
            json.dumps(result, allow_nan=False)
            return result, provenance
        except (WorkflowError,ModelError):
            raise
        except Exception:
            raise WorkflowError('模型调用或回复格式检查失败，本次输入与旧结果已保留，请手动重试。') from None

    def action(self, work_id, action, body):
        require(isinstance(body,dict), '操作参数无效。')
        revision = body.get('revision')
        # One revision snapshot. Slow network work happens without the storage lock.
        w = self.store.get(work_id)
        if type(revision) is not int or w['revision'] != revision:
            raise Conflict(self.decorate(w))
        result = None
        provenance = None
        if action in ('angles','choose-angle','plan','review'):
            if action == 'angles':
                require(bool(w['material'].strip()), '请先写下素材。')
                result, provenance = self.call('angles',{'material':w['material']})
                require(isinstance(result.get('angles'),list) and 0<len(result['angles'])<=4 and isinstance(result.get('questions',[]),list), '方向回复须包含1到4个方向。')
                ids=set()
                for a in result['angles']:
                    require(isinstance(a,dict), '方向格式无效。')
                    for k in ('id','title','audience','change','example','reason'):
                        text(a.get(k),'方向'+k,True)
                    require(a['id'] not in ids,'方向编号重复。')
                    ids.add(a['id'])
            elif action == 'choose-angle':
                angle = next((a for a in w['angles'] if a['id']==body.get('angle_id')),None)
                require(angle is not None,'请先选择一个已有方向。')
                result, provenance = self.call('package',{'material':w['material'],'angle':angle})
                require(isinstance(result.get('titles'),list) and bool(result['titles']), '标题候选格式无效。')
                for title in result['titles']: text(title,'标题候选',True)
                for k in ('cover','opening','note'): text(result.get(k),'包装'+k)
            elif action == 'plan':
                require(w['angle'] is not None,'请先选择方向。')
                result, provenance = self.call('plan',self.plan_inputs(w))
                text(result.get('report'),'内容建议',True)
            else:
                require(bool(w['spoken'].strip()),'请先输入本次口述。')
                result, provenance = self.call('review',{'spoken':w['spoken']})
                text(result.get('report'),'改稿报告',True)
                require(isinstance(result.get('edits'),list),'修改列表格式无效。')
                spans=[]
                for edit in result['edits']:
                    require(isinstance(edit,dict),'修改条目格式无效。')
                    original=text(edit.get('original'),'修改原文',True)
                    for k in ('replacement','reason','risk'): text(edit.get(k),'修改'+k)
                    require(w['spoken'].count(original)==1,'修改原文须在口述中唯一且精确出现，本次修改未保存。')
                    start=w['spoken'].index(original); end=start+len(original)
                    require(not any(start<e and end>s for s,e in spans),'修改片段互相重叠，本次修改未保存。')
                    spans.append((start,end))
                    edit.update(id=new_id(),start=start,end=end)
        elif action in ('judge-value','judge-cheat'):
            self.check_judgable(w)
            # Deliberate whitelist; account names and every creative/history field stay local.
            final={k:copy.deepcopy(w['final'][k]) for k in ('script','title','cover','opening','platform','duration_seconds')}
            if action=='judge-value':
                value_rules={k:copy.deepcopy(ZERO_RULES[k]) for k in ('rubric_version','baseline_plays','calibration_samples','benchmark_sample_count')}
                value_rules['focus']=['受众','开头','流量倾向','商业价值','可检验假设']
                value_rules['numeric_plays']=False
                result,provenance=self.call('value',{'final':final,'rules':value_rules})
                text(result.get('report'),'价值判断',True)
                text_list(result.get('hypotheses'),'价值假设')
                result={k:result[k] for k in ('report','hypotheses')}
            else:
                score_rules={k:copy.deepcopy(v) for k,v in ZERO_RULES.items() if k!='bucket_priors'}
                score,score_prov=self.call('blind-score',{'final':final,'rules':score_rules})
                dimensions=score.get('dimensions')
                require(isinstance(dimensions,dict) and set(dimensions)==set(ZERO_RULES['dimensions']),'盲评分须完整包含七个维度。')
                for d in dimensions.values():
                    require(isinstance(d,dict) and type(d.get('score')) is int and 0<=d['score']<=5,'盲评分须为0到5的整数。')
                    text(d.get('reason'),'评分理由',True)
                    require(d.get('confidence') in ('high','medium','low'),'评分信心等级无效。')
                composite=round(sum(d['score'] for d in dimensions.values())/7*2,4)
                result,provenance=self.call('cheat-predict',{'final':final,'rules':copy.deepcopy(ZERO_RULES),'blind_score':{'dimensions':dimensions,'composite':composite}})
                text(result.get('report'),'数值判断',True);text(result.get('bucket'),'概率桶',True)
                probabilities=result.get('probabilities')
                require(isinstance(probabilities,list) and len(probabilities)>0,'请返回完整概率分布。')
                seen=set(); total=0
                for p in probabilities:
                    require(isinstance(p,dict),'概率条目无效。')
                    bucket=text(p.get('bucket'),'概率桶',True)
                    require(bucket not in seen,'概率桶重复。'); seen.add(bucket)
                    total+=number(p.get('probability'),'概率',False,100)
                require(abs(total-100)<0.00001,'预测概率合计须为100。')
                require(seen=={'底部','基础盘','命中','小爆','大爆'},'预测须完整包含底部、基础盘、命中、小爆、大爆五个桶。')
                require(result['bucket'] in seen,'预测桶须位于概率分布中。')
                result['center']=number(result.get('center'),'中枢播放数',False)
                center=result['center']
                expected_bucket='底部' if center<100 else '基础盘' if center<1000 else '命中' if center<10000 else '小爆' if center<100000 else '大爆'
                require(result['bucket']==expected_bucket,'中枢播放数须落在选定概率桶的区间内。')
                for k in ('hypotheses','counterfactuals'):text_list(result.get(k),'预测'+k)
                result={k:result[k] for k in ('report','bucket','probabilities','center','hypotheses','counterfactuals')}
                result.update(dimensions=dimensions,composite=composite)
                provenance={**provenance,'blind_score_provenance':score_prov}
            # Model-provided metadata is never trusted.
            for k in ('baseline_plays','baseline','calibration_samples','benchmark_sample_count','rubric_version'):
                result.pop(k,None)
            result.update(baseline_plays=None,calibration_samples=0,rubric_version='v0')
            provenance.update(baseline_plays=None,calibration_samples=0,benchmark_sample_count=0,rubric_version='v0')
        elif action=='retro':
            metrics=next((m for m in w['metrics'] if m['id']==body.get('metrics_id')),None)
            require(metrics is not None,'请选择本作品的观察数据。')
            require(metrics['comments'] or bool(metrics.get('comments_unavailable_reason','').strip()),'没有评论时，请先记录评论缺失原因。')
            retro_publication=next(p for p in w['publications'] if p['id']==metrics['publication_id'])
            locked={k:[j for j in w['judgments'][k] if j['id'] in retro_publication['judgment_ids'][k] and j['status']=='locked'] for k in ('value','cheat')}
            result,provenance=self.call('retro',{'publication':copy.deepcopy(retro_publication),'judgments':copy.deepcopy(locked),'metrics':copy.deepcopy(metrics)})
            text(result.get('report'),'复盘报告',True)
            require(isinstance(result.get('comparisons'),list) and isinstance(result.get('observations'),list),'复盘对照和观察须为列表。')
            for comparison in result['comparisons']:
                require(isinstance(comparison,dict),'复盘对照项格式无效。')
                require(comparison.get('kind') in ('value','cheat'),'复盘对照方法无效。')
                for field,label in (('hypothesis','判断假设'),('verdict','对照结果'),('evidence','对照依据')):
                    text(comparison.get(field),label,True)
            for o in result['observations']:
                require(isinstance(o,dict),'观察格式无效。')
                for k in ('text','evidence','next_action'):text(o.get(k),'观察'+k,True)
        elif action not in ('save-spoken','merge','confirm-final','lock-judgment','publish','metrics'):
            raise WorkflowError('未知作品操作。')

        def commit(current):
            if provenance is not None:
                # Retain rule versions and generated package/plan revisions as an append-only trace.
                current.setdefault('method_runs',[]).append({'id':new_id(),'action':action,'created_at':now(),'provenance':copy.deepcopy(provenance),'result':copy.deepcopy(result)})
            if action=='angles':
                current['angles']=result['angles']; current['questions']=result.get('questions',[])
            elif action=='choose-angle':
                if current['plan'].get('report') and not current['plan'].get('source_hash'):
                    current['plan']['source_hash'] = digest(self.plan_inputs(current))
                current['angle']=angle; current['package']={**result,'provenance':provenance}
            elif action=='plan': current['plan']={**result,'source_hash':digest(self.plan_inputs(current)),'provenance':provenance}
            elif action=='save-spoken':
                require(bool(current['spoken'].strip()),'请先输入本次口述。')
                current['spoken_versions'].append({'id':new_id(),'text':current['spoken'],'source_hash':digest(current['spoken']),'created_at':now()})
            elif action=='review':
                source_hash = digest(current['spoken'])
                if not current['spoken_versions'] or current['spoken_versions'][-1]['source_hash'] != source_hash:
                    current['spoken_versions'].append({'id':new_id(),'text':current['spoken'],'source_hash':source_hash,'created_at':now()})
                current['reviews'].append({**result,'id':new_id(),'source_hash':digest(current['spoken']),'source':current['spoken'],'created_at':now(),'provenance':provenance})
            elif action=='merge':
                review=next((r for r in current['reviews'] if r['id']==body.get('review_id')),None)
                require(review is not None,'找不到这版修改记录。')
                require(review['source_hash']==digest(current['spoken']),'口述已经变化，请重新检查修改。')
                accepted=body.get('accepted_ids')
                require(isinstance(accepted,list) and len(set(accepted))==len(accepted) and all(i in [e['id'] for e in review['edits']] for i in accepted),'接受的修改编号无效。')
                script=current['spoken']
                for edit in sorted(review['edits'],key=lambda e:e['start'],reverse=True):
                    if edit['id'] in accepted:
                        script=script[:edit['start']]+edit['replacement']+script[edit['end']:]
                current['clean_script']=script
                review.setdefault('decisions',[]).append({'id':new_id(),'accepted_ids':accepted,'rejected_ids':[e['id'] for e in review['edits'] if e['id'] not in accepted],'created_at':now(),'clean_script':script})
            elif action=='confirm-final':
                for k in self.final_keys-{'confirmed','duration_seconds'}:text(current['final'].get(k),'实际'+k,True)
                duration=number(current['final'].get('duration_seconds'),'时长',False)
                require(duration>0,'时长须大于0。')
                current['final']['confirmed']=True
            elif action in ('judge-value','judge-cheat'):
                self.check_judgable(current)
                kind=action.split('-')[1]
                current['judgments'][kind].append({'id':new_id(),'status':'draft','basis_hash':digest(self.basis(current)),'snapshot':self.basis(current),'created_at':now(),'result':result,'provenance':provenance})
            elif action=='lock-judgment':
                self.check_judgable(current)
                kind=body.get('kind'); require(kind in ('value','cheat'),'请选择判断方法。')
                judgment=next((j for j in current['judgments'][kind] if j['id']==body.get('judgment_id')),None)
                require(judgment is not None,'找不到这版判断。')
                require(judgment['status']=='draft','这版判断已锁定。')
                require(judgment['basis_hash']==digest(self.basis(current)),'最终内容已变化，请重新生成判断。')
                judgment.update(status='locked',locked_at=now())
                canonical=self.store.freeze(work_id,kind,judgment)
                judgment.clear();judgment.update(canonical)
            elif action=='publish':
                require(current['final']['confirmed'],'请先核对并确认实际成片。')
                platform=text(body.get('platform'),'发布平台',True);account=text(body.get('account'),'发布账号',True)
                require(platform==current['final']['platform'] and account==current['final']['account'],'发布平台和账号须与已确认成片一致。')
                window=body.get('window_hours',72);require(type(window) is int and window in (24,72,168),'观察窗口请选择24、72或168小时。')
                published=timestamp(body.get('published_at'),'发布时间')
                require(published<=datetime.now(ZONE),'发布时间须为已发生的时间。')
                url=text(body.get('url',''),'发布链接')
                if url: require(url.startswith(('http://','https://')),'发布链接请填写完整的 http 或 https 地址。')
                ids={k:[j['id'] for j in current['judgments'][k] if j['status']=='locked' and j['basis_hash']==digest(self.basis(current))] for k in ('value','cheat')}
                require(all(ids.values()),'请先分别锁定当前版本的两项发布前判断。')
                publication={'id':new_id(),'platform':platform,'account':account,'url':url,'published_at':published.isoformat(timespec='seconds'),'window_hours':window,'snapshot':self.basis(current),'basis_hash':digest(self.basis(current)),'judgment_ids':ids,'referenced_judgments':copy.deepcopy(ids),'created_at':now()}
                self.store.freeze(work_id,'publication',publication)
                current['publications'].append(publication)
            elif action=='metrics':
                publication=next((p for p in current['publications'] if p['id']==body.get('publication_id')),None)
                require(publication is not None,'请选择本作品的发布登记。')
                observed=timestamp(body.get('observed_at'),'观察时间');published=timestamp(publication['published_at'],'发布时间')
                require(observed>=published,'观察时间须晚于或等于发布时间。')
                require(observed<=datetime.now(ZONE),'观察时间须为已发生的时间。')
                elapsed=round((observed-published).total_seconds()/3600,4)
                m={'id':new_id(),'publication_id':publication['id'],'source':'manual','observed_at':observed.isoformat(timespec='seconds'),'window_hours':publication['window_hours'],'elapsed_hours':elapsed,'early':elapsed<publication['window_hours'],'created_at':now()}
                m['tolerance_hours']=round(min(6,max(1,publication['window_hours']*0.05)),4)
                m['late']=elapsed>publication['window_hours']+m['tolerance_hours']
                m['eligible_same_window']=not m['early'] and not m['late']
                for k in ('plays','likes','comments_count','shares','favorites'):
                    m[k]=number(body.get(k),k)
                    require(m[k] is None or isinstance(m[k],int),k+'须为整数。')
                m['completion_rate']=number(body.get('completion_rate'),'完播率',True,100)
                comments=body.get('comments',[]);require(isinstance(comments,list),'评论须逐条记录。')
                m['comments']=[]
                for c in comments:
                    require(isinstance(c,dict),'评论格式无效。')
                    likes=number(c.get('likes'),'评论点赞')
                    require(likes is None or isinstance(likes,int),'评论点赞须为整数。')
                    m['comments'].append({'text':text(c.get('text'),'评论',True),'likes':likes})
                m['comments_unavailable_reason']=text(body.get('comments_unavailable_reason',''),'评论缺失原因')
                current['metrics'].append(m)
            elif action=='retro':
                retro={**result,'id':new_id(),'metrics_id':metrics['id'],'publication_id':retro_publication['id'],'created_at':now(),'provenance':provenance}
                for observation in retro['observations']:observation['id']=new_id()
                current['retros'].append(retro)
        return self.decorate(self.store.update(work_id,revision,commit))

    def check_judgable(self, w):
        require(not w['publications'],'这条作品已经登记发布，发布前判断已关闭。')
        require(w['final']['confirmed'],'请先核对并确认实际成片。')

    def observations(self):
        observations=[]
        for w in self.store.list():
            for r in w['retros']:
                for o in r['observations']:
                    observations.append({**copy.deepcopy(o),'work_id':w['id'],'work_title':self.name(w)[0],'retro_id':r['id']})
        return observations

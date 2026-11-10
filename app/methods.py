"""Read only a fixed whitelist of source snapshots and separate adapters."""
import hashlib
import json
from pathlib import Path
from .storage import digest

ROOT = Path(__file__).resolve().parents[1]
U = 'skills/upstream/'
C = U + 'cheat-on-content/'
FILES = {
    'angles':[U+'dbs-title-cover-intro/SKILL.md']+[U+'dbs-title-cover-intro/references/'+p+'.md' for p in ('angle-discovery-fast','topic-ladder','writing-rules','production-state','material-selection','candidate-selection')],
    'package':[U+'dbs-title-cover-intro/SKILL.md']+[U+'dbs-title-cover-intro/references/'+p+'.md' for p in ('cover-title','opening-continuity','writing-rules','title-surfaces','promise-check')],
    'plan':[U+'dbs-content/SKILL.md'],
    'review':[U+'dbs-script-flow/SKILL.md'],
    'value':[U+'dbs-content-value/SKILL.md', U+'dbs-content-value/references/diagnostic-model.md'],
    'blind-score':[C+'starter-rubrics/opinion-video-zero.md'],
    'cheat-predict':[C+'skills/cheat-predict/SKILL.md', C+'shared-references/prediction-anatomy.md', C+'shared-references/state-management.md', C+'starter-rubrics/opinion-video-zero.md'],
    'retro':[C+'skills/cheat-retro/SKILL.md'],
}
ZERO_RULES = {'rubric_version':'v0','baseline_plays':None,'calibration_samples':0,'benchmark_sample_count':0,'content_form':'opinion-video','confidence':'极低','dimensions':['ER','HP','QL','NA','AB','SR','SAT'],'formula':'sum(dimensions)/7*2','bucket_priors':[{'bucket':'底部','range':'<100','probability':30},{'bucket':'基础盘','range':'100-1000','probability':40},{'bucket':'命中','range':'1000-10000','probability':20},{'bucket':'小爆','range':'10000-100000','probability':8},{'bucket':'大爆','range':'>100000','probability':2}]}


class Methods:
    def __init__(self):
        self.cache = {}
        manifest = json.loads((ROOT/'skills/source-manifest.json').read_text(encoding='utf-8'))
        self.expected = {f['path']:f['sha256'] for f in manifest['files']}

    def load(self, task):
        if task not in self.cache:
            hashes = {}
            fragments = {}
            texts = []
            for relative in FILES[task] + ['skills/adapters/contracts.json', 'skills/adapters/output-schemas.json']:
                raw = (ROOT/relative).read_bytes()
                sha = hashlib.sha256(raw).hexdigest()
                if relative in self.expected and sha != self.expected[relative]:
                    raise ValueError('原方法快照校验失败：' + relative)
                hashes[relative] = sha
                if relative.endswith('contracts.json'):
                    adapter = json.loads(raw.decode('utf-8'))
                elif relative.endswith('output-schemas.json'):
                    schema = json.loads(raw.decode('utf-8'))[task]
                else:
                    content=raw.decode('utf-8')
                    if task=='blind-score':
                        content=content[content.index('## v0 综合分公式'):content.index('## Bucket 预测')]
                        fragments[relative]=digest(content)
                    texts.append('原方法 ' + relative + '\n' + content)
            system = adapter['general'] + '\n' + '\n\n'.join(texts) + '\n\n本次应用协议（优先约束执行）\n' + adapter['tasks'][task]
            system += '\n\n返回字段类型（JSON Schema，必须遵循）：\n' + json.dumps(schema, ensure_ascii=False)
            system += '\n所有 report 字段都是一份完整 Markdown 报告的字符串，禁止返回分节对象。正文内换行须使用合法 JSON 转义。不要输出推理过程。'
            self.cache[task] = (system, {'methods':hashes,'fragments':fragments,'method_sha256':digest({'methods':hashes,'fragments':fragments}),'adapter_version':adapter['version']})
        return self.cache[task]

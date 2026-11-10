"""Disposable E-drive browser fixture. Does not access user data or model APIs."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import create_server
from app.model import ModelError


class FixtureModel:
    def complete(self, task, system, inputs):
        if task == 'angles':
            value = {'angles': [
                {'id': 'a1', 'title': '朋友为什么开始找我解决问题', 'audience': '想把 AI 能力用到实际工作的人',
                 'change': '从朋友那次具体的提问开始', 'example': '会用 AI 之后，别人开始找我帮忙了', 'reason': '一件小事让方向变得具体'},
                {'id': 'a2', 'title': '从学习工具到解决一个问题', 'audience': '学了很多工具却不知道用在哪的人',
                 'change': '讲清楚这次需求是什么', 'example': '学会工具以后，我开始做这一件事', 'reason': '别人能看到你做了什么'}], 'questions': []}
        elif task == 'package':
            value = {'titles': ['会用 AI 之后，别人开始找我帮忙了'], 'cover': '会用 AI 之后\n有人来找我了',
                     'opening': '昨天，一位朋友带着一个具体的问题来找我。', 'note': '从朋友的真实问题接回自己的经历。'}
        elif task == 'plan':
            value = {'report': '## 这条内容怎么讲\n\n从朋友的问题开始，交代你做了什么，再讲自己的方向怎样变得具体。\n\n保留朋友的原话。先说实际发生的事，再说自己的想法。'}
        elif task == 'review':
            if '模拟失败' in json.dumps(inputs, ensure_ascii=False):
                raise ModelError('模拟模型暂时不可用，请稍后重试。')
            value = {'report': '## 最先改的一处\n\n第一句话可以更快进入具体的事，保留后面的真实经历。', 'edits': [
                {'original': '今天我想谈谈自己的经历。', 'replacement': '今天我想说一件刚发生的事。',
                 'reason': '更快进入这次经历。', 'risk': 'medium'},
                {'original': '朋友昨天带着一个具体的问题来找我。', 'replacement': '昨天，朋友带着一个具体问题来找我。',
                 'reason': '调整句子顺序。', 'risk': 'low'}]}
        elif task == 'value':
            value = {'report': '## 一句话结论\n\n正在尝试把 AI 用到工作中的人会愿意继续看，因为朋友提出了一个具体问题。\n\n| 观察点 | 判断 |\n| --- | --- |\n| 关注意图 | 观众可能继续看具体做法 |\n\n## 最该保留的地方\n\n保留朋友的原话，让观众知道需求怎样出现。',
                     'hypotheses': ['观众会在评论中讲自己的类似需求。']}
        elif task == 'blind-score':
            value = {'dimensions': {key: {'score': 3 if key in ('SR', 'SAT') else 4,
                                         'confidence': 'medium', 'reason': '朋友提出具体问题，经历容易理解。'}
                                    for key in ('ER', 'HP', 'QL', 'NA', 'AB', 'SR', 'SAT')}}
        elif task == 'cheat-predict':
            value = {'report': '## 预测\n\n0 基准，无个人历史样本。本次押基础盘，试验中枢 480 播放。\n\n## 锚点对照\n\nN/A：尚未使用个人历史。\n\n## 待验证\n\n看观众是否会讲自己的实际问题。',
                     'bucket': '基础盘', 'center': 480,
                     'probabilities': [{'bucket': key, 'probability': p} for key, p in
                                       [('底部', 25), ('基础盘', 40), ('命中', 25), ('小爆', 8), ('大爆', 2)]],
                     'hypotheses': ['这条作品最可能落在基础盘。'],
                     'counterfactuals': ['超过一千播放时，继续观察朋友提问的开头。', '不足一百播放时，核对开头是否足够具体。']}
        elif task == 'retro':
            plays = inputs['metrics']['plays']
            value = {'report': f'## 对照结果\n\n本次同窗记录为 {plays} 播放，评论提到了自己的需求。内容价值判断押的评论方向出现了；内容作弊的中枢为 480。\n\n## 下一条可以试什么\n\n继续从一条真实提问开始，记录评论是否仍然出现具体需求。',
                     'comparisons': [
                         {'kind': 'value', 'hypothesis': '评论出现实际需求', 'verdict': '出现了', 'evidence': '已录入的评论提到具体使用问题。'},
                         {'kind': 'cheat', 'hypothesis': '最可能落在基础盘', 'verdict': '落在该区间', 'evidence': f'72 小时记录为 {plays} 播放。'}],
                     'observations': [{'text': f'{plays} 播放：真实提问可以作为下一条的开头素材。',
                                       'evidence': '这次录入的评论出现了具体需求。',
                                       'next_action': '下一条挑一条真实提问开头，继续记录同一窗口评论。'}]}
        else:
            raise AssertionError('Unexpected fixture task: ' + task)
        return json.dumps(value, ensure_ascii=False)


if __name__ == '__main__':
    runtime = ROOT / 'runtime'
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime, prefix='browser-data-') as directory:
        with create_server(port=0, data_root=Path(directory), model=FixtureModel()) as server:
            print('http://127.0.0.1:' + str(server.server_port), flush=True)
            server.serve_forever()

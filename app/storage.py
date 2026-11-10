"""Atomic, revisioned local storage. User files are never repaired implicitly."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid
from datetime import datetime, timezone, timedelta

ZONE = timezone(timedelta(hours=8))


def now():
    return datetime.now(ZONE).isoformat(timespec='seconds')


def new_id():
    return uuid.uuid4().hex


def digest(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class StorageError(Exception):
    status = 400


class Conflict(StorageError):
    status = 409
    def __init__(self, work):
        super().__init__('作品已在其他窗口更新，请核对当前版本后再保存。')
        self.work = work


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    tmp = path.with_name(path.name + '.' + new_id() + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        # Verify the actual filesystem name and strict UTF-8 bytes after saving.
        if path.name not in [p.name for p in path.parent.iterdir()]:
            raise StorageError('保存后的文件名核验失败。')
        if path.read_bytes().decode('utf-8', errors='strict') != text:
            raise StorageError('保存后的文件内容核验失败。')
    finally:
        if tmp.exists():
            tmp.unlink()


def read_json(path):
    try:
        return json.loads(Path(path).read_bytes().decode('utf-8', errors='strict'))
    except (ValueError, UnicodeError, OSError) as exc:
        raise StorageError('资料读取失败，已保留原文件：' + str(path)) from None


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'works').mkdir(exist_ok=True)
        self.lock = threading.RLock()

    def path(self, work_id):
        if not isinstance(work_id, str) or not re.fullmatch(r'[0-9a-f]{32}', work_id):
            raise StorageError('作品编号无效。')
        return self.root / 'works' / (work_id + '.json')

    def get(self, work_id, include_deleted=False):
        with self.lock:
            path = self.path(work_id)
            if not path.exists():
                err = StorageError('找不到这条作品。')
                err.status = 404
                raise err
            value = read_json(path)
            if not isinstance(value, dict) or value.get('id') != work_id or type(value.get('revision')) is not int:
                raise StorageError('作品格式损坏，已保留原文件：' + str(path))
            if value.get('deleted_at') and not include_deleted:
                err = StorageError('这条作品已删除。')
                err.status = 404
                raise err
            try:
                records = [(kind,record) for kind,items in value['judgments'].items() for record in items if record['status']=='locked']
                records += [('publication',p) for p in value['publications']]
                for kind,record in records:
                    if kind not in ('value','cheat','publication') or not re.fullmatch(r'[0-9a-f]{32}',record['id']):
                        raise ValueError()
                    if digest(record['snapshot']) != record['basis_hash']:
                        raise ValueError()
                    frozen = self.root/'snapshots'/work_id/(kind+'-'+record['id']+'.json')
                    if not frozen.exists() or read_json(frozen)!=record:
                        raise ValueError()
            except (KeyError,TypeError,ValueError):
                raise StorageError('锁定快照核验失败，已保留作品与快照原文件：'+str(path)) from None
            return value

    def create(self, title='', applied_observation=None):
        stamp = now()
        title = title.strip()
        work = dict(id=new_id(), title=title, title_mode='manual' if title and title != '未命名作品' else 'auto', revision=1, stage=0,
                    created_at=stamp, updated_at=stamp, material='', angles=[], questions=[],
                    angle=None, package={}, plan={}, spoken='', spoken_versions=[], reviews=[],
                    clean_script='', final=dict(script='',title='',cover='',opening='',platform='',account='',duration_seconds=None,confirmed=False),
                    judgments={'value': [], 'cheat': []}, publications=[], metrics=[], retros=[],
                    applied_observation=applied_observation, method_runs=[])
        with self.lock:
            atomic_json(self.path(work['id']), work)
        return copy.deepcopy(work)

    def update(self, work_id, revision, mutate):
        with self.lock:
            work = self.get(work_id)
            if type(revision) is not int or work['revision'] != revision:
                raise Conflict(work)
            candidate = copy.deepcopy(work)
            mutate(candidate)
            candidate['revision'] += 1
            candidate['updated_at'] = now()
            atomic_json(self.path(work_id), candidate)
            return copy.deepcopy(candidate)

    def list(self):
        with self.lock:
            works = [self.get(path.stem, include_deleted=True) for path in (self.root / 'works').glob('*.json')]
            works = [work for work in works if not work.get('deleted_at')]
            return sorted(works, key=lambda w: (w['updated_at'], w['id']), reverse=True)

    def delete(self, work_id, revision):
        with self.lock:
            work = self.get(work_id, include_deleted=True)
            if work.get('deleted_at'):
                return
            if type(revision) is not int or work['revision'] != revision:
                raise Conflict(work)
            self.update(work_id, revision, lambda candidate: candidate.update(deleted_at=now()))

    def freeze(self, work_id, kind, record):
        self.path(work_id)
        if kind not in ('value', 'cheat', 'publication') or not re.fullmatch(r'[0-9a-f]{32}', record['id']):
            raise StorageError('快照编号无效。')
        path = self.root / 'snapshots' / work_id / (kind + '-' + record['id'] + '.json')
        if path.exists():
            canonical=read_json(path)
            comparable=lambda item:{k:v for k,v in item.items() if k!='locked_at'}
            if canonical==record:
                return copy.deepcopy(canonical)
            # Work-file replace may fail after freezing. Retrying adopts the first
            # immutable lock and its original timestamp, never regenerating a result.
            if kind in ('value','cheat') and isinstance(canonical,dict) and canonical.get('status')=='locked' and canonical.get('locked_at') and digest(canonical.get('snapshot'))==canonical.get('basis_hash') and comparable(canonical)==comparable(record):
                return copy.deepcopy(canonical)
            else:
                raise StorageError('已有锁定快照受保护，停止写入。')
        atomic_json(path, record)
        return copy.deepcopy(record)

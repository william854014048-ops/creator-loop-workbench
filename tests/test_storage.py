import json
import tempfile
import unittest
from pathlib import Path

from app.storage import Store, Conflict, StorageError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)

    def test_reopen_conflict_and_safe_ids(self):
        w = self.store.create('中文')
        w = self.store.update(w['id'], w['revision'], lambda x: x.update(material='原话'))
        self.assertEqual(Store(self.tmp.name).get(w['id'])['material'], '原话')
        with self.assertRaises(Conflict):
            self.store.update(w['id'], 1, lambda x: x.update(title='过期'))
        with self.assertRaises(StorageError):
            self.store.get('../other')

    def test_corrupt_json_is_retained(self):
        w = self.store.create()
        path = self.store.root / 'works' / (w['id'] + '.json')
        path.write_bytes(b'{broken')
        with self.assertRaises(StorageError):
            self.store.get(w['id'])
        self.assertEqual(path.read_bytes(), b'{broken')

    def test_delete_conflict_reopen_and_idempotent_retry(self):
        work = self.store.create('要删除的作品')
        saved = self.store.update(work['id'], work['revision'], lambda item: item.update(material='新素材'))
        with self.assertRaises(Conflict):
            self.store.delete(work['id'], work['revision'])
        self.assertEqual(len(self.store.list()), 1)
        self.store.delete(saved['id'], saved['revision'])
        self.store.delete(saved['id'], saved['revision'])
        reopened = Store(self.tmp.name)
        self.assertEqual(reopened.list(), [])
        with self.assertRaises(StorageError) as error:
            reopened.get(saved['id'])
        self.assertEqual(error.exception.status, 404)
        retained = reopened.get(saved['id'], include_deleted=True)
        self.assertEqual(retained['material'], '新素材')
        self.assertTrue(retained['deleted_at'])
        with self.assertRaises(StorageError):
            reopened.update(saved['id'], retained['revision'], lambda item: item.update(title='重新出现'))

"""Reusable visual directions shared by batch and inbox workflows."""
import json
from core import Problem, now
from models import text
from workflow_visual import options

SCHEMA_SQL='''CREATE TABLE IF NOT EXISTS visual_presets(
  name TEXT PRIMARY KEY, recipe TEXT NOT NULL, updated_at TEXT NOT NULL);'''

class VisualPresets:
    def __init__(self,store):
        self.store=store
        with store.connect() as c:c.executescript(SCHEMA_SQL)

    def list(self):
        with self.store.connect() as c:
            rows=c.execute('SELECT name,recipe,updated_at FROM visual_presets ORDER BY name COLLATE NOCASE').fetchall()
        return [{'name':row['name'],'recipe':json.loads(row['recipe']),'updated_at':row['updated_at']} for row in rows]

    def save(self,raw):
        name=text(raw.get('name'),'配方名称',80)
        if '/' in name or '\\' in name:raise Problem('配方名称不能包含路径分隔符')
        recipe=options(raw.get('recipe'))
        # A preset never carries a particular product's reference photos.
        if 'reference_asset_ids' in recipe:raise Problem('配方不能保存单个商品的原图')
        with self.store.connect() as c:
            exists=c.execute('SELECT 1 FROM visual_presets WHERE name=?',(name,)).fetchone()
            if not exists and c.execute('SELECT count(*) FROM visual_presets').fetchone()[0]>=50:raise Problem('最多保存50个视觉配方，请先删除不用的配方')
            c.execute('INSERT INTO visual_presets(name,recipe,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET recipe=excluded.recipe,updated_at=excluded.updated_at',
                (name,json.dumps(recipe,ensure_ascii=False),now()))
        return self.list()

    def delete(self,raw):
        name=text(raw.get('name'),'配方名称',80)
        with self.store.connect() as c:
            if not c.execute('DELETE FROM visual_presets WHERE name=?',(name,)).rowcount:raise Problem('视觉配方不存在',404)
        return self.list()

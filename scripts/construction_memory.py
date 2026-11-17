"""Portable local photo-memory CLI for conversational construction review."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from PIL import ExifTags, Image
except ImportError:
    ExifTags = Image = None

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
CONFIG = Path(os.environ.get('CONSTRUCTION_MEMORY_CONFIG', Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'construction-memory' / 'config.json'))


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def config():
    if not CONFIG.is_file():
        raise ValueError(f'尚未配置项目：{CONFIG}。先运行 configure。')
    return json.loads(CONFIG.read_text(encoding='utf-8'))


def connect(data_dir):
    path = Path(data_dir) / 'db' / 'site_memory.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS photos(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),file_path TEXT NOT NULL,sha256 TEXT NOT NULL,original_name TEXT NOT NULL,captured_at TEXT,captured_at_source TEXT NOT NULL,captured_timezone TEXT,uploaded_at TEXT NOT NULL,width INTEGER,height INTEGER);
    CREATE TABLE IF NOT EXISTS photo_location_reviews(photo_id INTEGER PRIMARY KEY REFERENCES photos(id),status TEXT NOT NULL,drawing_page_id INTEGER,building TEXT,floor TEXT,area TEXT,camera_direction TEXT,recorded_date TEXT,note TEXT,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS photo_progress_reviews(photo_id INTEGER PRIMARY KEY REFERENCES photos(id),status TEXT NOT NULL,annotations_json TEXT NOT NULL,progress_text TEXT,note TEXT,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS progress_context(photo_id INTEGER PRIMARY KEY REFERENCES photos(id),building TEXT,area TEXT,work_item TEXT,stage TEXT,location_key TEXT,status TEXT NOT NULL,note TEXT,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS photo_comparisons(before_photo_id INTEGER NOT NULL REFERENCES photos(id),after_photo_id INTEGER NOT NULL REFERENCES photos(id),status TEXT NOT NULL,change_note TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(before_photo_id,after_photo_id));
    CREATE TABLE IF NOT EXISTS site_observations(id INTEGER PRIMARY KEY,photo_id INTEGER NOT NULL REFERENCES photos(id),project_id INTEGER NOT NULL REFERENCES projects(id),spatial_region_id INTEGER,observed_activity TEXT NOT NULL,observed_state TEXT NOT NULL,camera_direction TEXT,recorded_date TEXT,review_status TEXT NOT NULL,reviewer_note TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS photo_ai_drafts(photo_id INTEGER PRIMARY KEY REFERENCES photos(id),model TEXT NOT NULL,result_json TEXT NOT NULL,status TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY,entity_type TEXT NOT NULL,entity_id INTEGER NOT NULL,action TEXT NOT NULL,old_json TEXT,new_json TEXT,at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS memory_locations(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),code TEXT UNIQUE,building TEXT NOT NULL DEFAULT '',floor TEXT NOT NULL DEFAULT '',area TEXT NOT NULL DEFAULT '',detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,UNIQUE(project_id,building,floor,area,detail));
    CREATE TABLE IF NOT EXISTS photo_memory_locations(photo_id INTEGER PRIMARY KEY REFERENCES photos(id),location_id INTEGER NOT NULL REFERENCES memory_locations(id),status TEXT NOT NULL,source TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS photo_location_mentions(photo_id INTEGER NOT NULL REFERENCES photos(id),location_id INTEGER NOT NULL REFERENCES memory_locations(id),status TEXT NOT NULL,source TEXT NOT NULL,annotation_x REAL,annotation_y REAL,evidence_path TEXT,note TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(photo_id,location_id));
    CREATE TABLE IF NOT EXISTS photo_date_reviews(photo_id INTEGER PRIMARY KEY REFERENCES photos(id),recorded_date TEXT NOT NULL,status TEXT NOT NULL,source TEXT NOT NULL,note TEXT,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS photo_scope_reviews(photo_id INTEGER PRIMARY KEY REFERENCES photos(id),scope TEXT NOT NULL,status TEXT NOT NULL,source TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS project_domain_rules(project_id INTEGER NOT NULL REFERENCES projects(id),rule_key TEXT NOT NULL,statement TEXT NOT NULL,source TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(project_id,rule_key));
    ''')
    return db


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def photo_time(path, image):
    exif = {ExifTags.TAGS.get(k, k): v for k, v in image.getexif().items()} if image is not None else {}
    for field, source in (('DateTimeOriginal', 'exif_original_unverified'), ('DateTime', 'exif_datetime_unverified')):
        if exif.get(field):
            return str(exif[field]).replace(':', '-', 2).replace(' ', 'T', 1), source
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(), 'file_modified_unverified'


def project_settings(name=None):
    settings = config()
    projects = settings.get('projects', {})
    name = name or settings.get('default_project')
    if name not in projects:
        raise ValueError(f'项目未配置：{name}；可用项目：{list(projects)}')
    return name, projects[name]


def configure(args):
    data_dir = Path(args.data_dir).expanduser().resolve()
    folder = Path(args.photos).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f'照片文件夹不存在：{folder}')
    settings = json.loads(CONFIG.read_text(encoding='utf-8')) if CONFIG.is_file() else {'projects': {}}
    with connect(data_dir) as db:
        db.execute('INSERT OR IGNORE INTO projects(name,created_at) VALUES(?,?)', (args.project, now()))
        project_id = db.execute('SELECT id FROM projects WHERE name=?', (args.project,)).fetchone()['id']
    previous = settings.setdefault('projects', {}).get(args.project, {})
    settings['projects'][args.project] = {**previous, 'data_dir': str(data_dir), 'photos_dir': str(folder), 'project_id': project_id}
    settings['default_project'] = args.project
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    emit({'project': args.project, 'project_id': project_id, 'data_dir': str(data_dir), 'photos_dir': str(folder), 'config': str(CONFIG)})


def ingest(args):
    name, settings = project_settings(args.project)
    root = Path(settings['photos_dir'])
    if not root.is_dir():
        raise ValueError(f'照片文件夹不存在：{root}')
    files = sorted((p for p in root.rglob('*') if p.is_file() and not p.is_symlink() and p.suffix.lower() in EXTENSIONS), key=lambda p: str(p).lower())
    if len(files) > 2000:
        raise ValueError('单次扫描超过 2000 张，请分批处理')
    data_dir = Path(settings['data_dir'])
    pid = settings['project_id']
    result = {'project': name, 'scanned': len(files), 'imported': [], 'pending_analysis': [], 'duplicate_count': 0, 'errors': []}
    with connect(data_dir) as db:
        for source in files:
            try:
                if source.stat().st_size > 40 * 1024 * 1024:
                    raise ValueError('照片超过 40 MB')
                if Image is not None:
                    with Image.open(source) as image:
                        if image.width * image.height > 100_000_000:
                            raise ValueError('照片像素过大')
                        image.verify()
                digest = sha256(source)
                old = db.execute('SELECT id,file_path FROM photos WHERE project_id=? AND sha256=?', (pid, digest)).fetchone()
                if old:
                    result['duplicate_count'] += 1
                    photo_id = old['id']
                    rel = old['file_path']
                else:
                    if Image is not None:
                        with Image.open(source) as image:
                            width, height = image.size
                            captured_at, time_source = photo_time(source, image)
                    else:
                        width = height = None
                        captured_at, time_source = photo_time(source, None)
                    target = data_dir / 'photos' / str(pid) / (digest + source.suffix.lower())
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        shutil.copy2(source, target)
                    rel = target.relative_to(data_dir).as_posix()
                    photo_id = db.execute('''INSERT INTO photos(project_id,file_path,sha256,original_name,captured_at,captured_at_source,uploaded_at,width,height)
                        VALUES(?,?,?,?,?,?,?,?,?)''', (pid, rel, digest, source.name, captured_at, time_source, now(), width, height)).lastrowid
                    result['imported'].append({'photo_id': photo_id, 'name': source.name})
                observed = db.execute('SELECT 1 FROM site_observations WHERE photo_id=? LIMIT 1', (photo_id,)).fetchone()
                if not observed:
                    result['pending_analysis'].append({'photo_id': photo_id, 'path': str((data_dir / rel).resolve()), 'name': source.name})
            except Exception as exc:
                result['errors'].append({'path': str(source), 'error': f'{type(exc).__name__}: {exc}'})
        db.commit()
    emit(result)


def records(args):
    name, settings = project_settings(args.project)
    data_dir = Path(settings['data_dir'])
    pid = settings['project_id']
    with connect(data_dir) as db:
        rows = [dict(row) for row in db.execute('''SELECT p.id,p.original_name,p.file_path,p.captured_at,p.captured_at_source,p.uploaded_at,
            l.status AS location_status,l.building AS reviewed_building,l.floor AS reviewed_floor,l.area AS reviewed_area,l.recorded_date,l.note AS location_note,
            dl.recorded_date AS confirmed_date,dl.status AS date_status,dl.source AS date_source,
            sr.scope AS project_scope,sr.status AS scope_status,sr.source AS scope_source,
            ml.code AS location_code,ml.building AS location_building,ml.floor AS location_floor,ml.area AS location_area,ml.detail AS location_detail,pl.status AS memory_location_status,
            c.building AS context_building,c.area AS context_area,c.work_item,c.stage,c.location_key,c.status AS context_status,
            o.observed_activity,o.observed_state,o.review_status AS observation_status,o.reviewer_note,
            r.progress_text,r.status AS progress_status
            FROM photos p LEFT JOIN photo_location_reviews l ON l.photo_id=p.id
            LEFT JOIN photo_date_reviews dl ON dl.photo_id=p.id
            LEFT JOIN photo_scope_reviews sr ON sr.photo_id=p.id
            LEFT JOIN photo_memory_locations pl ON pl.photo_id=p.id
            LEFT JOIN memory_locations ml ON ml.id=pl.location_id
            LEFT JOIN progress_context c ON c.photo_id=p.id
            LEFT JOIN site_observations o ON o.id=(SELECT id FROM site_observations WHERE photo_id=p.id ORDER BY CASE review_status WHEN 'confirmed' THEN 0 ELSE 1 END,id DESC LIMIT 1)
            LEFT JOIN photo_progress_reviews r ON r.photo_id=p.id
            WHERE p.project_id=? ORDER BY COALESCE(dl.recorded_date,l.recorded_date,p.captured_at) DESC,p.id DESC''', (pid,))]
        comparisons = [dict(row) for row in db.execute('''SELECT c.* FROM photo_comparisons c JOIN photos p ON p.id=c.before_photo_id WHERE p.project_id=?''', (pid,))]
        mentions = [dict(row) for row in db.execute('''SELECT pm.photo_id,ml.code,ml.building,ml.floor,ml.area,ml.detail,pm.status,pm.source,pm.annotation_x,pm.annotation_y,pm.evidence_path,pm.note
            FROM photo_location_mentions pm JOIN memory_locations ml ON ml.id=pm.location_id
            JOIN photos p ON p.id=pm.photo_id WHERE p.project_id=? ORDER BY pm.photo_id,ml.id''', (pid,))]
        project_rules = [dict(row) for row in db.execute('SELECT rule_key,statement,source,updated_at FROM project_domain_rules WHERE project_id=? ORDER BY rule_key', (pid,))]
    mentions_by_photo = {}
    for mention in mentions:
        mentions_by_photo.setdefault(mention.pop('photo_id'), []).append(mention)
    for row in rows:
        row['mentioned_locations'] = mentions_by_photo.get(row['id'], [])
        row['photo_path'] = str((data_dir / row['file_path']).resolve())
        row['effective_date'] = (row['confirmed_date'] or row['recorded_date'] or row['captured_at'] or '')[:10] or None
        row['effective_date_source'] = row['date_source'] or ('legacy_review' if row['recorded_date'] else row['captured_at_source'])
    if args.photo_id is not None:
        rows = [r for r in rows if r['id'] == args.photo_id]
    if args.date:
        rows = [r for r in rows if r['effective_date'] == args.date]
    from_date = getattr(args, 'from_date', None)
    to_date = getattr(args, 'to_date', None)
    month = getattr(args, 'month', None)
    location_code = getattr(args, 'location_code', None)
    review_status = getattr(args, 'review_status', None)
    scope = getattr(args, 'scope', None)
    if month:
        datetime.strptime(month, '%Y-%m')
        rows = [r for r in rows if r['effective_date'] and r['effective_date'].startswith(month + '-')]
    if from_date:
        datetime.strptime(from_date, '%Y-%m-%d')
        rows = [r for r in rows if r['effective_date'] and r['effective_date'] >= from_date]
    if to_date:
        datetime.strptime(to_date, '%Y-%m-%d')
        rows = [r for r in rows if r['effective_date'] and r['effective_date'] <= to_date]
    if location_code:
        rows = [r for r in rows if r['location_code'] == location_code or any(m['code'] == location_code for m in r['mentioned_locations'])]
    if review_status:
        rows = [r for r in rows if r['observation_status'] == review_status]
    if scope:
        rows = [r for r in rows if r['project_scope'] == scope]
    if args.location:
        rows = [r for r in rows if args.location in ' '.join(str(r.get(k) or '') for k in ('location_building','location_floor','location_area','location_detail','reviewed_building','reviewed_floor','reviewed_area','context_building','context_area','reviewer_note'))
                or any(args.location in ' '.join(str(m.get(k) or '') for k in ('building','floor','area','detail')) for m in r['mentioned_locations'])]
    if args.keyword:
        rows = [r for r in rows if args.keyword in ' '.join(str(v or '') for v in r.values())]
    matched_count = len(rows)
    rows = rows[:args.limit]
    ids = {r['id'] for r in rows}
    emit({'project': name, 'count': len(rows), 'matched_count': matched_count, 'photos': rows, 'comparisons': [c for c in comparisons if c['before_photo_id'] in ids or c['after_photo_id'] in ids], 'project_rules': project_rules})


def record(args):
    name, settings = project_settings(args.project)
    payload = json.loads(Path(args.json).read_text(encoding='utf-8'))
    for key in ('photo_id', 'observed_activity', 'observed_state'):
        if not payload.get(key):
            raise ValueError(f'缺少字段：{key}')
    pid = settings['project_id']
    with connect(settings['data_dir']) as db:
        photo = db.execute('SELECT id,captured_at FROM photos WHERE id=? AND project_id=?', (payload['photo_id'], pid)).fetchone()
        if not photo:
            raise ValueError('照片不属于此项目')
        old = db.execute('SELECT id FROM site_observations WHERE photo_id=? LIMIT 1', (photo['id'],)).fetchone()
        if old:
            raise ValueError('照片已有观察记录；请在复核流程中修改，避免覆盖')
        t = now()
        note = '位置线索：' + str(payload.get('location_clues') or '无') + '；不确定点：' + str(payload.get('uncertainties') or '未说明')
        observation_id = db.execute('''INSERT INTO site_observations(photo_id,project_id,observed_activity,observed_state,recorded_date,review_status,reviewer_note,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)''', (photo['id'], pid, payload['observed_activity'], payload['observed_state'], (photo['captured_at'] or '')[:10] or None, 'candidate', note, t, t)).lastrowid
        db.execute('''INSERT OR REPLACE INTO photo_ai_drafts(photo_id,model,result_json,status,updated_at) VALUES(?,?,?,?,?)''',
                   (photo['id'], 'codex-conversation', json.dumps(payload, ensure_ascii=False), 'candidate', t))
        db.execute('''INSERT OR IGNORE INTO progress_context(photo_id,work_item,stage,status,note,updated_at) VALUES(?,?,?,?,?,?)''',
                   (photo['id'], str(payload['observed_activity'])[:120], str(payload['observed_state'])[:120], 'candidate', '对话模型初判，部位待核实', t))
        db.execute('INSERT INTO audit_events(entity_type,entity_id,action,new_json,at) VALUES(?,?,?,?,?)',
                   ('observation', observation_id, 'model_candidate', json.dumps(payload, ensure_ascii=False), t))
    emit({'project': name, 'photo_id': photo['id'], 'observation_id': observation_id, 'status': 'candidate'})


def review(args):
    """Apply an explicit user correction without erasing earlier evidence."""
    name, settings = project_settings(args.project)
    payload = json.loads(Path(args.json).read_text(encoding='utf-8'))
    photo_id = payload.get('photo_id')
    if not isinstance(photo_id, int) or not any(k in payload for k in ('date', 'location', 'observation', 'scope')):
        raise ValueError('需要 photo_id，以及 date、location、observation 或 scope 中至少一项')
    pid = settings['project_id']
    changed = {}
    with connect(settings['data_dir']) as db:
        photo = db.execute('SELECT id,captured_at FROM photos WHERE id=? AND project_id=?', (photo_id, pid)).fetchone()
        if not photo:
            raise ValueError('照片不属于此项目')
        t = now()
        if 'scope' in payload:
            scope = str(payload['scope']).strip()
            if not scope:
                raise ValueError('scope 不能为空')
            old = db.execute('SELECT * FROM photo_scope_reviews WHERE photo_id=?', (photo_id,)).fetchone()
            db.execute('''INSERT INTO photo_scope_reviews(photo_id,scope,status,source,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(photo_id) DO UPDATE SET scope=excluded.scope,status=excluded.status,source=excluded.source,updated_at=excluded.updated_at''',
                (photo_id, scope, 'confirmed', 'user_reported', t))
            db.execute('INSERT INTO audit_events(entity_type,entity_id,action,old_json,new_json,at) VALUES(?,?,?,?,?,?)',
                       ('photo_scope', photo_id, 'user_review', json.dumps(dict(old), ensure_ascii=False) if old else None, json.dumps({'scope': scope, 'source': 'user_reported'}, ensure_ascii=False), t))
            changed['scope'] = scope
        if 'date' in payload:
            value = payload['date']
            if not isinstance(value, str):
                raise ValueError('date 应为 YYYY-MM-DD')
            datetime.strptime(value, '%Y-%m-%d')
            old = db.execute('SELECT * FROM photo_date_reviews WHERE photo_id=?', (photo_id,)).fetchone()
            db.execute('''INSERT INTO photo_date_reviews(photo_id,recorded_date,status,source,note,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(photo_id) DO UPDATE SET recorded_date=excluded.recorded_date,status=excluded.status,source=excluded.source,note=excluded.note,updated_at=excluded.updated_at''',
                (photo_id, value, 'confirmed', 'user_reported', payload.get('note'), t))
            db.execute('INSERT INTO audit_events(entity_type,entity_id,action,old_json,new_json,at) VALUES(?,?,?,?,?,?)',
                       ('photo_date', photo_id, 'user_review', json.dumps(dict(old), ensure_ascii=False) if old else None, json.dumps({'date': value, 'source': 'user_reported'}, ensure_ascii=False), t))
            changed['date'] = value
        if 'location' in payload:
            loc = payload['location']
            if not isinstance(loc, dict):
                raise ValueError('location 应为对象')
            if loc.get('code'):
                entity = db.execute('SELECT * FROM memory_locations WHERE project_id=? AND code=?', (pid, loc['code'])).fetchone()
                if not entity:
                    raise ValueError('位置编号不存在或不属于此项目')
            else:
                parts = tuple(str(loc.get(k) or '').strip() for k in ('building', 'floor', 'area', 'detail'))
                if not any(parts):
                    raise ValueError('位置至少需要楼栋、楼层、区域或具体部位之一')
                db.execute('INSERT OR IGNORE INTO memory_locations(project_id,building,floor,area,detail,created_at) VALUES(?,?,?,?,?,?)',
                           (pid, *parts, t))
                entity = db.execute('SELECT * FROM memory_locations WHERE project_id=? AND building=? AND floor=? AND area=? AND detail=?', (pid, *parts)).fetchone()
                if not entity['code']:
                    code = f'P{pid}-L{entity["id"]:04d}'
                    db.execute('UPDATE memory_locations SET code=? WHERE id=?', (code, entity['id']))
                    entity = db.execute('SELECT * FROM memory_locations WHERE id=?', (entity['id'],)).fetchone()
            old = db.execute('SELECT * FROM photo_memory_locations WHERE photo_id=?', (photo_id,)).fetchone()
            db.execute('''INSERT INTO photo_memory_locations(photo_id,location_id,status,source,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(photo_id) DO UPDATE SET location_id=excluded.location_id,status=excluded.status,source=excluded.source,updated_at=excluded.updated_at''',
                (photo_id, entity['id'], 'confirmed', 'user_reported', t))
            db.execute('INSERT INTO audit_events(entity_type,entity_id,action,old_json,new_json,at) VALUES(?,?,?,?,?,?)',
                       ('photo_location', photo_id, 'user_review', json.dumps(dict(old), ensure_ascii=False) if old else None, json.dumps({'location_code': entity['code'], 'source': 'user_reported'}, ensure_ascii=False), t))
            changed['location_code'] = entity['code']
        if 'observation' in payload:
            observation = payload['observation']
            if not isinstance(observation, dict):
                raise ValueError('observation 应为对象')
            old = db.execute('SELECT * FROM site_observations WHERE photo_id=? ORDER BY id DESC LIMIT 1', (photo_id,)).fetchone()
            activity = observation.get('activity') or (old['observed_activity'] if old else None)
            state = observation.get('state') or (old['observed_state'] if old else None)
            if not activity or not state:
                raise ValueError('新观察需要 activity 和 state')
            obs_id = db.execute('''INSERT INTO site_observations(photo_id,project_id,observed_activity,observed_state,recorded_date,review_status,reviewer_note,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)''', (photo_id, pid, activity, state, payload.get('date') or (old['recorded_date'] if old else (photo['captured_at'] or '')[:10] or None), 'confirmed', '用户在对话中确认或修正；' + str(payload.get('note') or ''), t, t)).lastrowid
            db.execute('INSERT INTO audit_events(entity_type,entity_id,action,old_json,new_json,at) VALUES(?,?,?,?,?,?)',
                       ('observation', obs_id, 'user_review', json.dumps(dict(old), ensure_ascii=False) if old else None, json.dumps({'activity': activity, 'state': state, 'source': 'user_reported'}, ensure_ascii=False), t))
            changed['observation_id'] = obs_id
    emit({'project': name, 'photo_id': photo_id, 'reviewed': changed})


def review_scope_batch(args):
    name, settings = project_settings(args.project)
    scope = args.scope.strip()
    if not scope:
        raise ValueError('scope 不能为空')
    with connect(settings['data_dir']) as db:
        photos = db.execute('SELECT id FROM photos WHERE project_id=? ORDER BY id', (settings['project_id'],)).fetchall()
        if len(photos) != args.expected_count:
            raise ValueError(f'照片数量为 {len(photos)}，与预期 {args.expected_count} 不一致；没有写入')
        t = now()
        for photo in photos:
            photo_id = photo['id']
            old = db.execute('SELECT * FROM photo_scope_reviews WHERE photo_id=?', (photo_id,)).fetchone()
            db.execute('''INSERT INTO photo_scope_reviews(photo_id,scope,status,source,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(photo_id) DO UPDATE SET scope=excluded.scope,status=excluded.status,source=excluded.source,updated_at=excluded.updated_at''',
                (photo_id, scope, 'confirmed', 'user_reported', t))
            db.execute('INSERT INTO audit_events(entity_type,entity_id,action,old_json,new_json,at) VALUES(?,?,?,?,?,?)',
                       ('photo_scope', photo_id, 'user_review_batch', json.dumps(dict(old), ensure_ascii=False) if old else None,
                        json.dumps({'scope': scope, 'source': 'user_reported'}, ensure_ascii=False), t))
    emit({'project': name, 'scope': scope, 'updated_photos': len(photos), 'source': 'user_reported'})


def review_multi_location(args):
    """Record multiple user-identified buildings visible in one photograph."""
    name, settings = project_settings(args.project)
    payload = json.loads(Path(args.json).read_text(encoding='utf-8'))
    photo_id = payload.get('photo_id')
    locations = payload.get('locations')
    if not isinstance(photo_id, int) or not isinstance(locations, list) or not locations:
        raise ValueError('需要 photo_id 和非空 locations 列表')
    if len(locations) > 50:
        raise ValueError('单张照片最多登记 50 个位置')
    pid = settings['project_id']
    confirmed = []
    with connect(settings['data_dir']) as db:
        if not db.execute('SELECT 1 FROM photos WHERE id=? AND project_id=?', (photo_id, pid)).fetchone():
            raise ValueError('照片不属于此项目')
        t = now()
        for loc in locations:
            if not isinstance(loc, dict):
                raise ValueError('locations 的每一项必须是对象')
            parts = tuple(str(loc.get(k) or '').strip() for k in ('building', 'floor', 'area', 'detail'))
            if not any(parts):
                raise ValueError('每个位置至少要有楼栋、楼层、区域或具体部位')
            x, y = loc.get('annotation_x'), loc.get('annotation_y')
            if (x is None) != (y is None):
                raise ValueError('标注坐标需要同时提供 x 和 y')
            if x is not None:
                if not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in (x, y)):
                    raise ValueError('标注坐标须为 0 到 1 的归一化数值')
            db.execute('INSERT OR IGNORE INTO memory_locations(project_id,building,floor,area,detail,created_at) VALUES(?,?,?,?,?,?)',
                       (pid, *parts, t))
            entity = db.execute('SELECT * FROM memory_locations WHERE project_id=? AND building=? AND floor=? AND area=? AND detail=?', (pid, *parts)).fetchone()
            if not entity['code']:
                code = f'P{pid}-L{entity["id"]:04d}'
                db.execute('UPDATE memory_locations SET code=? WHERE id=?', (code, entity['id']))
                entity = db.execute('SELECT * FROM memory_locations WHERE id=?', (entity['id'],)).fetchone()
            old = db.execute('SELECT * FROM photo_location_mentions WHERE photo_id=? AND location_id=?', (photo_id, entity['id'])).fetchone()
            evidence = str(loc.get('evidence_path') or payload.get('evidence_path') or '').strip()
            note = str(loc.get('note') or payload.get('note') or '').strip()
            db.execute('''INSERT INTO photo_location_mentions(photo_id,location_id,status,source,annotation_x,annotation_y,evidence_path,note,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(photo_id,location_id) DO UPDATE SET status=excluded.status,source=excluded.source,
                annotation_x=excluded.annotation_x,annotation_y=excluded.annotation_y,evidence_path=excluded.evidence_path,note=excluded.note,updated_at=excluded.updated_at''',
                (photo_id, entity['id'], 'confirmed', 'user_annotated', x, y, evidence, note, t))
            db.execute('INSERT INTO audit_events(entity_type,entity_id,action,old_json,new_json,at) VALUES(?,?,?,?,?,?)',
                       ('photo_location_mention', photo_id, 'user_review_multi', json.dumps(dict(old), ensure_ascii=False) if old else None,
                        json.dumps({'location_code': entity['code'], 'annotation_x': x, 'annotation_y': y, 'evidence_path': evidence, 'source': 'user_annotated'}, ensure_ascii=False), t))
            confirmed.append({'code': entity['code'], 'building': entity['building'], 'annotation_x': x, 'annotation_y': y})
    emit({'project': name, 'photo_id': photo_id, 'confirmed_locations': confirmed})


def record_location_candidates(args):
    """Keep model-derived visual matches separate from user-confirmed locations."""
    name, settings = project_settings(args.project)
    payload = json.loads(Path(args.json).read_text(encoding='utf-8'))
    photo_id = payload.get('photo_id')
    locations = payload.get('locations')
    if not isinstance(photo_id, int) or not isinstance(locations, list) or not locations:
        raise ValueError('需要 photo_id 和非空 locations 列表')
    pid = settings['project_id']
    saved = []
    with connect(settings['data_dir']) as db:
        if not db.execute('SELECT 1 FROM photos WHERE id=? AND project_id=?', (photo_id, pid)).fetchone():
            raise ValueError('照片不属于此项目')
        t = now()
        for loc in locations:
            if not isinstance(loc, dict) or not str(loc.get('building') or '').strip():
                raise ValueError('每个候选位置需要 building')
            building = str(loc['building']).strip()
            entity = db.execute("SELECT * FROM memory_locations WHERE project_id=? AND building=? AND floor='' AND area='' AND detail=''", (pid, building)).fetchone()
            if not entity:
                raise ValueError(f'楼栋尚无用户确认的基准位置：{building}')
            x, y = loc.get('annotation_x'), loc.get('annotation_y')
            if (x is None) != (y is None) or (x is not None and not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in (x, y))):
                raise ValueError('标注坐标需同时提供，范围 0 到 1')
            confidence = str(loc.get('confidence') or 'medium').strip()
            if confidence not in ('high', 'medium', 'low'):
                raise ValueError('confidence 只能是 high、medium 或 low')
            note = f"confidence={confidence}; {str(loc.get('note') or payload.get('note') or '').strip()}"
            evidence = str(loc.get('evidence_path') or payload.get('evidence_path') or '').strip()
            old = db.execute('SELECT * FROM photo_location_mentions WHERE photo_id=? AND location_id=?', (photo_id, entity['id'])).fetchone()
            if old and old['status'] == 'confirmed':
                saved.append({'building': building, 'status': 'kept_confirmed'})
                continue
            db.execute('''INSERT INTO photo_location_mentions(photo_id,location_id,status,source,annotation_x,annotation_y,evidence_path,note,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(photo_id,location_id) DO UPDATE SET status=excluded.status,source=excluded.source,
                annotation_x=excluded.annotation_x,annotation_y=excluded.annotation_y,evidence_path=excluded.evidence_path,note=excluded.note,updated_at=excluded.updated_at''',
                (photo_id, entity['id'], 'candidate', 'model_visual_match', x, y, evidence, note, t))
            db.execute('INSERT INTO audit_events(entity_type,entity_id,action,old_json,new_json,at) VALUES(?,?,?,?,?,?)',
                       ('photo_location_mention', photo_id, 'model_candidate', json.dumps(dict(old), ensure_ascii=False) if old else None,
                        json.dumps({'location_code': entity['code'], 'annotation_x': x, 'annotation_y': y, 'note': note, 'source': 'model_visual_match'}, ensure_ascii=False), t))
            saved.append({'building': building, 'status': 'candidate', 'confidence': confidence})
    emit({'project': name, 'photo_id': photo_id, 'locations': saved})


def record_comparison(args):
    """Store a traceable, model-derived change between two dated photos."""
    name, settings = project_settings(args.project)
    payload = json.loads(Path(args.json).read_text(encoding='utf-8'))
    before_id, after_id = payload.get('before_photo_id'), payload.get('after_photo_id')
    change_note = str(payload.get('change_note') or '').strip()
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (before_id, after_id)) or before_id == after_id:
        raise ValueError('需要两张不同照片的整数编号')
    if not change_note:
        raise ValueError('需要 change_note')
    if len(change_note) > 2000:
        raise ValueError('change_note 不得超过 2000 字')
    pid = settings['project_id']
    with connect(settings['data_dir']) as db:
        photos = db.execute('SELECT id,captured_at,captured_at_source FROM photos WHERE project_id=? AND id IN (?,?)', (pid, before_id, after_id)).fetchall()
        if len(photos) != 2:
            raise ValueError('对比照片必须都属于当前项目')
        by_id = {p['id']: p for p in photos}
        if not by_id[before_id]['captured_at'] or not by_id[after_id]['captured_at']:
            raise ValueError('照片缺少时间线索，不能记录时间先后')
        if by_id[before_id]['captured_at'] >= by_id[after_id]['captured_at']:
            raise ValueError('前一张照片的时间线索应早于后一张')
        old = db.execute('SELECT * FROM photo_comparisons WHERE before_photo_id=? AND after_photo_id=?', (before_id, after_id)).fetchone()
        if old and old['status'] == 'confirmed':
            raise ValueError('已有确认对比，模型候选不得覆盖')
        t = now()
        db.execute('''INSERT INTO photo_comparisons(before_photo_id,after_photo_id,status,change_note,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(before_photo_id,after_photo_id) DO UPDATE SET
            status=excluded.status,change_note=excluded.change_note,updated_at=excluded.updated_at''',
            (before_id, after_id, 'candidate', change_note, t))
        db.execute('INSERT INTO audit_events(entity_type,entity_id,action,old_json,new_json,at) VALUES(?,?,?,?,?,?)',
                   ('photo_comparison', before_id, 'model_candidate', json.dumps(dict(old), ensure_ascii=False) if old else None,
                    json.dumps({'before_photo_id': before_id, 'after_photo_id': after_id, 'status': 'candidate',
                                'change_note': change_note}, ensure_ascii=False), t))
    emit({'project': name, 'before_photo_id': before_id, 'after_photo_id': after_id,
          'status': 'candidate', 'change_note': change_note})


def record_project_rule(args):
    """Store a project-specific interpretation explicitly supplied by the user."""
    name, settings = project_settings(args.project)
    payload = json.loads(Path(args.json).read_text(encoding='utf-8'))
    key = str(payload.get('rule_key') or '').strip()
    statement = str(payload.get('statement') or '').strip()
    if not key or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in key):
        raise ValueError('rule_key 只能包含小写英文字母、数字和下划线')
    if not statement or len(statement) > 1000:
        raise ValueError('statement 需要 1 到 1000 字')
    pid = settings['project_id']
    with connect(settings['data_dir']) as db:
        old = db.execute('SELECT * FROM project_domain_rules WHERE project_id=? AND rule_key=?', (pid, key)).fetchone()
        t = now()
        db.execute('''INSERT INTO project_domain_rules(project_id,rule_key,statement,source,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(project_id,rule_key) DO UPDATE SET
            statement=excluded.statement,source=excluded.source,updated_at=excluded.updated_at''',
            (pid, key, statement, 'user_reported', t))
        db.execute('INSERT INTO audit_events(entity_type,entity_id,action,old_json,new_json,at) VALUES(?,?,?,?,?,?)',
                   ('project_domain_rule', pid, 'user_reported', json.dumps(dict(old), ensure_ascii=False) if old else None,
                    json.dumps({'rule_key': key, 'statement': statement, 'source': 'user_reported'}, ensure_ascii=False), t))
    emit({'project': name, 'rule_key': key, 'statement': statement, 'source': 'user_reported'})


def status(args):
    name, settings = project_settings(args.project)
    with connect(settings['data_dir']) as db:
        pid = settings['project_id']
        total = db.execute('SELECT COUNT(*) FROM photos WHERE project_id=?', (pid,)).fetchone()[0]
        pending = db.execute('SELECT COUNT(*) FROM photos p WHERE p.project_id=? AND NOT EXISTS(SELECT 1 FROM site_observations o WHERE o.photo_id=p.id)', (pid,)).fetchone()[0]
        confirmed = db.execute("SELECT COUNT(DISTINCT p.id) FROM photos p JOIN site_observations o ON o.photo_id=p.id WHERE p.project_id=? AND o.review_status='confirmed'", (pid,)).fetchone()[0]
        confirmed_locations = db.execute('''SELECT COUNT(DISTINCT photo_id) FROM (
            SELECT pl.photo_id FROM photo_memory_locations pl JOIN photos p ON p.id=pl.photo_id WHERE p.project_id=? AND pl.status='confirmed'
            UNION ALL
            SELECT pm.photo_id FROM photo_location_mentions pm JOIN photos p ON p.id=pm.photo_id WHERE p.project_id=? AND pm.status='confirmed')''', (pid, pid)).fetchone()[0]
        confirmed_dates = db.execute('SELECT COUNT(*) FROM photo_date_reviews d JOIN photos p ON p.id=d.photo_id WHERE p.project_id=? AND d.status=?', (pid, 'confirmed')).fetchone()[0]
        confirmed_scopes = db.execute('SELECT COUNT(*) FROM photo_scope_reviews s JOIN photos p ON p.id=s.photo_id WHERE p.project_id=? AND s.status=?', (pid, 'confirmed')).fetchone()[0]
    emit({'project': name, 'project_id': pid, 'photos': total, 'pending_analysis': pending, 'candidate_or_unconfirmed_observations': total - pending - confirmed, 'confirmed_observations': confirmed, 'confirmed_locations': confirmed_locations, 'confirmed_dates': confirmed_dates, 'confirmed_scopes': confirmed_scopes, 'data_dir': settings['data_dir'], 'photos_dir': settings['photos_dir']})


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='施工过程证据记忆库（照片索引）')
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('configure', help='配置项目路径')
    p.add_argument('--project', required=True)
    p.add_argument('--data-dir', required=True)
    p.add_argument('--photos', required=True)
    p.set_defaults(func=configure)
    p = commands.add_parser('ingest', help='登记新增照片并列出待分析图片')
    p.add_argument('--project')
    p.set_defaults(func=ingest)
    p = commands.add_parser('search', help='只读检索证据')
    p.add_argument('--project')
    p.add_argument('--date')
    p.add_argument('--month', help='YYYY-MM')
    p.add_argument('--from-date', help='YYYY-MM-DD')
    p.add_argument('--to-date', help='YYYY-MM-DD')
    p.add_argument('--location')
    p.add_argument('--scope', help='按已确认的项目范围精确筛选')
    p.add_argument('--location-code')
    p.add_argument('--review-status', choices=('candidate', 'confirmed', 'needs_info', 'pending_initial_review'))
    p.add_argument('--keyword')
    p.add_argument('--photo-id', type=int)
    p.add_argument('--limit', type=int, default=100)
    p.set_defaults(func=records)
    p = commands.add_parser('record', help='写入模型观察初稿')
    p.add_argument('--project')
    p.add_argument('--json', required=True)
    p.set_defaults(func=record)
    p = commands.add_parser('review', help='写入用户在对话中确认的日期、位置或观察，保留审计记录')
    p.add_argument('--project')
    p.add_argument('--json', required=True)
    p.set_defaults(func=review)
    p = commands.add_parser('review-scope-batch', help='将用户确认的项目范围写入当前全部照片')
    p.add_argument('--project')
    p.add_argument('--scope', required=True)
    p.add_argument('--expected-count', required=True, type=int)
    p.set_defaults(func=review_scope_batch)
    p = commands.add_parser('review-multi-location', help='登记同一照片中由用户标出的多个位置')
    p.add_argument('--project')
    p.add_argument('--json', required=True)
    p.set_defaults(func=review_multi_location)
    p = commands.add_parser('record-location-candidates', help='登记同一照片中由模型推断的多个候选位置')
    p.add_argument('--project')
    p.add_argument('--json', required=True)
    p.set_defaults(func=record_location_candidates)
    p = commands.add_parser('record-comparison', help='登记两张照片间可追溯的候选变化')
    p.add_argument('--project')
    p.add_argument('--json', required=True)
    p.set_defaults(func=record_comparison)
    p = commands.add_parser('record-project-rule', help='记录用户明确提供的项目工程判断规则')
    p.add_argument('--project')
    p.add_argument('--json', required=True)
    p.set_defaults(func=record_project_rule)
    p = commands.add_parser('status', help='项目数据状态')
    p.add_argument('--project')
    p.set_defaults(func=status)
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        emit({'error': f'{type(exc).__name__}: {exc}'})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

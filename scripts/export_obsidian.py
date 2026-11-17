"""One-way, repeatable SQLite -> Obsidian projection for construction photos."""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import construction_memory as memory

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = ImageOps = None

MARKER = '<!-- construction-photo-memory generated; edit the SQLite record via Codex -->'


def safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .') or '项目'


def yaml(value):
    return json.dumps(value, ensure_ascii=False)


def write_generated(path: Path, content: str):
    if path.exists() and MARKER not in path.read_text(encoding='utf-8'):
        raise ValueError(f'拒绝覆盖非导出笔记：{path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    if content.startswith('---\n'):
        end = content.find('\n---\n', 4)
        if end < 0:
            raise ValueError('YAML frontmatter 未闭合')
        content = content[:end + 5] + MARKER + '\n' + content[end + 5:]
    else:
        content = MARKER + '\n' + content
    temp.write_text(content, encoding='utf-8')
    temp.replace(path)


def write_base(path: Path, content: str):
    marker = '# construction-photo-memory generated'
    if path.exists():
        existing = path.read_text(encoding='utf-8')
        if marker not in existing:
            # Obsidian may normalize a .base file and remove YAML comments.
            if existing.strip() == content.strip():
                return
            raise ValueError(f'拒绝覆盖已修改的表格：{path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(marker + '\n' + content, encoding='utf-8')
    temp.replace(path)


def write_canvas(path: Path, document: dict):
    if path.exists():
        old = json.loads(path.read_text(encoding='utf-8'))
        if any(not str(node.get('id', '')).startswith('cpm-') for node in old.get('nodes', [])):
            raise ValueError(f'拒绝覆盖包含手工节点的 Canvas：{path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def build_canvas(folder: str, project_name: str, rows: list[dict], by_month: dict, by_date: dict, by_location: dict):
    nodes = []
    edges = []
    month_positions = []
    for column, (month, days) in enumerate(sorted(by_month.items())):
        month_x = column * 350
        month_id = f'cpm-month-{month}'
        month_count = sum(len(by_date[day]) for day in days)
        nodes.append({'id': month_id, 'type': 'text',
                      'text': f'## [[{folder}/月份/{month}|{month}]]\n\n{month_count} 张照片 · {len(days)} 个日期',
                      'x': month_x, 'y': 250, 'width': 280, 'height': 130, 'color': '5'})
        month_positions.append(month_id)
        for index, day in enumerate(sorted(days)):
            day_id = f'cpm-day-{day}'
            nodes.append({'id': day_id, 'type': 'text',
                          'text': f'### [[{folder}/日期/{day}|{day}]]\n\n{len(by_date[day])} 张照片 · 日期线索',
                          'x': month_x, 'y': 460 + index * 155, 'width': 280, 'height': 115, 'color': '4'})
            edges.append({'id': f'cpm-edge-{month}-{day}', 'fromNode': month_id, 'fromSide': 'bottom',
                          'toNode': day_id, 'toSide': 'top'})
    project_x = max(0, (len(by_month) - 1) * 350 // 2 - 10)
    nodes.append({'id': 'cpm-project', 'type': 'text', 'x': project_x, 'y': 0, 'width': 300, 'height': 150,
                  'text': f'## {project_name}\n\n[[{folder}/{safe_name(project_name)}-总览|打开项目总览]]\n\n照片 {len(rows)} 张 · 日期为线索', 'color': '6'})
    for month_id in month_positions:
        edges.append({'id': f'cpm-edge-project-{month_id}', 'fromNode': 'cpm-project', 'fromSide': 'bottom',
                      'toNode': month_id, 'toSide': 'top'})
    if by_location:
        loc_x = len(by_month) * 350
        nodes.append({'id': 'cpm-confirmed-locations', 'type': 'text', 'x': loc_x, 'y': 250, 'width': 280, 'height': 100,
                      'text': '## 已确认部位\n\n只列位置经用户确认的照片', 'color': '3'})
        edges.append({'id': 'cpm-edge-project-locations', 'fromNode': 'cpm-project', 'fromSide': 'right',
                      'toNode': 'cpm-confirmed-locations', 'toSide': 'top'})
        for index, code in enumerate(sorted(by_location)):
            nodes.append({'id': f'cpm-location-{code}', 'type': 'text',
                          'text': f'### [[{folder}/部位/{code}|{code}]]\n\n{location_title(by_location[code][0])}\n{len(by_location[code])} 张照片',
                          'x': loc_x, 'y': 460 + index * 155, 'width': 280, 'height': 130, 'color': '3'})
            edges.append({'id': f'cpm-edge-location-{code}', 'fromNode': 'cpm-confirmed-locations', 'fromSide': 'bottom',
                          'toNode': f'cpm-location-{code}', 'toSide': 'top'})
    return {'nodes': nodes, 'edges': edges}


def load_records(db, project_id):
    query = '''SELECT p.id,p.original_name,p.file_path,p.captured_at,p.captured_at_source,p.uploaded_at,
        l.building AS legacy_building,l.floor AS legacy_floor,l.area AS legacy_area,l.status AS legacy_location_status,
        d.recorded_date AS confirmed_date,d.source AS date_source,
        s.scope AS project_scope,s.status AS scope_status,
        ml.code AS location_code,ml.building,ml.floor,ml.area,ml.detail,pl.status AS location_status,
        o.observed_activity,o.observed_state,o.review_status AS observation_status,o.reviewer_note
        FROM photos p LEFT JOIN photo_location_reviews l ON l.photo_id=p.id
        LEFT JOIN photo_date_reviews d ON d.photo_id=p.id
        LEFT JOIN photo_scope_reviews s ON s.photo_id=p.id
        LEFT JOIN photo_memory_locations pl ON pl.photo_id=p.id
        LEFT JOIN memory_locations ml ON ml.id=pl.location_id
        LEFT JOIN site_observations o ON o.id=(SELECT id FROM site_observations WHERE photo_id=p.id ORDER BY CASE review_status WHEN 'confirmed' THEN 0 ELSE 1 END,id DESC LIMIT 1)
        WHERE p.project_id=? ORDER BY p.id'''
    return [dict(row) for row in db.execute(query, (project_id,))]


def effective_date(row):
    return (row['confirmed_date'] or row['captured_at'] or '')[:10] or None


def date_label(row):
    if row['confirmed_date']:
        return '用户确认'
    source = row['captured_at_source']
    if source and source.startswith('exif'):
        return 'EXIF 线索，未核实'
    if source == 'file_modified_unverified':
        return '文件修改时间线索，不能视为拍摄日期'
    return '未知'


def location_title(row):
    return ' · '.join(x for x in (row['building'], row['floor'], row['area'], row['detail']) if x)


def export(project: str | None, vault: Path, folder_name: str | None = None):
    vault = vault.expanduser().resolve()
    if not (vault / '.obsidian').is_dir():
        raise ValueError(f'不是已存在的 Obsidian 库：{vault}')
    project_name, settings = memory.project_settings(project)
    output = vault / (folder_name or ('施工照片记忆-' + safe_name(project_name)))
    if output.resolve() == vault or vault not in output.resolve().parents:
        raise ValueError('导出目录必须位于 Obsidian 库内')
    with memory.connect(settings['data_dir']) as db:
        rows = load_records(db, settings['project_id'])
    by_date = defaultdict(list)
    by_month = defaultdict(list)
    by_location = defaultdict(list)
    thumbnails = 0
    for row in rows:
        day = effective_date(row)
        if day:
            by_date[day].append(row)
            if day not in by_month[day[:7]]:
                by_month[day[:7]].append(day)
        if row['location_code'] and row['location_status'] == 'confirmed':
            by_location[row['location_code']].append(row)
        photo_note = f'照片/P{row["id"]:04d}'
        source = (Path(settings['data_dir']) / row['file_path']).resolve()
        thumb = output / '缩略图' / f'P{row["id"]:04d}.jpg'
        if Image is not None and source.is_file():
            thumb.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(source) as image:
                picture = ImageOps.exif_transpose(image).convert('RGB')
                picture.thumbnail((960, 960))
                picture.save(thumb, format='JPEG', quality=82, optimize=True)
            thumbnails += 1
        links = []
        if day:
            links.append(f'[[日期/{day}|{day}（日期线索）]]')
        if row['location_code'] and row['location_status'] == 'confirmed':
            links.append(f'[[部位/{row["location_code"]}|{location_title(row)}]]')
        legacy = ' · '.join(x for x in (row['legacy_building'], row['legacy_floor'], row['legacy_area']) if x)
        body = f'''---
type: photo
photo_id: {row['id']}
source_name: {yaml(row['original_name'])}
project_scope: {yaml(row['project_scope'] or '')}
scope_status: {yaml(row['scope_status'] or '未确认')}
record_date: {yaml(day or '')}
date_source: {yaml(date_label(row))}
location_code: {yaml(row['location_code'] or '')}
location_status: {yaml(row['location_status'] or '具体部位待核实')}
observation_status: {yaml(row['observation_status'] or '待分析')}
---
# {row['original_name']}

> 此页从 SQLite 自动生成。修正日期、位置或施工结论请在 Codex 对话中提出；个人分析写在库内其他笔记。

{'![[缩略图/P'+format(row['id'],'04d')+'.jpg]]' if thumb.is_file() else '缩略图不可用'}

- 关联：{' · '.join(links)}
- 项目范围：{row['project_scope'] or '待确认'}（{row['scope_status'] or '未确认'}）
- 日期：{day or '未知'}；依据：{date_label(row)}
- 精确部位：{location_title(row) if row['location_code'] else '待核实'}
- 既有位置记录：{legacy or '无'}（{row['legacy_location_status'] or '无状态'}；不自动视为精确位置确认）
- 可见内容：{row['observed_activity'] or '待分析'}
- 状态描述：{row['observed_state'] or '待分析'}
- 观察复核：{row['observation_status'] or '待分析'}
- 备注与不确定性：{row['reviewer_note'] or '无'}
- 原图路径：`{source}`
- 数据库照片 ID：{row['id']}
'''
        write_generated(output / f'{photo_note}.md', body)
    for day, items in sorted(by_date.items()):
        lines = '\n'.join(f'- [[照片/P{r["id"]:04d}|{r["original_name"]}]] · {r["observed_state"] or "待分析"} · {date_label(r)}' for r in items)
        write_generated(output / '日期' / f'{day}.md', f'# {day} · 照片日期线索\n\n所属月份：[[月份/{day[:7]}|{day[:7]}]]。本页按照片时间线索归档，不能据此断言当天全部施工内容。\n\n{lines}\n')
    for month, days in sorted(by_month.items()):
        lines = '\n'.join(f'- [[日期/{day}|{day}]]：{len(by_date[day])} 张' for day in sorted(days))
        write_generated(output / '月份' / f'{month}.md', f'# {month} · 施工照片线索\n\n项目：[[{safe_name(project_name)}-总览|{project_name}]]。以下日期来自照片记录，未经逐项确认时仅为时间线索。\n\n{lines}\n')
    for code, items in sorted(by_location.items()):
        title = location_title(items[0])
        lines = '\n'.join(f'- [[照片/P{r["id"]:04d}|{r["original_name"]}]] · {effective_date(r) or "日期未知"}（{date_label(r)}） · {r["observed_state"] or "待分析"}' for r in items)
        write_generated(output / '部位' / f'{code}.md', f'# {title}\n\n位置编号：`{code}`。以下照片的部位关联已由用户确认；各照片的观察与日期仍按各自状态判断。\n\n{lines}\n')
    confirmed_observations = sum(r['observation_status'] == 'confirmed' for r in rows)
    confirmed_scopes = sum(r['scope_status'] == 'confirmed' for r in rows)
    confirmed_dates = sum(bool(r['confirmed_date']) for r in rows)
    month_lines = '\n'.join(f'- [[月份/{month}|{month}]]：{sum(len(by_date[day]) for day in days)} 张照片' for month, days in sorted(by_month.items()))
    location_lines = '\n'.join(f'- [[部位/{code}|{location_title(items[0])}]]：{len(items)} 张' for code, items in sorted(by_location.items())) or '- 暂无通过新位置编号确认的部位'
    homepage = f'''---
type: project
project_name: {yaml(project_name)}
photo_count: {len(rows)}
---
# {project_name} · 施工照片记忆

本页从 SQLite 自动生成。照片原件和施工判断仍以项目数据库为准。

## 当前覆盖

- 照片：{len(rows)} 张；项目范围已确认：{confirmed_scopes} 张。
- 施工观察已确认：{confirmed_observations} 张；拍摄日期由用户确认：{confirmed_dates} 张。
- 确认的具体部位：{len(by_location)} 个。项目范围确认不等于具体部位确认。

## 可筛选照片表

![[照片总览.base]]

## 清晰的关系导航图

[[施工进度导航.canvas|打开固定排版的项目—月份—日期导航图]]。图中不铺开全部照片，点日期卡片查看照片清单。

## 按月份查看

{month_lines or '- 暂无日期线索'}

## 按部位查看

{location_lines}

所有照片可在上方表格中按日期、状态和位置筛选。
'''
    write_generated(output / f'{safe_name(project_name)}-总览.md', homepage)
    folder = output.relative_to(vault).as_posix()
    write_canvas(output / '施工进度导航.canvas', build_canvas(folder, project_name, rows, by_month, by_date, by_location))
    write_base(output / '照片总览.base', f'''filters:
  and:
    - file.inFolder({yaml(folder + '/照片')})
views:
  - type: table
    name: 施工照片
    order:
      - file.name
      - source_name
      - record_date
      - date_source
      - location_code
      - observation_status
      - scope_status
''')
    print(json.dumps({'vault': str(vault), 'output': str(output), 'photos': len(rows), 'month_notes': len(by_month), 'date_notes': len(by_date), 'location_notes': len(by_location), 'thumbnails': thumbnails, 'canvas': str(output / '施工进度导航.canvas')}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description='导出施工照片记忆到 Obsidian')
    parser.add_argument('--project')
    parser.add_argument('--vault')
    parser.add_argument('--save-vault', action='store_true', help='成功导出后保存此项目的 Obsidian 库路径')
    parser.add_argument('--folder-name')
    args = parser.parse_args()
    name, settings = memory.project_settings(args.project)
    vault_path = args.vault or settings.get('obsidian_vault')
    if not vault_path:
        parser.error('需要 --vault，或先用 --vault ... --save-vault 保存项目库路径')
    export(name, Path(vault_path), args.folder_name)
    if args.save_vault:
        config = memory.config()
        config['projects'][name]['obsidian_vault'] = str(Path(vault_path).expanduser().resolve())
        memory.CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()

from i18n import tr, set_language
'Recoverable removal of application-owned session folders, never arbitrary paths.'
import json
from pathlib import Path
from datetime import datetime

def move_to_bin(root, folders):
    root = Path(root).resolve()
    allowed = {(root / 'data/sessions').resolve(), (root / 'data/history').resolve()}
    folders = list(dict.fromkeys((Path(p).absolute() for p in folders)))
    if not folders:
        raise ValueError(tr('请先选择记录。'))
    for p in folders:
        if p.is_symlink() or p.resolve().parent not in allowed or p.parent.resolve() != p.resolve().parent:
            raise ValueError(tr('只能删除软件内的实验记录，不能删除外部目录。'))
        if not p.is_dir() or not (p / 'measurements.csv').is_file():
            raise ValueError(tr('记录目录不存在或不完整。'))
        report = p / 'report.json'
        if report.exists() and json.loads(report.read_text(encoding='utf-8')).get('status') == 'recording':
            raise ValueError(tr('记录仍标为采集中，不能删除；请先确认采集已正常停止。'))
    batch = root / 'recycle_bin' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    batch.mkdir(parents=True)
    entries = [{'original': str(p.relative_to(root)), 'stored': str(i)} for i, p in enumerate(folders)]
    (batch / 'manifest.json').write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
    moved = []
    try:
        for p, e in zip(folders, entries):
            p.rename(batch / e['stored'])
            moved.append((p, batch / e['stored']))
    except Exception:
        for p, d in reversed(moved):
            d.rename(p)
        raise
    return batch

def restore_batch(root, batch):
    root = Path(root).resolve()
    batch = Path(batch).resolve()
    if batch.parent != (root / 'recycle_bin').resolve():
        raise ValueError(tr('无效的删除批次。'))
    entries = json.loads((batch / 'manifest.json').read_text(encoding='utf-8'))
    pairs = []
    for e in entries:
        dest = (root / e['original']).resolve()
        source = (batch / e['stored']).resolve()
        if dest.parent not in {(root / 'data/sessions').resolve(), (root / 'data/history').resolve()} or source.parent != batch:
            raise ValueError(tr('恢复路径不属于实验目录。'))
        if dest.exists() or not source.is_dir():
            raise ValueError(tr('目标已存在或记录已恢复，未覆盖任何数据。'))
        pairs.append((source, dest))
    moved = []
    try:
        for source, dest in pairs:
            source.rename(dest)
            moved.append((source, dest))
    except Exception:
        for source, dest in reversed(moved):
            dest.rename(source)
        raise
    return [d for _, d in pairs]

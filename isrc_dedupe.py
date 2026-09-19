#!/usr/bin/env python3
"""Plan -> validate -> quarantine. Never permanently delete audio.

Planning reads the existing local index and inspects candidates in bounded
parallel batches. Applying REQUIRES the saved plan and rechecks disk evidence.
The append-only fsynced journal records intent BEFORE rename, then completion.
Rollback handles interrupted intent records as well as completed moves.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import datetime
import json
import os
from pathlib import Path
import uuid

from orpheus.audio_evidence import AudioEvidence, canonical, compatible, inspect_audio, in_library
from orpheus.isrc_library_index import IsrcLibraryIndex
from utils.atomic_io import file_lock, temporary_sibling, remove_temporary

CONFIG_DIR = Path(__file__).resolve().parent / 'config'
QUARANTINE = '_DUPLICADOS'


def save_json(path, value):
    path = Path(path)
    temp = temporary_sibling(path)
    try:
        with open(temp, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        remove_temporary(temp)


def fingerprint(e):
    return (e.isrc, e.size, e.mtime_ns, e.duration, e.channels,
            e.lossless, e.bits, e.sample_rate, e.bitrate)


def unchanged(saved, root=None):
    evidence = inspect_audio(saved['path'], root)
    return evidence if evidence and fingerprint(evidence) == fingerprint(AudioEvidence(**saved)) else None


def make_plan(idx, workers=8, progress=print):
    groups = idx.duplicates()
    paths = sorted({path for values in groups.values() for path in values})
    evidence = {}
    with ThreadPoolExecutor(max_workers=max(1, min(32, workers))) as pool:
        for start in range(0, len(paths), 128):
            batch = paths[start:start + 128]
            for path, item in zip(batch, pool.map(lambda p: inspect_audio(p, idx.root), batch)):
                evidence[path] = item
            progress(f'Inspected {min(start + len(batch), len(paths))}/{len(paths)} candidates')
    plan = {'version': 1, 'id': uuid.uuid4().hex, 'root': idx.root,
            'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'index_last_scan': idx.last_scan(), 'groups': [], 'review': []}
    for isrc, group_paths in groups.items():
        items = [evidence[p] for p in group_paths]
        if any(item is None or item.isrc != isrc for item in items):
            plan['review'].append({'isrc': isrc, 'reason': 'missing, changed ISRC or unreadable audio', 'paths': group_paths})
            continue
        if (len({e.channels for e in items}) != 1
                or max(e.duration for e in items) - min(e.duration for e in items) > 2.0
                or any(e.lossless and not e.bits for e in items)):
            plan['review'].append({'isrc': isrc, 'reason': 'duration/channels/quality evidence ambiguous', 'paths': group_paths})
            continue
        # Deterministic tie: preserve shortest path, then lexical name.
        items.sort(key=lambda e: (len(e.path), e.path))
        items.sort(key=lambda e: e.rank, reverse=True)
        keeper, *victims = items
        plan['groups'].append({'isrc': isrc, 'keep': keeper.to_dict(),
                               'move': [e.to_dict() for e in victims]})
    return plan


def export_csv(plan, path):
    with open(path, 'w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow(['isrc', 'action', 'lossless', 'bits', 'sample_rate', 'duration', 'path', 'reason'])
        for group in plan['groups']:
            for action, items in [('KEEP', [group['keep']]), ('MOVE', group['move'])]:
                for e in items:
                    writer.writerow([group['isrc'], action, e['lossless'], e['bits'], e['sample_rate'], e['duration'], e['path'], ''])
        for review in plan['review']:
            for path in review['paths']:
                writer.writerow([review['isrc'], 'REVIEW', '', '', '', '', path, review['reason']])


def journal_event(handle, **event):
    event['utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    handle.write(json.dumps(event, ensure_ascii=False) + '\n')
    handle.flush()
    os.fsync(handle.fileno())


def read_journal(path):
    if not Path(path).exists():
        return []
    raw = Path(path).read_bytes()
    lines = raw.splitlines()
    events = []
    for i, line in enumerate(lines):
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Only the final torn record after a crash can be ignored.
            if i != len(lines) - 1 or raw.endswith(b'\n'):
                raise ValueError('Corrupt journal; refusing automatic recovery')
    return events


def open_journal(path):
    """Archive a torn final record, then resume after the last complete record.

    Caller holds the journal lock. Never discard a complete malformed record.
    """
    path = Path(path)
    if path.exists():
        raw = path.read_bytes()
        if raw and not raw.endswith(b'\n'):
            tail_start = raw.rfind(b'\n') + 1
            tail = raw[tail_start:]
            try:
                json.loads(tail)
            except (ValueError, UnicodeDecodeError):
                path.with_name(path.name + '.torn-' + uuid.uuid4().hex).write_bytes(tail)
                with open(path, 'r+b') as handle:
                    handle.truncate(tail_start)
                    handle.flush()
                    os.fsync(handle.fileno())
            else:
                with open(path, 'ab') as handle:
                    handle.write(b'\n')
                    handle.flush()
                    os.fsync(handle.fileno())
    return open(path, 'a', encoding='utf-8')


def target_for(plan, source):
    root = canonical(plan['root'])
    if not in_library(source, root):
        raise ValueError('Source outside library or inside quarantine: ' + source)
    plan_id = plan['id']
    if len(plan_id) != 32 or any(c not in '0123456789abcdef' for c in plan_id):
        raise ValueError('Invalid plan identifier')
    qroot = Path(root) / QUARANTINE
    if canonical(qroot) != os.path.normcase(os.path.abspath(qroot)):
        raise ValueError('Quarantine must not be a symbolic link/junction')
    target = qroot / plan_id / os.path.relpath(canonical(source), root)
    if os.path.commonpath((canonical(target), canonical(qroot))) != canonical(qroot):
        raise ValueError('Quarantine target escapes root')
    return str(target)


def validate_plan(plan, idx):
    if plan.get('version') != 1 or canonical(plan['root']) != canonical(idx.root):
        raise ValueError('Plan version/root mismatch')
    keepers = {canonical(g['keep']['path']) for g in plan['groups']}
    victims = set()
    for g in plan['groups']:
        if not in_library(g['keep']['path'], idx.root):
            raise ValueError('Keeper outside library')
        for saved in g['move']:
            key = canonical(saved['path'])
            if key in keepers or key in victims or saved['isrc'] != g['isrc'] or g['keep']['isrc'] != g['isrc']:
                raise ValueError('Conflicting plan entries')
            victims.add(key)
            target_for(plan, saved['path'])


def apply_plan(idx, plan, journal_path, progress=print):
    validate_plan(plan, idx)
    stats = {'moved': 0, 'already_moved': 0, 'errors': 0}
    Path(journal_path).parent.mkdir(parents=True, exist_ok=True)
    with file_lock(str(journal_path) + '.apply', timeout=0):
        events = read_journal(journal_path)
        if any(e.get('plan_id') != plan['id'] for e in events):
            raise ValueError('Journal belongs to another plan')
        if any(e.get('event') in {'rollback_intent', 'restored'} for e in events):
            raise ValueError('Plan was rolled back; generate a new plan')
        intents = {e['source']: e for e in events if e.get('event') == 'intent'}
        with open_journal(journal_path) as journal:
            for group in plan['groups']:
                with idx.claim(group['isrc']):
                    for saved in group['move']:
                        source = saved['path']
                        target = target_for(plan, source)
                        try:
                            keeper = unchanged(group['keep'], idx.root)
                            if keeper is None:
                                raise ValueError('Keeper changed or unreadable')
                            if not os.path.exists(source) and source in intents:
                                moved_saved = dict(saved, path=target)
                                moved = unchanged(moved_saved)
                                if moved is None:
                                    raise ValueError('Incomplete move requires manual review')
                                idx.remove(source)
                                stats['already_moved'] += 1
                                continue
                            victim = unchanged(saved, idx.root)
                            if victim is None or not compatible(keeper, victim) or keeper.rank < victim.rank:
                                raise ValueError('Audio evidence changed or keeper is not preferable')
                            if os.path.exists(target):
                                raise FileExistsError('Quarantine target already exists')
                            Path(target).parent.mkdir(parents=True, exist_ok=True)
                            target_for(plan, source)  # check resolved parents again
                            journal_event(journal, event='intent', plan_id=plan['id'], root=idx.root,
                                          source=source, target=target, evidence=saved)
                            os.rename(source, target)  # same filesystem; no copy/delete fallback
                            journal_event(journal, event='moved', plan_id=plan['id'], source=source, target=target)
                            idx.remove(source)
                            stats['moved'] += 1
                        except (OSError, ValueError) as exc:
                            stats['errors'] += 1
                            journal_event(journal, event='error', plan_id=plan['id'], source=source, message=str(exc))
                            progress(f'REVIEW: {source}: {exc}')
                progress(f"Moved {stats['moved']}; already moved {stats['already_moved']}; errors {stats['errors']}")
    return stats


def rollback(idx, journal_path, progress=print):
    stats = {'restored': 0, 'already_restored': 0, 'errors': 0}
    with file_lock(str(journal_path) + '.apply', timeout=0):
        events = read_journal(journal_path)
        intents = {e['source']: e for e in events if e.get('event') == 'intent'}
        with open_journal(journal_path) as journal:
            for e in reversed(list(intents.values())):
                source, target = e['source'], e['target']
                plan = {'root': e['root'], 'id': e['plan_id']}
                if canonical(e['root']) != canonical(idx.root) or canonical(target_for(plan, source)) != canonical(target):
                    raise ValueError('Journal path/root mismatch')
                with idx.claim(e['evidence']['isrc']):
                    try:
                        if os.path.exists(source):
                            if not os.path.exists(target) and unchanged(e['evidence'], idx.root):
                                idx.add(e['evidence']['isrc'], source)
                                stats['already_restored'] += 1
                                continue
                            raise FileExistsError('Original path occupied; refusing overwrite')
                        if unchanged(dict(e['evidence'], path=target)) is None:
                            raise ValueError('Quarantined audio changed or missing')
                        Path(source).parent.mkdir(parents=True, exist_ok=True)
                        journal_event(journal, event='rollback_intent', plan_id=e['plan_id'], source=source, target=target)
                        os.rename(target, source)
                        journal_event(journal, event='restored', plan_id=e['plan_id'], source=source, target=target)
                        idx.add(e['evidence']['isrc'], source)
                        stats['restored'] += 1
                    except (OSError, ValueError) as exc:
                        stats['errors'] += 1
                        journal_event(journal, event='rollback_error', plan_id=e['plan_id'], source=source, message=str(exc))
                        progress(f'REVIEW: {source}: {exc}')
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dir', required=True)
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--out', help='CSV report; JSON plan is saved beside it')
    ap.add_argument('--plan', help='JSON plan path; required with --apply')
    ap.add_argument('--apply', action='store_true', help='Apply saved plan to quarantine')
    ap.add_argument('--journal', help='Override journal path')
    ap.add_argument('--rollback', help='Restore files from this journal')
    args = ap.parse_args()
    if args.apply and not args.plan:
        ap.error('--apply requires --plan; generate and inspect a plan first')
    if args.rollback and args.apply:
        ap.error('choose either --apply or --rollback')
    if not os.path.isdir(args.dir):
        ap.error('library root unavailable')
    idx = IsrcLibraryIndex(args.dir, str(CONFIG_DIR), print_fn=print)
    try:
        if args.rollback:
            stats = rollback(idx, args.rollback)
        elif args.apply:
            plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
            journal = args.journal or str(Path(args.plan).with_suffix('.journal.jsonl'))
            stats = apply_plan(idx, plan, journal)
        else:
            if not idx.is_built():
                ap.error('index not built; use isrc_index_tool.py --build first')
            out = Path(args.out or (CONFIG_DIR / ('dedupe-plan-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '.csv')))
            plan_path = Path(args.plan or out.with_suffix('.json'))
            if plan_path.exists() or out.exists():
                ap.error('plan/report already exists; choose new output names')
            plan = make_plan(idx, args.workers)
            save_json(plan_path, plan)
            out.parent.mkdir(parents=True, exist_ok=True)
            export_csv(plan, out)
            stats = {'groups': len(plan['groups']), 'candidates_to_move': sum(len(g['move']) for g in plan['groups']),
                     'review_groups': len(plan['review']), 'plan': str(plan_path), 'csv': str(out)}
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 1 if stats.get('errors') else 0
    finally:
        idx.close()


if __name__ == '__main__':
    raise SystemExit(main())

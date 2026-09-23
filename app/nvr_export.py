#!/usr/bin/env python3
"""
Export footage from a Scrypted NVR store to MP4 files, without running Scrypted.

    # what is on disk
    python3 nvr_export.py list

    # one clip
    python3 nvr_export.py clip --cam camera-id \
        --start '2025-01-15 02:20' --duration 600 --out ~/Desktop

    # everything in a time range, as hourly files
    python3 nvr_export.py bulk --cam camera-id \
        --start '2025-01-15' --end '2025-01-16' --out ~/Desktop/camera

Reads only; the recording store is never modified.
"""
import argparse
import datetime
import os
import sys

import nvr_lib


def parse_time(s):
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return int(datetime.datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit('cannot parse time: %s (use "YYYY-MM-DD HH:MM")' % s)


def stamp(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime('%Y-%m-%d_%H-%M-%S')


def pick_segments(index, cam_id, res, start_ms, end_ms, root):
    cam = index['cameras'][cam_id]
    segs = cam['lowSegments'] if res == 'low' else cam['segments']
    picked, prev = [], None
    for s, rel, _size in segs:
        if s + 65000 <= start_ms:
            prev = (s, rel)
            continue
        if s >= end_ms:
            break
        picked.append((s, rel))
    if not picked:
        return []
    if prev and 0 < picked[0][0] - prev[0] <= 130000:
        picked.insert(0, prev)
    return [(s, os.path.join(root, rel)) for s, rel in picked]


def export(index, root, cam_id, res, start_ms, end_ms, out_dir):
    segs = pick_segments(index, cam_id, res, start_ms, end_ms, root)
    if not segs:
        return None
    name = '%s_%s.mp4' % (index['cameras'][cam_id]['name'], stamp(start_ms))
    path = os.path.join(out_dir, name)
    tmp = path + '.part'
    with open(tmp, 'wb') as f:
        meta = nvr_lib.build_clip(segs, f, start_ms=start_ms, end_ms=end_ms)
    os.replace(tmp, path)
    return path, meta


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['list', 'clip', 'bulk'])
    ap.add_argument('--root', default=os.environ.get(
        'NVR_ROOT', os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument('--cam')
    ap.add_argument('--res', choices=['high', 'low'], default='high')
    ap.add_argument('--start')
    ap.add_argument('--end')
    ap.add_argument('--duration', type=int, default=600,
                    help='clip length in seconds (clip), or file length (bulk)')
    ap.add_argument('--out', default='.')
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    index = nvr_lib.build_index(root)
    if not index['cameras']:
        raise SystemExit(
            'no Scrypted NVR camera directories found under %s\n'
            '(looking for <camera>/<session>/session.json); pass --root' % root)

    if args.command == 'list':
        for cam in index['cameras'].values():
            total = sum(s[2] for s in cam['segments'])
            print('\n%s  (%s, %s)  %.1f h  %.1f GiB' % (
                cam['name'], cam['id'], cam['ip'],
                len(cam['segments']) / 60.0, total / 2**30))
            for a, b in nvr_lib.runs_of(cam['segments']):
                print('   %s  ->  %s   %5.1f h' % (
                    stamp(a), stamp(b), (b - a) / 3600000.0))
        return

    if not args.cam or args.cam not in index['cameras']:
        raise SystemExit('--cam must be one of: %s' % ', '.join(index['cameras']))
    if not args.start:
        raise SystemExit('--start is required')
    out = os.path.realpath(args.out)
    if out == root or out.startswith(root + os.sep):
        raise SystemExit(
            '--out (%s) is inside the recording store (%s).\n'
            'The store is read-only; write exports somewhere else.' % (out, root))
    os.makedirs(args.out, exist_ok=True)
    start = parse_time(args.start)

    if args.command == 'clip':
        result = export(index, root, args.cam, args.res, start,
                        start + args.duration * 1000, args.out)
        if not result:
            raise SystemExit('no footage at that time')
        path, meta = result
        print('%s  %.1fs  %d frames  %dx%d' % (
            path, meta['duration'], meta['frames'], meta['width'], meta['height']))
        return

    if not args.end:
        raise SystemExit('--end is required for bulk')
    end = parse_time(args.end)
    step = args.duration * 1000
    written = 0
    t = start
    while t < end:
        result = export(index, root, args.cam, args.res, t, min(t + step, end),
                        args.out)
        if result:
            path, meta = result
            written += 1
            print('%s  %.1fs' % (os.path.basename(path), meta['duration']))
            sys.stdout.flush()
        t += step
    print('%d files written to %s' % (written, args.out))


if __name__ == '__main__':
    main()

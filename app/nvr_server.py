#!/usr/bin/env python3
"""
Read-only web browser for a Scrypted NVR recording store.

    python3 nvr_server.py [--root /Volumes/NVR] [--port 8787] [--reindex]

Serves a timeline UI at http://127.0.0.1:8787. Clips are muxed on demand from
the raw .rtsp segments (see nvr_lib) and cached outside the recording store;
nothing in the store is ever written to.

Every option also reads an environment variable (NVR_ROOT, NVR_HOST, NVR_PORT,
NVR_CACHE, NVR_CACHE_LIMIT_GB, NVR_CLIP_SECONDS) so the container image can be
configured entirely from compose.yaml.
"""
import argparse
import datetime
import html
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import nvr_lib

def default_cache():
    if os.environ.get('NVR_CACHE'):
        return os.environ['NVR_CACHE']
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Caches/nvr-browser')
    return os.path.join(
        os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache')),
        'nvr-browser')


CACHE = default_cache()
CACHE_LIMIT = int(float(os.environ.get('NVR_CACHE_LIMIT_GB', 8)) * 2**30)
CLIP_SECONDS = int(os.environ.get('NVR_CLIP_SECONDS', 300))
# Scrub previews are snapped to this grid. A day-wide timeline is roughly a
# minute per pixel, so a 60 s grid gives about one thumbnail per pixel while
# making repeat hovers over the same spot a cache hit instead of a fresh decode.
THUMB_BUCKET = int(os.environ.get('NVR_THUMB_BUCKET', 60)) * 1000
THUMB_BATCH = 24          # frames per ffmpeg process while warming

# Warming reads a few hundred KB per frame, scattered across the archive. That
# is fine on an SSD and punishing on a mechanical USB drive, especially one that
# is busy with something else, so it is deliberately unhurried and opt-in for
# the whole archive.
#   off = never warm, decode previews only when hovered
#   day = warm the day you open (default)
#   all = also warm the entire archive in the background at startup
WARM_MODE = os.environ.get('NVR_WARM', 'day').strip().lower()
WARM_DELAY = max(0, int(os.environ.get('NVR_WARM_DELAY_MS', 250))) / 1000.0
_warm_tokens = {}          # per camera: warming one must not cancel another

INDEX = None
ROOT = None
_locks = {}
_locks_guard = threading.Lock()


# ---------------------------------------------------------------- index setup

def assert_outside_store(path, root, what):
    """Refuse to start if a writable path sits inside the recording store.

    The store holds irreplaceable originals; this process must only ever read
    from it. Every write this program performs goes to the cache directory, so
    keeping the cache outside `root` is what makes that guarantee structural
    rather than a promise.
    """
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    if path == root or path.startswith(root + os.sep):
        raise SystemExit(
            '%s (%s) is inside the recording store (%s).\n'
            'The store is treated as read-only; point it somewhere else.'
            % (what, path, root))


def archive_dirs(value):
    return [part.strip() for part in value.split(',') if part.strip()] or ['.']


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def load_index(root, archives, group_by_ip, reindex=False):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, 'index.json')
    if not reindex and os.path.exists(path):
        try:
            with open(path) as f:
                index = json.load(f)
            if (index.get('root') == os.path.abspath(root)
                    and index.get('archiveDirs', ['.']) == archives
                    and index.get('groupByIp', False) == group_by_ip):
                return index
        except Exception:
            pass
    print('indexing %s ...' % root)
    index = nvr_lib.build_index(root, archives, group_by_ip)
    with open(path, 'w') as f:
        json.dump(index, f)
    return index


def day_key(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime('%Y-%m-%d')


def camera_days(cam):
    days = {}
    for start, _rel, size in cam['segments']:
        d = days.setdefault(day_key(start), {'day': None, 'segments': 0, 'bytes': 0})
        d['segments'] += 1
        d['bytes'] += size
    for k, v in days.items():
        v['day'] = k
    return [days[k] for k in sorted(days)]


def segments_for(cam, res, start_ms, end_ms):
    """Segments overlapping [start_ms, end_ms).

    The segment before the first one is prepended when it is contiguous, because
    segments start mid-GOP: it supplies the keyframe the first frames reference.
    Returns [] when this stream has no coverage for the window.
    """
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
    return [(s, os.path.join(ROOT, rel)) for s, rel in picked]


def segment_at(cam, res, ms):
    """The single segment covering `ms`, as (start_ms, path), or None."""
    segs = cam['lowSegments'] if res == 'low' else cam['segments']
    lo, hi, found = 0, len(segs) - 1, None
    while lo <= hi:                       # segments are sorted by start time
        mid = (lo + hi) // 2
        if segs[mid][0] <= ms:
            found = segs[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    if not found or ms >= found[0] + 65000:
        return None
    return found[0], os.path.join(ROOT, found[1])


def resolve_segments(cam, res, start_ms, end_ms):
    """Segments plus the resolution actually used (the substream is patchy)."""
    if res == 'low':
        segs = segments_for(cam, 'low', start_ms, end_ms)
        if segs:
            return segs, 'low'
    return segments_for(cam, 'high', start_ms, end_ms), 'high'


# --------------------------------------------------------------- clip caching

def clip_lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def prune_cache():
    """Evict cached clips only.

    Preview frames are tiny and expensive to rebuild (they are what makes
    scrubbing instant), so they are kept; clips are large and cheap to remux.
    """
    files = []
    for name in os.listdir(CACHE):
        if name.endswith('.mp4'):
            p = os.path.join(CACHE, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_atime, st.st_size, p))
    total = sum(f[1] for f in files)
    for _atime, size, path in sorted(files):
        if total <= CACHE_LIMIT:
            break
        try:
            os.remove(path)
            os.path.exists(path + '.json') and os.remove(path + '.json')
            total -= size
        except OSError:
            pass


def make_clip(cam_id, res, start_ms, duration_s):
    key = '%s-%s-%d-%d' % (cam_id, res, start_ms, duration_s)
    mp4 = os.path.join(CACHE, key + '.mp4')
    meta_path = mp4 + '.json'
    with clip_lock(key):
        if os.path.exists(mp4) and os.path.exists(meta_path):
            os.utime(mp4, None)
            with open(meta_path) as f:
                return mp4, json.load(f)
        cam = INDEX['cameras'][cam_id]
        end_ms = start_ms + duration_s * 1000
        segs, actual = resolve_segments(cam, res, start_ms, end_ms)
        if not segs:
            raise KeyError('no footage')
        tmp = mp4 + '.part'
        with open(tmp, 'wb') as f:
            meta = nvr_lib.build_clip(segs, f, start_ms=start_ms, end_ms=end_ms)
        os.replace(tmp, mp4)
        meta['key'] = key
        meta['res'] = actual
        meta['requestedStart'] = start_ms
        with open(meta_path, 'w') as f:
            json.dump(meta, f)
        prune_cache()
        return mp4, meta


def thumb_path(cam_id, ms):
    return os.path.join(CACHE, 'thumb-%s-%d.jpg' % (cam_id, ms))


def keyframe_at(cam_id, ms):
    """Annex-B keyframe near `ms`, preferring the cheaper 360p substream."""
    cam = INDEX['cameras'][cam_id]
    hit = segment_at(cam, 'low', ms) or segment_at(cam, 'high', ms)
    if not hit:
        return None
    seg_start, path = hit
    session = os.path.join(os.path.dirname(os.path.dirname(path)), 'session.json')
    return nvr_lib.extract_keyframe(path, seg_start, ms, session)


def encode_jpegs(frames):
    """Decode many independent keyframes in one ffmpeg process.

    Spawning ffmpeg costs more than the decode itself (about 31 ms against 7 ms),
    so warming feeds it a batch and splits the MJPEG stream back apart.
    """
    if not frames:
        return []
    out = subprocess.run(
        ['ffmpeg', '-v', 'error', '-f', 'h264', '-i', 'pipe:0',
         '-vf', 'scale=320:-2', '-q:v', '5', '-f', 'mjpeg', 'pipe:1'],
        input=b''.join(frames), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if out.returncode != 0:
        return []
    return split_jpegs(out.stdout)


def split_jpegs(blob):
    marks = []
    i = blob.find(b'\xff\xd8\xff')
    while i != -1:
        marks.append(i)
        i = blob.find(b'\xff\xd8\xff', i + 3)
    return [blob[a:b] for a, b in zip(marks, marks[1:] + [len(blob)])]


def write_thumb(cam_id, ms, jpeg):
    path = thumb_path(cam_id, ms)
    tmp = path + '.part'
    with open(tmp, 'wb') as f:
        f.write(jpeg)
    os.replace(tmp, path)
    return path


def make_thumb(cam_id, ms):
    """One preview frame, decoded from a single keyframe rather than a whole segment."""
    ms = ms - ms % THUMB_BUCKET
    path = thumb_path(cam_id, ms)
    if os.path.exists(path):
        return path
    with clip_lock('thumb-%s-%d' % (cam_id, ms)):
        if os.path.exists(path):
            return path
        frame = keyframe_at(cam_id, ms)
        if not frame:
            raise KeyError('no footage')
        images = encode_jpegs([frame])
        if not images:
            raise KeyError('could not decode frame')
        return write_thumb(cam_id, ms, images[0])


def warm_buckets(cam_id, buckets, token, label):
    """Generate and store preview frames for a list of times, in batches.

    `token` None means "run to completion": the startup prewarm must not be
    cancelled just because the user opened a day, which only bumps the token so
    that one *day* warm supersedes the previous one.
    """
    pending, made = [], 0
    for t in buckets:
        if token is not None and _warm_tokens.get(cam_id) != token:
            return made
        if os.path.exists(thumb_path(cam_id, t)):
            continue
        try:
            frame = keyframe_at(cam_id, t)
        except Exception:
            frame = None
        if not frame:
            continue
        pending.append((t, frame))
        if len(pending) >= THUMB_BATCH:
            made += flush_batch(cam_id, pending)
            pending = []
            if WARM_DELAY:
                time.sleep(WARM_DELAY)   # leave the disk alone between batches
    made += flush_batch(cam_id, pending)
    if made:
        print('warmed %s %s: %d frames' % (cam_id, label, made))
    return made


def flush_batch(cam_id, pending):
    if not pending:
        return 0
    images = encode_jpegs([f for _t, f in pending])
    if len(images) != len(pending):
        # fall back to one-at-a-time rather than mismatching frames to times
        n = 0
        for t, frame in pending:
            one = encode_jpegs([frame])
            if one:
                write_thumb(cam_id, t, one[0])
                n += 1
        return n
    for (t, _frame), jpeg in zip(pending, images):
        write_thumb(cam_id, t, jpeg)
    return len(pending)


def day_buckets(cam, day):
    t0 = datetime.datetime.strptime(day, '%Y-%m-%d')
    a = int(t0.timestamp() * 1000)
    segs = [x for x in cam['segments'] if a <= x[0] < a + 86400 * 1000]
    out = []
    for start, end in nvr_lib.runs_of(segs):
        t = start - start % THUMB_BUCKET
        while t < end:
            out.append(t)
            t += THUMB_BUCKET
    return out


def warm_day(cam_id, day, token):
    """Prioritise the day the user just opened."""
    try:
        warm_buckets(cam_id, day_buckets(INDEX['cameras'][cam_id], day), token, day)
    except Exception as e:
        print('warm failed: %s' % e)


def prewarm_all():
    """Fill the configured preview cache for the whole archive, once.

    Only runs with NVR_WARM=all. Frames already present in the current cache
    are skipped. With the default tmpfs deployment, warming starts fresh after
    the viewer container is recreated.
    """
    total = 0
    try:
        for cam_id, cam in INDEX['cameras'].items():
            buckets = []
            for start, end in nvr_lib.runs_of(cam['segments']):
                t = start - start % THUMB_BUCKET
                while t < end:
                    buckets.append(t)
                    t += THUMB_BUCKET
            missing = [t for t in buckets if not os.path.exists(thumb_path(cam_id, t))]
            if not missing:
                print('preview cache complete for %s (%d frames)' % (cam_id, len(buckets)))
                continue
            print('prewarming %s: %d of %d frames missing'
                  % (cam_id, len(missing), len(buckets)))
            total += warm_buckets(cam_id, missing, None, 'prewarm')
    except Exception as e:
        print('prewarm failed: %s' % e)
    if total:
        print('prewarm done: %d frames' % total)


# ----------------------------------------------------------------- http layer

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'nvr-browser'

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype, download_name=None):
        """Serve a file with Range support so <video> can seek."""
        size = os.path.getsize(path)
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        code = 200
        if rng and rng.startswith('bytes='):
            spec = rng[6:].split(',')[0]
            a, _, b = spec.partition('-')
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            elif b:
                start = max(0, size - int(b))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */%d' % size)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if code == 206:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        if download_name:
            self.send_header('Content-Disposition',
                             'attachment; filename="%s"' % download_name)
        self.end_headers()
        with open(path, 'rb') as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        one = lambda k, d=None: q.get(k, [d])[0]
        try:
            if url.path == '/':
                body = (PAGE.replace('__CLIP_SECONDS__', str(CLIP_SECONDS))
                            .replace('__THUMB_BUCKET__', str(THUMB_BUCKET)).encode())
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif url.path == '/api/cameras':
                self._json([{
                    'id': c['id'], 'name': c['name'], 'ip': c['ip'],
                    'hours': round(len(c['segments']) / 60.0, 1),
                    'bytes': sum(s[2] for s in c['segments']),
                    'hasLow': bool(c['lowSegments']),
                    'days': camera_days(c),
                } for c in INDEX['cameras'].values()])

            elif url.path == '/api/day':
                cam = INDEX['cameras'][one('cam')]
                day = one('day')
                t0 = datetime.datetime.strptime(day, '%Y-%m-%d')
                a = int(t0.timestamp() * 1000)
                b = a + 86400 * 1000
                segs = [s for s in cam['segments'] if a <= s[0] < b]
                self._json({
                    'day': day, 'dayStart': a,
                    'runs': nvr_lib.runs_of(segs),
                    'motion': [m for m in cam['motion'] if a <= m < b],
                })

            elif url.path == '/api/clip':
                start = int(one('start'))
                dur = min(int(one('dur', CLIP_SECONDS)), 900)
                _path, meta = make_clip(one('cam'), one('res', 'high'), start, dur)
                meta['url'] = '/media/%s.mp4' % meta['key']
                self._json(meta)

            elif url.path.startswith('/media/'):
                name = os.path.basename(url.path)
                path = os.path.join(CACHE, name)
                if not os.path.exists(path):
                    self.send_error(404)
                    return
                self._send_file(path, 'video/mp4',
                                one('download') and name or None)

            elif url.path == '/api/warm':
                if WARM_MODE == 'off':
                    self._json({'warming': False, 'bucket': THUMB_BUCKET})
                    return
                cam_id = one('cam')
                _warm_tokens[cam_id] = _warm_tokens.get(cam_id, 0) + 1
                threading.Thread(target=warm_day,
                                 args=(cam_id, one('day'), _warm_tokens[cam_id]),
                                 daemon=True).start()
                self._json({'warming': True, 'bucket': THUMB_BUCKET})

            elif url.path == '/thumb':
                path = make_thumb(one('cam'), int(one('t')))
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.send_header('Content-Length', str(os.path.getsize(path)))
                self.end_headers()
                with open(path, 'rb') as f:
                    self.wfile.write(f.read())

            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except KeyError as e:
            self._json({'error': str(e)}, 404)
        except Exception as e:
            self._json({'error': '%s: %s' % (type(e).__name__, e)}, 500)


PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NVR Archive</title>
<style>
  :root{color-scheme:dark;--bg:#0e1013;--panel:#171a1f;--line:#272c34;
        --fg:#e6e9ee;--dim:#8b93a1;--accent:#4a9eff;--motion:#ffb03a}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  header{display:flex;align-items:center;gap:16px;padding:10px 16px;
         border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
  h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.2px}
  .tabs{display:flex;gap:6px}
  button{font:inherit;color:var(--fg);background:#20242b;border:1px solid var(--line);
         border-radius:7px;padding:5px 11px;cursor:pointer}
  button:hover{border-color:#3a424e}
  button.on{background:var(--accent);border-color:var(--accent);color:#04121f;font-weight:600}
  a{color:inherit;text-decoration:none}
  .wrap{display:grid;grid-template-columns:220px 1fr;height:calc(100vh - 53px)}
  aside{border-right:1px solid var(--line);overflow-y:auto;background:var(--panel)}
  .day{padding:8px 14px;border-bottom:1px solid var(--line);cursor:pointer}
  .day:hover{background:#1e222a}
  .day.on{background:#1d2b3d;box-shadow:inset 3px 0 0 var(--accent)}
  .day b{display:block;font-weight:600}
  .day span{color:var(--dim);font-size:12px}
  main{display:flex;flex-direction:column;min-width:0;padding:14px;gap:12px}
  .stage{flex:1;min-height:0;display:grid;gap:8px;
         grid-template-columns:repeat(var(--cols),minmax(0,1fr))}
  .tile{position:relative;background:#000;border-radius:10px;overflow:hidden;min-height:0}
  .tile video{width:100%;height:100%;object-fit:contain;background:#000;display:block}
  .tile .tag{position:absolute;top:8px;left:10px;font-size:12px;font-weight:600;
             padding:2px 8px;border-radius:5px;background:#000a;color:#fff;
             pointer-events:none}
  .empty{grid-column:1/-1;display:flex;align-items:center;justify-content:center;
         color:var(--dim);background:#000;border-radius:10px}
  .bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .clock{font-variant-numeric:tabular-nums;font-size:15px;font-weight:600}
  .tl{position:relative;height:54px;background:var(--panel);
      border:1px solid var(--line);border-radius:8px;cursor:crosshair}
  canvas{display:block;width:100%;height:100%}
  .head{position:absolute;top:0;bottom:0;width:2px;background:#fff;pointer-events:none;
        display:none}
  .hover{position:absolute;bottom:60px;pointer-events:none;display:none;z-index:5;
         background:#000;border:1px solid var(--line);border-radius:6px;padding:3px;
         transform:translateX(-50%)}
  .hover figure{margin:0}
  .hover img{display:block;width:200px;border-radius:3px;background:#111}
  .hover figcaption,.hover .t{text-align:center;color:var(--dim);font-size:11px}
  .hint{color:var(--dim);font-size:12px}
  @media (max-width:900px){ .stage{grid-template-columns:1fr} }
</style>

<header>
  <h1>NVR Archive</h1>
  <div class="tabs" id="cams"></div>
  <div style="flex:1"></div>
  <div class="tabs">
    <button id="q-high" class="on">1080p</button>
    <button id="q-low">360p</button>
  </div>
  <div class="tabs" id="dl"></div>
</header>

<div class="wrap">
  <aside id="days"></aside>
  <main>
    <div class="stage" id="stage" style="--cols:1">
      <div class="empty" id="empty">Pick a day, then click the timeline</div>
    </div>
    <div class="bar">
      <span class="clock" id="clock">--:--:--</span>
      <span class="hint" id="status"></span>
    </div>
    <div class="tl" id="tl">
      <canvas id="cv"></canvas>
      <div class="head" id="head"></div>
    </div>
    <div class="hover" id="hov"></div>
    <div class="hint" id="foot"></div>
  </main>
</div>

<script>
const $ = s => document.querySelector(s);
const DAY_MS = 86400000;
const CLIP = __CLIP_SECONDS__;
const BUCKET = __THUMB_BUCKET__;

let cams = [], sel = [], day = null, dayStart = 0;
let runs = [], motion = {}, res = 'high';
let clips = {}, tiles = {}, prefetch = null;
let hovTimer = null, lastBucket = null, prevBucket = null;

const pad = n => String(n).padStart(2, '0');
const hhmm = ms => { const d = new Date(ms);
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); };
const gb = b => (b / 2**30).toFixed(1) + ' GB';
const primary = () => tiles[sel[0]];

async function boot() {
  $('#foot').textContent = 'Orange ticks are motion events. Clips are ' +
    (CLIP % 60 ? CLIP + ' seconds' : CLIP / 60 + ' minutes') +
    ' and advance automatically.';
  cams = await (await fetch('/api/cameras')).json();
  $('#cams').innerHTML =
    cams.map((c, i) => `<button data-sel="${c.id}">${c.name}</button>`).join('') +
    (cams.length > 1 ? '<button data-sel="*">Both</button>' : '');
  $('#cams').onclick = e => {
    const v = e.target.dataset.sel;
    if (v) choose(v === '*' ? cams.map(c => c.id) : [v]);
  };
  $('#q-high').onclick = () => setRes('high');
  $('#q-low').onclick = () => setRes('low');
  choose([cams[0].id]);
}

function choose(ids) {
  const changed = ids.join() !== sel.join();
  sel = ids;
  [...$('#cams').children].forEach(b => {
    const v = b.dataset.sel;
    b.classList.toggle('on', v === '*' ? sel.length > 1
                                       : sel.length === 1 && sel[0] === v);
  });
  buildDays();
  if (!changed) return;
  const at = currentWall();
  buildTiles();
  if (day) pickDay(day, at);
}

function camById(id) { return cams.find(c => c.id === id); }

// Days for the selected cameras, merged.
function buildDays() {
  const seen = {};
  for (const id of sel)
    for (const d of camById(id).days) {
      const e = seen[d.day] || (seen[d.day] = { day: d.day, segments: 0, bytes: 0 });
      e.segments += d.segments; e.bytes += d.bytes;
    }
  const list = Object.values(seen).sort((a, b) => a.day < b.day ? -1 : 1);
  $('#days').innerHTML = list.map(d =>
    `<div class="day${d.day === day ? ' on' : ''}" data-day="${d.day}"><b>${d.day}</b>
     <span>${(d.segments / 60 / sel.length).toFixed(1)} h &middot; ${gb(d.bytes)}</span></div>`
  ).join('');
  $('#days').onclick = e => {
    const el = e.target.closest('.day'); if (el) pickDay(el.dataset.day);
  };
  if (!day && list.length) pickDay(list[list.length - 1].day);
}

function buildTiles() {
  const stage = $('#stage');
  stage.style.setProperty('--cols', sel.length);
  stage.innerHTML = sel.map(id =>
    `<div class="tile"><video id="v-${id}" playsinline${
       id === sel[0] ? ' controls' : ''}></video>
     <span class="tag">${camById(id).name}</span></div>`).join('');
  tiles = {};
  sel.forEach(id => { tiles[id] = document.getElementById('v-' + id); });

  // The first tile drives playback; the others follow it.
  const p = primary();
  p.onplay = () => sel.slice(1).forEach(id => tiles[id].play().catch(() => {}));
  p.onpause = () => sel.slice(1).forEach(id => tiles[id].pause());
  p.onseeked = () => resync(true);
  p.ontimeupdate = onTick;
  p.onended = () => {
    const c = clips[sel[0]];
    if (c) play(c.clipStart + c.duration * 1000);
  };
}

function setRes(r) {
  res = r;
  $('#q-high').classList.toggle('on', r === 'high');
  $('#q-low').classList.toggle('on', r === 'low');
  const at = currentWall();
  if (at) play(at);
}

function currentWall() {
  const c = clips[sel[0]], p = primary();
  return (c && p) ? c.clipStart + p.currentTime * 1000 : null;
}

async function pickDay(d, seekTo) {
  day = d;
  [...$('#days').children].forEach(el => el.classList.toggle('on', el.dataset.day === d));
  const infos = await Promise.all(sel.map(id =>
    fetch(`/api/day?cam=${id}&day=${d}`).then(r => r.json())));
  dayStart = infos[0].dayStart;
  motion = {};
  sel.forEach((id, i) => { motion[id] = infos[i].motion; });
  runs = mergeRuns([].concat(...infos.map(i => i.runs)));
  lastBucket = prevBucket = null;
  draw();
  sel.forEach(id => fetch(`/api/warm?cam=${id}&day=${d}`));
  if (seekTo) play(seekTo);
}

function mergeRuns(list) {
  const out = [];
  for (const [a, b] of list.sort((x, y) => x[0] - y[0])) {
    const last = out[out.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b);
    else out.push([a, b]);
  }
  return out;
}

function draw() {
  const cv = $('#cv'), r = cv.parentElement.getBoundingClientRect();
  const dpr = devicePixelRatio || 1;
  cv.width = r.width * dpr; cv.height = r.height * dpr;
  const g = cv.getContext('2d'); g.scale(dpr, dpr);
  const W = r.width, H = r.height;
  g.clearRect(0, 0, W, H);
  const x = ms => (ms - dayStart) / DAY_MS * W;

  g.strokeStyle = '#272c34'; g.fillStyle = '#5a6474';
  g.font = '10px -apple-system,sans-serif';
  for (let h = 0; h <= 24; h += 2) {
    const px = h / 24 * W;
    g.beginPath(); g.moveTo(px, 0); g.lineTo(px, H); g.stroke();
    if (h < 24) g.fillText(pad(h) + ':00', px + 3, H - 4);
  }
  g.fillStyle = '#2f6ea8';
  for (const [a, b] of runs) g.fillRect(x(a), 6, Math.max(1, x(b) - x(a)), 18);

  // one motion row per selected camera
  const rows = sel.length;
  const rowH = rows > 1 ? 7 : 9;
  sel.forEach((id, i) => {
    g.fillStyle = '#ffb03a';
    for (const m of (motion[id] || [])) g.fillRect(x(m), 28 + i * (rowH + 2), 1.5, rowH);
  });
}
addEventListener('resize', () => { if (runs.length) draw(); });

$('#tl').onclick = e => {
  const r = e.currentTarget.getBoundingClientRect();
  play(dayStart + (e.clientX - r.left) / r.width * DAY_MS);
};

$('#tl').onmousemove = e => {
  const r = e.currentTarget.getBoundingClientRect();
  const t = Math.round(dayStart + (e.clientX - r.left) / r.width * DAY_MS);
  if (!runs.some(([a, b]) => t >= a && t < b)) { $('#hov').style.display = 'none'; return; }
  const hov = $('#hov');
  hov.style.display = 'block';
  hov.style.left = (e.clientX - $('main').getBoundingClientRect().left) + 'px';
  const b = t - (t % BUCKET);
  if (b === lastBucket) { hov.querySelector('.t').textContent = hhmm(t); return; }
  lastBucket = b;
  clearTimeout(hovTimer);
  hovTimer = setTimeout(() => {
    hov.innerHTML = sel.map(id =>
      `<figure><img src="/thumb?cam=${id}&t=${b}">${
        sel.length > 1 ? `<figcaption>${camById(id).name}</figcaption>` : ''
      }</figure>`).join('') + `<div class="t">${hhmm(t)}</div>`;
    const dir = b >= (prevBucket === null ? b : prevBucket) ? 1 : -1;
    prevBucket = b;
    for (const id of sel)
      for (let i = 1; i <= 3; i++)
        new Image().src = `/thumb?cam=${id}&t=${b + dir * i * BUCKET}`;
  }, 50);
};
$('#tl').onmouseleave = () => { $('#hov').style.display = 'none'; clearTimeout(hovTimer); };

async function play(ms) {
  ms = Math.round(ms);
  const run = runs.find(([a, b]) => ms >= a - 1000 && ms < b);
  if (!run) { $('#status').textContent = 'No footage at that time'; return; }
  ms = Math.max(ms, run[0]);
  $('#status').textContent = 'Preparing clip...';
  if (!Object.keys(tiles).length) buildTiles();

  const metas = await Promise.all(sel.map(id =>
    fetch(`/api/clip?cam=${id}&res=${res}&start=${ms}&dur=${CLIP}`)
      .then(r => r.json()).catch(() => ({ error: 'failed' }))));

  clips = {}; prefetch = null;
  const ready = [];
  sel.forEach((id, i) => {
    const meta = metas[i];
    if (meta.error) return;
    clips[id] = meta;
    const v = tiles[id];
    v.src = meta.url;
    ready.push(new Promise(done => {
      v.onloadedmetadata = () => {
        v.currentTime = Math.max(0, (ms - meta.clipStart) / 1000);
        done();
      };
      v.onerror = done;
    }));
  });
  if (!Object.keys(clips).length) { $('#status').textContent = 'Clip failed'; return; }

  await Promise.all(ready);                 // start both together, not staggered
  for (const id of sel) if (tiles[id].src) tiles[id].play().catch(() => {});

  const m = clips[sel[0]];
  $('#dl').innerHTML = sel.filter(id => clips[id]).map(id =>
    `<a href="${clips[id].url}?download=1"><button>Download${
      sel.length > 1 ? ' ' + camById(id).name : ' clip'}</button></a>`).join('');
  $('#status').textContent = sel.map(id => clips[id]
    ? `${camById(id).name} ${clips[id].width}x${clips[id].height}` : '').join('  ·  ') +
    (res === 'low' && m && m.res === 'high' ? '  ·  no 360p for this time' : '');
  $('#head').style.display = 'block';
}

// Keep the followers locked to the primary. Each clip starts at its own
// keyframe, so their timelines are offset by a second or two; comparing wall
// clock rather than currentTime is what keeps the views aligned.
function resync(force) {
  const c0 = clips[sel[0]], p = primary();
  if (!c0 || !p) return;
  const wall = c0.clipStart + p.currentTime * 1000;
  for (const id of sel.slice(1)) {
    const c = clips[id], v = tiles[id];
    if (!c || !v.duration) continue;
    const want = (wall - c.clipStart) / 1000;
    if (want >= 0 && want <= v.duration && (force || Math.abs(v.currentTime - want) > 0.5))
      v.currentTime = want;
  }
}

function onTick() {
  const c = clips[sel[0]], p = primary();
  if (!c) return;
  const now = c.clipStart + p.currentTime * 1000;
  $('#clock').textContent = hhmm(now);
  const r = $('#tl').getBoundingClientRect();
  $('#head').style.left = ((now - dayStart) / DAY_MS * r.width) + 'px';
  resync(false);
  const next = c.clipStart + c.duration * 1000;
  if (!prefetch && p.duration - p.currentTime < 25) {
    prefetch = next;
    for (const id of sel)
      fetch(`/api/clip?cam=${id}&res=${res}&start=${Math.round(next)}&dur=${CLIP}`);
  }
}

boot();
</script>
"""

def main():
    global INDEX, ROOT, CACHE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', default=os.environ.get(
        'NVR_ROOT', os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument('--archives', default=os.environ.get('NVR_ARCHIVE_DIRS', '.'))
    ap.add_argument('--group-by-ip', action='store_true',
                    default=env_bool('NVR_GROUP_CAMERAS_BY_IP'))
    ap.add_argument('--port', type=int, default=int(os.environ.get('NVR_PORT', 8787)))
    ap.add_argument('--host', default=os.environ.get('NVR_HOST', '127.0.0.1'))
    ap.add_argument('--cache', default=CACHE)
    ap.add_argument('--reindex', action='store_true')
    args = ap.parse_args()

    CACHE = os.path.abspath(args.cache)
    ROOT = os.path.abspath(args.root)
    archives = archive_dirs(args.archives)
    assert_outside_store(CACHE, ROOT, 'cache directory')
    os.makedirs(CACHE, exist_ok=True)
    try:
        INDEX = load_index(ROOT, archives, args.group_by_ip, args.reindex)
    except ValueError as e:
        raise SystemExit(str(e))
    if not INDEX['cameras']:
        raise SystemExit(
            'no Scrypted NVR camera directories found in %s under %s\n'
            '(looking for <archive>/<camera>/<session>/session.json)'
            % (', '.join(archives), ROOT))
    for c in INDEX['cameras'].values():
        print('  %-14s %-10s %6.1f h  %5.1f GiB' % (
            c['id'], c['name'], len(c['segments']) / 60.0,
            sum(s[2] for s in c['segments']) / 2**30))
    print('preview warming: %s (%.0f ms between batches)'
          % (WARM_MODE, WARM_DELAY * 1000))
    if WARM_MODE == 'all':
        threading.Thread(target=prewarm_all, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print('\nhttp://%s:%d' % (args.host, args.port))
    print('recording store: %s  (read-only, never written to)' % ROOT)
    print('archive roots:   %s' % ', '.join(archives))
    print('clip cache:      %s' % CACHE)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

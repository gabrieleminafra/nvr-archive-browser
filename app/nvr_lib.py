#!/usr/bin/env python3
"""
Read-only reader for a Scrypted NVR recording store.

Scrypted NVR writes recordings as raw RTSP-interleaved RTP dumps:

    [ 0x24 | channel | 16-bit BE length | RTP packet ] ...

laid out on disk as <camera>/<session>/<hour>/<segment-start-epoch-ms>.rtsp,
with a sibling .json holding endTime/seek points, a session.json holding the
SDP, and a parallel <camera>.events tree holding motion events.

This module depacketizes the H.264 (RFC 6184) and muxes it into MP4, keeping
the original 90 kHz RTP timestamps so the cameras' variable frame timing is
preserved exactly. Nothing here writes to the recording store.
"""
import base64
import json
import os
import struct

CLOCK = 90000
GAP_MS = 120000          # segments further apart than this start a new run
MAX_DELTA = CLOCK * 2    # clamp implausible timestamp jumps


# ---------------------------------------------------------------- depacketize

def iter_rtp(data, channel=0):
    """Yield (timestamp, marker, payload) for each RTP packet in an interleaved dump."""
    i, n = 0, len(data)
    while i + 4 <= n:
        if data[i] != 0x24:
            i += 1  # resync
            continue
        ch = data[i + 1]
        ln = struct.unpack('>H', data[i + 2:i + 4])[0]
        pkt = data[i + 4:i + 4 + ln]
        i += 4 + ln
        if ch != channel or len(pkt) < 12 or pkt[0] >> 6 != 2:
            continue
        off = 12 + 4 * (pkt[0] & 0x0F)
        if pkt[0] & 0x10:  # extension header
            if off + 4 > len(pkt):
                continue
            off += 4 + 4 * struct.unpack('>H', pkt[off + 2:off + 4])[0]
        if off >= len(pkt):
            continue
        yield struct.unpack('>I', pkt[4:8])[0], pkt[1] & 0x80, pkt[off:]


def iter_nals(data):
    """Reassemble RTP payloads into NAL units: yields (timestamp, nal)."""
    fu = None
    for ts, _marker, payload in iter_rtp(data):
        t = payload[0] & 0x1F
        if t == 28:  # FU-A
            if len(payload) < 2:
                continue
            head = payload[1]
            if head & 0x80:
                fu = bytearray([(payload[0] & 0xE0) | (head & 0x1F)])
                fu += payload[2:]
            elif fu is not None:
                fu += payload[2:]
                if head & 0x40:
                    yield ts, bytes(fu)
                    fu = None
        elif t == 24:  # STAP-A
            fu = None
            p = 1
            while p + 2 <= len(payload):
                sz = struct.unpack('>H', payload[p:p + 2])[0]
                p += 2
                if sz and p + sz <= len(payload):
                    yield ts, payload[p:p + sz]
                p += sz
        elif 1 <= t <= 23:
            fu = None
            yield ts, bytes(payload)


def iter_access_units(data):
    """Yield access units as (timestamp, [nals], is_keyframe, sps, pps)."""
    sps = pps = None
    cur, cur_ts = [], None
    for ts, nal in iter_nals(data):
        t = nal[0] & 0x1F
        if t == 7:
            sps = nal
            continue
        if t == 8:
            pps = nal
            continue
        if t in (9, 12):  # AUD / filler
            continue
        if cur_ts is not None and ts != cur_ts:
            yield cur_ts, cur, any(n[0] & 0x1F == 5 for n in cur), sps, pps
            cur = []
        cur_ts = ts
        cur.append(nal)
    if cur:
        yield cur_ts, cur, any(n[0] & 0x1F == 5 for n in cur), sps, pps


def sps_pps_from_sdp(session_json):
    """Fall back to the SDP's sprop-parameter-sets when a segment carries no SPS."""
    sps = pps = None
    try:
        sdp = json.load(open(session_json))['mediaStreamOptions']['sdp']
    except Exception:
        return None, None
    for line in sdp.replace('\r\n', '\n').split('\n'):
        if 'sprop-parameter-sets=' in line:
            for part in line.split('sprop-parameter-sets=')[1].split(';')[0].split(','):
                try:
                    nal = base64.b64decode(part + '==')
                except Exception:
                    continue
                if not nal:
                    continue
                if nal[0] & 0x1F == 7:
                    sps = sps or nal
                elif nal[0] & 0x1F == 8:
                    pps = pps or nal
    return sps, pps


# ------------------------------------------------------------------ sps parse

def sps_dimensions(sps):
    """Decode width/height from an SPS NAL."""
    rbsp = bytearray()
    i = 1
    while i < len(sps):
        if sps[i] == 3 and len(rbsp) >= 2 and rbsp[-1] == 0 and rbsp[-2] == 0:
            i += 1
            continue
        rbsp.append(sps[i])
        i += 1
    pos = [0]

    def bit():
        b = (rbsp[pos[0] >> 3] >> (7 - (pos[0] & 7))) & 1
        pos[0] += 1
        return b

    def bits(n):
        v = 0
        for _ in range(n):
            v = (v << 1) | bit()
        return v

    def ue():
        z = 0
        while not bit():
            z += 1
        return (1 << z) - 1 + bits(z) if z else 0

    def se():
        v = ue()
        return (v + 1) // 2 if v % 2 else -(v // 2)

    profile = bits(8)
    bits(16)     # constraint flags + level
    ue()         # sps id
    chroma = 1
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = ue()
        if chroma == 3:
            bit()
        ue(); ue(); bit()
        if bit():
            for i in range(8 if chroma != 3 else 12):
                if bit():
                    last = nxt = 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            nxt = (last + se() + 256) % 256
                        last = nxt or last
    ue()
    order = ue()
    if order == 0:
        ue()
    elif order == 1:
        bit(); se(); se()
        for _ in range(ue()):
            se()
    ue(); bit()
    w_mbs = ue() + 1
    h_maps = ue() + 1
    frame_mbs_only = bit()
    if not frame_mbs_only:
        bit()
    bit()
    cl = cr = ct = cb = 0
    if bit():
        cl, cr, ct, cb = ue(), ue(), ue(), ue()
    sub_w = 2 if chroma in (1, 2) else 1
    sub_h = 2 if chroma == 1 else 1
    width = w_mbs * 16 - sub_w * (cl + cr)
    height = ((2 - frame_mbs_only) * h_maps * 16
              - sub_h * (2 - frame_mbs_only) * (ct + cb))
    return width, height


# ------------------------------------------------------------------ mp4 muxer

def _box(kind, *payload):
    body = b''.join(payload)
    return struct.pack('>I', 8 + len(body)) + kind + body


def _full(kind, version, flags, *payload):
    return _box(kind, struct.pack('>BBH', version, flags >> 16, flags & 0xFFFF),
                *payload)


class Mp4Writer:
    """Streaming MP4 writer: samples go straight to disk, moov is written last."""

    FTYP = _box(b'ftyp', b'isom' + struct.pack('>I', 512) + b'isomiso2avc1mp41')

    def __init__(self, fileobj):
        self.f = fileobj
        self.f.write(self.FTYP)
        # 64-bit mdat header, size patched on close
        self.mdat_pos = self.f.tell()
        self.f.write(struct.pack('>I', 1) + b'mdat' + struct.pack('>Q', 0))
        self.data_start = self.f.tell()
        self.sizes, self.offsets, self.durations, self.keys = [], [], [], []
        self.sps = self.pps = None
        self.pending_ts = None

    def add(self, nals, is_key, delta):
        """Append one access unit. `delta` is its predecessor's duration in 90 kHz ticks."""
        if self.sizes:
            self.durations.append(delta)
        payload = b''.join(struct.pack('>I', len(n)) + n for n in nals)
        self.offsets.append(self.data_start + sum(self.sizes))
        self.sizes.append(len(payload))
        if is_key:
            self.keys.append(len(self.sizes))
        self.f.write(payload)

    def close(self):
        if not self.sizes:
            raise ValueError('no frames')
        if self.sps is None or self.pps is None:
            raise ValueError('no SPS/PPS')
        default = min(self.durations) if self.durations else CLOCK // 25
        self.durations.append(default)
        duration = sum(self.durations)

        end = self.f.tell()
        self.f.seek(self.mdat_pos + 8)
        self.f.write(struct.pack('>Q', end - self.mdat_pos))
        self.f.seek(end)

        stts = []
        for d in self.durations:
            if stts and stts[-1][1] == d:
                stts[-1][0] += 1
            else:
                stts.append([1, d])

        w, h = sps_dimensions(self.sps)
        avcc = _box(b'avcC',
                    b'\x01' + self.sps[1:4] + b'\xff' +
                    b'\xe1' + struct.pack('>H', len(self.sps)) + self.sps +
                    b'\x01' + struct.pack('>H', len(self.pps)) + self.pps)
        avc1 = _box(b'avc1',
                    b'\x00' * 6 + struct.pack('>H', 1) + b'\x00' * 16 +
                    struct.pack('>HH', w, h) +
                    struct.pack('>II', 0x00480000, 0x00480000) +
                    b'\x00' * 4 + struct.pack('>H', 1) + b'\x00' * 32 +
                    struct.pack('>Hh', 24, -1),
                    avcc)
        stbl = _box(
            b'stbl',
            _full(b'stsd', 0, 0, struct.pack('>I', 1), avc1),
            _full(b'stts', 0, 0, struct.pack('>I', len(stts)),
                  b''.join(struct.pack('>II', c, d) for c, d in stts)),
            _full(b'stss', 0, 0, struct.pack('>I', len(self.keys)),
                  b''.join(struct.pack('>I', k) for k in self.keys)),
            _full(b'stsc', 0, 0, struct.pack('>I', 1), struct.pack('>III', 1, 1, 1)),
            _full(b'stsz', 0, 0, struct.pack('>II', 0, len(self.sizes)),
                  b''.join(struct.pack('>I', s) for s in self.sizes)),
            _full(b'co64', 0, 0, struct.pack('>I', len(self.offsets)),
                  b''.join(struct.pack('>Q', o) for o in self.offsets)))

        unity = struct.pack('>9i', 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
        minf = _box(b'minf',
                    _box(b'vmhd', struct.pack('>IHHHH', 1, 0, 0, 0, 0)),
                    _box(b'dinf', _full(b'dref', 0, 0, struct.pack('>I', 1),
                                        _full(b'url ', 0, 1))),
                    stbl)
        mdia = _box(b'mdia',
                    _full(b'mdhd', 0, 0,
                          struct.pack('>IIII', 0, 0, CLOCK, duration) +
                          struct.pack('>HH', 0x55C4, 0)),
                    _full(b'hdlr', 0, 0, struct.pack('>I', 0) + b'vide' +
                          b'\x00' * 12 + b'VideoHandler\x00'),
                    minf)
        trak = _box(b'trak',
                    _full(b'tkhd', 0, 7,
                          struct.pack('>IIIII', 0, 0, 1, 0, duration) +
                          b'\x00' * 8 + struct.pack('>HHHH', 0, 0, 0, 0) + unity +
                          struct.pack('>II', w << 16, h << 16)),
                    mdia)
        self.f.write(_box(b'moov',
                          _full(b'mvhd', 0, 0,
                                struct.pack('>IIII', 0, 0, CLOCK, duration) +
                                struct.pack('>IH', 0x00010000, 0x0100) +
                                b'\x00' * 10 + unity + b'\x00' * 24 +
                                struct.pack('>I', 2)),
                          trak))
        return duration / CLOCK, len(self.sizes), w, h


def build_clip(segments, out, start_ms=None, end_ms=None):
    """Mux `segments` (list of (start_ms, path)) into the MP4 file object `out`.

    Output begins at the last keyframe at or before `start_ms`, so playback is
    clean even though segments themselves start mid-GOP. Returns a dict with the
    actual clip start, so a caller can seek to the requested moment.
    """
    writer = Mp4Writer(out)
    prev_ts = None
    clip_start = None
    started = False
    pending = []  # AUs held back until we know where the clip starts

    for seg_start, path in segments:
        with open(path, 'rb') as f:
            data = f.read()
        base_ts = None
        for ts, nals, is_key, sps, pps in iter_access_units(data):
            if sps is not None:
                writer.sps = writer.sps or sps
            if pps is not None:
                writer.pps = writer.pps or pps
            if base_ts is None:
                base_ts = ts
            wall = seg_start + (ts - base_ts) / 90.0

            if not started:
                if end_ms is not None and wall >= end_ms:
                    break
                if is_key and (start_ms is None or wall <= start_ms):
                    pending = [(wall, ts, nals, True)]   # newer keyframe wins
                    continue
                if is_key and start_ms is not None and wall > start_ms:
                    if not pending:
                        pending = [(wall, ts, nals, True)]
                        continue
                    # first keyframe past the requested start: flush what we held
                    started = True
                elif pending:
                    pending.append((wall, ts, nals, is_key))
                    continue
                else:
                    continue
                for w, t, n, k in pending:
                    if clip_start is None:
                        clip_start = w
                    delta = 0 if prev_ts is None else t - prev_ts
                    if not (0 < delta <= MAX_DELTA):
                        delta = CLOCK // 25
                    writer.add(n, k, delta)
                    prev_ts = t
                pending = []

            if started:
                if end_ms is not None and wall >= end_ms:
                    break
                delta = 0 if prev_ts is None else ts - prev_ts
                if not (0 < delta <= MAX_DELTA):
                    delta = CLOCK // 25
                writer.add(nals, is_key, delta)
                prev_ts = ts
        else:
            continue
        break

    if not started and pending:  # short clip that never passed a second keyframe
        for w, t, n, k in pending:
            if clip_start is None:
                clip_start = w
            delta = 0 if prev_ts is None else t - prev_ts
            if not (0 < delta <= MAX_DELTA):
                delta = CLOCK // 25
            writer.add(n, k, delta)
            prev_ts = t

    if writer.sps is None and segments:
        sess = os.path.join(os.path.dirname(os.path.dirname(segments[0][1])),
                            'session.json')
        writer.sps, writer.pps = sps_pps_from_sdp(sess)

    secs, frames, w, h = writer.close()
    return {'clipStart': clip_start, 'duration': secs, 'frames': frames,
            'width': w, 'height': h}


# ------------------------------------------------------- fast frame extraction

ANNEXB = b'\x00\x00\x00\x01'


def seek_points(seg_path):
    """(byte offsets, interval_ms) from a segment's sidecar, or ([], 0)."""
    try:
        with open(seg_path[:-5] + '.json') as f:
            doc = json.load(f)
    except Exception:
        return [], 0
    return doc.get('videoSeekPoints') or [], doc.get('videoSeekPointsInterval') or 0


class _NalReader:
    """Incremental RTSP-interleaved parser, so a caller can stop reading early."""

    def __init__(self):
        self.buf = b''
        self.fu = None

    def feed(self, chunk):
        """Add bytes; yield (timestamp, nal) for everything now complete."""
        self.buf += chunk
        i, n = 0, len(self.buf)
        while True:
            if i + 4 > n:
                break
            if self.buf[i] != 0x24:
                i += 1
                continue
            ln = struct.unpack('>H', self.buf[i + 2:i + 4])[0]
            if i + 4 + ln > n:
                break
            pkt = self.buf[i + 4:i + 4 + ln]
            ch = self.buf[i + 1]
            i += 4 + ln
            if ch != 0 or len(pkt) < 12 or pkt[0] >> 6 != 2:
                continue
            off = 12 + 4 * (pkt[0] & 0x0F)
            if pkt[0] & 0x10:
                if off + 4 > len(pkt):
                    continue
                off += 4 + 4 * struct.unpack('>H', pkt[off + 2:off + 4])[0]
            if off >= len(pkt):
                continue
            ts = struct.unpack('>I', pkt[4:8])[0]
            payload = pkt[off:]
            t = payload[0] & 0x1F
            if t == 28:
                if len(payload) < 2:
                    continue
                head = payload[1]
                if head & 0x80:
                    self.fu = bytearray([(payload[0] & 0xE0) | (head & 0x1F)])
                    self.fu += payload[2:]
                elif self.fu is not None:
                    self.fu += payload[2:]
                    if head & 0x40:
                        yield ts, bytes(self.fu)
                        self.fu = None
            elif t == 24:
                self.fu = None
                p = 1
                while p + 2 <= len(payload):
                    sz = struct.unpack('>H', payload[p:p + 2])[0]
                    p += 2
                    if sz and p + sz <= len(payload):
                        yield ts, payload[p:p + sz]
                    p += sz
            elif 1 <= t <= 23:
                self.fu = None
                yield ts, bytes(payload)
        self.buf = self.buf[i:]


def extract_keyframe(seg_path, seg_start_ms, t_ms, session_json=None,
                     chunk=384 << 10):
    """Annex-B bytes for one keyframe near `t_ms`, reading as little as possible.

    Seek points mark every ~2 s of video, not keyframes, so this starts a couple
    of points before the target and streams forward until the first complete IDR
    access unit appears — typically a few hundred KB, against a 13 MB segment.
    For a scrub preview the exact instant does not matter; the read size does.
    """
    points, interval = seek_points(seg_path)
    size = os.path.getsize(seg_path)
    start = 0
    if points and interval:
        idx = max(0, min(int((t_ms - seg_start_ms) / interval), len(points) - 1))
        start = points[max(0, idx - 2)]

    reader = _NalReader()
    sps = pps = None
    au, au_ts, is_key, done = [], None, False, False
    with open(seg_path, 'rb') as f:
        f.seek(start)
        read = 0
        while not done and start + read < size:
            data = f.read(chunk)
            if not data:
                break
            read += len(data)
            for ts, nal in reader.feed(data):
                kind = nal[0] & 0x1F
                if kind == 7:
                    sps = sps or nal
                    continue
                if kind == 8:
                    pps = pps or nal
                    continue
                if kind in (9, 12):
                    continue
                if au_ts is not None and ts != au_ts:
                    if is_key and au:
                        done = True
                        break
                    au, is_key = [], False
                au_ts = ts
                au.append(nal)
                if kind == 5:
                    is_key = True

    if not (is_key and au):
        return None
    if (sps is None or pps is None) and session_json:
        s2, p2 = sps_pps_from_sdp(session_json)
        sps, pps = sps or s2, pps or p2
    if sps is None or pps is None:
        return None
    return ANNEXB + sps + ANNEXB + pps + b''.join(ANNEXB + n for n in au)


# --------------------------------------------------------------------- index

# Camera directory names are used by default. Override them without changing
# the code, for example:
# NVR_CAMERA_NAMES="scrypted-26=Front Door,scrypted-31=Garage".
SUFFIXES = ('.events', '.remote', '.low-resolution')


def camera_names():
    names = {}
    for pair in os.environ.get('NVR_CAMERA_NAMES', '').split(','):
        if '=' in pair:
            key, _, value = pair.partition('=')
            names[key.strip()] = value.strip()
    return names


def discover_cameras(root):
    """Find camera directories in a Scrypted NVR store.

    A camera is a directory holding <session>/session.json. Its substream and
    metadata siblings (<name>.remote, <name>.events, ...) are not cameras in
    their own right, so they are excluded.
    """
    found = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return found
    for name in entries:
        if name.startswith('.') or name.endswith(SUFFIXES):
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            sessions = sorted(os.listdir(path))[:40]
        except OSError:
            continue
        if any(os.path.exists(os.path.join(path, s, 'session.json'))
               for s in sessions):
            found.append(name)
    return found


def camera_ip(root, cam):
    """The camera's address, as recorded in the stream URL of its first session."""
    base = os.path.join(root, cam)
    try:
        sessions = sorted(os.listdir(base))
    except OSError:
        return None
    for s in sessions:
        session = os.path.join(base, s, 'session.json')
        if not os.path.exists(session):
            continue
        try:
            with open(session) as f:
                url = json.load(f)['mediaStreamOptions'].get('url', '')
        except Exception:
            continue
        host = url.split('//', 1)[-1].split('/')[0].split('@')[-1]
        return host.split(':')[0] or None
    return None


def scan_camera(root, cam):
    """Return sorted [(start_ms, relpath, size)] for a camera directory."""
    base = os.path.join(root, cam)
    out = []
    if not os.path.isdir(base):
        return out
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            if not name.endswith('.rtsp'):
                continue
            try:
                start = int(name[:-5])
            except ValueError:
                continue
            full = os.path.join(dirpath, name)
            out.append((start, os.path.relpath(full, root),
                        os.path.getsize(full)))
    out.sort()
    return out


def scan_motion(root, cam):
    """Motion-start timestamps (ms) from the camera's .events tree."""
    base = os.path.join(root, cam + '.events')
    times = []
    if not os.path.isdir(base):
        return times
    for dirpath, _dirs, files in os.walk(base):
        if 'hour.json' not in files:
            continue
        try:
            with open(os.path.join(dirpath, 'hour.json')) as f:
                doc = json.load(f)
        except Exception:
            continue
        for ev in doc.get('recordedEvents', []):
            if ev.get('data') is True:
                t = ev.get('details', {}).get('eventTime')
                if t:
                    times.append(int(t))
    times.sort()
    return times


def runs_of(segments, seg_ms=60000):
    """Collapse segment starts into contiguous [start_ms, end_ms] runs."""
    runs = []
    for start, _rel, _size in segments:
        if runs and start - runs[-1][1] <= GAP_MS:
            runs[-1][1] = start + seg_ms
        else:
            runs.append([start, start + seg_ms])
    return runs


def build_index(root):
    names = camera_names()
    index = {'root': os.path.abspath(root), 'cameras': {}}
    for cam in discover_cameras(root):
        segments = scan_camera(root, cam)
        if not segments:
            continue
        index['cameras'][cam] = {
            'id': cam,
            'name': names.get(cam, cam),
            'ip': camera_ip(root, cam),
            'segments': segments,
            'lowSegments': scan_camera(root, cam + '.remote'),
            'motion': scan_motion(root, cam),
        }
    return index

"""Opt-in, read-only conversation history for the mobile reader.

No connection or process starts on import. The phone supplies only a canonical
agent name and, for older pages, an opaque token this reader issued. It never
supplies or receives a file path, transcript cursor or cockpit reference. The
reader calls one fixed loopback cockpit route, which resolves the dashboard's
bound record itself and rechecks it after reading; it does not mask text. This
module keeps an explicit allow-list of event fields and bounds every page.

File and image previews follow the same rule. Cockpit attaches its own
reference IDs to tool events; this reader keeps them and gives the phone a
different opaque ID per reference. A preview asks the fixed cockpit preview
route, which rechecks the record, the root, the canonical path, the type, the
size and the pixel count on every read, and refuses nothing by name. This module then bounds
the answer again: text keeps its start, images pass only as a plain PNG
(IHDR, IDAT and IEND chunks) within side, pixel and byte limits.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
import http.client
import json
import re
import secrets
import socket
import threading
import time
from urllib.parse import urlencode
import zlib

NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,95}\Z')
TOKEN = re.compile(r'[a-f0-9]{32}\Z')
# Cockpit cursors are opaque to the phone. They stay in this process.
UPSTREAM_CURSOR = re.compile(r'[A-Za-z0-9:_.-]{1,160}\Z')
REASON_CODE = re.compile(r'[a-z0-9_]{1,64}\Z')
# Cockpit's own reference IDs (secrets.token_urlsafe(24)). They stay here.
COCKPIT_REF = re.compile(r'[A-Za-z0-9_-]{32}\Z')
PAGE_EVENTS = 40
MAX_UPSTREAM_BYTES = 8 * 1024 * 1024
MAX_TEXT = 16 * 1024
MAX_MAIL_BODY = 16 * 1024
MAX_PAGE_BYTES = 512 * 1024
# What the relay actually sends (UTF-8, not \u escapes). The phone refuses
# more than 768 KiB, so this leaves headroom for the response envelope.
MAX_WIRE_BYTES = 640 * 1024
MAX_TOKENS = 256
TOKEN_SECONDS = 600
# Cockpit's conversation route rechecks the roster and the binding, which
# takes several seconds when the computer is busy (2-8 s at load 12-14).
UPSTREAM_TIMEOUT = 15
MAX_REFS = 1024
MAX_PREVIEW_TEXT = 32 * 1024
MAX_PREVIEW_PNG = 4 * 1024 * 1024
MAX_PREVIEW_SIDE = 8192
MAX_PREVIEW_PIXELS = 8_000_000
# Base64 of the largest accepted PNG plus a small JSON envelope.
MAX_PREVIEW_UPSTREAM = MAX_PREVIEW_PNG * 4 // 3 + 64 * 1024
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
ERRORS = frozenset({
    'agent_unavailable', 'provider_unavailable', 'dashboard_upgrade_required',
    'history_unconfirmed', 'history_stale', 'invalid_binding', 'history_unreadable',
    'history_changed', 'history_unavailable', 'dashboard_unavailable', 'claude_next_phase',
    'record_too_large', 'invalid_cursor', 'invalid_agent', 'history_slow',
    'preview_unavailable', 'preview_too_large', 'preview_type_denied', 'preview_path_denied',
    'preview_changed',
})


class HistoryUnavailable(ValueError):
    """A fixed public error code with an optional fixed-format reason code."""
    def __init__(self, code, reason_code=''):
        super().__init__(code if code in ERRORS else 'history_unavailable')
        self.code = str(self)
        self.reason_code = reason_code if isinstance(reason_code, str) and REASON_CODE.fullmatch(reason_code) else ''


def encode(payload):
    """The exact bytes the relay sends: compact UTF-8 JSON."""
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _text(value, limit):
    if not isinstance(value, str):
        return '', False
    # Lone surrogates cannot be sent as UTF-8; replace them instead of
    # failing the whole page.
    data = value.encode('utf-8', errors='replace')
    if len(data) <= limit:
        return data.decode('utf-8'), False
    return data[:limit].decode('utf-8', errors='ignore'), True


def _label(value, limit=160):
    text, _ = _text(value, limit)
    return ''.join(c for c in text if c >= ' ' and c != '\x7f')


def project_mail(item, body_limit=MAX_MAIL_BODY):
    if not isinstance(item, dict) or item.get('direction') not in {'received', 'sent'}:
        return None
    body, cut = _text(item.get('body'), body_limit)
    recipients = item.get('recipients') if isinstance(item.get('recipients'), list) else []
    importance = item.get('importance') if item.get('importance') in {'low', 'normal', 'high', 'urgent'} else 'normal'
    coverage = item.get('coverage') if item.get('coverage') in {'complete', 'preview', 'unavailable', 'request'} else 'unavailable'
    return {'direction': item['direction'], 'sender': _label(item.get('sender')),
            'recipients': [_label(r) for r in recipients[:24] if isinstance(r, str) and r][:24],
            'subject': _label(item.get('subject'), 512), 'importance': importance, 'coverage': coverage,
            'body': body, 'truncated': cut or item.get('truncated') is True}


def project_event(raw, text_limit=MAX_TEXT, mail_limit=MAX_MAIL_BODY, issue=None):
    """Only displayed fields. Diffs, cockpit reference IDs, cursors and paths
    never pass. ``issue`` turns a well-formed cockpit reference into this
    reader's own opaque ID; without it a file is a label only."""
    if not isinstance(raw, dict):
        return None
    kind, role = raw.get('kind'), raw.get('role')
    # Generated notifications use the existing concise activity projection;
    # private task IDs, output paths and full system details are not forwarded.
    if kind == 'system' and role == 'system':
        kind, role = 'tool', 'assistant'
    if kind not in {'text', 'tool', 'mail'} or role not in {'user', 'assistant'}:
        return None
    event_id = raw.get('id')
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 160 or not event_id.isascii():
        return None
    text, cut = _text(raw.get('text'), 160 if kind == 'tool' else text_limit)
    if kind == 'tool':
        text = _label(text)
    result = {'id': event_id, 'kind': kind, 'role': role, 'text': text,
              'ts': _label(raw.get('ts'), 40), 'truncated': cut or raw.get('truncated') is True}
    mail = [m for m in (project_mail(x, mail_limit) for x in (raw.get('mail') or [])[:8]) if m] if isinstance(raw.get('mail'), list) else []
    if kind == 'mail' and not mail:
        return None
    if mail:
        result['mail'] = mail
    refs = raw.get('references') if isinstance(raw.get('references'), list) else []
    chips = []
    for r in refs[:8]:
        if not isinstance(r, dict) or not _label(r.get('label')):
            continue
        chip = {'label': _label(r.get('label')), 'kind': 'image' if r.get('kind') == 'image' else 'file'}
        if issue is not None and isinstance(r.get('id'), str) and COCKPIT_REF.fullmatch(r['id']):
            chip['ref'] = issue(r['id'])
        chips.append(chip)
    if chips:
        result['files'] = chips
    return result


# Smaller limits for a single message that is still too large for one page
# (for example text made of characters JSON must escape).
SHRINK_LIMITS = ((4096, 2048), (1024, 512), (256, 128))


def project_page(raw, issue=None):
    """Newest displayable events within one bounded page.

    Returns (events, before, limited, omitted). When events are trimmed for
    count or size, ``before`` restarts at the oldest kept event's own cursor,
    so Show older returns the trimmed events instead of skipping them. Only if
    that cursor is unusable, or one event cannot fit at all, is ``omitted``
    true so the phone can mark the gap.
    """
    if not isinstance(raw, dict) or raw.get('ok') is not True or not isinstance(raw.get('events'), list):
        raise HistoryUnavailable('history_unavailable')
    pairs = [(e, x) for x in raw['events'][-200:] for e in (project_event(x, issue=issue),) if e]
    trimmed = len(raw['events']) > 200 or len(pairs) > PAGE_EVENTS
    pairs = pairs[-PAGE_EVENTS:]
    limited = raw.get('limited') is True or trimmed
    while len(pairs) > 1 and len(encode([e for e, _ in pairs])) > MAX_PAGE_BYTES:
        pairs.pop(0)
        trimmed = limited = True
    omitted, anchor = False, pairs[0][1] if pairs else None
    if pairs and len(encode([pairs[0][0]])) > MAX_PAGE_BYTES:
        limited = True
        for text_limit, mail_limit in SHRINK_LIMITS:
            small = project_event(anchor, text_limit, mail_limit, issue)
            small['truncated'] = True
            if len(encode([small])) <= MAX_PAGE_BYTES:
                pairs = [(small, anchor)]
                break
        else:
            pairs, trimmed, omitted = [], True, True
    if trimmed:
        cursor = anchor.get('cursor_before') if isinstance(anchor, dict) else None
        if isinstance(cursor, str) and UPSTREAM_CURSOR.fullmatch(cursor):
            # Without a page, the cursor still steps past the unfit event.
            return [e for e, _ in pairs], cursor, limited, omitted
        return [e for e, _ in pairs], None, limited, True
    before = raw.get('before') if raw.get('has_older') is True else None
    if before is not None and (not isinstance(before, str) or not UPSTREAM_CURSOR.fullmatch(before)):
        before = None
    return [e for e, _ in pairs], before, limited, omitted


def png_size(data):
    """(width, height) of a plain PNG: IHDR, then IDAT chunks, then IEND, each
    with a valid CRC and nothing after. Text, EXIF, ICC and animation chunks
    are refused rather than stripped."""
    if not isinstance(data, bytes) or data[:8] != PNG_SIGNATURE:
        raise HistoryUnavailable('preview_unavailable')
    kinds, at = [], 8
    while at < len(data):
        if at + 12 > len(data):
            raise HistoryUnavailable('preview_unavailable')
        size = int.from_bytes(data[at:at + 4], 'big')
        end = at + 12 + size
        kind = data[at + 4:at + 8]
        if end > len(data) or zlib.crc32(data[at + 4:end - 4]) != int.from_bytes(data[end - 4:end], 'big'):
            raise HistoryUnavailable('preview_unavailable')
        kinds.append(kind)
        if kind == b'IHDR' and size != 13:
            raise HistoryUnavailable('preview_unavailable')
        at = end
        if kind == b'IEND':
            break
    if (at != len(data) or len(kinds) < 3 or kinds[0] != b'IHDR' or kinds[-1] != b'IEND'
            or set(kinds[1:-1]) != {b'IDAT'}):
        raise HistoryUnavailable('preview_unavailable')
    width, height = int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')
    if not width or not height:
        raise HistoryUnavailable('preview_unavailable')
    return width, height


def project_preview(raw):
    """A bounded text start or a checked PNG. No path, MIME passthrough or HTML."""
    if not isinstance(raw, dict) or raw.get('ok') is not True:
        raise HistoryUnavailable('preview_unavailable')
    label = _label(raw.get('label'))
    notice = _label(raw.get('notice'))
    if raw.get('kind') == 'text' and raw.get('mime', 'text/plain') == 'text/plain' and isinstance(raw.get('text'), str):
        body, cut = _text(raw['text'], MAX_PREVIEW_TEXT)
        return {'ok': True, 'kind': 'text', 'label': label, 'text': body, 'truncated': cut, 'notice': notice}
    if raw.get('kind') != 'image' or raw.get('mime') != 'image/png':
        raise HistoryUnavailable('preview_type_denied')
    data = raw.get('data')
    if not isinstance(data, str) or not data.isascii():
        raise HistoryUnavailable('preview_unavailable')
    if len(data) > MAX_PREVIEW_PNG * 4 // 3 + 4:
        raise HistoryUnavailable('preview_too_large')
    try:
        decoded = base64.b64decode(data, validate=True)
    except ValueError:
        raise HistoryUnavailable('preview_unavailable') from None
    if len(decoded) > MAX_PREVIEW_PNG:
        raise HistoryUnavailable('preview_too_large')
    width, height = png_size(decoded)
    if (width, height) != (raw.get('width'), raw.get('height')):
        raise HistoryUnavailable('preview_unavailable')
    if width > MAX_PREVIEW_SIDE or height > MAX_PREVIEW_SIDE or width * height > MAX_PREVIEW_PIXELS:
        raise HistoryUnavailable('preview_too_large')
    return {'ok': True, 'kind': 'image', 'label': label, 'width': width, 'height': height,
            'data': base64.b64encode(decoded).decode('ascii'), 'notice': notice}


class HistoryClient:
    """Fixed loopback GET to cockpit; no redirects, proxy env or retries."""
    def __init__(self, port, *, clock=time.monotonic):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('history_port')
        self.port, self.clock = port, clock
        # Both tables stay in expiry order (each issue or renewal moves its
        # entry to the end with the same lifetime), and each has a reverse
        # index, so issuing and expiring never walk a whole table.
        self._tokens = OrderedDict()
        self._token_keys = {}
        self._refs = OrderedDict()
        self._ref_keys = {}
        self._lock = threading.Lock()

    def page(self, name, token=None):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise HistoryUnavailable('invalid_agent')
        before = None
        if token is not None:
            with self._lock:
                self._expire()
                entry = self._tokens.get(token) if isinstance(token, str) and TOKEN.fullmatch(token) else None
            if entry is None or entry[0] != name:
                raise LookupError('page_expired')
            before = entry[1]
        query = {'agent': name, 'limit': str(PAGE_EVENTS)}
        if before is not None:
            query['before'] = before
        status, raw = self._get('/telemetry/conversation?' + urlencode(query))
        if status != 200:
            error = raw.get('error') if isinstance(raw, dict) else None
            raise HistoryUnavailable(error if isinstance(error, str) else 'history_unavailable',
                                     raw.get('reason_code', '') if isinstance(raw, dict) else '')
        events, older, limited, omitted = project_page(raw, lambda ref: self._issue_ref(name, ref))
        issued = self._issue_token(name, older) if older is not None else None
        provider = raw.get('provider') if raw.get('provider') in {'codex', 'claude', 'dsh', 'hermes'} else ''
        result = {'ok': True, 'agent': name, 'provider': provider, 'events': events,
                  'older': issued, 'limited': limited, 'omitted': omitted,
                  'tail_only': raw.get('mode') == 'fresh_tail'}
        # The page is at most MAX_PAGE_BYTES and the envelope a few hundred
        # bytes; refuse rather than trim if that ever stops holding.
        if len(encode(result)) > MAX_WIRE_BYTES:
            raise HistoryUnavailable('record_too_large')
        return result

    def preview(self, name, ref):
        """One previewable file named by an ID this reader issued for ``name``."""
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise HistoryUnavailable('invalid_agent')
        with self._lock:
            self._expire()
            entry = self._refs.get(ref) if isinstance(ref, str) and TOKEN.fullmatch(ref) else None
        if entry is None or entry[0] != name:
            raise LookupError('preview_expired')
        status, raw = self._get('/telemetry/conversation/preview?' + urlencode({'ref': entry[1]}),
                                limit=MAX_PREVIEW_UPSTREAM, too_large='preview_too_large')
        if status != 200 or not isinstance(raw, dict) or raw.get('ok') is not True:
            error = raw.get('error') if isinstance(raw, dict) else None
            if error == 'reference_expired':
                raise LookupError('preview_expired')
            if error == 'record_too_large':
                # The cockpit record holding the reference, not the file.
                raise HistoryUnavailable('preview_too_large')
            known = isinstance(error, str) and error in ERRORS and error != 'history_unavailable'
            raise HistoryUnavailable(error if known else 'preview_unavailable')
        return project_preview(raw)

    def _issue_ref(self, name, cockpit_ref):
        """One phone ID per (agent, cockpit reference), renewed while its
        page is polled, like page tokens."""
        return self._issue(self._refs, self._ref_keys, MAX_REFS, name, cockpit_ref)

    def _issue_token(self, name, cursor):
        """One token per (agent, cursor). Polling the same latest page reuses
        and renews it, so an open sheet's Show older does not lapse and the
        table does not grow with every poll."""
        return self._issue(self._tokens, self._token_keys, MAX_TOKENS, name, cursor)

    def _issue(self, table, keys, limit, name, value):
        with self._lock:
            self._expire()
            expires = self.clock() + TOKEN_SECONDS
            issued = keys.get((name, value))
            if issued is None:
                issued = secrets.token_hex(16)
                keys[(name, value)] = issued
            table[issued] = (name, value, expires)
            table.move_to_end(issued)
            while len(table) > limit:
                _, (owner, kept, _) = table.popitem(last=False)
                keys.pop((owner, kept), None)
            return issued

    def _expire(self):
        now = self.clock()
        for table, keys in ((self._tokens, self._token_keys), (self._refs, self._ref_keys)):
            while table:
                key = next(iter(table))
                owner, kept, expires = table[key]
                if expires > now:
                    break
                del table[key]
                keys.pop((owner, kept), None)

    def _get(self, path, *, limit=MAX_UPSTREAM_BYTES, too_large='history_unavailable'):
        con = http.client.HTTPConnection('127.0.0.1', self.port, timeout=UPSTREAM_TIMEOUT)
        timer = None
        deadline = time.monotonic() + UPSTREAM_TIMEOUT
        # Running out of time is its own code: waiting and retrying fixes it.
        late = threading.Event()
        try:
            con.connect()
            sock = con.sock
            def expire():
                late.set()
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(max(.001, deadline - time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            # Originless loopback request to the fixed cockpit route. No phone
            # header, owner login or cookie is forwarded.
            con.request('GET', path, headers={'Host': f'127.0.0.1:{self.port}',
                                              'Accept': 'application/json', 'Connection': 'close'})
            response = con.getresponse()
            if (response.status not in {200, 400, 409, 502}
                    or response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/json'
                    or response.getheader('Content-Encoding', 'identity').lower() != 'identity'):
                raise HistoryUnavailable('history_unavailable')
            length = response.getheader('Content-Length')
            if length is not None and not length.isdigit():
                raise HistoryUnavailable('history_unavailable')
            if length is not None and int(length) > limit:
                raise HistoryUnavailable(too_large)
            data = response.read(limit + 1)
            if len(data) > limit:
                raise HistoryUnavailable(too_large)
            # A body cut short by the deadline is slow, not malformed.
            if late.is_set() or time.monotonic() >= deadline:
                raise HistoryUnavailable('history_slow')
            # read(amt) returns a short body without complaint; a cut answer
            # is refused even when what arrived happens to parse.
            if length is not None and len(data) != int(length):
                raise HistoryUnavailable('history_unavailable')
            return response.status, json.loads(data)
        except HistoryUnavailable:
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeError):
            raise HistoryUnavailable('history_slow' if late.is_set() or time.monotonic() >= deadline else 'history_unavailable') from None
        finally:
            if timer is not None:
                timer.cancel()
            con.close()

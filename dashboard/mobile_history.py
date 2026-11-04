"""Opt-in, read-only conversation history for the mobile reader.

No connection or process starts on import. The phone supplies only a canonical
agent name and, for older pages, an opaque token this reader issued. It never
supplies or receives a file path, transcript cursor or cockpit reference. The
reader calls one fixed loopback cockpit route, which resolves the dashboard's
bound record itself, rechecks it after reading and applies its redaction. This
module keeps an explicit allow-list of event fields and bounds every page.
"""
from __future__ import annotations

from collections import OrderedDict
import http.client
import json
import re
import secrets
import socket
import threading
import time
from urllib.parse import urlencode

NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,95}\Z')
TOKEN = re.compile(r'[a-f0-9]{32}\Z')
# Cockpit cursors are opaque to the phone. They stay in this process.
UPSTREAM_CURSOR = re.compile(r'[A-Za-z0-9:_.-]{1,160}\Z')
REASON_CODE = re.compile(r'[a-z0-9_]{1,64}\Z')
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
UPSTREAM_TIMEOUT = 5
ERRORS = frozenset({
    'agent_unavailable', 'provider_unavailable', 'dashboard_upgrade_required',
    'history_unconfirmed', 'history_stale', 'invalid_binding', 'history_unreadable',
    'history_changed', 'history_unavailable', 'dashboard_unavailable', 'claude_next_phase',
    'record_too_large', 'invalid_cursor', 'invalid_agent',
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


def project_event(raw, text_limit=MAX_TEXT, mail_limit=MAX_MAIL_BODY):
    """Only displayed fields. Diffs, reference IDs, cursors and paths never pass."""
    if not isinstance(raw, dict):
        return None
    kind, role = raw.get('kind'), raw.get('role')
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
    chips = [{'label': _label(r.get('label')), 'kind': 'image' if r.get('kind') == 'image' else 'file'}
             for r in refs[:8] if isinstance(r, dict) and _label(r.get('label'))]
    if chips:
        result['files'] = chips
    return result


# Smaller limits for a single message that is still too large for one page
# (for example text made of characters JSON must escape).
SHRINK_LIMITS = ((4096, 2048), (1024, 512), (256, 128))


def project_page(raw):
    """Newest displayable events within one bounded page.

    Returns (events, before, limited, omitted). When events are trimmed for
    count or size, ``before`` restarts at the oldest kept event's own cursor,
    so Show older returns the trimmed events instead of skipping them. Only if
    that cursor is unusable, or one event cannot fit at all, is ``omitted``
    true so the phone can mark the gap.
    """
    if not isinstance(raw, dict) or raw.get('ok') is not True or not isinstance(raw.get('events'), list):
        raise HistoryUnavailable('history_unavailable')
    pairs = [(e, x) for x in raw['events'][-200:] for e in (project_event(x),) if e]
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
            small = project_event(anchor, text_limit, mail_limit)
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


class HistoryClient:
    """Fixed loopback GET to cockpit; no redirects, proxy env or retries."""
    def __init__(self, port, *, clock=time.monotonic):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('history_port')
        self.port, self.clock = port, clock
        self._tokens = OrderedDict()
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
        events, older, limited, omitted = project_page(raw)
        issued = self._issue(name, older) if older is not None else None
        provider = raw.get('provider') if raw.get('provider') in {'codex', 'claude', 'dsh', 'hermes'} else ''
        result = {'ok': True, 'agent': name, 'provider': provider, 'events': events,
                  'older': issued, 'limited': limited, 'omitted': omitted,
                  'tail_only': raw.get('mode') == 'fresh_tail'}
        # The page is at most MAX_PAGE_BYTES and the envelope a few hundred
        # bytes; refuse rather than trim if that ever stops holding.
        if len(encode(result)) > MAX_WIRE_BYTES:
            raise HistoryUnavailable('record_too_large')
        return result

    def _issue(self, name, cursor):
        """One token per (agent, cursor). Polling the same latest page reuses
        and renews it, so an open sheet's Show older does not lapse and the
        table does not grow with every poll."""
        with self._lock:
            self._expire()
            expires = self.clock() + TOKEN_SECONDS
            for key, (owner, before, _) in self._tokens.items():
                if owner == name and before == cursor:
                    self._tokens[key] = (owner, before, expires)
                    self._tokens.move_to_end(key)
                    return key
            issued = secrets.token_hex(16)
            self._tokens[issued] = (name, cursor, expires)
            while len(self._tokens) > MAX_TOKENS:
                self._tokens.popitem(last=False)
            return issued

    def _expire(self):
        now = self.clock()
        for key in [k for k, v in self._tokens.items() if v[2] <= now]:
            del self._tokens[key]

    def _get(self, path):
        con = http.client.HTTPConnection('127.0.0.1', self.port, timeout=UPSTREAM_TIMEOUT)
        timer = None
        deadline = time.monotonic() + UPSTREAM_TIMEOUT
        try:
            con.connect()
            sock = con.sock
            def expire():
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
            if length is not None and (not length.isdigit() or int(length) > MAX_UPSTREAM_BYTES):
                raise HistoryUnavailable('history_unavailable')
            data = response.read(MAX_UPSTREAM_BYTES + 1)
            if len(data) > MAX_UPSTREAM_BYTES or time.monotonic() >= deadline:
                raise HistoryUnavailable('history_unavailable')
            return response.status, json.loads(data)
        except HistoryUnavailable:
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeError):
            raise HistoryUnavailable('history_unavailable') from None
        finally:
            if timer is not None:
                timer.cancel()
            con.close()

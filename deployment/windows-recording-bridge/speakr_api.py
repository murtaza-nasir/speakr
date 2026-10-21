"""Session/CSRF API client. Credentials are restricted to the configured loopback origin."""
from __future__ import annotations

import http.cookiejar
import json
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


class LocalRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, base):
        self.origin = urllib.parse.urlsplit(base)[:2]

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl)[:2] != self.origin:
            raise PermissionError('External redirect refused')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SpeakrAPI:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg['speakr_url'].rstrip('/')
        url = urllib.parse.urlsplit(self.base)
        if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('Speakr must use HTTP on loopback')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), LocalRedirect(self.base))
        self.csrf = None

    def login(self):
        env = dict(line.split('=', 1) for line in Path(self.cfg['env_file']).read_text(
            encoding='utf-8-sig').splitlines() if line and not line.startswith('#') and '=' in line)
        page = self.opener.open(self.base + '/login', timeout=30).read().decode()
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
        if not token:
            raise PermissionError('Login CSRF token unavailable')
        data = urllib.parse.urlencode({'csrf_token': token.group(1), 'email': env['ADMIN_EMAIL'],
                                       'password': env['ADMIN_PASSWORD']}).encode()
        response = self.opener.open(self.base + '/login', data, timeout=30)
        page = response.read().decode()
        if '/login' in response.url:
            raise PermissionError('Speakr login failed')
        meta = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page)
        self.csrf = meta.group(1) if meta else token.group(1)

    def request(self, path, data=None, method=None):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Expected local API path')
        if not self.csrf:
            self.login()
        headers = {'Accept': 'application/json', 'X-CSRFToken': self.csrf}
        if data is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(data).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=60) as response:
                if 'application/json' not in response.headers.get('Content-Type', ''):
                    self.csrf = None
                    raise PermissionError('Speakr API session expired')
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403):
                self.csrf = None
            raise

    def recordings(self):
        page = 1
        while True:
            data = self.request(f'/api/v1/recordings?per_page=100&page={page}')
            yield from data['recordings']
            if not data['pagination']['has_next']:
                return
            page += 1
            if page > 100000:
                raise RuntimeError('Invalid API pagination')

    def find(self, filename):
        found = [r for r in self.recordings() if r['original_filename'] == filename]
        if len(found) > 1:
            raise RuntimeError('Multiple remote records for one bridge-id')
        return found[0] if found else None

    def detail(self, rid):
        return self.request(f'/api/v1/recordings/{rid}')

    def transcript(self, rid):
        try:
            result = self.request(f'/api/v1/recordings/{rid}/transcript?format=json')
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            detail = self.detail(rid)  # a missing recording is not silence
            if detail['status'] == 'COMPLETED' and not detail.get('transcription'):
                return {'segments': [], 'raw': ''}
            raise RuntimeError('Transcript temporarily unavailable for existing recording') from None
        if not isinstance(result.get('segments', []), list):
            raise ValueError('Invalid transcript segments')
        return {'segments': result.get('segments', []), 'raw': result.get('raw', '')}

    def upload(self, path, filename, title, mtime, meeting_date=None, notes=None,
               min_speakers=None, max_speakers=None):
        if not self.csrf:
            self.login()
        boundary = 'SpeakrBridge' + uuid.uuid4().hex
        fields = {'title': title, 'file_last_modified': str(int(mtime * 1000)),
                  'language': 'ru', 'transcription_model': 'large-v3'}
        if meeting_date:
            fields['meeting_date'] = meeting_date
        if notes:
            fields['notes'] = notes
        if min_speakers is not None:
            fields['min_speakers'] = str(min_speakers)
        if max_speakers is not None:
            fields['max_speakers'] = str(max_speakers)
        # Disk-backed multipart keeps multi-hour audio out of process memory.
        with tempfile.TemporaryFile() as body:
            for name, value in fields.items():
                body.write((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                            f'\r\n\r\n{value}\r\n').encode('utf-8'))
            body.write((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                        f'filename="{filename}"\r\nContent-Type: audio/mp4\r\n\r\n').encode())
            with Path(path).open('rb') as media:
                for block in iter(lambda: media.read(1024 * 1024), b''):
                    body.write(block)
            body.write(f'\r\n--{boundary}--\r\n'.encode())
            size = body.tell()
            body.seek(0)
            req = urllib.request.Request(self.base + '/api/v1/recordings/upload', data=body,
                headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                         'Content-Length': str(size), 'X-CSRFToken': self.csrf,
                         'Accept': 'application/json'}, method='POST')
            with self.opener.open(req, timeout=600) as response:
                return json.load(response)

    def retry(self, rid, min_speakers=None, max_speakers=None):
        fields = {'language': 'ru', 'transcription_model': 'large-v3'}
        if min_speakers is not None:
            fields['min_speakers'] = min_speakers
        if max_speakers is not None:
            fields['max_speakers'] = max_speakers
        return self.request(f'/recording/{rid}/reprocess_transcription', fields, 'POST')

    def jobs(self):
        result = self.request('/api/recordings/job-queue-status')
        if not isinstance(result.get('jobs'), list):
            raise ValueError('Job queue contract unavailable')
        return result['jobs']

    def summarize(self, rid):
        return self.request(f'/api/v1/recordings/{rid}/summarize', {}, 'POST')

    def events(self, rid):
        data = self.request(f'/api/v1/recordings/{rid}/events')
        if not isinstance(data.get('events'), list):
            raise ValueError('Event preservation contract unavailable')
        return data['events']

    def summary_allowed(self):
        profile = self.request('/api/v1/users/me')
        allowed = profile.get('preferences', {}).get('auto_summarization')
        if not isinstance(allowed, bool):
            raise ValueError('Auto summary preference contract unavailable')
        if not allowed:
            return False
        # The admin flag is not exposed by REST. Account markup hides the actual
        # checkbox when globally disabled; fail closed if that contract changes.
        with self.opener.open(self.base + '/account', timeout=10) as r:
            page = r.read().decode('utf8')
            if '/login' in r.url:
                self.csrf = None
                raise PermissionError('Preference session expired')
        if re.search(r'<input\b[^>]*\bid="autoSummarizationToggle"', page):
            return True
        if 'data-i18n="account.autoSummarizationDisabled"' in page:
            return False
        raise ValueError('Admin auto summary preference contract unavailable')

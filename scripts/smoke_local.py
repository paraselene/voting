"""Exercise an empty local Docker deployment; never connects to a remote host."""
import concurrent.futures
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
BASE = 'http://localhost:18000'
COMPOSE = ['docker', 'compose', '-p', 'voting-local', '-f', 'compose.local.yaml']


def docker(*args, input=None):
    return subprocess.run(COMPOSE + list(args), input=input, text=True, cwd=ROOT, check=True, capture_output=True).stdout


def shell(source):
    return docker('exec', '-T', 'voting-web', 'python', 'manage.py', 'shell', '-c', source)


class Browser:
    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path, fields=None, csrf=True):
        if fields is not None:
            fields = list(fields)
            if csrf:
                token = next(c.value for c in self.cookies if c.name == 'csrftoken')
                fields.append(('csrfmiddlewaretoken', token))
            data = urllib.parse.urlencode(fields).encode()
        else:
            data = None
        try:
            response = self.opener.open(BASE + path, data=data, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), response.headers

    def login(self, password):
        assert self.request('/')[0] == 200
        status, body, _ = self.request('/', [('password', password.lower())])
        assert status == 200 and '登入密碼'.encode() not in body


def wait_ready():
    for _ in range(60):
        try:
            if Browser().request('/')[0] == 200:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise RuntimeError('容器未能就緒')


def main():
    wait_ready()
    seed = shell("""
import json
from election.models import Identity, Candidate
from election.services import create_identity, manage_election
assert not Identity.objects.exists(), 'Smoke test requires an empty local database.'
admin, password = create_identity('本機測試管理員', True)
users = []
for i in range(20):
    user, code = create_identity('測試用戶' + str(i + 1) if i else '陳王李張劉林黃吳周徐' * 8)
    users.append({'id': user.pk, 'password': code})
for i in range(12):
    manage_election('candidate', f'測試候選人{i + 1}')
manage_election('open')
print(json.dumps({'admin': password, 'users': users, 'candidates': list(Candidate.objects.values_list('pk', flat=True))}))
""")
    credentials = json.loads(seed.strip().splitlines()[-1])
    credential_path = ROOT / 'data/local-docker/demo-credentials.json'
    fd = os.open(credential_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(credentials, stream, ensure_ascii=False, indent=2)
    admin = Browser()
    admin.login(credentials['admin'])
    voter = Browser()
    voter.login(credentials['users'][0]['password'])
    ids = credentials['candidates']
    assert voter.request('/manage/')[0] == 403
    assert voter.request('/vote/', [('version', '1'), ('blank', 'yes')], csrf=False)[0] == 403
    status, body, _ = voter.request('/vote/', [('version', '1'), ('blank', 'yes')])
    assert status == 200 and '空白票'.encode() in body
    fields = [('version', '1')] + [('candidate', value) for value in ids[:10]]
    assert '選票已儲存'.encode() in voter.request('/vote/', fields)[1]
    assert '最多 10 位有效候選人'.encode() in voter.request('/vote/', fields + [('candidate', ids[10])])[1]
    assert '選票已儲存'.encode() in voter.request('/vote/', [('version', '1'), ('candidate', ids[1])])[1]
    admin.request('/manage/', [('action', 'close')])
    assert '投票已關閉'.encode() in voter.request('/vote/', fields)[1]
    admin.request('/manage/', [('action', 'open')])
    start = time.monotonic()
    def submit(user):
        browser = Browser()
        browser.login(user['password'])
        status, body, _ = browser.request('/vote/', [('version', '1'), ('blank', 'yes')])
        assert status == 200 and '選票已儲存'.encode() in body
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(submit, credentials['users'][1:]))
    print(f'HTTP login + vote: 19 users / 10 concurrent clients in {time.monotonic() - start:.2f}s')
    pdf_dir = ROOT / 'tmp/pdfs'
    pdf_dir.mkdir(parents=True, exist_ok=True)
    status, pdf, headers = admin.request('/credentials/', [('scope', 'all')])
    assert status == 200 and pdf.startswith(b'%PDF') and 'no-store' in headers['Cache-Control']
    (pdf_dir / 'docker-credentials.pdf').write_bytes(pdf)
    from pypdf import PdfReader
    reader = PdfReader(pdf_dir / 'docker-credentials.pdf')
    assert [page.extract_text().count('登入憑證') for page in reader.pages] == [8, 8, 4]
    print('PDF: 20 credentials, 3 pages, 8/8/4 cards')
    count_code = "from election.models import Ballot; print(Ballot.objects.count())"
    assert shell(count_code).strip().splitlines()[-1] == '20'
    docker('restart', 'voting-web')
    wait_ready()
    assert shell(count_code).strip().splitlines()[-1] == '20'
    backup = docker('exec', '-T', 'voting-web', 'python', 'manage.py', 'backup_db').strip()
    admin.request('/manage/', [('action', 'clear'), ('confirmation', '清除全部選票')])
    admin.request('/manage/', [('action', 'open')])
    assert '投票資料已重設'.encode() in voter.request('/vote/', fields)[1]
    assert shell(count_code).strip().splitlines()[-1] == '0'
    docker('stop', 'voting-web')
    docker('run', '--rm', '--no-deps', 'voting-web', 'python', 'manage.py', 'restore_db', backup, '--confirm', '還原資料庫')
    docker('up', '-d')
    wait_ready()
    assert shell(count_code).strip().splitlines()[-1] == '20'
    assert '登入密碼'.encode() in voter.request('/vote/')[1]
    admin.login(credentials['admin'])
    assert '已關閉'.encode() in admin.request('/manage/')[1]
    print('Restart persistence, clear/stale-page rejection, offline restore, session revocation: passed')
    print('Local demo is ready at http://localhost:18000; credentials: data/local-docker/demo-credentials.json')


if __name__ == '__main__':
    main()

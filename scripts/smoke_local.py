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
BASE = 'http://localhost:' + os.environ.get('VOTING_LOCAL_PORT', '18000')
COMPOSE = ['docker', 'compose', '-p', os.environ.get('VOTING_LOCAL_PROJECT', 'voting-local'), '-f', 'compose.local.yaml']


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
from election.models import Identity, Candidate, Election
from election.services import create_identity, manage_election, fresh_setup, password_for
from django.conf import settings
assert not Identity.objects.exists(), 'Smoke test requires an empty local database.'
admin, password = create_identity('本機測試管理員', True)
fresh_setup()
users = [{'id': u.pk, 'password': password_for(u)} for u in Identity.objects.filter(is_admin=False)]
round = manage_election('create', election_id=0, version=0, title=settings.ELECTION_TITLES[0], selected=list(Candidate.objects.values_list('pk',flat=True)), maximum=10)
print(json.dumps({'admin': password, 'users': users, 'candidates': list(Candidate.objects.values_list('pk', flat=True)), 'election': round.pk}))
""")
    credentials = json.loads(seed.strip().splitlines()[-1])
    credential_path = Path(os.environ.get('VOTING_LOCAL_DATA', str(ROOT / 'data/local-docker'))) / 'demo-credentials.json'
    fd = os.open(credential_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(credentials, stream, ensure_ascii=False, indent=2)
    admin = Browser()
    admin.login(credentials['admin'])
    voter = Browser()
    voter.login(credentials['users'][0]['password'])
    ids = credentials['candidates']
    election_id = credentials['election']
    version = 1
    def token():
        return [('election_id', election_id), ('version', version)]
    def lifecycle(action, extra=()):
        nonlocal version
        result = admin.request('/manage/', token() + [('action', action)] + list(extra))
        version += 1
        return result
    assert len(credentials['users']) == len(ids) == 19
    assert voter.request('/manage/')[0] == 403
    assert voter.request('/vote/', token() + [('blank', 'yes')], csrf=False)[0] == 403
    assert '空白票'.encode() in voter.request('/vote/', token() + [('blank', 'yes')])[1]
    choices = [('candidate', value) for value in ids[:10]]
    assert '選票已儲存'.encode() in voter.request('/vote/', token() + choices)[1]
    assert '最多 10 位有效候選人'.encode() in voter.request('/vote/', token() + choices + [('candidate', ids[10])])[1]
    assert '選票已儲存'.encode() in voter.request('/vote/', token() + [('candidate', ids[1])])[1]
    stale = token()
    lifecycle('close')
    assert '輪次資料已過期'.encode() in voter.request('/vote/', stale + choices)[1]
    lifecycle('open')
    start = time.monotonic()
    def submit(user):
        browser = Browser()
        browser.login(user['password'])
        status, body, _ = browser.request('/vote/', token() + [('blank', 'yes')])
        assert status == 200 and '選票已儲存'.encode() in body
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(submit, credentials['users'][1:]))
    print(f'HTTP login + vote: 18 users / 10 concurrent clients in {time.monotonic() - start:.2f}s')
    pdf_dir = ROOT / 'tmp/pdfs'
    pdf_dir.mkdir(parents=True, exist_ok=True)
    status, pdf, headers = admin.request('/credentials/', [])
    assert status == 200 and pdf.startswith(b'%PDF') and 'no-store' in headers['Cache-Control']
    (pdf_dir / 'docker-credentials.pdf').write_bytes(pdf)
    from pypdf import PdfReader
    reader = PdfReader(pdf_dir / 'docker-credentials.pdf')
    assert [page.extract_text().count('登入憑證') for page in reader.pages] == [8, 8, 3]
    assert '姓名' not in ''.join(page.extract_text() for page in reader.pages)
    print('PDF: 19 numbered credentials, 3 pages, 8/8/3 cards')
    count_code = "from election.models import Ballot; print(Ballot.objects.count())"
    assert shell(count_code).strip().splitlines()[-1] == '19'
    docker('restart', 'voting-web')
    wait_ready()
    assert shell(count_code).strip().splitlines()[-1] == '19'
    backup = docker('exec', '-T', 'voting-web', 'python', 'manage.py', 'backup_db').strip()
    stale = token()
    lifecycle('clear', [('confirmation', '清除本輪選票')])
    lifecycle('open')
    assert '輪次資料已過期'.encode() in voter.request('/vote/', stale + choices)[1]
    assert shell(count_code).strip().splitlines()[-1] == '0'
    docker('stop', 'voting-web')
    docker('run', '--rm', '--no-deps', 'voting-web', 'python', 'manage.py', 'restore_db', backup, '--confirm', '還原資料庫')
    docker('up', '-d')
    wait_ready()
    assert shell(count_code).strip().splitlines()[-1] == '19'
    assert '登入密碼'.encode() in voter.request('/vote/')[1]
    admin.login(credentials['admin'])
    assert '已關閉'.encode() in admin.request('/manage/')[1]
    # A repeated title starts a separate round with a different candidate subset.
    latest = json.loads(shell("import json; from election.models import Election; e=Election.objects.latest('pk'); print(json.dumps({'id':e.pk,'version':e.version,'title':e.title}))").strip().splitlines()[-1])
    election_id, version = latest['id'], latest['version']
    admin.request('/manage/', token() + [('action','create'),('title',latest['title']),('maximum',1),('candidate',ids[0])])
    newer = json.loads(shell("import json; from election.models import Election; e=Election.objects.latest('pk'); print(json.dumps({'id':e.pk,'version':e.version}))").strip().splitlines()[-1])
    assert newer['id'] != election_id
    election_id, version = newer['id'], newer['version']
    voter.login(credentials['users'][0]['password'])
    assert '選票已儲存'.encode() in voter.request('/vote/',token()+[('candidate',ids[0])])[1]
    assert shell(count_code).strip().splitlines()[-1] == '20'
    assert '已投票'.encode() in admin.request('/manage/?round=' + str(credentials['election']))[1]
    print('Independent repeated-title rounds, stable credentials: passed')
    print('Restart persistence, clear/stale-page rejection, offline restore, session revocation: passed')
    print(f'Local demo is ready at {BASE}; credentials stored privately in the local data directory')


if __name__ == '__main__':
    main()

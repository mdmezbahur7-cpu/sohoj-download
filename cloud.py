"""Sohoj Download — small public deployment, one Gunicorn worker only."""
import atexit
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from egress import start_proxy

ROOT = Path(__file__).resolve().parent
DATA = Path(tempfile.mkdtemp(prefix='sohoj-'))
TTL = 30 * 60
MAX_JOB = 512 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
DOMAINS = ('youtube.com','youtu.be','facebook.com','fb.watch','instagram.com','tiktok.com','twitter.com','x.com','vimeo.com','dailymotion.com','dai.ly','reddit.com','redd.it','twitch.tv','pinterest.com','pin.it','soundcloud.com','bandcamp.com','bilibili.com')
JOBS = {}
PROCESSES = {}
LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=1)
PROXY_SERVER, PROXY_URL = start_proxy()
app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get('SESSION_SECRET') or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=8192, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER_EXTERNAL_HOSTNAME')),
                  SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=timedelta(days=1))
host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
if host:
    app.config['TRUSTED_HOSTS'] = [host, 'localhost', '127.0.0.1']
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


def owner():
    if 'owner' not in session:
        session['owner'] = secrets.token_hex(32)
        session.permanent = True
    return session['owner']


def visible(job):
    return {k: v for k, v in job.items() if k not in ('owner', 'file', 'process')}


def validate_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('Enter a valid public media link.')
    parsed = urlparse(value)
    host = (parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Use a public HTTPS video or audio link.')
    social = any(host == d or host.endswith('.'+d) for d in DOMAINS)
    direct = Path(parsed.path.lower()).suffix in ('.mp4','.webm','.mov','.mkv','.mp3','.m4a','.ogg','.wav')
    if not social and not direct:
        raise ValueError('Paste a supported social-media link or a direct audio/video file link.')
    if not host or '.' not in host or host.endswith(('.local','.internal')):
        raise ValueError('Use a public media source.')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError('Private network destinations are blocked.')
    if any(ord(c) < 32 for c in value):
        raise ValueError('Invalid media URL.')
    return value


def stop_process(process):
    if process and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def update(jid, **changes):
    with LOCK:
        if jid in JOBS:
            JOBS[jid].update(**changes)


def watchdog(jid, process, done):
    start = time.monotonic()
    while not done.wait(2):
        folder = DATA / jid
        try:
            size = sum(f.stat().st_size for f in folder.rglob('*') if f.is_file())
            total = sum(f.stat().st_size for f in DATA.rglob('*') if f.is_file())
            reason = None
            if size > MAX_JOB or total > MAX_TOTAL:
                reason = 'Free-server storage limit reached. Try a smaller video or lower quality.'
            if time.monotonic() - start > 900:
                reason = 'This job exceeded the 15-minute processing limit. Try a shorter video.'
            if reason:
                update(jid, error=reason)
                stop_process(process)
                return
        except FileNotFoundError:
            pass


def run_job(jid):
    with LOCK:
        job = JOBS.get(jid)
        if not job or job['status'] == 'cancelled':
            return
        job.update(status='running', phase='Reading source')
    folder = DATA / jid
    folder.mkdir(exist_ok=True)
    args = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
            '--no-simulate', '--no-warnings', '--newline', '--no-colors',
            '--proxy', PROXY_URL, '--socket-timeout', '25', '--retries', '1',
            '--fragment-retries', '1', '--concurrent-fragments', '1',
            '--max-filesize', '500M', '--match-filter', '!is_live',
            '--restrict-filenames', '--no-mtime', '--no-overwrites', '--downloader', 'native',
            '--postprocessor-args', 'ffmpeg_i:-protocol_whitelist file,pipe,crypto',
            '--postprocessor-args', 'ffmpeg_o:-threads 1',
            '--print', 'before_dl:SOHOJ_TITLE:%(title)j',
            '--print', 'after_move:SOHOJ_FILE:%(filepath)j',
            '--progress', '--progress-template', 'download:SOHOJ_PROGRESS:%(progress._percent_str)s',
            '-o', str(folder / '%(title).100s.%(ext)s')]
    if job['mode'] == 'audio':
        args += ['-f', 'bestaudio/best', '-x', '--audio-format', 'mp3', '--audio-quality', job['quality']+'K']
    else:
        limit = '[height<=1080]' if job['quality'] == 'best' else '[height<='+job['quality']+']'
        args += ['-f', f'bv*{limit}+ba/b{limit}', '--merge-output-format', 'mp4/mkv']
    args += ['--', job['url']]
    process = None
    done = threading.Event()
    try:
        env = {**os.environ, 'http_proxy': PROXY_URL, 'https_proxy': PROXY_URL,
               'HTTP_PROXY': PROXY_URL, 'HTTPS_PROXY': PROXY_URL, 'ALL_PROXY': PROXY_URL,
               'no_proxy': '', 'NO_PROXY': ''}
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace',
                                   env=env, start_new_session=True)
        with LOCK:
            PROCESSES[jid] = process
            if job['status'] == 'cancelled':
                stop_process(process)
        threading.Thread(target=watchdog, args=(jid, process, done), daemon=True).start()
        file = None
        error = ''
        for line in process.stdout:
            line = line.strip()
            if line.startswith('SOHOJ_TITLE:'):
                update(jid, title=json.loads(line.split(':', 1)[1]), phase='Downloading')
            elif line.startswith('SOHOJ_FILE:'):
                candidate = Path(json.loads(line.split(':', 1)[1])).resolve()
                if candidate.is_relative_to(folder.resolve()):
                    file = candidate
            elif line.startswith('SOHOJ_PROGRESS:'):
                match = re.search(r'(\d+(?:\.\d+)?)%', line)
                if match:
                    update(jid, progress=min(99, float(match.group(1))))
            elif '[ExtractAudio]' in line or '[Merger]' in line:
                update(jid, phase='Converting / merging', progress=99)
            elif 'ERROR:' in line:
                error = line[-600:]
        code = process.wait()
        if job['status'] == 'cancelled':
            return
        if job.get('error'):
            raise RuntimeError(job['error'])
        if code or not file or not file.is_file():
            raise RuntimeError(error or 'The source returned no file. It may require login, block server downloads, or exceed the size limit. Try a shorter public video.')
        if file.stat().st_size > MAX_JOB:
            raise RuntimeError('The file is too large for this free server.')
        update(jid, status='completed', progress=100, phase='Ready',
               file=str(file), size=file.stat().st_size, finished=time.time())
    except Exception as error:
        if job['status'] != 'cancelled':
            update(jid, status='failed', error=str(error)[:600], finished=time.time())
    finally:
        done.set()
        stop_process(process)
        with LOCK:
            PROCESSES.pop(jid, None)
        if job['status'] != 'completed':
            shutil.rmtree(folder, ignore_errors=True)


def cleanup():
    while True:
        time.sleep(30)
        now = time.time()
        with LOCK:
            expired = [jid for jid, job in JOBS.items()
                       if job['status'] not in ('queued', 'running') and
                       now-job.get('finished', job['created']) > TTL]
            for jid in expired:
                JOBS.pop(jid, None)
                shutil.rmtree(DATA / jid, ignore_errors=True)


threading.Thread(target=cleanup, daemon=True).start()


@app.before_request
def check_request():
    if request.method == 'POST':
        expected = request.host_url.rstrip('/')
        if request.headers.get('Origin') != expected:
            return jsonify(error='Open this website directly to submit a download.'), 403
        if not request.is_json:
            return jsonify(error='JSON request required.'), 415
    if request.path.startswith('/api/'):
        owner()


@app.after_request
def response_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; style-src-attr 'unsafe-inline'; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.errorhandler(HTTPException)
def handle_http(error):
    return jsonify(error=error.description), error.code


@app.get('/')
def index():
    owner()
    return send_from_directory(ROOT / 'web', 'index.html')


@app.get('/style.css')
@app.get('/app.js')
def assets():
    return send_from_directory(ROOT / 'web', request.path.lstrip('/'))


@app.get('/api/health')
def health():
    return jsonify(ok=importlib.util.find_spec('yt_dlp') is not None,
                   ffmpeg=bool(shutil.which('ffmpeg')), hosted=True,
                   maxFileMB=512, retentionMinutes=30)


@app.get('/api/jobs')
def list_jobs():
    with LOCK:
        jobs = [visible(job) for job in JOBS.values() if job['owner'] == owner()]
    return jsonify(sorted(jobs, key=lambda j: j['created'], reverse=True))


@app.post('/api/jobs')
def submit():
    try:
        body = request.get_json()
        if not isinstance(body, dict):
            raise ValueError('Invalid request.')
        url = validate_url(body.get('url'))
        mode = body.get('mode')
        quality = body.get('quality')
        allowed = {'1080', '720', '480', 'best'} if mode == 'video' else {'128', '192', '320'}
        if mode not in ('audio', 'video') or quality not in allowed:
            raise ValueError('Choose a valid format and quality.')
        if not shutil.which('ffmpeg'):
            return jsonify(error='The server is missing FFmpeg. Please contact the site owner.'), 503
    except (ValueError, TypeError) as error:
        return jsonify(error=str(error)), 400
    with LOCK:
        active = [job for job in JOBS.values() if job['status'] in ('queued', 'running')]
        if len(active) >= 10 or sum(job['owner'] == owner() for job in active) >= 2:
            return jsonify(error='The free-server queue is busy. Wait for a download to finish.'), 429
        if len(JOBS) >= 200:
            return jsonify(error='The server is busy. Try again after older downloads expire.'), 429
        recent = [j['created'] for j in JOBS.values() if j['owner'] == owner()]
        if recent and time.time()-max(recent) < 5:
            return jsonify(error='Wait a few seconds before starting another download.'), 429
        jid = uuid.uuid4().hex
        JOBS[jid] = dict(id=jid, owner=owner(), url=url, mode=mode, quality=quality,
                         status='queued', title=url, progress=0, created=time.time())
    POOL.submit(run_job, jid)
    return jsonify(id=jid), 202


@app.post('/api/jobs/<jid>/cancel')
def cancel(jid):
    with LOCK:
        job = JOBS.get(jid)
        if not job or job['owner'] != owner():
            return jsonify(error='Download not found.'), 404
        if job['status'] in ('queued', 'running'):
            job.update(status='cancelled', finished=time.time())
            stop_process(PROCESSES.get(jid))
    return jsonify(ok=True)


@app.get('/api/file/<jid>')
def file(jid):
    with LOCK:
        job = JOBS.get(jid)
        if not job or job['owner'] != owner():
            return jsonify(error='Download not found in this browser session.'), 404
        if job['status'] != 'completed' or not Path(job.get('file', '')).is_file():
            return jsonify(error='File is not ready or has expired. Submit the link again.'), 410
        path = Path(job['file']).resolve()
        if not path.is_relative_to(DATA.resolve()):
            return jsonify(error='Invalid file.'), 404
    return send_file(path, as_attachment=True, download_name=path.name,
                     mimetype='application/octet-stream', conditional=True)


def shutdown():
    for process in list(PROCESSES.values()):
        stop_process(process)
    POOL.shutdown(wait=False, cancel_futures=True)
    PROXY_SERVER.shutdown()
    shutil.rmtree(DATA, ignore_errors=True)


atexit.register(shutdown)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '8765')), threaded=True)

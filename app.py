"""Loopback-only YouTube download queue. Run: python app.py."""
import copy
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from urllib.parse import urlparse
import uuid

from flask import Flask, abort, jsonify, render_template, request, send_file
import yt_dlp
from yt_dlp.postprocessor import PostProcessor

ROOT = Path(__file__).resolve().parent
DOWNLOADS = ROOT / 'downloads'
STATE = ROOT / 'state.json'
DOWNLOADS.mkdir(exist_ok=True)
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=8)
SETTINGS = ROOT / 'settings.json'
try:
    PARALLEL = json.loads(SETTINGS.read_text())['parallel']
    if type(PARALLEL) is not int or not 1 <= PARALLEL <= 8:
        PARALLEL = 2
except (OSError, ValueError, KeyError, TypeError):
    PARALLEL = 2
PENDING = deque()
ACTIVE = set()


def dispatch():
    """Called with LOCK held; slots include extraction and FFmpeg processing."""
    while PENDING and len(ACTIVE) < PARALLEL:
        job_id, config = PENDING.popleft()
        ACTIVE.add(job_id)
        POOL.submit(execute_job, job_id, config)


def execute_job(job_id, config):
    try:
        if config['playlist']:
            expand_playlist(job_id, config)
        else:
            run_download(job_id, config)
    finally:
        with LOCK:
            ACTIVE.discard(job_id)
            dispatch()


def new_job(config, **extra):
    job_id = uuid.uuid4().hex
    JOBS[job_id] = dict(id=job_id, url=config['url'], title='YouTube download', status='queued', message='Waiting in queue', percent=None, quality=config['quality'], format=config['format'], files=[], warnings=[], completed_items=0, created=time.time(), config=copy.deepcopy(config), **extra)
    return job_id


def expand_playlist(job_id, config):
    update(job_id, status='extracting', message='Reading playlist items')
    logger = Logger(job_id)
    try:
        opts = options(config, job_id, logger)
        opts.update(extract_flat='in_playlist', skip_download=True, ignoreerrors=True)
        with yt_dlp.YoutubeDL(opts) as downloader:
            info = downloader.extract_info(config['url'], download=False)
            if not info:
                raise RuntimeError('Could not read this playlist.')
            entries = list(info.get('entries') or [info])
        if not entries:
            raise RuntimeError('This playlist has no accessible videos.')
        with LOCK:
            for index, entry in enumerate(entries, 1):
                child_config = dict(config, playlist=False)
                child_config['url'] = f"https://www.youtube.com/watch?v={entry['id']}" if entry and entry.get('id') else config['url']
                child_id = new_job(child_config, parent_id=job_id, playlist_index=index, playlist_count=len(entries))
                JOBS[child_id]['title'] = (entry or {}).get('title') or f'Playlist item {index}'
                if entry and entry.get('id'):
                    PENDING.append((child_id, child_config))
                else:
                    JOBS[child_id].update(status='failed', message='Playlist item unavailable')
            JOBS[job_id].update(status='playlist', title=info.get('title') or 'Playlist', message='Playlist added to queue', playlist_count=len(entries))
            for error in logger.errors:
                logger.warning(error)
            save()
            dispatch()
    except Exception as exc:
        update(job_id, status='failed', message=str(exc))
        save()
JOBS = {}
BROWSERS = {'chrome', 'firefox', 'safari', 'edge', 'brave', 'chromium', 'opera', 'vivaldi'}
QUALITIES = {360, 480, 720, 1080, 1440, 2160}


def save():
    with LOCK:
        temp = STATE.with_suffix('.tmp')
        temp.write_text(json.dumps(JOBS, ensure_ascii=False, indent=2))
        temp.replace(STATE)


if STATE.exists():
    try:
        JOBS = json.loads(STATE.read_text())
        for job in JOBS.values():
            if job['status'] not in {'complete', 'failed', 'playlist'}:
                job.update(status='failed', message='Server stopped. Start a new download to retry.')
    except (ValueError, OSError):
        JOBS = {}


def update(job_id, **fields):
    with LOCK:
        JOBS[job_id].update(fields)


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('Send a download request as a JSON object.')
    url = data.get('url', '')
    if not isinstance(url, str):
        raise ValueError('Enter a YouTube link.')
    parsed = urlparse(url.strip())
    if parsed.scheme != 'https' or parsed.hostname not in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'} or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError('Use an https://youtube.com or https://youtu.be link.')
    if parsed.path not in {'/watch', '/playlist'} and not parsed.path.startswith(('/shorts/', '/live/')) and not (parsed.hostname == 'youtu.be' and parsed.path.strip('/')):
        raise ValueError('Paste a video or playlist link, rather than a channel or search page.')
    quality = data.get('quality', 1080)
    if isinstance(quality, bool) or quality not in QUALITIES:
        raise ValueError('Choose a quality from 360p to 2160p.')
    container = data.get('format', 'mp4')
    if container not in {'mp4', 'mkv', 'webm', 'mp3', 'm4a'}:
        raise ValueError('Choose a supported format.')
    browser = data.get('browser', '')
    if browser and browser not in BROWSERS:
        raise ValueError('Choose a supported browser.')
    languages = data.get('subtitle_languages', 'ur,en')
    if not isinstance(languages, str) or len(languages) > 160 or not all(code.strip() not in {'all', 'live-chat'} and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{1,19}', code.strip()) for code in languages.split(',')):
        raise ValueError('Use comma-separated subtitle language codes, such as ur,en,th.')
    return dict(subtitles=data.get('subtitles') is True, subtitle_languages=languages, thai=data.get('thai') is True, url=url.strip(), quality=quality, format=container, browser=browser, playlist=parsed.path == '/playlist' or data.get('playlist') is True)


class Logger:
    def __init__(self, job_id):
        self.job_id = job_id
        self.errors = []
    def debug(self, message):
        pass
    def warning(self, message):
        with LOCK:
            warnings = JOBS[self.job_id]['warnings']
            if message not in warnings:
                warnings.append(message)
                del warnings[:-8]
    def error(self, message):
        self.errors.append(message)


class RecordFile(PostProcessor):
    def __init__(self, downloader, job_id):
        super().__init__(downloader)
        self.job_id = job_id
    def run(self, info):
        path = Path(info['filepath']).resolve()
        if path.parent == DOWNLOADS.resolve() and path.is_file():
            with LOCK:
                files = JOBS[self.job_id]['files']
                if path.name not in files:
                    files.append(path.name)
                JOBS[self.job_id]['completed_items'] = len(files)
            save()
        return [], info


def options(config, job_id, logger):
    fmt = config['format']
    height = config['quality']
    if fmt in {'mp3', 'm4a'}:
        selector = 'bestaudio/best'
        processors = [{'key': 'FFmpegExtractAudio', 'preferredcodec': fmt, 'preferredquality': '192'}]
    else:
        # Every branch must include audio. Never fall back to a silent video stream.
        restriction = '[ext=webm]' if fmt == 'webm' else ''
        selector = f'bestvideo[height<={height}]{restriction}+bestaudio{restriction}/best[height<={height}][acodec!=none]{restriction}'
        processors = [{'key': 'FFmpegVideoRemuxer', 'preferedformat': fmt}]
    def progress(d):
        info = d.get('info_dict', {})
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            update(job_id, status='downloading', message='Downloading audio' if info.get('vcodec') == 'none' else 'Downloading media', title=info.get('title', 'YouTube video'), percent=min(100, downloaded / total * 100) if total else None, speed=d.get('speed'), eta=d.get('eta'), playlist_index=info.get('playlist_index') or JOBS[job_id].get('playlist_index'), playlist_count=info.get('playlist_count') or JOBS[job_id].get('playlist_count'))
        elif d['status'] == 'finished':
            update(job_id, status='processing', message='Media received · preparing FFmpeg', percent=None, speed=None, eta=None)
    def processing(d):
        if d['status'] == 'started':
            update(job_id, status='processing', message=f"Processing · {d.get('postprocessor', 'FFmpeg')}", percent=None, speed=None, eta=None)
    opts = dict(format=selector, outtmpl=str(DOWNLOADS / f'%(title).140B [%(id)s] {job_id[:8]}.%(ext)s'), noplaylist=not config['playlist'], ignoreerrors=config['playlist'], merge_output_format=fmt if fmt in {'mp4', 'mkv', 'webm'} else None, postprocessors=processors, progress_hooks=[progress], postprocessor_hooks=[processing], logger=logger, quiet=True, no_warnings=False, windowsfilenames=True, restrictfilenames=False, socket_timeout=30, retries=3, fragment_retries=5, skip_unavailable_fragments=False, continuedl=not config.get("overwrite", False), overwrites=config.get("overwrite", False))
    if config.get('output_template'):
        opts['outtmpl'] = str(DOWNLOADS / config['output_template'])
    if config['browser']:
        opts['cookiesfrombrowser'] = (config['browser'],)
    runtime = next((r for r in ('deno', 'node', 'bun') if shutil.which(r)), None)
    if runtime:
        opts['js_runtimes'] = {runtime: {}}
    return opts


def run_download(job_id, config):
    logger = Logger(job_id)
    update(job_id, status='extracting', message='Reading video information', percent=None)
    try:
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise RuntimeError('FFmpeg and ffprobe are required. Install FFmpeg and restart the server.')
        with yt_dlp.YoutubeDL(options(config, job_id, logger)) as downloader:
            downloader.add_post_processor(RecordFile(downloader, job_id), when='after_move')
            downloader.extract_info(config['url'], download=True)
        if logger.errors:
            raise RuntimeError('\n'.join(logger.errors[-4:]))
        if not JOBS[job_id]['files']:
            raise RuntimeError('No files were downloaded. Check the link and your account access.')
        if config.get('subtitles') or config.get('thai'):
            download_subtitles(job_id, config)
        update(job_id, status='complete', message='Ready to download', percent=100, speed=None, eta=None)
    except Exception as exc:
        update(job_id, status='failed', message=str(exc), percent=None, speed=None, eta=None)
    finally:
        save()


def download_subtitles(job_id, config):
    update(job_id, status='processing', message='Downloading subtitles · including Thai when available', percent=None)
    logger = Logger(job_id)
    languages = [code.strip() for code in config.get('subtitle_languages', 'ur,en').split(',')] if config.get('subtitles') else []
    if config.get('thai') and 'th' not in languages:
        languages.append('th')
    opts = options(config, job_id, logger)
    opts.update(skip_download=True, writesubtitles=True, writeautomaticsub=True,
                subtitleslangs=languages, subtitlesformat='vtt/best',
                postprocessors=[], progress_hooks=[], postprocessor_hooks=[], ignoreerrors=False)
    try:
        with yt_dlp.YoutubeDL(opts) as downloader:
            info = downloader.extract_info(config['url'], download=True)
        requested = (info or {}).get('requested_subtitles') or {}
        saved = []
        for language, subtitle in requested.items():
            path = Path(subtitle.get('filepath', '')).resolve()
            if path.parent == DOWNLOADS.resolve() and path.is_file() and not path.is_symlink():
                with LOCK:
                    if path.name not in JOBS[job_id]['files']:
                        JOBS[job_id]['files'].append(path.name)
                saved.append(language)
        missing = [language for language in languages if language not in saved]
        message = f"Saved subtitles: {', '.join(saved)}" if saved else 'No requested subtitles were downloaded.'
        if missing:
            message += f" Unavailable: {', '.join(missing)}. Thai requires a Thai track or a YouTube auto-translation."
        update(job_id, subtitle_status=message)
    except Exception as exc:
        update(job_id, subtitle_status=f'Subtitles failed; video is saved. {exc}')
    save()


@app.post('/api/downloads/<job_id>/retry')
def retry_download(job_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='Invalid retry options.'), 400
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        if job['status'] not in {'complete', 'failed'} or job_id in ACTIVE:
            return jsonify(error='This download is already active or is a playlist summary.'), 409
        if urlparse(job['url']).path == '/playlist':
            return jsonify(error='Retry individual playlist videos.'), 400
        base = job.get('config') or dict(url=job['url'], quality=job['quality'], format=job['format'], browser='', playlist=False)
        try:
            config = validate(dict(base, **{key: data[key] for key in ('browser', 'subtitles', 'subtitle_languages', 'thai') if key in data}))
        except (ValueError, TypeError):
            return jsonify(error='Invalid browser or subtitle language settings.'), 400
        config['overwrite'] = True
        # Reuse the original filename even if YouTube changes the title.
        media = next((name for name in job['files'] if Path(name).suffix in {'.mp4', '.mkv', '.webm', '.mp3', '.m4a'}), None)
        if base.get('output_template'):
            config['output_template'] = base['output_template']
        if media:
            config['output_template'] = Path(media).stem.replace('%', '%%') + '.%(ext)s'
        job.update(status='queued', message='Waiting to retry · overwrite enabled', percent=None, speed=None, eta=None, warnings=[], files=[], completed_items=0, subtitle_status=None, config=config)
        save()
        PENDING.append((job_id, config))
        dispatch()
    return jsonify(id=job_id), 202


@app.before_request
def local_only():
    if request.host.split(':')[0] not in {'127.0.0.1', 'localhost'}:
        abort(403)
    if request.method == 'POST' and request.headers.get('X-App-Token') != TOKEN:
        abort(403)


@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/')
def index():
    return render_template('index.html', token=TOKEN)


@app.get('/api/status')
def status():
    with LOCK:
        jobs = copy.deepcopy(list(JOBS.values()))
        active_count = len(ACTIVE)
        queued_count = len(PENDING)
        parallel = PARALLEL
    for job in jobs:
        if job['status'] == 'playlist':
            children = [child for child in jobs if child.get('parent_id') == job['id']]
            complete = sum(child['status'] == 'complete' for child in children)
            failed = sum(child['status'] == 'failed' for child in children)
            job['message'] = f'{complete} completed · {failed} failed · {len(children) - complete - failed} remaining'
            if complete + failed == len(children):
                job['status'] = 'failed' if failed else 'complete'
            job['completed_items'] = complete
    files = []
    for job in jobs:
        for name in job['files'] if job['id'] not in ACTIVE else []:
            path = DOWNLOADS / name
            if path.is_file() and not path.is_symlink():
                files.append(dict(name=name, size=path.stat().st_size, job_id=job['id']))
    return jsonify(jobs=list(reversed(jobs)), files=list(reversed(files)), ffmpeg=bool(shutil.which('ffmpeg') and shutil.which('ffprobe')), runtime=next((r for r in ('deno', 'node', 'bun') if shutil.which(r)), None), folder=str(DOWNLOADS), parallel=parallel, active_count=active_count, queued_count=queued_count)


@app.post('/api/settings')
def change_settings():
    global PARALLEL
    data = request.get_json(silent=True)
    value = data.get('parallel') if isinstance(data, dict) else None
    if type(value) is not int or not 1 <= value <= 8:
        return jsonify(error='Choose between 1 and 8 active downloads.'), 400
    with LOCK:
        temp = SETTINGS.with_suffix('.tmp')
        temp.write_text(json.dumps({'parallel': value}))
        temp.replace(SETTINGS)
        PARALLEL = value
        dispatch()
    return jsonify(parallel=value)


@app.post('/api/downloads')
def create_download():
    try:
        config = validate(request.get_json(silent=True))
    except (ValueError, TypeError):
        return jsonify(error='Invalid request. Use a YouTube video or playlist HTTPS link and supported settings.'), 400
    with LOCK:
        if sum(j['status'] not in {'complete', 'failed', 'playlist'} and not j.get('parent_id') for j in JOBS.values()) >= 25:
            return jsonify(error='The queue is full. Wait for downloads to finish.'), 429
        job_id = new_job(config)
        save()
        PENDING.append((job_id, config))
        dispatch()
    return jsonify(id=job_id), 202


@app.get('/files/<job_id>/<path:name>')
def download_file(job_id, name):
    with LOCK:
        job = JOBS.get(job_id)
        if not job or name not in job['files'] or job_id in ACTIVE:
            abort(404)
    path = (DOWNLOADS / name).resolve()
    if path.parent != DOWNLOADS.resolve() or not path.is_file() or (DOWNLOADS / name).is_symlink():
        abort(404)
    return send_file(path, as_attachment=True, download_name=name, conditional=True)


if __name__ == '__main__':
    from waitress import serve
    port = int(os.environ.get('PORT', '8080'))
    print(f'Frame is running at http://127.0.0.1:{port}', flush=True)
    serve(app, host='127.0.0.1', port=port, threads=8)

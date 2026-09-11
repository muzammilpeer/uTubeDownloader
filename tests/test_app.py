import pytest
import app as server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DOWNLOADS', tmp_path)
    monkeypatch.setattr(server, 'STATE', tmp_path / 'state.json')
    monkeypatch.setattr(server, 'JOBS', {})
    monkeypatch.setattr(server, 'SETTINGS', tmp_path / 'settings.json')
    monkeypatch.setattr(server, 'PENDING', server.deque())
    monkeypatch.setattr(server, 'ACTIVE', set())
    monkeypatch.setattr(server, 'PARALLEL', 2)
    return server.app.test_client()


@pytest.mark.parametrize('url', ['http://youtube.com/watch?v=x', 'https://evil.com/watch?v=x', 'https://youtube.com.evil.com/watch?v=x', 'https://youtube.com:999/watch?v=x', 'https://youtube.com/@channel'])
def test_reject_links(client, url):
    result = client.post('/api/downloads', json={'url':url}, headers={'X-App-Token':server.TOKEN})
    assert result.status_code == 400


def test_local_protection(client):
    assert client.get('/', headers={'Host':'evil.com'}).status_code == 403
    assert client.post('/api/downloads', json={}).status_code == 403
    assert client.get('/').status_code == 200


def test_queue(client, monkeypatch):
    queued = []
    monkeypatch.setattr(server.POOL, 'submit', lambda *args: queued.append(args))
    result = client.post('/api/downloads', json={'url':'https://youtu.be/abc','quality':2160}, headers={'X-App-Token':server.TOKEN})
    assert result.status_code == 202
    assert len(queued) == 1
    assert client.get('/api/status').json['jobs'][0]['status'] == 'queued'


def test_file_access(client):
    (server.DOWNLOADS/'ready.mp4').write_bytes(b'video')
    (server.DOWNLOADS/'partial.mp4').write_bytes(b'partial')
    server.JOBS['job'] = {'files':['ready.mp4']}
    assert client.get('/files/job/ready.mp4').data == b'video'
    assert client.get('/files/job/partial.mp4').status_code == 404
    assert client.get('/files/job/../app.py').status_code == 404


@pytest.mark.parametrize('fmt', ['mp4','mkv','webm'])
def test_selector_requires_audio(fmt):
    config = server.validate({'url':'https://youtu.be/abc','format':fmt,'quality':2160})
    opts = server.options(config, 'job', server.Logger('job'))
    separate, combined = opts['format'].split('/')
    assert '+bestaudio' in separate
    assert '[acodec!=none]' in combined
    assert '[height<=2160]' in separate and '[height<=2160]' in combined
    with server.yt_dlp.YoutubeDL(opts):
        pass


def test_failed_download(client, monkeypatch):
    server.JOBS['job'] = {'files':[], 'warnings':[]}
    monkeypatch.setattr(server.shutil, 'which', lambda _: None)
    server.run_download('job', server.validate({'url':'https://youtu.be/abc'}))
    assert server.JOBS['job']['status'] == 'failed'
    assert 'FFmpeg' in server.JOBS['job']['message']


def test_progress_and_processing(client):
    server.JOBS['job'] = {'files':[], 'warnings':[]}
    opts = server.options(server.validate({'url':'https://youtu.be/abc'}), 'job', server.Logger('job'))
    opts['progress_hooks'][0]({'status':'downloading','downloaded_bytes':50,'total_bytes':100,'info_dict':{'title':'Test'}})
    assert server.JOBS['job']['percent'] == 50
    opts['postprocessor_hooks'][0]({'status':'started','postprocessor':'Merger'})
    assert server.JOBS['job']['status'] == 'processing'
    assert server.JOBS['job']['percent'] is None


def test_real_ffmpeg_merge_and_record(client):
    import json
    import shutil
    import subprocess
    from yt_dlp.postprocessor.ffmpeg import FFmpegMergerPP
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg not installed')
    video = server.DOWNLOADS / 'video.mp4'
    audio = server.DOWNLOADS / 'audio.m4a'
    output = server.DOWNLOADS / 'merged.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=black:s=320x180:d=0.3','-an','-c:v','libx264',str(video)],check=True)
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=0.3','-vn','-c:a','aac',str(audio)],check=True)
    server.JOBS['job'] = {'files':[], 'warnings':[]}
    with server.yt_dlp.YoutubeDL({'quiet':True}) as ydl:
        info = {'filepath':str(output),'ext':'mp4','requested_formats':[{'vcodec':'h264','acodec':'none'},{'vcodec':'none','acodec':'aac','protocol':'https'}],'__files_to_merge':[str(video),str(audio)]}
        FFmpegMergerPP(ydl).run(info)
        server.RecordFile(ydl, 'job').run(info)
    result = subprocess.check_output(['ffprobe','-v','quiet','-show_streams','-of','json',str(output)])
    assert {s['codec_type'] for s in json.loads(result)['streams']} == {'video','audio'}
    assert server.JOBS['job']['files'] == ['merged.mp4']
    assert client.get('/files/job/merged.mp4').status_code == 200


@pytest.mark.parametrize('value', [0,9,True,2.5,'3',None])
def test_invalid_parallel_limit(client, value):
    assert client.post('/api/settings', json={'parallel':value}, headers={'X-App-Token':server.TOKEN}).status_code == 400
    assert server.PARALLEL == 2


def test_shared_limit_and_resize(client, monkeypatch):
    calls = []
    monkeypatch.setattr(server.POOL, 'submit', lambda *args: calls.append(args))
    config = server.validate({'url':'https://youtu.be/abc'})
    with server.LOCK:
        for i in range(6):
            job_id = server.new_job(config)
            server.PENDING.append((job_id,config))
        server.dispatch()
    assert len(calls) == len(server.ACTIVE) == 2
    assert len(server.PENDING) == 4
    assert client.post('/api/settings', json={'parallel':4}, headers={'X-App-Token':server.TOKEN}).status_code == 200
    assert len(calls) == len(server.ACTIVE) == 4
    client.post('/api/settings', json={'parallel':1}, headers={'X-App-Token':server.TOKEN})
    monkeypatch.setattr(server, 'run_download', lambda *args: None)
    for args in calls[:3]:
        server.execute_job(*args[1:])
    assert len(calls) == 4  # No replacement until below the lower limit.
    server.execute_job(*calls[3][1:])
    assert len(calls) == 5
    assert len(server.ACTIVE) == 1
    assert server.SETTINGS.read_text() == '{"parallel": 1}'


def test_playlist_expansion_and_partial_failure(client, monkeypatch):
    config = server.validate({'url':'https://youtube.com/playlist?list=test','playlist':True})
    parent = server.new_job(config)
    calls = []
    monkeypatch.setattr(server.POOL, 'submit', lambda *args: calls.append(args))
    class Downloader:
        def __init__(self, opts):
            assert opts['extract_flat'] == 'in_playlist'
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download):
            assert download is False
            return {'title':'Test playlist','entries':[{'id':'abc','title':'First'},None,{'id':'def','title':'Third'}]}
    monkeypatch.setattr(server.yt_dlp,'YoutubeDL',Downloader)
    server.expand_playlist(parent, config)
    assert len(calls) == 2
    assert all(args[2]['playlist'] is False for args in calls)
    assert {args[2]['url'] for args in calls} == {'https://www.youtube.com/watch?v=abc','https://www.youtube.com/watch?v=def'}
    for args in calls:
        server.JOBS[args[1]]['status'] = 'complete'
    result = client.get('/api/status').json
    summary = next(j for j in result['jobs'] if j['id'] == parent)
    assert summary['status'] == 'failed'
    assert summary['message'] == '2 completed · 1 failed · 0 remaining'


def test_workers_actually_overlap(client, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    barrier = threading.Barrier(2)
    completed = []
    def work(job_id, config):
        barrier.wait(timeout=3)
        completed.append(job_id)
    monkeypatch.setattr(server,'run_download',work)
    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(server,'POOL',pool)
        with server.LOCK:
            for i in range(2):
                config = server.validate({'url':'https://youtu.be/abc'})
                job_id = server.new_job(config)
                server.PENDING.append((job_id,config))
            server.dispatch()
    assert len(completed) == 2
    assert not server.ACTIVE


def test_direct_playlist_automatically_expands(client, monkeypatch):
    calls = []
    monkeypatch.setattr(server.POOL, 'submit', lambda *args: calls.append(args))
    result = client.post('/api/downloads', json={'url':'https://www.youtube.com/playlist?list=test','playlist':False}, headers={'X-App-Token':server.TOKEN})
    assert result.status_code == 202
    assert calls[0][2]['playlist'] is True
    assert server.validate({'url':'https://youtube.com/watch?v=abc&list=test','playlist':False})['playlist'] is False


def test_retry_overwrites_same_job_and_file(client, monkeypatch):
    calls = []
    monkeypatch.setattr(server.POOL,'submit',lambda *args:calls.append(args))
    config = server.validate({'url':'https://youtu.be/abc'})
    job_id = server.new_job(config)
    server.JOBS[job_id].update(status='failed',files=['Original 100%.mp4'])
    result = client.post(f'/api/downloads/{job_id}/retry',json={'thai':True},headers={'X-App-Token':server.TOKEN})
    assert result.status_code == 202
    assert len(server.JOBS) == 1
    opts = server.options(calls[0][2],job_id,server.Logger(job_id))
    assert opts['overwrites'] is True and opts['continuedl'] is False
    assert opts['skip_unavailable_fragments'] is False
    assert opts['outtmpl'].endswith('Original 100%%.%(ext)s')
    assert server.JOBS[job_id]['files'] == []
    assert client.post(f'/api/downloads/{job_id}/retry',json={},headers={'X-App-Token':server.TOKEN}).status_code == 409


def test_subtitle_recording_and_thai(client, monkeypatch):
    config = server.validate({'url':'https://youtu.be/abc','subtitles':True,'subtitle_languages':'ur','thai':True})
    job_id = server.new_job(config)
    subtitle = server.DOWNLOADS / 'example.th.vtt'
    subtitle.write_text('WEBVTT\n')
    class Downloader:
        def __init__(self, opts):
            assert opts['subtitleslangs'] == ['ur','th']
            assert opts['writeautomaticsub'] and opts['writesubtitles'] and opts['skip_download']
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def extract_info(self,*args,**kwargs):
            return {'requested_subtitles':{'th':{'filepath':str(subtitle)}}}
    monkeypatch.setattr(server.yt_dlp,'YoutubeDL',Downloader)
    server.download_subtitles(job_id,config)
    assert server.JOBS[job_id]['files'] == ['example.th.vtt']
    assert 'Unavailable: ur' in server.JOBS[job_id]['subtitle_status']
    assert client.get(f'/files/{job_id}/example.th.vtt').status_code == 200


def test_subtitle_failure_keeps_video(client, monkeypatch):
    config = server.validate({'url':'https://youtu.be/abc','thai':True})
    job_id = server.new_job(config)
    server.JOBS[job_id]['files'] = ['video.mp4']
    monkeypatch.setattr(server.yt_dlp,'YoutubeDL',lambda *args: (_ for _ in ()).throw(RuntimeError('Subtitle server unavailable')))
    server.download_subtitles(job_id,config)
    assert server.JOBS[job_id]['files'] == ['video.mp4']
    assert 'Subtitles failed; video is saved.' in server.JOBS[job_id]['subtitle_status']


@pytest.mark.parametrize('language',['.*','all','../../file','en,','', ['th']])
def test_invalid_subtitle_language(language):
    with pytest.raises(ValueError):
        server.validate({'url':'https://youtu.be/abc','subtitle_languages':language})

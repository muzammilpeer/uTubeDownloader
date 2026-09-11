# Frame — local YouTube downloader

A Python website for downloading videos and playlists into `downloads/`. Runs only on your Mac at **http://127.0.0.1:8080**. No cloud service or Docker required.

## Run on macOS

Python 3.11+, FFmpeg (including ffprobe), and Node 22+ or a current Deno installation are required. This Mac already has Python, FFmpeg, and Node installed.

```sh
cd /Users/muzammilpeer/Git/uTubeDownloader
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

After setup you can double-click `start.command` in Finder. Leave the Terminal window open while downloading; press Control-C to stop. If port 8080 is occupied, run `PORT=8081 .venv/bin/python app.py` and open that port instead. Do not run two instances against the same folder.

## Use

1. Paste a YouTube video, Shorts, live recording, or playlist HTTPS link.
2. Select maximum quality: 360p, 480p, 720p, 1080p, 1440p, or 2160p (4K).
3. Choose MP4, MKV, WebM, MP3, or M4A.
4. Enable the whole-playlist option if wanted, then start the download.
5. Follow queue, extraction, download, processing, and completion states.
6. Finished files appear under **Your files**, with browser download buttons. All originals stay together in `downloads/`.

Download percentages describe the current stream, not an entire playlist. HD downloads usually fetch video and audio separately. FFmpeg then merges them without re-encoding. During merging/conversion the progress bar is indeterminate because yt-dlp reports stages, not an accurate FFmpeg percentage. MP3/M4A conversion uses FFmpeg too.

Quality is a ceiling, not an upscale: a 720p source cannot become true 4K. WebM requires available WebM streams. MP4 preserves the best available codecs; some older players may not support modern AV1/VP9 video. Try VLC if the file does not play in your usual player.

Choose **Active downloads** above the queue to run 1–8 videos at once (default: 2). This saved limit is shared by individual videos and all playlists. Each playlist is expanded into separate video jobs, with individual progress and a playlist summary. A slot covers video extraction, downloading, and FFmpeg merging/conversion. Raising the limit starts waiting jobs immediately; lowering it allows active jobs to finish before more begin. Playlist discovery briefly occupies a slot too. Completed file history persists in `state.json`. Interrupted jobs become failed on restart; submit the link again to retry. Playlist failures keep successfully completed files and show the error. Partial temporary files may remain in `downloads/` after failures; they are not exposed by the website. A new submission starts a separate job rather than resuming an old job.

## Private videos and playlists

Sign into YouTube in a browser on this Mac. Expand **Playlist & account options** and choose that browser. yt-dlp reads its default profile cookies locally for that job. macOS may request keychain access; some browsers require closing first. Only content accessible to that account can be downloaded. Browser authentication and YouTube restrictions can still cause failures. This is cookie-based access, not a Google OAuth login flow. No passwords or cookies are stored in job history.

See the [official yt-dlp cookie guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies) and [JavaScript runtime requirements](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

## Maintenance and tests

YouTube changes regularly. To update the downloader:

```sh
.venv/bin/python -m pip install --upgrade 'yt-dlp[default]'
```

Restart the server after updates. `requirements-lock.txt` records the verified environment. To run the tests:

```sh
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```

The website binds to loopback, rejects unexpected Host headers, and requires a page token for queue submissions. Keep it local. Browser downloads create another copy wherever the browser normally saves files. Only download content you have permission to save.

Docker deployment is deferred; the current deployment uses native macOS tools.

## Retry and subtitles

Use **Retry & overwrite** on a failed or completed video. It reuses the job and original media filename, restarts the transfer rather than resuming partial data, and uses the browser and subtitle choices currently shown in the form. Other episodes are unaffected. Missing fragments now fail the download instead of silently producing incomplete media.

Subtitle defaults in the form are Urdu (`ur`), English (`en`), and Thai (`th`). Enter other YouTube language codes as a comma-separated list. Manual captions are preferred; automatic captions and YouTube-provided translations are accepted. Subtitles are separate VTT files in the same download folder, listed with download links. Thai translation is only available when YouTube exposes it; this app does not independently translate or transcribe audio. Subtitle failure is reported separately and preserves a successful video.

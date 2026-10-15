# Sohoj Download — hosted edition

A small Flask/Gunicorn web application for public, authorized audio and video downloads. The browser UI works on phones and computers without local Python installation.

## Deploy to Render (Free web service)

1. Push this repository to GitHub, including Dockerfile, requirements.lock.txt, cloud.py, egress.py and web/.
2. Connect the repository to a **Web Service**, runtime **Docker**, plan **Free**, region **Singapore**.
3. Use Dockerfile `./Dockerfile` and Docker context `.`. No custom build or start command is needed.
4. Render supplies PORT and RENDER_EXTERNAL_HOSTNAME. The included command binds to 0.0.0.0:$PORT.
5. Health endpoint: `/api/health`. Check `ok` and `ffmpeg` are true after deployment.
6. Test a short public video you own or are authorized to download, then test MP3 conversion. A successful deploy does not guarantee a platform allows cloud downloads.

No payment method or paid plan should be selected without the owner's approval. If Render requires billing or rejects free service creation, stop rather than changing to a paid plan.

## Free-server behavior

- One processing job at a time; up to ten shared queued/running jobs and two per browser session.
- Files expire 30 minutes after completion. Restarting or sleeping may lose files and session state earlier. Save files promptly.
- Job directory budget approximately 512 MB, total temporary media budget 1 GB, processing timeout 15 minutes. A watchdog checks storage every 2 seconds, so the budget can briefly overshoot. FFmpeg temporary/merged files count toward the budget.
- Up to 1080p video, MP4 when possible, MKV fallback for incompatible streams. MP3 output supports 128/192/320 kbps. The source must offer the requested quality.
- No paid subscription or daily application quota. Host bandwidth/compute policies and source platform rate limits apply. Free services may sleep after inactivity and take time to restart.
- Public input platforms: YouTube, Facebook, Instagram, TikTok, X/Twitter, Vimeo, Dailymotion, Reddit, Twitch, Pinterest, SoundCloud, Bandcamp, Bilibili and listed short-link domains.
- Not every URL works. Platforms may block data-center IPs, require authentication or change their interfaces. No DRM/paywall/private-account bypass, cookies, playlists, livestreams or account credential collection.

## Isolation and request handling

Signed, HttpOnly, SameSite=Strict browser sessions isolate job listings, cancellation and file access. Secure cookies are enabled on Render. No login/database is required for this small temporary-file demo. Browser history belongs to the browser, not an authenticated personal account.

POST endpoints require JSON and a matching Origin. Public input domains are allowlisted. yt-dlp uses a loopback HTTP proxy which rejects private/reserved destinations and pins resolved numeric addresses for each HTTP connection or HTTPS tunnel, covering redirects as well. Media uses the native downloader; FFmpeg input protocols are limited to local file/pipe/crypto during postprocessing. Container runs as a non-root user.

Run exactly **one** Gunicorn worker: jobs/queue/session fallback secret are held in memory. A runtime SESSION_SECRET can be set in Render, but is not required because jobs are discarded on restart. Never put secrets in repository files. Horizontal scaling and durable accounts require a shared database and object storage; those are deliberately outside this free single-instance setup.

## Development and verification

Python 3.12 recommended. Install `pip install -r requirements.lock.txt`; install FFmpeg and Deno on the host. `python cloud.py` starts a local development server on port 8765. Production uses the Docker command/Gunicorn, not the Flask development server.

Run `python -m unittest -v test_cloud.py` and `node --check web/app.js`.
Tests cover source validation, DNS/network blocking, DNS pinning, browser isolation, CSRF/request size checks, queue/cancellation, download result handling, option parsing against installed yt-dlp, and failed-job cleanup. Extraction subprocesses are mocked in unit tests. An actual end-to-end platform download must be tested on the deployed host.

The Dockerfile bundles FFmpeg and Deno. Dependency versions in requirements.lock.txt match the tested environment. Update the lock intentionally when platform extractor fixes are needed, then redeploy.

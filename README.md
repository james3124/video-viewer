# Viewer — play any video link

A tiny web player: paste a video link (direct file, HLS `.m3u8`, or any of the
~1000 sites yt-dlp knows) and it plays. Works browser-only for direct files and
embeds; with the Python backend it also resolves YouTube, Vimeo, TikTok and more.

## Layout

| File | Purpose |
| --- | --- |
| `index.html`, `style.css`, `app.js`, `ytdlp.js`, `config.js` | static frontend |
| `server.py` | local backend: yt-dlp + optional ffmpeg merging/streaming |
| `bin/yt-dlp` | vendored yt-dlp binary (used when `yt-dlp` isn't on PATH) |
| `api/*.py` | Vercel serverless wrappers around `server.py`'s handler |
| `vercel.json` | Vercel config |

## Run locally

```bash
python3 server.py                 # http://localhost:8080/
python3 server.py --port 8090     # custom port
python3 server.py --cookies cookies.txt   # age/geo-gated sites
python3 server.py --install       # (re)download the yt-dlp binary
```

Open **http://localhost:8080/** (served by the same process, so no CORS issues).

## Deploying

### Static frontend on Vercel

Import the repo at <https://vercel.com/new>. Framework preset **Other**, leave the
build/output settings empty. `/api/*` is handled by the Python functions in `api/`.

> **Heads-up:** many video sites (TikTok, Instagram, …) block datacenter IPs such
> as Vercel's, and the 4.5 MB function payload limit can also break long proxied
> streams. YouTube and CORS-friendly HLS usually work; other sites may not.
> For full yt-dlp power, run the backend somewhere with a normal IP (see below).

### Full backend elsewhere (recommended for non-YouTube sites)

Any host that runs a long-lived process works — Render, Railway, Fly.io, a VPS:

```bash
python3 server.py --host 0.0.0.0 --port $PORT
```

Then point the Vercel frontend at it by editing `config.js`:

```js
window.VIEWER_BACKEND = 'https://your-backend.example.com';
```

(Or visit once with `?backend=https://your-backend.example.com`.) The backend
already sends `Access-Control-Allow-Origin: *`, so cross-origin calls work.

## Notes

- `server.py` binds `127.0.0.1` by default. Only expose `0.0.0.0` when you want
  other machines to reach it — whoever can reach it can make yt-dlp fetch URLs.
- ffmpeg is optional at runtime: if missing, `server.py` falls back to a vendored
  ffmpeg from the `imageio-ffmpeg` package (installed via `requirements.txt`).

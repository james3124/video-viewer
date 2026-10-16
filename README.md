# Viewer — play any video link

A tiny web player: paste a video link (direct file, HLS `.m3u8`, or any of the
~1000 sites yt-dlp knows) and it plays. Works browser-only for direct files and
embeds; with the Python backend it also resolves YouTube, Vimeo, TikTok, Reddit,
playlists, channels — and the sites that only hand their streams to a real
browser session (age-gated / 18+ hosts, Cloudflare-protected CDNs).

## Layout

| File | Purpose |
| --- | --- |
| `index.html`, `style.css`, `app.js`, `ytdlp.js`, `config.js` | static frontend |
| `server.py` | the backend: yt-dlp + proxy + HLS + cache + WSGI app |
| `api/*.py` | Vercel serverless wrappers (export both `app` and `handler`) |
| `tools/backend_check.py` | self-test for the backend (no internet needed) |
| `tools/wsgi_check.py` | self-test for the WSGI/serverless entry point |
| `vercel.json`, `render.yaml` | deployment config |
| `bin/yt-dlp` | optional vendored yt-dlp (git-ignored; pip installs it) |

## Run locally

```bash
python3 server.py                 # http://localhost:8080/  (PORT is honoured)
python3 server.py --port 8090     # custom port
python3 server.py --cookies cookies.txt   # age/geo-gated sites
python3 server.py --token secret  # require a token on every /api call
python3 server.py --install       # (re)download the standalone yt-dlp binary
```

Open **http://localhost:8080/** — served by the same process, so no CORS issues.

Check it after changing anything:

```bash
python3 tools/backend_check.py    # 29 checks: proxy, HLS, cache, token, limits
python3 tools/wsgi_check.py      # the same handler through WSGI
```

## How it makes "hard" sites play

A `<video>` element and hls.js can only send `Origin`/`User-Agent` to a
cross-origin host. Most of the sites that fail in a plain browser fail because
their CDN demands a `Referer`, a session `Cookie` or an exact UA:

1. `/api/info` reports each format with `needsProxy` — true as soon as the host
   wants more than a plain UA.
2. `/api/play?...&proxy=1` re-fetches the bytes with those headers (`Range` and
   seeking included). `/api/hls` does the same for adaptive streams and
   rewrites every segment, `EXT-X-KEY` and `EXT-X-MAP` URI to point back here,
   master playlist → media playlist → segments all the way down.
3. Signed CDN URLs expire. When the CDN answers `403`, the backend re-extracts a
   fresh URL with yt-dlp and retries — once, silently.
4. If the host answers with a web page instead of media, you get a sentence
   explaining why (expired, needs cookies, region-locked) instead of a video
   element that silently fails.

Extra bits that help on hostile networks:

- `--impersonate chrome` (automatic when `curl_cffi` is installed) gets past
  Cloudflare/DataDome checks that block datacenter IPs.
- `--age-limit 18` handles age-gated YouTube; `--cookies cookies.txt` unlocks
  members-only and region-locked hosts.
- Retries, socket timeouts and a shared yt-dlp cache directory are always on.
- Playlists, channels and profile pages are detected and listed in the UI.

## Caching

| Layer | Where | Default |
| --- | --- | --- |
| yt-dlp info | memory LRU | `VIEWER_MAX_CACHE` 256 entries, `VIEWER_TTL` 900 s |
| yt-dlp info | disk JSON in `VIEWER_CACHE_DIR` | same TTL — survives restarts, warms up cold serverless containers |
| failures | memory | `VIEWER_NEG_TTL` 45 s, so a dead link is not hammered |
| HTTP | `ETag` + `Cache-Control: public, max-age=…` | browser/CDN skip the round trip entirely (`VIEWER_HTTP_CACHE=0` to disable) |
| static files | `ETag` + `Last-Modified` | revalidate → instant 304s |
| HLS manifests | `Cache-Control: public, max-age=15` | `VIEWER_MANIFEST_TTL` |
| captions | converted `.vtt` on disk | `VIEWER_SUB_TTL` 6 h |

Concurrent requests for the same link share a single yt-dlp run (single-flight),
and only `VIEWER_YTDLP_CONCURRENCY` (4) extractions may run at once — a busy
instance answers `429` instead of forking itself to death.
`GET /api/health` reports hit/miss/refresh counters and the cache directory.

## Configuration (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `VIEWER_TOKEN` | *(empty)* | shared secret; `?token=` or `X-Viewer-Token` on every `/api/*` except `/api/health` |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | bind address (Render sets both for you) |
| `VIEWER_CACHE_DIR` | `$TMPDIR/viewer-cache` | cache root (must be writable) |
| `VIEWER_TTL` | `900` | how long an extraction stays valid |
| `VIEWER_NEG_TTL` | `45` | how long a failure is remembered |
| `VIEWER_MAX_CACHE` | `256` | in-memory entries |
| `VIEWER_DISK_CACHE` | `1` | on-disk cache on/off |
| `VIEWER_YTDLP_CONCURRENCY` | `4` | parallel extractions |
| `VIEWER_RATE` | `300` | extraction calls per minute per IP (0 = off) |
| `VIEWER_BLOCK_PRIVATE` | `0` | SSRF guard: refuse loopback/private targets |
| `VIEWER_PROXY_TIMEOUT` | `60` | upstream timeout for proxied bytes |
| `VIEWER_MANIFEST_TTL` | `15` | `Cache-Control` for rewritten playlists |
| `VIEWER_HTTP_CACHE` | `1` | `ETag`/`Cache-Control` on API responses |
| `VIEWER_PLAYLIST_PROBE` | `1` | also look for playlist/channel entries |
| `VIEWER_AGE_LIMIT` | `18` | yt-dlp `--age-limit` |
| `YTDLP_COOKIES` | *(empty)* | path to `cookies.txt` |
| `YTDLP_IMPERSONATE` | `chrome` if `curl_cffi` is present | yt-dlp `--impersonate` |
| `YTDLP_ARGS` | *(empty)* | extra raw yt-dlp flags |
| `YTDLP_IPV4` | `0` | `--force-ipv4` (some hosts break on IPv6) |

`ffmpeg` is optional at runtime: without it, video+audio merging is off and the
other formats still play. If `ffmpeg` is not on the `PATH`, `imageio-ffmpeg`
(requirements.txt) supplies one.

## Deploying

### Static frontend on Vercel

Import the repo at <https://vercel.com/new>, framework preset **Other**, no
build step. `/api/*` is handled by the Python functions in `api/`.

> **Heads-up:** Vercel's function limits (payload, duration, cold starts) mean
> YouTube and HLS usually work there, but other hosts may not. For full yt-dlp
> power, run the backend somewhere with a normal IP.

### Full backend elsewhere (recommended)

Any host that runs a long-lived process — Render (`render.yaml` is ready-made),
Railway, Fly.io, a VPS:

```bash
python3 server.py --host 0.0.0.0 --port $PORT
```

Then point the frontend at it in `config.js`:

```js
window.VIEWER_BACKEND = 'https://your-backend.example.com';
window.VIEWER_TOKEN = 'the-same-token-as-the-server';   // only if you set one
```

(Or visit once with `?backend=https://…` / `?token=…`.) The backend sends
`Access-Control-Allow-Origin: *`, so cross-origin calls work; with a token every
URL the page builds carries it, since `<video>` cannot send custom headers.

## Security

- This runs a video extractor over whatever URL you hand it, so it binds
  `127.0.0.1` by default. Anyone who can reach a `0.0.0.0` instance can make
  yt-dlp fetch URLs — set `VIEWER_TOKEN` (and `VIEWER_BLOCK_PRIVATE=1`) before
  exposing it.
- Only `Referer`, `Origin`, `Cookie`, `Authorization`, `User-Agent` and a couple
  of `x-*` hints are ever replayed upstream; nothing else from the extractor is
  forwarded.
- Cookies stay on the server: the browser's own cookies are never forwarded to
  a third-party CDN.
- `/api/*` refuses to proxy itself, and the rate limiter only counts extraction
  calls, so a single stream is never cut off mid-playback.
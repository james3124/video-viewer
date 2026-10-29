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
python3 tools/backend_check.py    # 123 checks: proxy, HLS, adapters, cache, token, limits
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
  Cloudflare/DataDome checks that block datacenter IPs. It is retried without
  impersonation if a host rejects it, then switched off if it keeps failing.
- `--age-limit 18` handles age-gated YouTube; `--cookies cookies.txt` unlocks
  members-only and region-locked hosts.
- Retries, socket timeouts and a shared yt-dlp cache directory are always on.
- Playlists, channels and profile pages are detected and listed in the UI.
- The streaming proxy never follows a redirect off the domain the media was
  resolved on (cap: 5 hops) — a poisoned or expired CDN link fails with both
  hostnames in the message instead of leading the phone somewhere else. Tune
  with `VIEWER_MAX_REDIRECTS`, or allow another CDN family with
  `VIEWER_REDIRECT_HOSTS=a.com,b.com`.
- The frontend has no third-party CDN assets: hls.js is vendored in
  `vendor/` and served from this server, so an ad blocker or DNS filter can
  no longer silently break every HLS stream. When a browser really has no
  MSE, the player still hands the stream to its native player and reports
  whatever that says.
- Every error the browser shows is echoed to the server log (`[ui] …`, via
  `/api/ui-log`, rate-limited): "it doesn't play" becomes a diagnosable line
  instead of a back-and-forth.
- `VIEWER_ALWAYS_PROXY=1` (or the "stream through this server" checkbox in the
  quality panel) keeps the browser off CDNs entirely: every byte comes from
  this server, so no remote host can redirect the player's tab.
- When extraction fails, the error box and the ↗ button in the control bar
  offer the honest fallback: open the original page in a new tab — their
  player, their session, `target="_blank" rel="noopener noreferrer"` so it
  cannot pull this page with it.

### What works out of the box

yt-dlp is the universal layer: one dependency covers YouTube, TikTok, Facebook
and ~1,800 other sites. Measured on this deployment (2026-10):

| Site | Without cookies | With cookies (`VIEWER_COOKIES_B64`) |
|---|---|---|
| YouTube | works, ~5 s cold, 54 formats on a 4K video | n/a |
| TikTok | **audio only** — its video tokens are minted for the requesting session and 403 even in a real fresh browser; the music CDN (`tiktokcdn.com`) streams from anywhere | video expected to work — tokens minted under your session are normally self-contained (this is how cookie-based downloader sites operate); not yet verified on this deployment |
| Facebook | `watch?v=` public videos work; some `/page/videos/` permalinks answer "Cannot parse data" and want a session (yt-dlp issue #17583) | both shapes work |
| anime/18+ tube sites with obfuscated players | the three adapters below | n/a |

The TikTok case is a CDN design, not a missing codec: `v16/v19-webapp-prime`
URLs carry the fingerprint of whoever requested them (verified: a fresh,
impersonated, cookie-free real-browser fetch also gets 403). When the proxy
hits one, the error says exactly this instead of inviting retries that can't
work.

### Site adapters

Some hosts have no usable generic extraction, so there is a small explicit
adapter table in `server.py` instead of a heuristic. One entry per site, because
guessing at site shapes is how extractors rot.

| Host | What the adapter does |
|---|---|
| `hanime.tv` | runs the player's own signed handshake: the page's vendor bundle carries a WebAssembly signer that attests `(unix second, window.origin)`; the adapter executes that blob in wasmtime (no JS engine, every import a stub), AES-256-GCM seals `{timestamp, directive, slug}` with the site's own key, POSTs it with the signature to `auth.hanime.tv/api/v11/handshake`, and reads the per-resolution HLS sources out of the answer's `x-token` header |
| `watchhentai.net` | follows the player frame the page names, then runs that frame's own client-side decoder (`whDecodeMediaUrl`) to recover the real, per-quality MP4 URLs |
| `sulasokvid.co` | replays the site's own three-step player flow server-side: `watch.php` → `video.php` (its Skip button's target), read the `/e/` embed its JS injects, then that embed's `POST /api/stream` answers with the HLS manifest. Skips the ad redirect and the "intro" gate by construction |

None of them touch a login or a paywall: each reads what the site serves to any
visitor and runs the site's own decoder. For watchhentai the media URLs are
obfuscated in the HTML; for hanime the handshake is signed, but by the same
public blob the browser runs — the adapter never invents a credential, it just
runs the site's own code faster than a browser tab would. hanime's HLS key is
AES-128, so its playlists report `needsProxy` and `/api/hls` rewrites the
`EXT-X-KEY` URI back through this server; the CDN itself serves segments as
`.html` with a `text/html` type, which is why the proxy judges "is this a web
page?" by the body and not the header.

To add a site: diagnose it first — `.venv/bin/python tools/site_check.py <url>`
prints which layer answered, what formats it found, and why it failed. If
nothing can decode it, the page usually decodes the stream for every visitor
anyway: write the extractor, add a row to the table, and add a check to
`tools/backend_check.py`.

### Comment pages are not playlists

On a busy WordPress page yt-dlp's generic extractor sometimes latches onto the
first comment permalink and reports a 200-entry "Comments for …" playlist. It
has no playable formats, so accepting it as success leaves you with an empty
player — and following it just walks to the next page of comments. Those results
are rejected (`_is_comment_playlist`) and the search falls through to the
adapters and the HTML sniffer instead.

### When a host still will not play

Whether a host works is a property of the **server's IP**, not of the page, so
run the probe on the machine that runs `server.py`:

```bash
python3 tools/site_check.py                     # edge + extraction per host
python3 tools/site_check.py https://host/video  # just that one
```

It prints, per host: whether the edge answers or throws a bot challenge, how many
formats yt-dlp found, how many of those need the proxy, which headers they need,
and the error otherwise — structure only, never titles or media.

Failures come with the one action that usually fixes them:

| Message contains | What it means | Fix |
| --- | --- | --- |
| `bot check`, `cloudflare`, `datadome`, `captcha` | the host is fingerprinting the client | `curl_cffi` installed (`--impersonate chrome`), cookies, or a non-datacenter IP |
| `login`, `premium`, `members`, `subscribe` | account wall | export `cookies.txt` from a logged-in browser → `YTDLP_COOKIES` |
| `age`, `consent`, `verify` | age/consent gate | cookies from a browser that passed the gate |
| `in your country`, `region`, `geo` | region lock | `YTDLP_ARGS='--proxy …'` or cookies from the right region |
| `private`, `removed`, `unavailable` | the video itself is gone | nothing a server can do |

Hosts behind Cloudflare frequently block Vercel/Render IP ranges outright — if
`tools/site_check.py` fails there but succeeds on a VPS, that is the reason.

## Download

The ⬇ button (in the control bar) saves the video through the backend, so it
works even when the CDN refuses to let the browser download (cross-origin, needs
Referer/Cookie, age-gated). The backend picks the quality on screen if the
backend resolved it, otherwise a sensible (≤1080p, MP4) single file, sends it
with `Content-Disposition: attachment` (filename from the title, UTF-8 safe), and
falls back to the ffmpeg merge when the site only publishes split streams. The
file lands in the browser's downloads folder — no client-side `fetch`/blob
problems.

## Player controls:

The video element has no native `controls`; the page draws its own bar:

| Control | What it does |
| --- | --- |
| Seek bar | click or drag anywhere to jump; shows buffered range, hover tooltip, auto-hides after 2.6 s of playback |
| **Click the time** | opens a field — type `3:21`, `1:54`, `1:02:03` or `194` and press <kbd>Enter</kbd> to jump exactly there (garbage is refused with a message) |
| `−10` / `+10` | skip back/forward ten seconds (also <kbd>←</kbd><kbd>→</kbd> and <kbd>J</kbd><kbd>L</kbd>) |
| Volume + mute | remembered in `localStorage` per browser |
| Speed | 0.25×–4×, in sync with the <kbd>&lt;</kbd>/<kbd>&gt;</kbd> shortcuts |
| `CC` | subtitles, disabled automatically when there are no tracks |
| `PiP`, fullscreen | picture-in-picture and fullscreen; the bar follows into fullscreen because it lives inside the stage |

Everything is keyboard reachable: the seek bar is a real `<input type="range">`
(transparent, on top of the drawn bar), so <kbd>←</kbd>/<kbd>→</kbd> move 5 s and
<kbd>↑</kbd>/<kbd>↓</kbd> 10 s when it has focus.

Run the DOM test after touching `app.js` or the markup:

```bash
cd tools && npm install && cd .. && \
NODE_PATH=tools/node_modules node tools/ui_check.js      # 32 UI checks
NODE_PATH=tools/node_modules node tools/integration_check.js   # 15 end-to-end
```
(The backend needs its own check-run before these: `tools/backend_check.py`.)

## Caching

| Layer | Where | Default |
| --- | --- | --- |
| yt-dlp info | memory LRU | `VIEWER_MAX_CACHE` 256 entries, `VIEWER_TTL` 900 s |
| yt-dlp info | disk JSON in `VIEWER_CACHE_DIR` | same TTL — survives restarts, warms up cold serverless containers |
| failures | memory | `VIEWER_NEG_TTL` 45 s, so a dead link is not hammered |
| playback window | disk `VIEWER_CACHE_DIR/media` | size-tiered, follows the playhead, gone after 5 min idle **and** on exit |
| HTTP | `ETag` + `Cache-Control: public, max-age=…` | browser/CDN skip the round trip entirely (`VIEWER_HTTP_CACHE=0` to disable) |
| static files | `ETag` + `Last-Modified` | revalidate → instant 304s |
| HLS manifests | `Cache-Control: public, max-age=15` | `VIEWER_MANIFEST_TTL` |
| captions | converted `.vtt` on disk | `VIEWER_SUB_TTL` 6 h |

Concurrent requests for the same link share a single yt-dlp run (single-flight),
and only `VIEWER_YTDLP_CONCURRENCY` (4) extractions may run at once — a busy
instance answers `429` instead of forking itself to death.
`GET /api/health` reports hit/miss/refresh counters and the cache directory.

### Playback window

Direct MP4s are fetched through `/api/play?proxy=1`, and the bytes you are
watching are tee'd to disk. The window is sized by the **whole file**, so a
small file keeps most of itself and a huge one keeps only a slice:

| File size | Kept |
| --- | --- |
| under 100 MB | 75 % |
| 100–299 MB | 50 % |
| 300–999 MB | 35 % |
| 1 GB and up | 25 % |

It is anchored to the playhead (reported every few seconds by the UI), not to
the furthest prefetched byte. As playback advances, chunks behind the window are
unlinked and the next stretch is fetched in the background, throttled to
`VIEWER_PREFETCH_MBPS`. A part that is *not* in the window returns a clear `503`
when the source is unreachable instead of a truncated `206` that looks like a
corrupt video. The whole directory is wiped on a graceful exit, and a stale
`.owner` PID is detected on startup so a crash cannot leave the cache behind.

HLS is not windowed yet — it still streams through `/api/hls` (Phase 2).


## Configuration (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `VIEWER_TOKEN` | *(empty)* | shared secret; `?token=` or `X-Viewer-Token` on every `/api/*` except `/api/health` |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | bind address (Render sets both for you) |
| `VIEWER_CACHE_DIR` | `$TMPDIR/viewer-cache` | cache root (must be writable) |
| `VIEWER_MEDIA_CACHE` | `1` (off on serverless) | windowed playback cache on/off |
| `VIEWER_MEDIA_TTL` | `300` | seconds an idle playback window survives |
| `VIEWER_MEDIA_SWEEP` | `30` | how often the idle/quota sweeper runs |
| `VIEWER_MEDIA_MAX_MB` | `4096` | global ceiling for all playback windows |
| `VIEWER_MEDIA_CHUNK` | `1048576` | cache chunk size in bytes |
| `VIEWER_CACHE_BACK` | `10` | seconds of already-played video kept behind the playhead |
| `VIEWER_PREFETCH_MBPS` | `8` | ceiling for the background window filler |
| `VIEWER_MEDIA_CLEANUP_ON_EXIT` | `1` | wipe the playback window when the server exits |
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
| `VIEWER_YTDLP_MODE` | `auto` | `auto` uses the `yt_dlp` Python module (no subprocess, no PATH dependency), `binary`/`module` force one. Both are checked, so this reports "missing" only if neither exists |
| `YTDLP_COOKIES` | *(empty)* | path to a `cookies.txt` on disk |
| `VIEWER_COOKIES_B64` | *(empty)* | the same `cookies.txt`, base64-encoded, written into the cache dir (mode 0600) on first use — **use this on Vercel instead of putting cookies in a public repo** |
| `YTDLP_IMPERSONATE` | `chrome` if `curl_cffi` is present, `off` to disable | yt-dlp `--impersonate` |
| `VIEWER_IMPERSONATE_FAILURES` | `3` | failures in a row before impersonation is switched off |
| `YTDLP_ARGS` | *(empty)* | extra raw yt-dlp flags |
| `YTDLP_IPV4` | `0` | `--force-ipv4` (some hosts break on IPv6) |

`ffmpeg` is optional at runtime: without it, video+audio merging is off and the
other formats still play. If `ffmpeg` is not on the `PATH`, `imageio-ffmpeg`
(requirements.txt) supplies one.

`cryptography` and `wasmtime` are only used by the hanime adapter (sealing the
handshake and running the site's signer blob). Without them every other site is
unaffected; hanime then says which package to install and the UI still offers
"Open on site".

## Deploying

### Static frontend on Vercel

Import the repo at <https://vercel.com/new>, framework preset **Other**, no
build step. `/api/*` is handled by the Python functions in `api/`.

`requirements.txt` is installed automatically — **no environment variables are
needed**. yt-dlp runs as an imported Python module there, which needs neither
`PATH` nor process spawning.

Check what a deployment actually has with one request:

```bash
curl -s https://your-app.vercel.app/api/health
# ytDlpAvailable, ytDlpMode (module|binary), ytDlpVersion, python, features, cache
```

> **Heads-up:** Vercel's function limits (payload, duration, cold starts) mean
> YouTube and HLS usually work there, but other hosts may not. For full yt-dlp
> power, run the backend somewhere with a normal IP.

### Vercel is blocked by most media hosts

Vercel serves from datacenter IP ranges that YouTube, Pornhub and most
Cloudflare-fronted sites block outright. Extraction then fails with
`Redirection detected` / `Sign in to confirm you're not a bot`, and no code
change fixes that — it is the IP, not the app. The backend names the platform
in `/api/health` → `platform`, and appends it to extraction failures so the page
says so instead of failing silently.

What actually works, in order:

1. **Backend on a VPS / Fly.io / a home connection** — the real fix. Same
   `render.yaml`, same commands, no code changes.
2. **Cookies** (`VIEWER_COOKIES_B64`) — necessary anyway for age gates and
   logins, and enough on their own for some hosts.
3. **Vercel** — fine for YouTube *with* cookies, and fine as a static frontend
   in front of a backend hosted elsewhere.

### Full backend elsewhere (recommended)

Any host that runs a long-lived process — Render, Railway, Fly.io, a VPS.

**Render, in four steps**

1. <https://render.com/deploy?repo=https://github.com/james3124/video-viewer>
   (or New → Blueprint → pick the repo; `render.yaml` does the rest).
2. In the dashboard, set the two secrets Render prompts for:
   - `VIEWER_TOKEN` — generate with
     `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`
   - `VIEWER_COOKIES_B64` — `base64 -w0 cookies.txt`, or leave empty.
3. Wait for the deploy, then check it:
   ```bash
   curl -s https://video-viewer-backend.onrender.com/api/health
   ```
4. Point the frontend at it in `config.js`:
   ```js
   window.VIEWER_BACKEND = 'https://video-viewer-backend.onrender.com';
   ```
   then open the page **once** as
   `https://your-app.vercel.app/?token=<your VIEWER_TOKEN>` — it is remembered in
   that browser. Do **not** commit the token into `config.js`: this repo is
   public, so that would publish it.

Anything else:

```bash
python3 server.py --host 0.0.0.0 --port $PORT   # reads PORT/HOST from the env
```

> Free Render instances sleep after ~15 minutes idle, so the first play after a
> pause waits for a cold start (~30-60 s); after that the cache makes it fast.
> If `free` is not offered on your account, set `plan: starter` in `render.yaml`.
> Render is still a datacenter range: fewer hosts block it than Vercel, but
> cookies remain the cure for the stubborn ones.

## Cookies (age gates, logins, paywalls)

A `cookies.txt` export is what gets past age/consent gates, account walls and
some bot checks. There are two ways to give the backend one:

**1. Base64 in an environment variable — the safe one, nothing in git**

```bash
# 1. export cookies.txt from your browser (Get cookies.txt LOCALLY extension)
# 2. encode it (one line, no newlines/quotes to fight with):
base64 -w0 cookies.txt
# 3. Vercel → Settings → Environment Variables:
#    VIEWER_COOKIES_B64 = <paste>   (tick "Sensitive")
# 4. Redeploy
```

The backend decodes it on first use into `<cache dir>/cookies.txt` with mode
`0600`. A value that is not a real cookies.txt is reported in
`/api/health` → `notices` rather than failing silently.

**2. A file in the deployment** (fine for a VPS, wrong for a public repo)

```bash
YTDLP_COOKIES=/opt/render/project/src/cookies.txt   # Render
python3 server.py --cookies /path/cookies.txt        # local
```

> `cookies.txt` is a logged-in session — anyone holding it is logged in as you.
> `cookies.txt` is git-ignored, but **never commit it to a public repository**;
> use method 1. Scope the export to the one host you need where you can.

---

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
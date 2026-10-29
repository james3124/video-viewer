/* ytdlp.js — bridge between the page and the viewer backend (server.py + yt-dlp).
 *
 * Registers a resolver with window.ViewerResolvers, which app.js calls whenever
 * a link is not something the browser can play by itself (YouTube, TikTok, a
 * page with an embedded player, an unknown extension…, playlists and channels).
 *
 * The resolver turns a page URL into { stream, meta }:
 *   stream = { kind: 'direct'|'hls'|'merge', url, proxyUrl, proxied }
 *   meta   = { title, duration, thumbnail, playlist, subtitles: [{url, label}] }
 *
 * Probing order for the backend: ?backend=…, window.VIEWER_BACKEND, the saved
 * choice, same origin, then the usual localhost ports.
 *
 * If no backend answers, nothing here throws and the page still plays direct
 * URLs, HLS files and embeds on its own.
 */
(function () {
  'use strict';

  const state = {
    base: '',               // '' = same origin, otherwise the backend URL
    token: '',
    online: false,
    ytDlp: null,            // version string when found
    ytDlpMode: null,        // 'module' | 'binary' | 'none' — how it is invoked
    platform: null,        // 'Vercel' | 'Render' | … from /api/health
    ffmpeg: false,
    modes: new Map(),       // page URL → chosen format id
    lastInfo: null,
    cacheEnabled: false,    // backend keeps a short window of the bytes in view
  };

  const ALWAYS_KEY = 'viewer.alwaysProxy';
  function alwaysProxy() {
    try { return localStorage.getItem(ALWAYS_KEY) === '1'; } catch (_) { return false; }
  }

  const panel = document.createElement('div');
  panel.id = 'ytdlp';
  panel.hidden = true;

  /* ---------------------------------------------------------------- API -- */

  async function getJsonAt(base, path, timeoutMs) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs || 12000);
    try {
      const res = await fetch(base + path, {
        signal: ctrl.signal,
        cache: 'default',                 // the backend sends ETag + max-age
        headers: { Accept: 'application/json', ...authHeaders() },
      });
      const text = await res.text();
      let json = null;
      try { json = JSON.parse(text); } catch (_) { /* html error page */ }
      if (!res.ok) throw new Error((json && json.error) || `HTTP ${res.status}`);
      return json;
    } finally {
      clearTimeout(timer);
    }
  }

  function getJson(path, timeoutMs) {
    return getJsonAt(state.base, path, timeoutMs);
  }

  const info = (url, fresh) =>
    getJson('/api/info?url=' + encodeURIComponent(url) + (fresh ? '&fresh=1' : ''), 90000);

  /** Where is the yt-dlp backend? Priority: ?backend=…, window.VIEWER_BACKEND, saved. */
  function configuredBase() {
    let q = '';
    try { q = new URLSearchParams(location.search).get('backend') || ''; } catch (_) {}
    let stored = '';
    try { stored = localStorage.getItem('viewer.backend') || ''; } catch (_) {}
    const raw = String(q || window.VIEWER_BACKEND || stored || '').trim();
    return raw.replace(/\/+$/, '');
  }

  /** Token for token-protected backends: ?token=…, window.VIEWER_TOKEN, saved. */
  function configuredToken() {
    let q = '';
    try { q = new URLSearchParams(location.search).get('token') || ''; } catch (_) {}
    let stored = '';
    try { stored = localStorage.getItem('viewer.token') || ''; } catch (_) {}
    return String(q || window.VIEWER_TOKEN || stored || '').trim();
  }

  function authHeaders() {
    return state.token ? { 'X-Viewer-Token': state.token } : {};
  }

  /** Appended to every URL a <video>/hls.js/<track> has to fetch. */
  function withToken(url) {
    return state.token ? url + (url.includes('?') ? '&' : '?') +
      'token=' + encodeURIComponent(state.token) : url;
  }

  /* Media URLs — the page/format pair is enough: the backend re-resolves the
   * CDN link (and re-extracts it if the signed URL has expired). */
  const mergeUrl = (pageUrl) =>
    withToken(`${state.base}/api/merge?url=${encodeURIComponent(pageUrl)}`);
  const playUrl = (pageUrl, fmt) =>
    withToken(`${state.base}/api/play?url=${encodeURIComponent(pageUrl)}` +
      `&fmt=${encodeURIComponent(fmt)}&proxy=1`);
  const hlsUrl = (pageUrl, fmt) =>
    withToken(`${state.base}/api/hls?url=${encodeURIComponent(pageUrl)}` +
      `&fmt=${encodeURIComponent(fmt)}`);

  /** Turn a chosen format into something app.js can put in <video src>. */
  function buildStream(fmt, pageUrl) {
    if (fmt.kind === 'merge') {
      return { kind: 'merge', url: fmt.url, proxyUrl: mergeUrl(pageUrl), proxied: true };
    }
    if (fmt.kind === 'hls') {
      // Adaptive streams only work when every segment can be re-fetched with the
      // right headers — i.e. through /api/hls, which rewrites the playlist.
      const proxied = !!fmt.needsProxy || alwaysProxy();
      return {
        kind: 'hls', url: fmt.url,
        proxyUrl: proxied ? hlsUrl(pageUrl, fmt.id) : null,
        proxied,
      };
    }
    return {
      kind: 'direct', url: fmt.url,
      proxyUrl: playUrl(pageUrl, fmt.id),
      // CDNs that demand extra headers (Referer/Cookie) refuse the browser's
      // plain request, so those go straight through the backend. The per-device
      // toggle goes further: when it is set, the browser never touches the CDN
      // directly — every stream is re-fetched by this server. The windowed
      // cache needs the bytes to pass through us too, so it forces the proxy
      // for direct formats whenever the backend says caching is available.
      proxied: !!fmt.needsProxy || alwaysProxy() || state.cacheEnabled,
    };
  }

  function buildMeta(data, fmt) {
    const playlist = data.playlist && data.playlist.entries
      ? data.playlist.entries.filter((e) => e && e.url)
      : null;
    return {
      title: data.title,
      duration: data.duration,
      thumbnail: data.thumbnail,
      isLive: data.isLive,
      extractor: data.extractor,
      formatId: fmt.id,
      formatLabel: fmt.label,
      playlist: playlist && playlist.length ? playlist : null,
      subtitles: (data.subtitles || []).slice(0, 6).map((s) => ({
        url: withToken(`${state.base}/api/subsrc?url=${encodeURIComponent(data.url)}` +
          `&lang=${encodeURIComponent(s.raw_lang)}`),
        label: s.lang,
      })),
    };
  }

  /* ------------------------------------------------------------ resolver -- */

  async function resolver(pageUrl, fresh) {
    window.ViewerLastFailure = '';
    // A hard site can spend 15-30 s in yt-dlp's ladder before the adapters run,
    // so say we are working rather than leaving a dead-looking page.
    setStatus('Resolving — the backend is working on this link…');
    const data = await info(pageUrl, fresh);
    state.cacheEnabled = !!(data && data.cacheEnabled);

    if (data && data.extractable === false) {
      // A playlist still deserves its entry list, even with nothing to play.
      if (data.playlist && data.playlist.entries && data.playlist.entries.length) {
        // Tell app.js there is nothing to put in <video>: without this it falls
        // through and points the video element at the page URL, which is an HTML
        // document — that "plays" as nothing, with no error anywhere.
        window.ViewerLastFailure =
          'That link is a list, not a single video — pick an entry below.';
        renderPanel(data, null);
        note(`Playlist “${data.playlist.title || pageUrl}” — ${data.playlist.count} entries. `
           + 'Pick one below.');
        return null;
      }
      showBadNews(pageUrl, data.reason || 'yt-dlp could not extract anything playable.');
      return null;
    }
    if (!data || !data.formats || !data.formats.length) {
      window.ViewerLastFailure =
        'The backend answered but offered no playable format for this link.';
      return null;
    }

    state.lastInfo = data;
    renderPanel(data);

    const fmt = pick(data);
    if (!fmt) {
      showBadNews(pageUrl, 'No browser-playable format was offered.');
      return null;
    }

    return { stream: buildStream(fmt, pageUrl), meta: buildMeta(data, fmt) };
  }

  function pick(data) {
    const wanted = state.modes.get(data.url) || data.autoFormatId;
    return data.formats.find((f) => f.id === wanted)
        || data.formats.find((f) => f.kind === 'direct')
        || data.formats[0];
  }

  function showBadNews(pageUrl, why) {
    // The backend is online and just told us *why* it could not resolve this.
    // app.js checks this flag to avoid "playing" a web page as if it were media.
    window.ViewerLastFailure = why;
    const box = document.getElementById('errors');
    if (box) {
      box.hidden = false;
      box.textContent = '';
      const span = document.createElement('span');
      span.textContent = 'yt-dlp: ' + why;
      box.appendChild(span);
      if (typeof window.ViewerSiteLink === 'function' &&
          /^https?:/i.test(pageUrl || '')) {
        box.appendChild(window.ViewerSiteLink(pageUrl));
      }
    }
    setStatus(why);
    state.lastInfo = { reason: why, url: pageUrl };
  }

  /** Play a playlist entry (re-resolves that entry's own page URL). */
  async function playEntry(entry) {
    if (!entry || !entry.url) return;
    try {
      const r = await resolver(entry.url);
      if (r) window.ViewerApply(r);
      else if (window.ViewerPlay) window.ViewerPlay(entry.url);
    } catch (err) {
      showBadNews(entry.url, (err && err.message) || String(err));
    }
  }

  /* ---------------------------------------------------------------- UI -- */

  function clearError() {
    const box = document.getElementById('errors');
    if (box) { box.hidden = true; box.textContent = ''; }
  }
  function setStatus(text) {
    const meta = document.getElementById('meta');
    if (meta && text) meta.textContent = text;
  }

  function renderPanel(data, chosenFmt) {
    const current = chosenFmt === null ? null : pick(data);
    const fmts = (data.formats || []).filter((f) => f.kind !== 'dash').slice(0, 14);

    panel.innerHTML = '';
    panel.hidden = false;

    const head = document.createElement('div');
    head.className = 'yt-head';
    const pl = data.playlist && data.playlist.count > 1
      ? `<span class="yt-tag">playlist · ${data.playlist.count}</span>` : '';
    head.innerHTML = `
      <span class="yt-title" title="${esc(data.title || data.url)}">${esc(data.title || prettyUrl(data.url))}</span>
      ${pl}
      ${data.extractor ? `<span class="yt-tag">${esc(data.extractor)}</span>` : ''}
      ${data.isLive ? '<span class="yt-tag live">LIVE</span>' : ''}
      ${data.duration ? `<span class="yt-tag">${fmtTime(data.duration)}</span>` : ''}
      ${data.duration ? '' : '<span class="yt-tag">unknown length</span>'}`;
    panel.appendChild(head);

    if (fmts.length) {
      const row = document.createElement('div');
      row.className = 'yt-row';
      row.append(label('Quality'));

      fmts.forEach((f) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'yt-fmt' + (current && f.id === current.id ? ' active' : '');
        b.title = f.needsProxy ? 're-fetched through the backend (CDN needs headers)'
                               : 'played straight from the CDN';
        b.innerHTML = `${esc(f.label)}${f.sizeHuman ? ` <em>${esc(f.sizeHuman)}</em>` : ''}`;
        b.addEventListener('click', () => {
          state.modes.set(data.url, f.id);
          row.querySelectorAll('.yt-fmt').forEach((x) => x.classList.remove('active'));
          b.classList.add('active');
          resolver(data.url).then((r) => { if (r) window.ViewerApply(r); }).catch(() => {});
        });
        row.appendChild(b);
      });

      const proxy = document.createElement('button');
      proxy.type = 'button';
      proxy.className = 'yt-proxy';
      proxy.title = 'Re-fetch this stream through the server '
                  + '(use when direct playback is blocked or the link expired)';
      proxy.textContent = 'Force proxy';
      proxy.addEventListener('click', () => {
        const f = current || pick(data);
        const built = buildStream(f, data.url);
        if (built.kind === 'hls') built.url = hlsUrl(data.url, f.id);
        window.ViewerApply({ stream: { ...built, proxied: true }, meta: buildMeta(data, f) });
      });
      row.appendChild(proxy);

      const always = document.createElement('label');
      always.className = 'yt-always';
      always.title = 'Route every stream through this server, so the browser '
                   + 'never follows a CDN redirect it did not ask for';
      const alwaysBox = document.createElement('input');
      alwaysBox.type = 'checkbox';
      alwaysBox.checked = alwaysProxy();
      alwaysBox.addEventListener('change', () => {
        try { localStorage.setItem(ALWAYS_KEY, alwaysBox.checked ? '1' : '0'); }
        catch (_) { /* jsdom tests, private mode */ }
        resolver(data.url).then((r) => { if (r) window.ViewerApply(r); }).catch(() => {});
      });
      always.appendChild(alwaysBox);
      always.appendChild(document.createTextNode(' stream through this server'));
      row.appendChild(always);

      const hide = document.createElement('button');
      hide.type = 'button';
      hide.className = 'yt-close';
      hide.textContent = '×';
      hide.title = 'Hide this row';
      hide.addEventListener('click', () => { panel.hidden = true; });
      head.appendChild(hide);

      panel.appendChild(row);
    } else {
      const warn = document.createElement('div');
      warn.className = 'yt-warn';
      warn.textContent = data.reason
        || 'yt-dlp offered no browser-playable format for this link.';
      panel.appendChild(warn);
    }

    if (data.reason && fmts.length) {
      const warn = document.createElement('div');
      warn.className = 'yt-warn';
      warn.textContent = data.reason;
      panel.appendChild(warn);
    }

    // Playlist / channel listing — click an entry to play just that item.
    const entries = (data.playlist && data.playlist.entries || [])
      .filter((e) => e && e.url).slice(0, 300);
    if (entries.length > 1) {
      const list = document.createElement('div');
      list.className = 'yt-list';
      const head2 = document.createElement('div');
      head2.className = 'yt-label';
      head2.textContent = `${data.playlist.count || entries.length} entries`;
      list.appendChild(head2);
      entries.forEach((e, i) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'yt-item';
        b.title = e.url;
        b.innerHTML = `<span class="yt-idx">${e.duration ? fmtTime(e.duration) : (i + 1)}</span>`
                    + `<span class="yt-name">${esc(e.title)}</span>`;
        b.addEventListener('click', () => {
          const input = document.getElementById('urlInput');
          if (input) input.value = e.url;          // keep the box in sync
          b.classList.add('active');
          playEntry(e);
        });
        list.appendChild(b);
      });
      panel.appendChild(list);
    }
  }

  function label(text) {
    const s = document.createElement('span');
    s.className = 'yt-label';
    s.textContent = text;
    return s;
  }

  const esc = (s) => String(s == null ? '' : s)
    .replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const prettyUrl = (u) => { try { return new URL(u).hostname; } catch (_) { return u; } };
  const fmtTime = (s) => {
    s = Math.max(0, s | 0);
    const h = (s / 3600) | 0, m = ((s % 3600) / 60) | 0;
    return (h ? `${h}:${String(m).padStart(2, '0')}` : m) + ':' + String(s % 60).padStart(2, '0');
  };

  /* ----------------------------------------------------------- bootstrap -- */

  function statusChip(text, ok, title) {
    const chip = document.querySelector('#backendChip');
    if (!chip) return;
    chip.textContent = text;
    chip.dataset.state = ok ? 'on' : 'off';
    chip.title = title;
  }

  let probeTimer = null;

  /** Download URL for the current link (needs the backend: it names the file).
   Pass `fmt` to save exactly the quality that is on screen. */
  window.ViewerDownloadUrl = function (pageUrl, fmt) {
    if (!pageUrl || !/^https?:/i.test(pageUrl)) return null;
    const q = fmt ? `&fmt=${encodeURIComponent(fmt)}` : '';
    return withToken(`${state.base}/api/play?url=${encodeURIComponent(pageUrl)}&dl=1${q}`);
  };

  /** Auth header for HEAD requests app.js makes itself (download pre-flight). */
  window.ViewerAuthHeaders = () => authHeaders();

  /** Tell the backend where the playhead is, so its cache window can follow it.
   *  Range requests alone cannot tell "watching at 3:15" from "buffering ahead". */
  window.ViewerReportPosition = function (pageUrl, fmt, t, duration, paused) {
    if (!state.cacheEnabled || !pageUrl || !fmt) return;
    if (!/^https?:/i.test(pageUrl)) return;
    const q = `url=${encodeURIComponent(pageUrl)}&fmt=${encodeURIComponent(fmt)}`
      + `&t=${(Math.max(0, t || 0)).toFixed(1)}`
      + `&d=${(Math.max(0, duration || 0)).toFixed(1)}`
      + (paused ? '&paused=1' : '');
    try {
      fetch(withToken(`${state.base}/api/position?${q}`), {
        cache: 'no-store',
        keepalive: true,
        headers: authHeaders(),
      }).catch(() => {});
    } catch (_) { /* the heartbeat is best-effort */ }
  };

  /** Register with app.js once the backend answers. */
  function registerResolver() {
    window.ViewerResolvers = window.ViewerResolvers || [];
    if (!window.ViewerResolvers.includes(resolver)) window.ViewerResolvers.push(resolver);
  }

  // Probe a few origins: a configured backend first, then same-origin, then the
  // usual localhost ports. The first healthy one wins. If none answer we keep
  // retrying, so a slow/restarted server eventually comes back online.
  function checkBuild(h) {
    // A tab left open on a phone keeps running yesterday's JavaScript while the
    // backend already has today's fixes. That looks exactly like "the bug is
    // still there", so say what is actually wrong.
    const mine = document.querySelector('meta[name="viewer-build"]');
    const theirs = h && h.staticBuild;
    const here = mine && mine.getAttribute('content');
    // A plain static host (Vercel) never substitutes the placeholder, so there
    // is nothing to compare against — stay quiet rather than cry wolf.
    if (!theirs || !here || here === theirs || /[<>=]/.test(here)) return false;
    const box = document.getElementById('errors');
    const msg = 'This page is running older code than the server '
      + `(page ${here}, server ${theirs}) — reload it to get the fix.`;
    if (box) { box.hidden = false; box.textContent = msg; }
    console.warn('[viewer] ' + msg);
    note(msg);
    return true;
  }

  async function checkBackend() {
    const configured = configuredBase();
    const state0 = configuredToken();
    const candidates = [configured, '', 'http://127.0.0.1:8080', 'http://localhost:8080'];
    const unique = [...new Set(candidates)];
    const tried = [];

    for (const base of unique) {
      const label = base || (typeof location !== 'undefined' ? location.origin : 'same-origin');
      const saved = state0;
      state.token = saved;
      try {
        const h = await getJsonAt(base, '/api/health', 8000);
        if (h && h.ok) {
          const wasOffline = !state.online;
          state.base = base;
          state.online = true;
          state.ytDlp = h.ytDlpVersion;
          state.ytDlpMode = h.ytDlpMode;
          state.platform = h.platform;
          state.ffmpeg = !!(h.features && h.features.merge);
          checkBuild(h);
          if (base) { try { localStorage.setItem('viewer.backend', base); } catch (_) {} }
          if (h.tokenRequired && !state.token) {
            state.token = '';
            statusChip('token needed', false,
              'This backend requires a token — add ?token=… (or window.VIEWER_TOKEN)');
            tried.push(`${label} → token required`);
            continue;
          }
          if (h.tokenRequired && state.token) {
            try { localStorage.setItem('viewer.token', state.token); } catch (_) {}
          }
          if (h.ytDlpAvailable === false) {
            statusChip('yt-dlp missing', false,
              'server.py is reachable but yt-dlp is not installed — '
              + 'run: pip install -U yt-dlp, then restart server.py');
            note('server.py is reachable but yt-dlp is not installed — '
                 + 'run: pip install -U yt-dlp, then restart server.py');
          } else {
            const bits = [`yt-dlp ${h.ytDlpVersion || 'online'}`];
            if (state.ffmpeg) bits.push('ffmpeg');
            statusChip(bits.join(' · '), true,
              `backend ${label} · yt-dlp via ${state.ytDlpMode || 'binary'}`
              + (state.platform && state.platform !== 'self-hosted'
                  ? ` · running on ${state.platform} (its IP range is often blocked by media hosts)`
                  : '')
              + ' · click to re-check');
            if (wasOffline) note(`Backend online at ${label}`);
          }
          if (probeTimer) { clearInterval(probeTimer); probeTimer = null; }
          registerResolver();
          return true;
        }
        tried.push(`${label} → not ok`);
      } catch (err) {
        tried.push(`${label} → ${(err && err.message) || err}`);
      }
    }

    state.online = false;
    statusChip('backend offline', false,
      'server.py is not reachable — direct URLs, HLS files and embeds still work');
    note('Backend offline. Tried: ' + tried.join(' | '));
    if (!probeTimer) probeTimer = setInterval(checkBackend, 5000);
    return false;
  }

  async function boot() {
    document.body.appendChild(panel);

    const chip = document.querySelector('#backendChip');
    if (chip) {
      chip.style.cursor = 'pointer';
      chip.title = 'Click to re-check the backend';
      chip.addEventListener('click', checkBackend);
    }

    // A backend URL saved earlier can be stale — keep probing in the background.
    checkBackend();
  }

  function note(msg) {
    const el = document.getElementById('notices');
    if (el) { el.hidden = false; el.textContent = msg; }
    console.info('[viewer] ' + msg);
  }

  boot();
})();
/* ytdlp.js — optional bridge to server.py + yt-dlp.
 *
 * Registers a resolver with window.ViewerResolvers, which app.js calls whenever
 * a link is not something the browser can play by itself (YouTube, Vimeo, a
 * page with an embedded player, an unknown extension…).
 *
 * The resolver turns a page URL into { stream, meta }:
 *   stream = { kind: 'direct'|'hls', url, proxyUrl, proxied }
 *   meta   = { title, duration, thumbnail, subtitles: [{url, label}] }
 *
 * If the server is not running, nothing here throws and the page still plays
 * direct URLs, HLS and embeds on its own.
 */
(function () {
  'use strict';

  const state = {
    base: '',               // same-origin when served by server.py, else ''
    online: false,
    ytDlp: null,            // version string when found
    modes: new Map(),       // page URL → chosen format id
    lastInfo: null,
  };

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
        headers: { Accept: 'application/json' },
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

  async function getJson(path, timeoutMs) {
    return getJsonAt(state.base, path, timeoutMs);
  }

  const info = (url) => getJson('/api/info?url=' + encodeURIComponent(url), 90000);
  const health = () => getJson('/api/health', 2500);

  const mergeUrl = (pageUrl) => `${state.base}/api/merge?url=${encodeURIComponent(pageUrl)}`;
  const playUrl = (pageUrl, fmt) =>
    `${state.base}/api/play?url=${encodeURIComponent(pageUrl)}&fmt=${encodeURIComponent(fmt)}`;

  /** Turn a chosen format into something app.js can put in <video src>. */
  function buildStream(fmt, pageUrl) {
    if (fmt.kind === 'merge') {
      return { kind: 'merge', url: fmt.url, proxyUrl: mergeUrl(pageUrl), proxied: true };
    }
    if (fmt.kind === 'hls') {
      return { kind: 'hls', url: fmt.url, proxyUrl: null, proxied: false };
    }
    return {
      kind: 'direct', url: fmt.url,
      proxyUrl: playUrl(pageUrl, fmt.id) + '&proxy=1',
      proxied: false,
    };
  }

  function buildMeta(data, fmt) {
    return {
      title: data.title,
      duration: data.duration,
      thumbnail: data.thumbnail,
      isLive: data.isLive,
      extractor: data.extractor,
      formatLabel: fmt.label,
      subtitles: (data.subtitles || []).slice(0, 6).map((s) => ({
        url: `${state.base}/api/subsrc?url=${encodeURIComponent(data.url)}&lang=${encodeURIComponent(s.raw_lang)}`,
        label: s.lang,
      })),
    };
  }

  /* ------------------------------------------------------------ resolver -- */

  async function resolver(pageUrl) {
    const data = await info(pageUrl);

    if (data && data.extractable === false) {
      showBadNews(pageUrl, data.reason || 'yt-dlp could not extract anything playable.');
      return null;
    }
    if (!data || !data.formats || !data.formats.length) return null;

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
    clearError();
    setStatus(why + ' Falling back to the browser player.');
    state.lastInfo = { reason: why, url: pageUrl };
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

  function renderPanel(data) {
    const fmts = (data.formats || []).filter((f) => f.kind !== 'dash').slice(0, 14);

    panel.innerHTML = '';
    panel.hidden = false;

    const head = document.createElement('div');
    head.className = 'yt-head';
    head.innerHTML = `
      <span class="yt-title" title="${esc(data.title || data.url)}">${esc(data.title || prettyUrl(data.url))}</span>
      ${data.extractor ? `<span class="yt-tag">${esc(data.extractor)}</span>` : ''}
      ${data.isLive ? '<span class="yt-tag live">LIVE</span>' : ''}
      ${data.duration ? `<span class="yt-tag">${fmtTime(data.duration)}</span>` : ''}
      ${data.duration ? '' : '<span class="yt-tag">unknown length</span>'}`;
    panel.appendChild(head);

    if (!fmts.length) {
      const warn = document.createElement('div');
      warn.className = 'yt-warn';
      warn.textContent = 'yt-dlp offered no browser-playable format for this link.';
      panel.appendChild(warn);
      return;
    }

    const row = document.createElement('div');
    row.className = 'yt-row';
    row.append(label('Quality'));

    fmts.forEach((f) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'yt-fmt' + (f.id === pick(data).id ? ' active' : '');
      b.innerHTML = `${esc(f.label)}${f.sizeHuman ? ` <em>${esc(f.sizeHuman)}</em>` : ''}`;
      b.addEventListener('click', () => {
        state.modes.set(data.url, f.id);
        row.querySelectorAll('.yt-fmt').forEach((x) => x.classList.remove('active'));
        b.classList.add('active');
        // re-resolve with the new choice
        resolver(data.url).then((r) => {
          if (r) window.ViewerApply(r);
        }).catch(() => {});
      });
      row.appendChild(b);
    });

    const proxy = document.createElement('button');
    proxy.type = 'button';
    proxy.className = 'yt-proxy';
    proxy.title = 'Re-fetch this stream through the server (use when direct playback is blocked)';
    proxy.textContent = 'Force proxy';
    proxy.addEventListener('click', () => {
      const f = pick(data);
      window.ViewerApply({ stream: { ...stream, proxied: true }, meta: buildMeta(data, f) });
    });
    row.appendChild(proxy);

    const hide = document.createElement('button');
    hide.type = 'button';
    hide.className = 'yt-close';
    hide.textContent = '×';
    hide.title = 'Hide this row';
    hide.addEventListener('click', () => { panel.hidden = true; });
    head.appendChild(hide);

    panel.appendChild(row);
    if (data.reason) {
      const warn = document.createElement('div');
      warn.className = 'yt-warn';
      warn.textContent = data.reason;
      panel.appendChild(warn);
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

  function statusChip(text, ok) {
    const chip = document.querySelector('#backendChip');
    if (!chip) return;
    chip.textContent = text;
    chip.dataset.state = ok ? 'on' : 'off';
    chip.title = ok
      ? `server.py is running${state.ytDlp ? ` · yt-dlp ${state.ytDlp}` : ''}`
      : 'server.py is not reachable — direct URLs, HLS files and embeds still work';
  }

  async function boot() {
    document.body.appendChild(panel);

    // Probe a few origins: same-origin first, then the usual localhost ports.
    const candidates = ['', 'http://127.0.0.1:8080', 'http://localhost:8080'];
    // de-duplicate (when the page is already served from one of them)
    const unique = [...new Set(candidates)];
    let lastErr = null;
    for (const base of unique) {
      try {
        const h = await getJsonAt(base, '/api/health', 2500);
        if (h && h.ok) {
          state.base = base;
          state.online = true;
          state.ytDlp = h.ytDlpVersion;
          if (h.ytDlpAvailable === false) {
            statusChip('yt-dlp missing', false);
            note('server.py is reachable but yt-dlp is not installed — '
                 + 'run: pip install -U yt-dlp, then restart server.py');
          } else {
            statusChip(`yt-dlp ${h.ytDlpVersion || 'online'}`, true);
          }
          break;
        }
      } catch (err) {
        lastErr = err;
      }
    }
    if (!state.online) {
      state.online = false;
      statusChip('backend offline', false);
      if (lastErr) console.info('[viewer] backend probe failed:', lastErr.message || lastErr);
    }

    // Register last: app.js checks this array only when it needs help.
    window.ViewerResolvers = window.ViewerResolvers || [];
    if (state.online) window.ViewerResolvers.push(resolver);
  }

  function note(msg) {
    const el = document.getElementById('notices');
    if (el) { el.hidden = false; el.textContent = msg; }
    console.info('[viewer] ' + msg);
  }

  boot();
})();

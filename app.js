/* Viewer — play a video from any link.
 *
 * This file is browser-only: it can play direct media URLs, HLS streams and
 * embedded players all by itself. When a backend (server.py + yt-dlp) is
 * running, window.ViewerResolvers gets one more entry registered by ytdlp.js,
 * which lets it also play YouTube / Vimeo / Twitch / … links.
 */
(function () {
  'use strict';

  /* ------------------------------------------------------------------ *
   *  Elements
   * ------------------------------------------------------------------ */
  const form       = document.getElementById('urlForm');
  const urlInput   = document.getElementById('urlInput');
  const fileInput  = document.getElementById('fileInput');
  const pipBtn     = document.getElementById('pipBtn');
  const helpBtn    = document.getElementById('helpBtn');
  const help       = document.getElementById('help');
  const closeHelp  = document.getElementById('closeHelp');
  const stage      = document.getElementById('stage');
  const dropzone   = document.getElementById('dropzone');
  const video      = document.getElementById('video');
  const embed      = document.getElementById('embed');
  const errBox     = document.getElementById('errors');
  const metaEl     = document.getElementById('meta');
  const srcEl      = document.getElementById('src');
  const statusEl   = document.getElementById('status');

  /* ------------------------------------------------------------------ *
   *  Link classification
   * ------------------------------------------------------------------ */
  const DIRECT_RE = /\.(mp4|m4v|webm|ogv|ogg|mov|mkv|avi|mpeg|mpg|ts|mp3|m4a|aac|wav|flac)(\?|#|$)/i;
  const HLS_RE    = /\.(m3u8)(\?|#|$)/i;
  const EMBED_RE  = /^https?:\/\/(?:www\.|m\.)?(youtube\.com\/(?:watch\?v=|embed\/|live\/|shorts\/)|youtu\.be\/|youtube-nocookie\.com\/(?:watch\?v=|embed\/)|vimeo\.com\/(?:video\/)?|player\.vimeo\.com\/video\/|dailymotion\.com\/video\/|dai\.ly\/|tiktok\.com\/)/i;

  const kindOf = (url) => {
    if (!url) return 'native';
    if (HLS_RE.test(url)) return 'hls';
    if (DIRECT_RE.test(url)) return 'direct';
    if (EMBED_RE.test(url)) return 'embed';
    return 'unknown';
  };

  function embedUrlOf(url) {
    const yt = url.match(/(?:youtube\.com\/(?:watch\?v=|embed\/|live\/|shorts\/)|youtu\.be\/)([\w-]{6,})/);
    if (yt) {
      const t = (url.match(/[?&]t=(?:(\d+)h)?(?:(\d+)m)?(\d+)?s?/) || [])
        .slice(1).reduce((a, b, i) => a + (Number(b) || 0) * [3600, 60, 1][i], 0);
      const p = new URLSearchParams({ rel: '0', modestbranding: '1', playsinline: '1' });
      if (t) p.set('start', t);
      return `https://www.youtube-nocookie.com/embed/${yt[1]}?${p}`;
    }
    const vm = url.match(/vimeo\.com\/(?:video\/)?(\d+)/);
    if (vm) return `https://player.vimeo.com/video/${vm[1]}?dnt=1`;

    const dm = url.match(/dailymotion\.com\/video\/([^_?/]+)|dai\.ly\/([^_?/]+)/);
    if (dm) return `https://www.dailymotion.com/embed/video/${dm[1] || dm[2]}`;

    const tk = url.match(/tiktok\.com\/@[\w.]+\/video\/(\d+)/);
    if (tk) return `https://www.tiktok.com/embed/v2/${tk[1]}`;

    return url; // last resort: iframe it
  }

  const prettyName = (url) => {
    try {
      const p = new URL(url, location.href);
      return decodeURIComponent(p.pathname.split('/').filter(Boolean).pop() || p.hostname);
    } catch (_) { return url; }
  };

  const fmtTime = (s) => {
    if (!isFinite(s) || s < 0) s = 0;
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
    return (h ? h + ':' + String(m).padStart(2, '0') : String(m)) + ':' + String(sec).padStart(2, '0');
  };

  /* ------------------------------------------------------------------ *
   *  State
   * ------------------------------------------------------------------ */
  let hls = null;            // active hls.js instance
  let currentUrl = '';       // page/stream URL currently loaded
  let currentKind = 'file'; // native | hls | embed | file
  let queue = [], queueIndex = -1;
  let captionsOn = true;
  let lastAttempt = null;    // {src, proxied} — for the direct→proxy retry
  let retried = false;
  let currentFmtId = '';   // backend format id on screen, so downloads match it
  const history = [];

  const HISTORY_KEY = 'viewer.recent';
  try {
    (JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]') || []).forEach((u) => history.push(u));
  } catch (_) { /* private mode */ }

  /* ------------------------------------------------------------------ *
   *  UI helpers
   * ------------------------------------------------------------------ */
  function error(msg) {
    errBox.hidden = false;
    errBox.textContent = msg;
    // The phone is the only place this fires, so a server-side copy of what it
    // saw turns "still can't play" into a one-line diagnosis. Fire-and-forget,
    // never allowed to break the error box itself (jsdom may lack fetch).
    try {
      if (typeof fetch === 'function') {
        fetch(`/api/ui-log?msg=${encodeURIComponent(String(msg).slice(0, 200))}`)
          .catch(() => {});
      }
    } catch (_) { /* report path must never become the error */ }
  }
  /* Failure with the escape hatch attached: the explanation plus a way out that
     does not depend on our extractor working. */
  function errorWithSite(msg) {
    errBox.hidden = false;
    errBox.textContent = '';
    const span = document.createElement('span');
    span.textContent = msg;
    errBox.appendChild(span);
    if (typeof currentUrl === 'string' && /^https?:/i.test(currentUrl)) {
      errBox.appendChild(window.ViewerSiteLink(currentUrl));
    }
  }
  const clearError = () => { errBox.hidden = true; errBox.textContent = ''; };

  function status(text) {
    metaEl.textContent = text;
    statusEl.classList.toggle('playing',
      text === 'Playing' || text.startsWith('Playing'));
  }

  function setDocumentTitle(t) { document.title = t; }

  /* ------------------------------------------------------------------ *
   *  Player controls
   *
   *  Seek bar, skip buttons and a "jump to time" field, because keyboard
   *  shortcuts alone are no way to land on 3:21 of a 15 minute video.
   * ------------------------------------------------------------------ */
  const ctl = {
    root: document.getElementById('controls'),
    wrap: document.getElementById('seekWrap'),
    played: document.getElementById('seekPlayed'),
    buffer: document.getElementById('seekBuffer'),
    knob: document.getElementById('seekKnob'),
    seek: document.getElementById('seek'),
    tip: document.getElementById('seekTip'),
    play: document.getElementById('ctlPlay'),
    back: document.getElementById('ctlBack'),
    fwd: document.getElementById('ctlFwd'),
    time: document.getElementById('ctlTime'),
    timeIn: document.getElementById('ctlTimeInput'),
    mute: document.getElementById('ctlMute'),
    vol: document.getElementById('ctlVolume'),
    rate: document.getElementById('ctlRate'),
    cc: document.getElementById('ctlCC'),
    dl: document.getElementById('ctlDl'),
    site: document.getElementById('ctlSite'),
    pip: document.getElementById('ctlPip'),
    full: document.getElementById('ctlFull'),
  };

  const ICON = {
    play: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
    pause: '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>',
    vol: '<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4z"/></svg>',
    mute: '<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm18.5 1.6L20 9.1l-2.6 2.6L14.8 9 13.4 10.4l2.6 2.6L13.4 15.6 15 17.2l2.6-2.6L20 17.2l1.4-1.4-2.6-2.6 2.7-2.6z"/></svg>',
    full: '<svg viewBox="0 0 24 24"><path d="M4 9V4h5v2H6v3H4zm11-5h5v5h-2V6h-3V4zM4 15h2v3h3v2H4v-5zm14 0h2v5h-5v-2h3v-3z"/></svg>',
    unfull: '<svg viewBox="0 0 24 24"><path d="M9 4v3H6v2H4V4h5zm6 0h5v5h-2V6h-3V4zM4 15h2v3h3v2H4v-5zm14 0h2v5h-5v-2h3v-3z"/></svg>',
  };

  const VOL_KEY = 'viewer.volume';
  const SKIP = 10;
  let scrubbing = false;
  let ctlTimer = null;

  const clock = (s) => {
    if (!isFinite(s) || s < 0) s = 0;
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
    return (h ? h + ':' + String(m).padStart(2, '0') : String(m)) + ':' + String(sec).padStart(2, '0');
  };

  const duration = () => (isFinite(video.duration) && video.duration > 0 ? video.duration : 0);

  function bufferedEnd() {
    try {
      const b = video.buffered;
      for (let i = 0; i < b.length; i++) {
        if (b.start(i) <= video.currentTime + 0.25) return b.end(i);
      }
      return b.length ? b.end(b.length - 1) : 0;
    } catch (_) { return 0; }
  }

  function seekTo(seconds, keepState = true) {
    const total = duration();
    if (!total) return;
    const target = Math.max(0, Math.min(total - 0.15, seconds));
    try { video.currentTime = target; } catch (_) { /* not seekable yet */ }
    syncControls();
    if (keepState && video.paused) status('Paused');
  }

  /** "3:21", "1:54", "1:02:03" or "194" → seconds. */
  function parseClock(text) {
    const raw = String(text || '').trim().replace(',', '.');
    if (!raw) return null;
    if (/^\d+(\.\d+)?$/.test(raw)) return parseFloat(raw);
    if (!/^\d{1,2}(:\d{1,2}){1,2}(\.\d+)?$/.test(raw)) return null;
    const parts = raw.split(':').map(parseFloat);
    if (parts.some((n) => !isFinite(n))) return null;
    return parts.reduce((acc, n) => acc * 60 + n, 0);
  }

  function syncControls() {
    if (!ctl.root || ctl.root.hidden) return;
    const total = duration();
    const now = video.currentTime || 0;
    const editing = !ctl.timeIn.hidden;

    if (!scrubbing && !editing) {
      if (total) ctl.seek.value = String(now);
      const pct = total ? Math.min(100, (now / total) * 100) : 0;
      ctl.played.style.width = pct + '%';
      ctl.knob.style.left = pct + '%';
      ctl.time.textContent = clock(now) + ' / ' + (total ? clock(total) : '–:––');
    }
    ctl.buffer.style.width = (total ? Math.min(100, (bufferedEnd() / total) * 100) : 0) + '%';
    ctl.seek.disabled = !total;
    if (total) ctl.seek.max = String(total);

    ctl.play.innerHTML = video.paused ? ICON.play : ICON.pause;
    ctl.play.classList.toggle('on', !video.paused);
    ctl.mute.innerHTML = (video.muted || !video.volume) ? ICON.mute : ICON.vol;
    ctl.mute.classList.toggle('on', video.muted);
    ctl.vol.value = String(video.muted ? 0 : video.volume);
    if (ctl.rate.value !== String(video.playbackRate)) {
      ctl.rate.value = String([...ctl.rate.options].some((o) => +o.value === video.playbackRate)
        ? video.playbackRate : 1);
    }
    const tracks = video.textTracks.length;
    ctl.cc.disabled = !tracks;
    ctl.cc.classList.toggle('on', captionsOn && tracks > 0);
    ctl.pip.disabled = !document.pictureInPictureEnabled;
    ctl.dl.disabled = !downloadUrl();
    ctl.dl.title = ctl.dl.disabled
      ? 'Downloading needs the backend (it names the file and adds the CDN headers)'
      : 'Download this file';
    ctl.full.innerHTML = (document.fullscreenElement || document.webkitFullscreenElement)
      ? ICON.unfull : ICON.full;
  }

  /** The backend download URL for what is playing, or null without a backend. */
  function downloadUrl() {
    if (!currentUrl || !/^https?:/i.test(currentUrl)) return null;
    if (window.ViewerDownloadUrl) {
      // currentFmtId is set when the backend resolved this link, so the file we
      // save matches the quality on screen rather than a different default.
      return window.ViewerDownloadUrl(currentUrl, currentFmtId);
    }
    // Same origin and no bridge: the page is being served by server.py itself.
    const fmt = currentFmtId ? `&fmt=${encodeURIComponent(currentFmtId)}` : '';
    return location.protocol + '//' + location.host
      + '/api/play?url=' + encodeURIComponent(currentUrl) + '&dl=1' + fmt;
  }

  async function downloadCurrent() {
    const url = downloadUrl();
    if (!url) return error('Nothing to download for this source.');
    clearError();
    status('Preparing download…');
    // Ask first, so a "no single file here" answer becomes a message instead of
    // a browser tab full of JSON.
    try {
      const res = await fetch(url, {
        method: 'HEAD', cache: 'no-store',
        headers: window.ViewerAuthHeaders ? window.ViewerAuthHeaders() : {},
      });
      const type = res.headers.get('Content-Type') || '';
      if (!res.ok || type.includes('json')) {
        let msg = `HTTP ${res.status}`;
        try { msg = (await res.json()).error || msg; } catch (_) { /* html error page */ }
        return error('Download not available: ' + msg);
      }
      const a = document.createElement('a');
      a.href = url;
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      a.remove();
      status('Downloading…');
      wakeControls();
    } catch (err) {
      error('Download failed: ' + ((err && err.message) || err));
    }
  }

  function wakeControls() {
    if (!ctl.root || ctl.root.hidden) return;
    ctl.root.classList.remove('idle');
    clearTimeout(ctlTimer);
    if (!video.paused) ctlTimer = setTimeout(() => ctl.root.classList.add('idle'), 2600);
  }

  function showControls() {
    if (!ctl.root) return;
    ctl.root.hidden = false;
    ctl.play.innerHTML = ICON.play;
    ctl.mute.innerHTML = ICON.vol;
    ctl.full.innerHTML = ICON.full;
    syncControls();
    wakeControls();
  }

  function hideControls() {
    if (!ctl.root) return;
    clearTimeout(ctlTimer);
    ctl.root.hidden = true;
    ctl.root.classList.remove('idle');
  }

  /** Seek from a pointer event on the bar (click or drag). */
  function seekFromPointer(ev) {
    const total = duration();
    if (!total) return;
    const rect = ctl.seek.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
    seekTo(ratio * total);
  }

  function previewTime(ev) {
    const total = duration();
    if (!total) return;
    const rect = ctl.seek.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
    ctl.tip.hidden = false;
    ctl.tip.textContent = clock(ratio * total);
    ctl.tip.style.left = Math.round(rect.width * ratio) + 'px';
  }

  function openTimeField() {
    const total = duration();
    if (!total) return;
    ctl.timeIn.value = clock(video.currentTime || 0);
    ctl.timeIn.hidden = false;
    ctl.time.hidden = true;
    ctl.timeIn.focus();
    ctl.timeIn.select();
  }

  function closeTimeField(commit) {
    if (ctl.timeIn.hidden) return;
    const text = ctl.timeIn.value;
    ctl.timeIn.hidden = true;
    ctl.time.hidden = false;
    if (commit) {
      const seconds = parseClock(text);
      if (seconds === null) error(`"${text}" is not a time — try 3:21, 1:54 or 194`);
      else { clearError(); seekTo(seconds); }
    }
    ctl.time.focus();
    syncControls();
  }

  function wireControls() {
    if (!ctl.root) return;

    ctl.play.addEventListener('click', () => togglePlay());
    ctl.back.addEventListener('click', () => { seekTo(video.currentTime - SKIP); wakeControls(); });
    ctl.fwd.addEventListener('click', () => { seekTo(video.currentTime + SKIP); wakeControls(); });
    ctl.time.addEventListener('click', openTimeField);
    ctl.timeIn.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); closeTimeField(true); }
      else if (e.key === 'Escape') { e.preventDefault(); closeTimeField(false); }
    });
    ctl.timeIn.addEventListener('blur', () => closeTimeField(true));

    // Seek bar: pointer drag + keyboard, with live readout while scrubbing.
    ctl.seek.addEventListener('pointerdown', (e) => {
      scrubbing = true;
      ctl.wrap.classList.add('live');
      ctl.seek.setPointerCapture(e.pointerId);
      seekFromPointer(e);
    });
    ctl.seek.addEventListener('input', () => {
      scrubbing = true;
      const total = duration();
      const now = +ctl.seek.value;
      if (total) {
        ctl.played.style.width = Math.min(100, (now / total) * 100) + '%';
        ctl.knob.style.left = Math.min(100, (now / total) * 100) + '%';
        ctl.time.textContent = clock(now) + ' / ' + clock(total);
      }
    });
    const endScrub = () => {
      if (!scrubbing) return;
      scrubbing = false;
      ctl.wrap.classList.remove('live');
      seekTo(+ctl.seek.value);
    };
    ctl.seek.addEventListener('change', endScrub);
    ctl.seek.addEventListener('pointerup', endScrub);
    ctl.seek.addEventListener('pointercancel', endScrub);
    ctl.seek.addEventListener('keydown', (e) => {
      const jump = { ArrowLeft: -5, ArrowRight: 5, ArrowDown: -10, ArrowUp: 10 }[e.key];
      if (jump === undefined) return;
      e.preventDefault();
      seekTo(video.currentTime + jump);
    });
    ctl.wrap.addEventListener('mousemove', previewTime);
    ctl.wrap.addEventListener('mouseleave', () => { ctl.tip.hidden = true; });
    ctl.wrap.addEventListener('pointerdown', (e) => {
      if (e.target === ctl.seek) return;      // the range handles its own drags
      seekFromPointer(e);
    });

    ctl.mute.addEventListener('click', () => { video.muted = !video.muted; wakeControls(); });
    ctl.vol.addEventListener('input', () => {
      video.volume = +ctl.vol.value;
      video.muted = video.volume === 0;
      try { localStorage.setItem(VOL_KEY, String(video.volume)); } catch (_) {}
      wakeControls();
    });
    ctl.rate.addEventListener('change', () => {
      video.playbackRate = +ctl.rate.value;
      status('Speed ' + video.playbackRate + '×');
      wakeControls();
    });
    ctl.cc.addEventListener('click', () => {
      captionsOn = !captionsOn;
      applyCaptionState();
      wakeControls();
    });
    ctl.dl.addEventListener('click', downloadCurrent);
    if (ctl.site) {
      ctl.site.addEventListener('click', () => {
        if (!currentUrl || !/^https?:/i.test(currentUrl)) {
          return status('No link to open — paste one first.');
        }
        const a = window.ViewerSiteLink(currentUrl);
        document.body.appendChild(a);
        a.click();
        a.remove();
      });
    }
    ctl.pip.addEventListener('click', () => pipBtn.click());
    ctl.full.addEventListener('click', toggleFullscreen);

    // Keep the bar in step with the element.
    const events = ['timeupdate', 'progress', 'durationchange', 'loadedmetadata',
                    'play', 'playing', 'pause', 'seeked', 'seeking', 'volumechange',
                    'ratechange', 'waiting', 'ended', 'emptied'];
    events.forEach((name) => video.addEventListener(name, () => {
      if (name === 'play' || name === 'playing' || name === 'pause' || name === 'ended') {
        wakeControls();
      }
      syncControls();
    }));
    document.addEventListener('fullscreenchange', syncControls);
    document.addEventListener('webkitfullscreenchange', syncControls);

    // Any interaction brings the bar back.
    ['mousemove', 'touchstart', 'pointerdown', 'keydown'].forEach((name) =>
      stage.addEventListener(name, wakeControls, { passive: true }));
    ctl.root.addEventListener('pointerenter', () => clearTimeout(ctlTimer));
    ctl.root.addEventListener('pointerleave', wakeControls);

    // Restore the last volume.
    try {
      const saved = parseFloat(localStorage.getItem(VOL_KEY) || '');
      if (isFinite(saved)) { video.volume = Math.max(0, Math.min(1, saved)); video.muted = !saved; }
    } catch (_) { /* private mode */ }
  }

  function togglePlay() {
    if (!video.currentSrc && !video.src) return;
    if (video.paused) video.play().catch(() => status('Paused — press ▶ to start'));
    else video.pause();
    wakeControls();
  }

  function validateUrl(raw) {
    const u = String(raw || '').trim();
    if (!u) return null;
    if (/^(blob:|data:)/.test(u) || /^[./]/.test(u)) return u;
    try {
      const p = new URL(u);
      if (p.protocol !== 'http:' && p.protocol !== 'https:') return null;
      return u;
    } catch (_) { return null; }
  }

  /* ------------------------------------------------------------------ *
   *  Loading
   * ------------------------------------------------------------------ */
  function destroyHls() {
    if (hls) { try { hls.destroy(); } catch (_) {} hls = null; }
  }

  function play(url) {
    const clean = validateUrl(url);
    if (!clean) {
      return error('That is not a valid http(s) link. Example: https://example.com/video.mp4');
    }
    currentUrl = clean;
    clearError();
    urlInput.value = clean;
    srcEl.textContent = prettyName(clean);
    setQueueIfNeeded(clean);
    remember(clean);
    status('Loading…');

    const kind = kindOf(clean);

    // 1. Ask any backend (yt-dlp) first when the browser cannot play it alone.
    const needsHelp = kind === 'embed' || kind === 'unknown';
    if (needsHelp) {
      resolve(clean).then((resolved) => {
        if (!resolved) {                       // no backend / not extractable
          if (kind === 'embed') return playEmbed(clean);
          // The backend is up and already explained itself in the error box:
          // trying to play the page URL as media only produces a bogus
          // "codec error" on top of a real, already-reported reason.
          if (window.ViewerLastFailure) return errorWithSite(window.ViewerLastFailure);
          return playNative(clean);
        }
        playResolved(resolved);
      }).catch((err) => {
        error('Resolving failed: ' + err.message);
        if (kind === 'embed') playEmbed(clean);
      });
      return;
    }

    if (kind === 'hls') return playHls(clean);
    return playNative(clean);
  }

  /* Run every registered resolver until one answers. */
  function resolve(url) {
    const resolvers = window.ViewerResolvers || [];
    if (!resolvers.length) return Promise.resolve(null);
    return resolvers.reduce(
      (chain, r) => chain.then((found) => found || Promise.resolve(r(url)).catch(() => null)),
      Promise.resolve(null)
    );
  }

  function playNative(src) {
    destroyHls();
    showMedia();
    currentKind = 'native';
    currentFmtId = '';           // raw media: the backend never picked a format
    lastAttempt = { src, proxied: false };
    retried = false;
    video.src = src;
    setDocumentTitle(prettyName(src) + ' — Viewer');
    tryAutoplay();
  }

  function playHls(src) {
    destroyHls();
    showMedia();
    video.poster = '';
    currentKind = 'hls';
    currentFmtId = '';           // raw HLS, no backend format id to reuse
    lastAttempt = { src, proxied: false };
    retried = false;

    if (!video.canPlayType('application/vnd.apple.mpegurl')) {
      if (!window.Hls || !window.Hls.isSupported()) {
        // No MSE in this build and canPlayType claims no native HLS. But
        // canPlayType lies on Android WebView and some Chromium builds, so
        // throwing the URL away is worse than trying: hand it to the element
        // and let the real error events speak.
        status('No hls.js here — trying the browser player directly…');
        video.src = src;
        setDocumentTitle('HLS — ' + prettyName(src));
        tryAutoplay();
        return;
      }
      hls = new window.Hls({ lowLatencyMode: true, backBufferLength: 90 });
      hls.on(window.Hls.Events.ERROR, (_e, d) => {
        if (!d.fatal) return;
        if (d.type === window.Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
        else if (d.type === window.Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        else {
          destroyHls();
          error('HLS stream failed: ' + (d.details || 'unknown error'));
        }
      });
      hls.loadSource(src);
      hls.attachMedia(video);
    } else {
      video.src = src;
    }
    setDocumentTitle('HLS — ' + prettyName(src));
    tryAutoplay();
  }

  function playEmbed(src) {
    destroyHls();
    video.pause();
    video.removeAttribute('src');
    video.load();
    video.hidden = true;
    dropzone.hidden = true;
    embed.hidden = false;
    embed.src = embedUrlOf(src);
    currentKind = 'embed';
    hideControls();                 // the iframe brings its own controls
    setDocumentTitle('Embed — ' + prettyName(src));
    clearError();
    status('Embedded player · start server.py for the direct yt-dlp stream');
  }

  function playResolved(resolved) {
    const { stream, meta } = resolved;
    const title = (meta && meta.title) || prettyName(currentUrl);
    currentFmtId = (meta && meta.formatId) || '';

    clearError();
    srcEl.textContent = title;
    setDocumentTitle(title + ' — Viewer');

    destroyHls();
    showMedia();
    // Switching quality re-applies this: drop the previous captions/poster first.
    video.querySelectorAll('track').forEach((t) => t.remove());
    video.removeAttribute('poster');
    (meta && meta.subtitles || []).forEach((s) => addTrack(s.url, s.label));

    if (stream.kind === 'hls') {
      return playHls(stream.proxied && stream.proxyUrl ? stream.proxyUrl : stream.url);
    }
    if (stream.kind === 'merge') {
      // A single stream with both tracks, remuxed on the fly by yt-dlp+ffmpeg.
      currentKind = 'native';
      lastAttempt = null;
      video.src = stream.proxyUrl;
      status('Streaming · video + audio merged');
      tryAutoplay();
      return;
    }

    currentKind = 'native';
    lastAttempt = { src: stream.url, proxyUrl: stream.proxyUrl, proxied: !!stream.proxied };
    retried = false;
    video.src = stream.proxied ? stream.proxyUrl : stream.url;
    video.poster = (meta && meta.thumbnail) || '';
    status('Playing · yt-dlp');
    tryAutoplay();
  }

  /* Exposed for ytdlp.js: re-apply a different format of the same link. */
  window.ViewerApply = playResolved;
  /* Exposed for ytdlp.js: jump straight to another link (playlist entries). */
  window.ViewerPlay = play;
  /* Exposed for ytdlp.js and the error paths: a new-tab link to the pasted page,
     built once so target/rel can never regress to an unsafe default. */
  window.ViewerSiteLink = function (url) {
    const a = document.createElement('a');
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.className = 'site-link';
    a.textContent = 'open on the original site ↗';
    return a;
  };

  function showMedia() {
    dropzone.hidden = true;
    video.hidden = false;
    embed.hidden = true;
    embed.removeAttribute('src');
    showControls();
  }

  function addTrack(url, label) {
    if (!url) return;
    const track = document.createElement('track');
    track.kind = 'captions';
    track.label = label || 'Captions';
    track.srclang = 'en';
    track.src = url;
    track.addEventListener('error', () => track.remove());
    video.appendChild(track);
    applyCaptionState();
  }

  function applyCaptionState() {
    for (let i = 0; i < video.textTracks.length; i++) {
      video.textTracks[i].mode = captionsOn ? 'showing' : 'disabled';
    }
  }

  function tryAutoplay() {
    const p = video.play();
    if (p && p.catch) p.catch(() => status('Paused — press ▶ to start'));
  }

  function trySidecarSubs() {
    if (currentKind !== 'native' || !currentUrl) return;
    const m = currentUrl.match(/\.(mp4|m4v|webm|mov|mkv)(\?|#|$)/i);
    if (!m) return;
    addTrack(currentUrl.slice(0, m.index) + '.vtt', 'Auto .vtt');
  }

  function remember(url) {
    const i = history.indexOf(url);
    if (i !== -1) history.splice(i, 1);
    history.unshift(url);
    if (history.length > 60) history.length = 60;
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); } catch (_) {}
  }

  /* ------------------------------------------------------------------ *
   *  Playlist: one URL per line, ?url=…&url=…
   * ------------------------------------------------------------------ */
  function setQueueIfNeeded(url) {
    const lines = urlInput.value.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    if (lines.length > 1 && url === lines[0]) {
      queue = lines;
      queueIndex = 0;
    } else {
      queueIndex = queue.indexOf(url);
    }
  }
  function playNext() {
    if (queue.length <= 1) return status('Nothing next');
    queueIndex = (queueIndex + 1) % queue.length;
    play(queue[queueIndex]);
  }
  function playPrev() {
    if (queue.length <= 1) return status('Nothing previous');
    queueIndex = (queueIndex - 1 + queue.length) % queue.length;
    play(queue[queueIndex]);
  }

  /* ------------------------------------------------------------------ *
   *  Local files
   * ------------------------------------------------------------------ */
  function loadLocal(file) {
    destroyHls();
    showMedia();
    currentKind = 'file';
    currentUrl = file.name;
    currentFmtId = '';
    video.src = URL.createObjectURL(file);
    srcEl.textContent = file.name;
    setDocumentTitle(file.name + ' — Viewer');
    clearError();
    lastAttempt = null;
    status('Loading local file…');
    tryAutoplay();
  }

  /* ------------------------------------------------------------------ *
   *  Wiring
   * ------------------------------------------------------------------ */
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const lines = urlInput.value.split(/\s+/).map((s) => s.trim()).filter(Boolean);
    if (!lines.length) { urlInput.focus(); return; }
    queue = lines;
    queueIndex = 0;
    play(lines[0]);
  });

  fileInput.addEventListener('change', () => {
    const f = fileInput.files && fileInput.files[0];
    if (f) loadLocal(f);
    fileInput.value = '';
  });

  document.querySelectorAll('.ex').forEach((b) => {
    b.addEventListener('click', () => {
      urlInput.value = b.dataset.url;
      form.requestSubmit();
    });
  });

  /* drag & drop */
  let dragCounter = 0;
  window.addEventListener('dragenter', (e) => {
    e.preventDefault();
    if (++dragCounter === 1) document.body.classList.add('dragging');
  });
  window.addEventListener('dragleave', () => {
    if (--dragCounter <= 0) { dragCounter = 0; document.body.classList.remove('dragging'); }
  });
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    dragCounter = 0;
    document.body.classList.remove('dragging');

    const dt = e.dataTransfer;
    if (!dt) return;
    const file = dt.files && dt.files[0];
    if (file && (file.type.startsWith('video/') || DIRECT_RE.test(file.name))) {
      return loadLocal(file);
    }
    const text = dt.getData('text/uri-list') || dt.getData('text/plain');
    if (text) {
      urlInput.value = text.trim();
      form.requestSubmit();
    }
  });
  window.addEventListener('dragover', (e) => e.preventDefault());

  pipBtn.addEventListener('click', async () => {
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else if (currentKind !== 'embed') await video.requestPictureInPicture();
    } catch (err) { error('Picture-in-picture blocked: ' + err.message); }
  });

  helpBtn.addEventListener('click', () => help.showModal());
  closeHelp.addEventListener('click', () => help.close());

  // Touching the picture while the bar is hidden should *bring back the bar*,
  // not pause the video. Only a tap with the controls already on screen toggles
  // playback (on desktop the bar is kept awake by mousemove, so nothing changes).
  let barWasIdle = false;
  video.addEventListener('pointerdown', () => {
    barWasIdle = !!(ctl.root && !ctl.root.hidden && ctl.root.classList.contains('idle'));
  });
  video.addEventListener('click', (e) => {
    if (e.target !== video) return;
    if (barWasIdle) { barWasIdle = false; wakeControls(); return; }
    if (video.paused) video.play().catch(() => {}); else video.pause();
  });
  video.addEventListener('dblclick', toggleFullscreen);
  video.addEventListener('webkitbeginfullscreen', syncControls);
  video.addEventListener('webkitendfullscreen', syncControls);

  function toggleFullscreen() {
    const doc = document;
    const el = doc.fullscreenElement || doc.webkitFullscreenElement;
    if (el) {
      if (doc.exitFullscreen) doc.exitFullscreen();
      else if (doc.webkitExitFullscreen) doc.webkitExitFullscreen();
      return;
    }
    if (stage.requestFullscreen) { stage.requestFullscreen().catch(() => {}); return; }
    if (stage.webkitRequestFullscreen) { stage.webkitRequestFullscreen(); return; }
    // iOS Safari: only <video> can go fullscreen, and only via this vendor call.
    if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
  }

  /* ------------------------------------------------------------------ *
   *  Video events
   * ------------------------------------------------------------------ */
  video.addEventListener('loadedmetadata', () => {
    const bits = [];
    if (video.videoWidth) bits.push(`${video.videoWidth}×${video.videoHeight}`);
    if (isFinite(video.duration)) bits.push(fmtTime(video.duration));
    if (bits.length) setMeta(bits.join(' · '));
    if (!video.paused) status('Playing');
    trySidecarSubs();
  });

  video.addEventListener('playing', () => status('Playing'));
  video.addEventListener('pause', () => { status('Paused'); reportPosition(true); });
  video.addEventListener('play', () => reportPosition(true));
  video.addEventListener('waiting', () => status('Buffering…'));
  video.addEventListener('seeking', flash);
  video.addEventListener('seeked', () => { statusIfPlaying(); reportPosition(true); });

  function statusIfPlaying() { if (!video.paused) status('Playing'); }

  // Tell the backend where we are, throttled. This is what makes the cache
  // window follow the playhead instead of the furthest prefetched byte.
  const POS_EVERY = 4000;
  let posSentAt = 0;
  function reportPosition(force) {
    if (typeof window.ViewerReportPosition !== 'function') return;
    if (!currentUrl || !currentFmtId) return;      // raw URL or a local file
    if (currentFmtId === '__merge__') return;       // stitched: not a cacheable file
    const now = Date.now();
    if (!force && now - posSentAt < POS_EVERY) return;
    posSentAt = now;
    const dur = isFinite(video.duration) ? video.duration : 0;
    window.ViewerReportPosition(currentUrl, currentFmtId, video.currentTime, dur,
                                 video.paused);
  }

  video.addEventListener('timeupdate', () => {
    if (statusEl.classList.contains('playing') || video.paused) {
      setMeta([fmtTime(video.currentTime),
        isFinite(video.duration) ? '/ ' + fmtTime(video.duration) : '',
        video.playbackRate === 1 ? '' : video.playbackRate + '×'].filter(Boolean).join('  '));
    }
    reportPosition(false);
  });

  const ERRORS = {
    1: 'Playback was aborted.',
    2: 'Network error while loading this video.',
    3: 'The file is encoded in a format this browser cannot decode.',
    4: 'Could not play this link — it may need to go through the backend proxy, have expired, or be blocked by its host.'
  };

  video.addEventListener('error', () => {
    const code = video.error && video.error.code;

    // One retry through the yt-dlp proxy: CDNs often refuse the browser's direct request.
    if (lastAttempt && !retried && lastAttempt.proxyUrl && !lastAttempt.proxied && code !== 1) {
      retried = true;
      lastAttempt.proxied = true;
      status('Retrying through the backend proxy…');
      video.src = lastAttempt.proxyUrl;
      tryAutoplay();
      return;
    }

    error(ERRORS[code] || 'Playback failed (' + (code || 'unknown') + ').');
    status('Failed');
  });

  /* ------------------------------------------------------------------ *
   *  Keyboard
   * ------------------------------------------------------------------ */
  const SEEKABLE_TARGET = 'INPUT, TEXTAREA, [contenteditable="true"]';
  window.addEventListener('keydown', (e) => {
    const t = e.target;
    const typing = t instanceof HTMLElement && t.matches(SEEKABLE_TARGET);
    if (help.open) { if (e.key === 'Escape') help.close(); return; }
    if (typing) { if (e.key === 'Escape' && t === urlInput) t.blur(); return; }
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    const k = e.key;
    wakeControls();
    if (currentKind === 'embed') {
      if (k === 'f' || k === 'F') toggleFullscreen();
      if (k === 'o' || k === 'O') { urlInput.focus(); urlInput.select(); }
      return;
    }

    switch (k) {
      case ' ': case 'k': case 'K':
        e.preventDefault();
        togglePlay();
        return;
      case 'ArrowRight': seekBy(5); return;
      case 'ArrowLeft':  seekBy(-5); return;
      case 'j': case 'J': seekBy(10); return;
      case 'l': case 'L': seekBy(30); return;
      case 'ArrowUp':
        e.preventDefault();
        video.muted = false;
        video.volume = Math.min(1, video.volume + 0.05);
        return status('Volume ' + Math.round(video.volume * 100) + '%');
      case 'ArrowDown':
        e.preventDefault();
        video.volume = Math.max(0, video.volume - 0.05);
        return status('Volume ' + Math.round(video.volume * 100) + '%');
      case 'm': case 'M': video.muted = !video.muted; return status(video.muted ? 'Muted' : 'Volume ' + Math.round(video.volume * 100) + '%');
      case 'f': case 'F': toggleFullscreen(); return;
      case 'p': case 'P': pipBtn.click(); return;
      case 'n': case 'N': playNext(); return;
      case 'b': case 'B': playPrev(); return;
      case 'o': case 'O': urlInput.focus(); urlInput.select(); return;
      case 'c': case 'C': captionsOn = !captionsOn; applyCaptionState(); return status(captionsOn ? 'Captions on' : 'Captions off');
      case '>': case '.': video.playbackRate = Math.min(4, video.playbackRate + 0.25); return status('Speed ' + video.playbackRate + '×');
      case '<': case ',': video.playbackRate = Math.max(0.25, video.playbackRate - 0.25); return status('Speed ' + video.playbackRate + '×');
      default:
        if (/^[0-9]$/.test(k) && video.duration && isFinite(video.duration)) {
          video.currentTime = video.duration * (Number(k) / 10);
        }
    }
  });

  function seekBy(seconds) {
    if (!video.seekable.length && !isFinite(video.duration)) return;
    const end = video.duration || video.seekable.end(video.seekable.length - 1);
    seekTo(video.currentTime + seconds);
    status(Math.round(video.currentTime) + ' / ' + (isFinite(end) ? Math.round(end) : '?') + 's');
    wakeControls();
  }

  let flashTimer;
  function flash() {
    document.body.classList.add('seeking');
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => document.body.classList.remove('seeking'), 130);
  }
  function setMeta(text) { metaEl.textContent = text; }

  /* ------------------------------------------------------------------ *
   *  Boot: ?url=… / #url=…, with or without backend
   * ------------------------------------------------------------------ */
  function boot() {
    wireControls();
    hideControls();

    const urls = [];
    new URLSearchParams(location.search).getAll('url')
      .concat(location.hash.slice(1) ? [location.hash.slice(1)] : [])
      .map((u) => { try { return decodeURIComponent(u); } catch (_) { return u; } })
      .forEach((u) => u && urls.push(u));

    if (urls.length) {
      urlInput.value = urls.join('\n');
      queue = urls;
      queueIndex = 0;
      play(urls[0]);
    } else {
      status('Ready');
      urlInput.focus();
    }
  }
  boot();
})();

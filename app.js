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
  }
  const clearError = () => { errBox.hidden = true; errBox.textContent = ''; };

  function status(text) {
    metaEl.textContent = text;
    statusEl.classList.toggle('playing',
      text === 'Playing' || text.startsWith('Playing'));
  }

  function setDocumentTitle(t) { document.title = t; }

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
    lastAttempt = { src, proxied: false };
    retried = false;

    if (!video.canPlayType('application/vnd.apple.mpegurl')) {
      if (!window.Hls || !window.Hls.isSupported()) {
        return error('This browser cannot play HLS natively and hls.js did not load (offline?).');
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
    setDocumentTitle('Embed — ' + prettyName(src));
    clearError();
    status('Embedded player · start server.py for the direct yt-dlp stream');
  }

  function playResolved(resolved) {
    const { stream, meta } = resolved;
    const title = (meta && meta.title) || prettyName(currentUrl);

    clearError();
    srcEl.textContent = title;
    setDocumentTitle(title + ' — Viewer');

    if (stream.kind === 'hls') return playHls(stream.url);
    if (stream.kind === 'merge') {
      // A single stream with both tracks, remuxed on the fly by yt-dlp+ffmpeg.
      destroyHls();
      showMedia();
      currentKind = 'native';
      lastAttempt = null;
      video.src = stream.proxyUrl;
      status('Streaming · video + audio merged');
      tryAutoplay();
      return;
    }

    destroyHls();
    showMedia();
    currentKind = 'native';

    // Captions, if the backend found any.
    (meta && meta.subtitles || []).forEach((s) => addTrack(s.url, s.label));

    lastAttempt = { src: stream.url, proxyUrl: stream.proxyUrl, proxied: !!stream.proxied };
    retried = false;
    video.src = stream.proxied ? stream.proxyUrl : stream.url;
    video.poster = (meta && meta.thumbnail) || '';
    status('Playing · yt-dlp');
    tryAutoplay();
  }

  /* Exposed for ytdlp.js: re-apply a different format of the same link. */
  window.ViewerApply = playResolved;

  function showMedia() {
    dropzone.hidden = true;
    video.hidden = false;
    embed.hidden = true;
    embed.removeAttribute('src');
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

  video.addEventListener('click', (e) => {
    if (e.target !== video) return;
    if (video.paused) video.play().catch(() => {}); else video.pause();
  });
  video.addEventListener('dblclick', toggleFullscreen);

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else stage.requestFullscreen && stage.requestFullscreen();
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
  video.addEventListener('pause', () => status('Paused'));
  video.addEventListener('waiting', () => status('Buffering…'));
  video.addEventListener('seeking', flash);
  video.addEventListener('seeked', statusIfPlaying);

  function statusIfPlaying() { if (!video.paused) status('Playing'); }

  video.addEventListener('timeupdate', () => {
    if (statusEl.classList.contains('playing') || video.paused) {
      setMeta([fmtTime(video.currentTime),
        isFinite(video.duration) ? '/ ' + fmtTime(video.duration) : '',
        video.playbackRate === 1 ? '' : video.playbackRate + '×'].filter(Boolean).join('  '));
    }
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
    if (currentKind === 'embed') {
      if (k === 'f' || k === 'F') toggleFullscreen();
      if (k === 'o' || k === 'O') { urlInput.focus(); urlInput.select(); }
      return;
    }

    switch (k) {
      case ' ': case 'k': case 'K':
        e.preventDefault();
        if (video.paused) video.play().catch(() => {}); else video.pause();
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
    video.currentTime = Math.max(0, Math.min(end, video.currentTime + seconds));
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

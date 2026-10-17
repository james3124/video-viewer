/* Integration test: the real app.js + ytdlp.js against a live backend.
 *
 *   node tools/integration_check.js [url]
 *
 * ui_check.js proves app.js's player logic in isolation. This proves the two
 * files actually agree with each other and with the backend: the frontend must
 * end up with <video src> pointing at /api/play?...&proxy=1&fmt=… . A mismatch
 * between the JSON the backend sends and the JS that reads it shows up here and
 * nowhere else — the backend tests pass and the video still never plays.
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.join(__dirname, '..');
const read = (f) => fs.readFileSync(path.join(ROOT, f), 'utf8');
const BASE = process.env.VIEWER_BASE || 'http://127.0.0.1:8080';
const TARGET = process.argv[2]
  || 'https://watchhentai.net/videos/kotowarenai-haha-episode-2-id-01/';

let pass = 0;
const fail = [];
function check(name, cond, extra = '') {
  if (cond) { pass++; console.log(`ok   ${name}${extra ? '  — ' + extra : ''}`); }
  else { fail.push(name); console.log(`FAIL ${name}${extra ? '  — ' + extra : ''}`); }
}

const dom = new JSDOM(read('index.html'), {
  url: BASE + '/',
  pretendToBeVisual: true,
  runScripts: 'outside-only',
});
const { window } = dom;

// jsdom has no media stack and no fetch: give it just enough.
const noop = () => {};
Object.defineProperty(window.HTMLMediaElement.prototype, 'play', { value: () => Promise.resolve(), writable: true });
Object.defineProperty(window.HTMLMediaElement.prototype, 'pause', { value: noop, writable: true });
Object.defineProperty(window.HTMLMediaElement.prototype, 'load', { value: noop, writable: true });
Object.defineProperty(window.HTMLMediaElement.prototype, 'buffered', {
  value: { length: 1, start: () => 0, end: () => 40 }, configurable: true,
});
Object.defineProperty(window.HTMLMediaElement.prototype, 'canPlayType', { value: () => '' });
for (const [prop, val] of [['duration', 600], ['currentTime', 0], ['volume', 1], ['playbackRate', 1]]) {
  Object.defineProperty(window.HTMLMediaElement.prototype, prop, { value: val, writable: true, configurable: true });
}

// Every request the page makes is logged, so a silent "played the raw page URL"
// failure is visible instead of invisible.
const seen = [];
window.fetch = (input, init) => {
  const url = typeof input === 'string' && input.startsWith('/') ? BASE + input : input;
  seen.push(String(url));
  const opts = init ? { ...init } : undefined;
  if (opts && opts.signal) delete opts.signal;   // jsdom's AbortSignal is not Node's
  return fetch(url, opts);
};
window.Hls = undefined;

// config.js, then app.js, then ytdlp.js — the same order index.html uses.
for (const file of ['config.js', 'app.js', 'ytdlp.js']) {
  try {
    new window.Function(read(file)).call(window);
  } catch (exc) {
    console.log(`${file} threw during boot: ${exc.message}`);
    fail.push(`${file} boot`);
  }
}

const $ = (id) => window.document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  // ytdlp.js probes the backend on load; give it a moment to register itself.
  await sleep(1500);
  check('the backend bridge registered a resolver',
    Array.isArray(window.ViewerResolvers) && window.ViewerResolvers.length > 0,
    `${(window.ViewerResolvers || []).length} resolver(s)`);

  seen.length = 0;
  await window.ViewerPlay(TARGET);
  // A cold extraction on a hard site takes tens of seconds (yt-dlp's whole
  // ladder, then the adapters). Poll like a browser would instead of guessing.
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    if ($('video').getAttribute('src') || window.ViewerLastFailure) break;
    await sleep(500);
  }
  await sleep(500);

  const video = $('video');
  const src = video.getAttribute('src') || '';

  check('the backend was asked to resolve the link',
    seen.some((u) => u.includes('/api/info')), seen.find((u) => u.includes('/api/info')) || 'none');
  check('the video element was given a src', !!src, src.slice(0, 90) || '(empty)');
  check('playback is routed through the backend proxy, not the raw page',
    src.includes('/api/play') && src.includes('proxy=1'), src.slice(0, 110));
  check('the chosen quality is carried in the request',
    /[?&]fmt=[^&]+/.test(src), (src.match(/[?&]fmt=[^&]+/) || ['none'])[0]);
  check('the raw page URL is never the media itself',
    !/\/api\/play\?url=[^&]*$/.test(src) && !/^https?:\/\//.test(src),
    src.slice(0, 90));
  check('the quality row offered more than one choice',
    window.document.querySelectorAll('#ytdlp .yt-fmt').length >= 2,
    `${window.document.querySelectorAll('#ytdlp .yt-fmt').length} buttons`);
  check('no error banner is showing',
    $('errors').hidden === true, ($('errors').textContent || '').slice(0, 80));
  check('the title reached the page',
    /Kotowarenai/i.test(window.document.title), window.document.title);

  // ---- the silent-failure case ------------------------------------------
  // When the backend answers "this is a list, nothing playable here", the old
  // code returned null without saying so, and app.js fell through to
  // video.src = <the html page>. That "plays" as nothing, with no error: the
  // link simply did not work. Lock that shut.
  const realFetch = window.fetch;
  window.fetch = (input, init) => {
    const url = typeof input === 'string' && input.startsWith('/') ? BASE + input : input;
    if (String(url).includes('/api/info')) {
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({
          ok: true, url: 'https://list.test/feed/', title: 'Some list',
          extractable: false, formats: [],
          playlist: { title: 'Some list', count: 2, entries: [
            { title: 'a', url: 'https://list.test/a' },
            { title: 'b', url: 'https://list.test/b' }] },
        }),
        text: () => Promise.resolve(''),
      });
    }
    return realFetch(input, init);
  };
  $('video').removeAttribute('src');
  await window.ViewerPlay('https://list.test/feed/');
  await sleep(600);
  const listSrc = $('video').getAttribute('src') || '';
  check('a playlist-only answer does not become the media src',
    !/list\.test/.test(listSrc), listSrc.slice(0, 80) || '(empty)');
  check('a playlist-only answer explains itself instead of failing silently',
    !!window.ViewerLastFailure, window.ViewerLastFailure || '(no message)');
  window.fetch = realFetch;

  console.log(`\n${pass} passed, ${fail.length} failed`);
  if (fail.length) {
    console.log('failed:', fail.join(', '));
    console.log('\nrequests the page made:');
    for (const u of seen) console.log('   ', u.slice(0, 120));
  }
  process.exit(fail.length ? 1 : 0);
})();

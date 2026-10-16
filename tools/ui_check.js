/* DOM test for the player controls — runs app.js in jsdom.
 *
 *   node --experimental-vm-modules tools/ui_check.js
 *
 * Needs jsdom:  npm install jsdom
 * Verifies that every element app.js reaches for exists, that the control bar
 * wires up, and that the seek / "jump to time" maths is right.
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.join(__dirname, '..');
const read = (f) => fs.readFileSync(path.join(ROOT, f), 'utf8');

let pass = 0;
const fail = [];
function check(name, cond, extra = '') {
  if (cond) { pass++; console.log(`ok   ${name}${extra ? '  — ' + extra : ''}`); }
  else { fail.push(name); console.log(`FAIL ${name}${extra ? '  — ' + extra : ''}`); }
}

const dom = new JSDOM(read('index.html'), {
  url: 'http://127.0.0.1:8080/',
  pretendToBeVisual: true,
  runScripts: 'outside-only',
});
const { window } = dom;

// jsdom has no media stack: stub just enough for app.js to boot.
const noop = () => {};
Object.defineProperty(window.HTMLMediaElement.prototype, 'play', { value: () => Promise.resolve(), writable: true });
Object.defineProperty(window.HTMLMediaElement.prototype, 'pause', { value: noop, writable: true });
Object.defineProperty(window.HTMLMediaElement.prototype, 'load', { value: noop, writable: true });
Object.defineProperty(window.HTMLMediaElement.prototype, 'buffered', {
  value: { length: 1, start: () => 0, end: () => 40 },
});
Object.defineProperty(window.HTMLMediaElement.prototype, 'seekable', {
  value: { length: 1, start: () => 0, end: () => 600 },
});
Object.defineProperty(window.HTMLMediaElement.prototype, 'canPlayType', { value: () => '' });
Object.defineProperty(window.HTMLMediaElement.prototype, 'playbackRate', {
  value: 1, writable: true, configurable: true,
});
Object.defineProperty(window.HTMLMediaElement.prototype, 'volume', {
  value: 1, writable: true, configurable: true,
});
Object.defineProperty(window.HTMLMediaElement.prototype, 'currentTime', {
  value: 0, writable: true, configurable: true,
});
Object.defineProperty(window.HTMLMediaElement.prototype, 'duration', {
  value: 600, writable: true, configurable: true,
});
window.Hls = undefined;

// Run app.js inside the jsdom window.
const script = new window.Function(read('app.js'));
try {
  script.call(window);
} catch (exc) {
  console.log('app.js threw during boot:', exc.message);
  fail.push('boot');
}

const $ = (id) => window.document.getElementById(id);

// 1 — every id app.js reaches for must exist in index.html
const ids = [...read('app.js').matchAll(/getElementById\('([^']+)'\)/g)].map((m) => m[1]);
const missing = [...new Set(ids)].filter((id) => !$(id));
check('every getElementById target exists in index.html', missing.length === 0, missing.join(', '));

// 2 — the control bar markup is present and starts hidden
check('control bar exists', !!$('controls'));
check('control bar starts hidden', $('controls').hidden === true);
check('seek range exists', !!$('seek') && $('seek').tagName === 'INPUT');
check('skip buttons exist', !!$('ctlBack') && !!$('ctlFwd'));
check('time field exists', !!$('ctlTime') && !!$('ctlTimeInput'));

// 3 — the video has no native controls attribute (we draw our own)
check('native controls attribute absent', $('video').getAttribute('controls') === null);

// 4 — jump-to-time parsing (the "3:21 / 1:54" feature)
const { parseClock, clock } = window.__viewerTest || {};
const parse = (s) => {
  // app.js keeps parseClock private; re-implement the same contract via the
  // element behaviour below, and assert the documented cases directly here.
  return null;
};

// 5 — drive the real UI: show a video, then check the bar syncs
const video = $('video');
video.src = 'https://example.test/a.mp4';
$('stage').dispatchEvent(new window.MouseEvent('mousemove', { bubbles: true }));
window.ViewerPlay('https://example.test/a.mp4');

setTimeout(() => {
  const controls = $('controls');
  check('bar is shown once a video is loaded', controls.hidden === false);
  check('play button has an icon', $('ctlPlay').innerHTML.includes('<svg'));
  check('time shows position and duration',
    /\d+:\d\d \/ 10:00/.test($('ctlTime').textContent), $('ctlTime').textContent);
  check('seek range got the duration as max', $('seek').max === '600', $('seek').max);

  // Seek by clicking the middle of the bar → ~half way.
  const rect = $('seek').getBoundingClientRect = () => ({ left: 0, width: 200, top: 0 });
  const at = (x) => ({ clientX: x, clientY: 10, bubbles: true, pointerId: 1 });
  $('seekWrap').dispatchEvent(new window.PointerEvent('pointerdown', at(100)));
  check('clicking mid-bar seeks to ~5:00', Math.round(video.currentTime) === 300,
    String(video.currentTime));
  $('seekWrap').dispatchEvent(new window.PointerEvent('pointerdown', at(200)));
  check('clicking the end clamps to the duration', Math.round(video.currentTime) === 600,
    String(video.currentTime));

  // Skip buttons.
  $('ctlBack').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('back button moves back 10 s', Math.round(video.currentTime) === 590,
    String(video.currentTime));
  $('ctlFwd').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('forward button moves on 10 s', Math.round(video.currentTime) === 600,
    String(video.currentTime));

  // Jump-to-time field: type 3:21.
  $('ctlTime').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('time field opens on click', $('ctlTimeInput').hidden === false);
  $('ctlTimeInput').value = '3:21';
  $('ctlTimeInput').dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  check('typing 3:21 jumps to 201 s', Math.round(video.currentTime) === 201,
    String(video.currentTime));
  check('time field closes after Enter', $('ctlTimeInput').hidden === true);

  $('ctlTime').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  $('ctlTimeInput').value = '1:54';
  $('ctlTimeInput').dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  check('typing 1:54 jumps to 114 s', Math.round(video.currentTime) === 114,
    String(video.currentTime));

  $('ctlTime').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  $('ctlTimeInput').value = '1:02:03';
  $('ctlTimeInput').dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  check('typing 1:02:03 jumps to 3723 s (clamped)', Math.round(video.currentTime) === 600,
    String(video.currentTime));

  $('ctlTime').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  $('ctlTimeInput').value = '194';
  $('ctlTimeInput').dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  check('typing plain seconds works too', Math.round(video.currentTime) === 194,
    String(video.currentTime));

  // Garbage input must be rejected, not seeked to NaN.
  $('ctlTime').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  $('ctlTimeInput').value = 'banana';
  $('ctlTimeInput').dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  check('garbage time is refused, position unchanged', Math.round(video.currentTime) === 194,
    String(video.currentTime));
  check('garbage time explains itself', $('errors').textContent.includes('banana'),
    $('errors').textContent.slice(0, 60));

  // Volume + speed + captions.
  $('ctlVolume').value = '0.4';
  $('ctlVolume').dispatchEvent(new window.Event('input', { bubbles: true }));
  check('volume slider sets the volume', Math.abs(video.volume - 0.4) < 0.01, String(video.volume));
  $('ctlRate').value = '1.5';
  $('ctlRate').dispatchEvent(new window.Event('change', { bubbles: true }));
  check('speed select sets playbackRate', video.playbackRate === 1.5, String(video.playbackRate));

  // Live timeline updates.
  video.currentTime = 60;
  video.dispatchEvent(new window.Event('timeupdate'));
  check('time display follows playback',
    $('ctlTime').textContent.startsWith('1:00 /'), $('ctlTime').textContent);

  console.log(`\n${pass} passed, ${fail.length} failed`);
  if (fail.length) console.log('failed:', fail.join(', '));
  process.exit(fail.length ? 1 : 0);
}, 60);
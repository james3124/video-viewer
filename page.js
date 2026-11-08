/* page.js — the marketing page around the player.
   The player itself (app.js / ytdlp.js) is untouched; everything here is
   page furniture: reveals, the panel carousel, the platform tabs, the FAQ
   index, and the sample button's hand-off to the player. */

(function () {
  'use strict';

  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ------------------------------------------------ yt-dlp panel placement */
  /* ytdlp.js creates #ytdlp and appends it to <body>. Inside the page it
     belongs under the player's try strip, not after the footer. */
  function placeYtdlp() {
    var slot = document.getElementById('ytdlpSlot');
    var panel = document.getElementById('ytdlp');
    if (slot && panel && panel.parentElement !== slot) slot.appendChild(panel);
  }
  placeYtdlp();
  document.addEventListener('DOMContentLoaded', placeYtdlp);
  window.addEventListener('load', placeYtdlp);

  /* --------------------------------------------------------- scroll reveals */
  (function reveals() {
    var els = Array.prototype.slice.call(document.querySelectorAll('[data-reveal]'));
    if (!els.length) { window.__viewerReady = true; return; }

    if (reduce || !('IntersectionObserver' in window)) {
      document.documentElement.classList.add('reveal-all');
      window.__viewerReady = true;
      return;
    }

    /* A small stagger inside each section, so a row of columns or a call and
       its response arrive in order instead of as one block. */
    var groups = new Map();
    els.forEach(function (el) {
      var key = el.closest('.section, .site-foot, figure') || document.body;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(el);
    });
    groups.forEach(function (arr) {
      arr.forEach(function (el, i) {
        el.style.transitionDelay = Math.min(i, 4) * 65 + 'ms';
      });
    });

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-in');
        io.unobserve(entry.target);
      });
    }, { threshold: 0.08, rootMargin: '0px 0px -8% 0px' });

    els.forEach(function (el) { io.observe(el); });
    window.__viewerReady = true;
  })();

  /* ------------------------------------------------------- panel carousel */
  (function carousel() {
    var row = document.querySelector('.show-row');
    if (!row) return;
    var panels = Array.prototype.slice.call(row.querySelectorAll('.show-panel'));
    if (!panels.length) return;

    var capName = document.querySelector('.cap-name');
    var capDesc = document.querySelector('.cap-desc');

    var CAPS = [
      ['Formats', 'Every rendition the source offers, with proxy or direct decided per row.'],
      ['Seek', 'Click the clock and type 3:21; the player seeks on the spot, five or ten seconds at a time.'],
      ['Playlist', 'A channel or a playlist arrives as a clickable list, not a single video.'],
      ['Subtitles', 'Sidecar subtitles and the source\u2019s own tracks, toggled with C.'],
      ['Download', 'The quality on screen is saved through the backend, with the headers the CDN wants.'],
      ['Keys', 'Space, arrows, J/K/L, 0\u20139, F and P: the shortcuts the player already has.'],
      ['Honest failures', 'When a stream will not play, the player says which wall it hit, not just an error.']
    ];

    function step() {
      var w = panels[0].getBoundingClientRect().width;
      var cs = window.getComputedStyle(row);
      var gap = parseFloat(cs.columnGap || cs.gap || '0') || 0;
      return w + gap;
    }

    var current = -1;
    function update() {
      var i = Math.round(row.scrollLeft / step());
      i = Math.max(0, Math.min(panels.length - 1, i));
      if (i === current) return;
      current = i;
      panels.forEach(function (p, j) {
        p.classList.toggle('is-active', j === i);
      });
      if (CAPS[i] && capName && capDesc) {
        capName.textContent = CAPS[i][0];
        capDesc.textContent = CAPS[i][1];
      }
    }

    var ticking = false;
    row.addEventListener('scroll', function () {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(function () {
        ticking = false;
        update();
      });
    }, { passive: true });

    row.addEventListener('keydown', function (e) {
      var d = 0;
      if (e.key === 'ArrowRight') d = 1;
      else if (e.key === 'ArrowLeft') d = -1;
      else if (e.key === 'Home') { e.preventDefault(); row.scrollTo({ left: 0, behavior: reduce ? 'auto' : 'smooth' }); return; }
      else if (e.key === 'End') { e.preventDefault(); row.scrollTo({ left: row.scrollWidth, behavior: reduce ? 'auto' : 'smooth' }); return; }
      else return;
      e.preventDefault();
      row.scrollBy({ left: d * step(), behavior: reduce ? 'auto' : 'smooth' });
    });

    Array.prototype.forEach.call(document.querySelectorAll('.show-arrows .arrow'), function (b) {
      b.addEventListener('click', function () {
        var dir = Number(b.getAttribute('data-dir')) || 1;
        row.scrollBy({ left: dir * step(), behavior: reduce ? 'auto' : 'smooth' });
      });
    });

    panels.forEach(function (p) { p.classList.remove('is-active'); });
    panels[0].classList.add('is-active');
    current = 0;
    if (capName && capDesc && CAPS[0]) {
      capName.textContent = CAPS[0][0];
      capDesc.textContent = CAPS[0][1];
    }
    update();

    /* A resize changes the panel width; re-read the active panel. */
    window.addEventListener('resize', function () { update(); }, { passive: true });
  })();

  /* ------------------------------------------------------- platform tabs */
  (function tabs() {
    var list = document.querySelector('.plat-tabs');
    if (!list) return;
    var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
    if (!tabs.length) return;

    function select(tab, moveFocus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.classList.toggle('is-active', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
        var panel = document.getElementById(t.getAttribute('aria-controls'));
        if (panel) panel.classList.toggle('is-active', on);
      });
      if (moveFocus) tab.focus();
    }

    tabs.forEach(function (tab, i) {
      tab.addEventListener('click', function () { select(tab, false); });
      tab.addEventListener('keydown', function (e) {
        var next = -1;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (i + 1) % tabs.length;
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === 'Home') next = 0;
        else if (e.key === 'End') next = tabs.length - 1;
        else return;
        e.preventDefault();
        select(tabs[next], true);
      });
    });
  })();

  /* --------------------------------------------------------------- the FAQ */
  (function faq() {
    var list = document.querySelector('.faq-list');
    if (!list) return;
    var items = Array.prototype.slice.call(list.querySelectorAll('.faq-item'));
    if (!items.length) return;
    var pane = document.getElementById('faqPane');
    var wide = window.matchMedia('(min-width: 900px)');

    function open(item) {
      if (!item) return;
      items.forEach(function (it) {
        var btn = it.querySelector('.faq-q');
        var on = it === item;
        it.classList.toggle('is-open', on);
        if (btn) btn.setAttribute('aria-expanded', on ? 'true' : 'false');
      });
      var answer = item.querySelector('.faq-inline p');
      if (pane && answer) pane.textContent = answer.textContent;
    }

    items.forEach(function (it) {
      var btn = it.querySelector('.faq-q');
      if (!btn) return;
      btn.addEventListener('click', function () {
        if (it.classList.contains('is-open')) return;
        open(it);
      });
    });

    open(list.querySelector('.faq-item.is-open') || items[0]);

    /* Only now do the answers move into the right-hand pane on wide screens;
       if this script never runs, every answer stays in the list. */
    document.documentElement.classList.add('faq-ready');

    if (wide.addEventListener) {
      wide.addEventListener('change', function () {
        open(list.querySelector('.faq-item.is-open') || items[0]);
      });
    }
  })();

  /* --------------------------------------------- sample button → the player */
  (function sample() {
    var link = document.getElementById('sampleBtn');
    if (!link) return;
    link.addEventListener('click', function (e) {
      var form = document.getElementById('urlForm');
      var input = document.getElementById('urlInput');
      var url = link.getAttribute('data-sample');
      if (!form || !input || !url) return; /* the anchor itself still works */

      e.preventDefault();
      input.value = url;

      var card = document.getElementById('player');
      if (card) {
        var box = card.getBoundingClientRect();
        if (box.top > window.innerHeight * 0.5 || box.bottom < window.innerHeight * 0.2) {
          card.scrollIntoView({ block: 'center', behavior: reduce ? 'auto' : 'smooth' });
        }
      }

      if (form.requestSubmit) form.requestSubmit();
      else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
  })();

})();

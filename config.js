/* Optional runtime config.
 *
 * Point the frontend at a backend that is NOT served from this same origin,
 * e.g. when the static page is on Vercel and server.py runs on Render/Railway:
 *
 *   window.VIEWER_BACKEND = 'https://your-viewer-backend.onrender.com';
 *
 * Leave it empty to use the same origin (server.py serving this page) and the
 * automatic localhost:8080 fallback. You can also override per-visit with
 * ?backend=https://… in the URL.
 */
window.VIEWER_BACKEND = window.VIEWER_BACKEND || '';

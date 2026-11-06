/* Optional runtime config — safe to edit, no rebuild needed.
 *
 * Backend URL: needed when the page is served statically (Vercel, GitHub Pages,
 * …) but the backend runs elsewhere (Render, Railway, Fly, a VPS). Empty means
 * "same origin", and the page still probes localhost:8080 as a fallback.
 *
 *   window.VIEWER_BACKEND = 'https://viewer-backend.onrender.com';
 *
 * Shared secret: only needed if the backend was started with VIEWER_TOKEN set.
 * The page also reads ?token=… from the URL and remembers the last one it used.
 *
 *   window.VIEWER_TOKEN = 'paste-the-token-here';
 *
 * Display ad slot id for the home screen ONLY (AdSense → Ads → by ad
 * unit → Display → get code). The id is public — it appears in the page
 * source anyway. Blank = nothing renders. Never rendered while watching a
 * pasted link.
 *
 *   window.VIEWER_AD_SLOT = '1234567890';
 */
window.VIEWER_BACKEND = window.VIEWER_BACKEND || '';
window.VIEWER_TOKEN = window.VIEWER_TOKEN || '';
window.VIEWER_AD_SLOT = window.VIEWER_AD_SLOT || '5020288074';

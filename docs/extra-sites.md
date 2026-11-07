# Extra-site adapter (`sites_extra.py`)

This is the declarative adapter for **100 hosts** that are neither in the
project's `_PLAYER_SITES` / `_API_SITES` tables nor matched by any of
yt-dlp 2026.10.26's 1,755 extractors. The host list was produced by a live
probe of 247 adult/anime domains on 2026-11-07; parked, default, cam-only and
redirect-mismatched hosts were excluded.

## How it works

`sites_extra.SITES` maps a host to a frozen `SiteRule`. `server.py` calls
`extra_site_extract()` in the normal resolver ladder (API → player → extra →
yt-dlp → sniff → universal), fetches the page once with a browser User-Agent,
and applies the rule:

| Strategy | What it reads |
| --- | --- |
| `auto` | Tries `meta`, `jsonld`, `html5`, `player`, a generic media scan, a scored `iframe`, then a Dean Edwards unpacker for obfuscated player configs. The default for JS-rendered hosts. |
| `direct` | A URL from `patterns` is itself the media. |
| `meta` | `og:video`, `og:video:url`, `twitter:player:stream`. |
| `jsonld` | `VideoObject.contentUrl` in JSON-LD. |
| `html5` | `<video src>` / `<source src>`. |
| `player` | JW Player / Video.js `sources` / `file` arrays. |
| `iframe` | Returns the best-scoring same-site/known-player frame as an `embed`. Social buttons, trackers and ad zones are rejected. |
| `regex` | Host-specific `patterns` over the page source. |
| `embed` | The first `patterns` capture is a player/hoster frame (e.g. aniworld's `/redirect/<id>`). |
| `api` | Reads an id/CSRF token from the page, calls a JSON API (`api` template), and collects media from the response. |

Formats returned by `extra_site_extract()` are marked `_sniffed`, carry the
page as `Referer`, and are admitted to the media proxy only if they stay on
the origin host/subdomains. A rule can widen that with
`allow_domains=(...)`; the default remains deny-by-default.

## Coverage

- **100 hosts** — 52 anime, 48 adult.
- Strategies: api 1, auto 96, embed 1, meta 1, player 1.
- Verification: BLOCKED_AUTO 2, LIVE_AUTO 83, VERIFIED 15.
- `animeworld.ac` is registered as a canonical alias of `animeworld.tv`
  (101 table entries for 100 sites).

**Status legend**

- **VERIFIED** — a live video page returned a media/player surface; the
  strategy was chosen from that page.
- **LIVE_AUTO** — the host serves a real page, but the video page was
  JS-rendered or the link discovery only reached a listing; `auto` probes all
  strategies at request time.
- **BLOCKED_AUTO** — the host is real but refused this datacenter's IP; the
  recipe runs from a residential network or with `VIEWER_COOKIES_B64`.

## Live check (2026-11-07)

The 15 hosts with a discovered video page were re-checked with
`tools/adapter_check.py --extra`: **11 PASS** (direct formats) and
**4 PASS_EMBED** (player frames), **0 BROKEN**. The remaining 85 hosts are
LIVE_AUTO/BLOCKED_AUTO and intentionally have no sample URL, so the checker
reports SKIP for them, never FAIL.

## The 100

| Host | Kind | Strategy | Status | Verified sample |
| --- | --- | --- | --- | --- |
| `hentaicity.com` | adult | `meta` | VERIFIED | [www.hentaicity.com/video/succubus-nightmare-3-game-preview](https://www.hentaicity.com/video/succubus-nightmare-3-game-preview-succubus-in-stockade-is-fucked-from-both-ends-6w803G4xeh3.html) |
| `hentaigasm.com` | adult | `auto` | LIVE_AUTO | [hentaigasm.com/hentai/new-hentai-release/](https://hentaigasm.com/hentai/new-hentai-release/) |
| `hentaihaven.com` | adult | `auto` | LIVE_AUTO | [hentaihaven.com/video/abandon-100-nuki-shinai-to-derarenai](https://hentaihaven.com/video/abandon-100-nuki-shinai-to-derarenai-fushigi-na-kyoushitsu-2/) |
| `hentaistream.com` | adult | `auto` | LIVE_AUTO | — |
| `hentaitube.tv` | adult | `auto` | LIVE_AUTO | — |
| `hqporner.com` | adult | `auto` | LIVE_AUTO | — |
| `hustler.com` | adult | `auto` | LIVE_AUTO | — |
| `jav321.com` | adult | `auto` | VERIFIED | [www.jav321.com/video/1aiav00055](https://www.jav321.com/video/1aiav00055) |
| `javbangers.com` | adult | `auto` | VERIFIED | [www.javbangers.com/video/116843/fc2-ppv-1579620-1](https://www.javbangers.com/video/116843/fc2-ppv-1579620-1) |
| `javbus.com` | adult | `auto` | LIVE_AUTO | — |
| `javcl.com` | adult | `auto` | LIVE_AUTO | — |
| `javcool.com` | adult | `auto` | LIVE_AUTO | — |
| `javdesu.com` | adult | `auto` | LIVE_AUTO | — |
| `javdna.com` | adult | `auto` | LIVE_AUTO | — |
| `javfinder.com` | adult | `auto` | LIVE_AUTO | — |
| `javfree.me` | adult | `auto` | LIVE_AUTO | — |
| `javfull.net` | adult | `auto` | LIVE_AUTO | — |
| `javfun.me` | adult | `auto` | LIVE_AUTO | — |
| `javguru.com` | adult | `auto` | LIVE_AUTO | — |
| `javhay.com` | adult | `auto` | LIVE_AUTO | — |
| `javhd.com` | adult | `auto` | LIVE_AUTO | — |
| `javhd.pro` | adult | `auto` | LIVE_AUTO | — |
| `javhihi.com` | adult | `auto` | LIVE_AUTO | — |
| `javkiss.com` | adult | `auto` | LIVE_AUTO | — |
| `javleak.com` | adult | `auto` | LIVE_AUTO | — |
| `javmix.tv` | adult | `auto` | VERIFIED | [javmix.tv/video/mx-1dldss00570/](https://javmix.tv/video/mx-1dldss00570/) |
| `javporn.com` | adult | `auto` | LIVE_AUTO | — |
| `javpro.net` | adult | `auto` | LIVE_AUTO | — |
| `javqq.com` | adult | `auto` | LIVE_AUTO | — |
| `javrave.com` | adult | `auto` | LIVE_AUTO | — |
| `javsex.net` | adult | `auto` | LIVE_AUTO | — |
| `javsin.com` | adult | `auto` | LIVE_AUTO | — |
| `javstream.com` | adult | `auto` | LIVE_AUTO | — |
| `javsub.co` | adult | `auto` | LIVE_AUTO | — |
| `javsubtitle.com` | adult | `auto` | LIVE_AUTO | — |
| `javtiful.com` | adult | `player` | VERIFIED | [javtiful.com/video/100183/1pondo-123025-001](https://javtiful.com/video/100183/1pondo-123025-001) |
| `javtube.com` | adult | `auto` | VERIFIED | [javtube.com/tokyotube/jav/natsuko.mishima/video2662/1/](https://javtube.com/tokyotube/jav/natsuko.mishima/video2662/1/) |
| `javzoo.com` | adult | `auto` | LIVE_AUTO | — |
| `missav.com` | adult | `auto` | LIVE_AUTO | — |
| `porn300.com` | adult | `auto` | VERIFIED | [www.porn300.com/video/anal-toy-play-babe/](https://www.porn300.com/video/anal-toy-play-babe/) |
| `porndig.com` | adult | `auto` | VERIFIED | [www.porndig.com/videos/242510/how-delightful-it-is-to-see-](https://www.porndig.com/videos/242510/how-delightful-it-is-to-see-this-milf-mounted-on-a-mechanical-toy.html) |
| `pornhat.com` | adult | `auto` | VERIFIED | [www.pornhat.com/video/a-nerdy-guy-gets-lucky-as-two-tattoo](https://www.pornhat.com/video/a-nerdy-guy-gets-lucky-as-two-tattooed-alt-teens-share-his-cock-in-an-ffm/) |
| `pornone.com` | adult | `auto` | LIVE_AUTO | — |
| `porntrex.com` | adult | `auto` | VERIFIED | [www.porntrex.com/video/1203665/the-rub-down](https://www.porntrex.com/video/1203665/the-rub-down) |
| `pornzog.com` | adult | `auto` | VERIFIED | [pornzog.com/video/27670017/julia-en-trio-au-cap-d-agde/](https://pornzog.com/video/27670017/julia-en-trio-au-cap-d-agde/) |
| `sexvid.xxx` | adult | `auto` | VERIFIED | [www.sexvid.xxx/blonde-gets-on-her-knees-and-tongues-this-b](https://www.sexvid.xxx/blonde-gets-on-her-knees-and-tongues-this-brunette-raw.html) |
| `supjav.com` | adult | `auto` | BLOCKED_AUTO | — |
| `xozilla.com` | adult | `auto` | VERIFIED | [www.xozilla.com/videos/188898/couple-fucks-at-the-beach-pu](https://www.xozilla.com/videos/188898/couple-fucks-at-the-beach-public-sex/) |
| `9anime.me` | anime | `auto` | LIVE_AUTO | — |
| `9anime.pl` | anime | `auto` | LIVE_AUTO | — |
| `9anime.to` | anime | `auto` | LIVE_AUTO | — |
| `anilibria.tv` | anime | `auto` | LIVE_AUTO | — |
| `anime4up.com` | anime | `auto` | LIVE_AUTO | — |
| `animedao.com` | anime | `auto` | LIVE_AUTO | — |
| `animedao.to` | anime | `auto` | LIVE_AUTO | — |
| `animefenix.com` | anime | `auto` | LIVE_AUTO | — |
| `animeflix.io` | anime | `auto` | LIVE_AUTO | — |
| `animeforce.com` | anime | `auto` | LIVE_AUTO | — |
| `animefreak.tv` | anime | `auto` | LIVE_AUTO | — |
| `animefrenzy.com` | anime | `auto` | LIVE_AUTO | — |
| `animeheaven.com` | anime | `auto` | LIVE_AUTO | — |
| `animeheaven.eu` | anime | `auto` | LIVE_AUTO | — |
| `animehub.cc` | anime | `auto` | LIVE_AUTO | — |
| `animehub.com` | anime | `auto` | LIVE_AUTO | — |
| `animeid.com` | anime | `auto` | LIVE_AUTO | — |
| `animekisa.lol` | anime | `auto` | LIVE_AUTO | — |
| `animekisa.vip` | anime | `auto` | LIVE_AUTO | — |
| `animepahe.com` | anime | `auto` | BLOCKED_AUTO | — |
| `animepahe.ru` | anime | `auto` | LIVE_AUTO | — |
| `animepahe.si` | anime | `auto` | LIVE_AUTO | — |
| `animesdrive.com` | anime | `auto` | LIVE_AUTO | — |
| `animesgames.com` | anime | `auto` | LIVE_AUTO | — |
| `animeslatino.com` | anime | `auto` | LIVE_AUTO | — |
| `animesonline.cc` | anime | `auto` | LIVE_AUTO | — |
| `animesonline.top` | anime | `auto` | LIVE_AUTO | — |
| `animesonlinecc.com` | anime | `auto` | LIVE_AUTO | — |
| `animesroll.com` | anime | `auto` | LIVE_AUTO | — |
| `animestream.com` | anime | `auto` | LIVE_AUTO | — |
| `animesuge.io` | anime | `auto` | LIVE_AUTO | — |
| `animesup.com` | anime | `auto` | LIVE_AUTO | — |
| `animeszone.net` | anime | `auto` | LIVE_AUTO | — |
| `animeultima.com` | anime | `auto` | LIVE_AUTO | — |
| `animeunity.tv` | anime | `auto` | LIVE_AUTO | [www.animeunity.co/anime/7869-cyberpunk-edgerunners-2-ita](https://www.animeunity.co/anime/7869-cyberpunk-edgerunners-2-ita) |
| `animevost.org` | anime | `auto` | LIVE_AUTO | — |
| `animeworld.one` | anime | `auto` | LIVE_AUTO | — |
| `animeworld.tv` | anime | `api` | VERIFIED | [www.animeworld.ac/play/firefly-wedding-ita.zRBGG/GWc422POz](https://www.animeworld.ac/play/firefly-wedding-ita.zRBGG/GWc422POzD) |
| `animexin.com` | anime | `auto` | LIVE_AUTO | — |
| `animeyt.com` | anime | `auto` | LIVE_AUTO | — |
| `anitaku.com` | anime | `auto` | LIVE_AUTO | — |
| `anitube.com` | anime | `auto` | LIVE_AUTO | — |
| `aniwatch.me` | anime | `auto` | LIVE_AUTO | — |
| `aniworld.to` | anime | `embed` | VERIFIED | [aniworld.to/anime/stream/ranma-12-2024/staffel-3/episode-6](https://aniworld.to/anime/stream/ranma-12-2024/staffel-3/episode-6) |
| `anix.to` | anime | `auto` | LIVE_AUTO | — |
| `anixplayer.com` | anime | `auto` | LIVE_AUTO | — |
| `gogoanime.tel` | anime | `auto` | LIVE_AUTO | — |
| `gogoanime.vc` | anime | `auto` | LIVE_AUTO | — |
| `gogoanime.wiki` | anime | `auto` | LIVE_AUTO | — |
| `gogoanimes.co` | anime | `auto` | LIVE_AUTO | — |
| `monoshare.com` | anime | `auto` | LIVE_AUTO | — |
| `zorox.to` | anime | `auto` | LIVE_AUTO | — |

## Adding a host

```python
# sites_extra.py
_r("example.com", "player", referer="https://example.com/",
   note="JW Player sources"),
```

Keep `SITES` alphabetical, use the narrowest strategy that works, and add a
sample URL to `tools/adapter_urls.txt` (`host<TAB>url`). `auto` is the right
default until a page has been inspected.

## Checking

```bash
.venv/bin/python tools/adapter_check.py --extra            # all 101 recipes
.venv/bin/python tools/adapter_check.py --only hentaicity.com
.venv/bin/python tools/backend_check.py                   # offline contract tests
```

`--extra` is deliberately separate from the default `--list` matrix, so the
GitHub Actions adapter matrix keeps its current size. Run the extra pass
manually (or in a weekly job) because 100 live fetches are slow and many hosts
are bot-walled from CI.

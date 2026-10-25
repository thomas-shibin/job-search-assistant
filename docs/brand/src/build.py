#!/usr/bin/env python3
"""Render Hirlumo social and wallpaper assets into docs/brand.

Every asset is an HTML page built from the same pieces as the product logo
(app/web/static/brand/logo.svg): the blue rounded-square "h" mark, the amber
sparkle and the Inter ExtraBold "hirlumo" wordmark. Pages are screenshotted
with headless Chrome, so the output matches what a browser draws.

    python3 docs/brand/src/build.py            # render everything
    python3 docs/brand/src/build.py youtube    # only names containing "youtube"

Needs google-chrome (or chromium) on PATH. Fonts are bundled in src/fonts.
"""
from __future__ import annotations

import math
import random
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
BRAND = SRC.parent
HTML_DIR = SRC / "html"

# ---- palette (from logo.svg and app/web/static/style.css) -------------------
BLUE_LIGHT = "#3a6ff0"   # mark gradient start
BLUE_DARK = "#1c3fae"    # mark gradient end
PRIMARY = "#2453d4"      # app primary (light theme)
PRIMARY_DARK_UI = "#4c7cf0"  # app primary (dark theme)
AMBER = "#ffb547"        # sparkle
NAVY = "#0f1b3d"         # wordmark on light backgrounds
NIGHT = "#0a1024"        # deepest background, near the app's dark --bg #0c1120
SOFT_BLUE_TEXT = "#a9c0ff"  # app --primary-text (dark theme)

TAGLINE = "Find jobs in Canada that fit you"
FEATURES = ["Company career sites", "Match scores", "Tailored resumes"]
URL = "hirlumo.com"

# ---- shared SVG pieces ------------------------------------------------------
H_PATH = "M21 13v38M21 35a11 11 0 0 1 22 0v16"
SPARK_PATH = ("M46 7.5c.9 4.6 2.9 6.6 7.5 7.5-4.6.9-6.6 2.9-7.5 7.5"
              "-.9-4.6-2.9-6.6-7.5-7.5 4.6-.9 6.6-2.9 7.5-7.5z")

DEFS = f"""
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <defs>
    <linearGradient id="hl-bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{BLUE_LIGHT}"/>
      <stop offset="1" stop-color="{BLUE_DARK}"/>
    </linearGradient>
    <symbol id="mark" viewBox="0 0 64 64">
      <rect width="64" height="64" rx="16" fill="url(#hl-bg)"/>
      <path d="{H_PATH}" fill="none" stroke="#fff" stroke-width="7"
            stroke-linecap="round" stroke-linejoin="round"/>
      <path d="{SPARK_PATH}" fill="{AMBER}"/>
    </symbol>
    <!-- the h and sparkle without the tile, cropped to their bounding box -->
    <symbol id="glyph" viewBox="17.5 7.5 36 47">
      <path d="{H_PATH}" fill="none" stroke="#fff" stroke-width="7"
            stroke-linecap="round" stroke-linejoin="round"/>
      <path d="{SPARK_PATH}" fill="{AMBER}"/>
    </symbol>
    <symbol id="spark" viewBox="38.5 7.5 15 15">
      <path d="{SPARK_PATH}"/>
    </symbol>
  </defs>
</svg>"""

FONT_CSS = """
@font-face { font-family: "Inter"; font-weight: 500; src: url("../fonts/inter-500.woff") format("woff"); }
@font-face { font-family: "Inter"; font-weight: 600; src: url("../fonts/inter-600.woff") format("woff"); }
@font-face { font-family: "Inter"; font-weight: 800; src: url("../fonts/inter-800.woff") format("woff"); }
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { overflow: hidden; }
body { font-family: "Inter", sans-serif; -webkit-font-smoothing: antialiased; }
.wordmark { font-weight: 800; letter-spacing: -0.036em; line-height: 1; color: #fff; }
"""


def page(w: int, h: int, body: str, css: str = "", transparent: bool = False) -> str:
    bg = "transparent" if transparent else NIGHT
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Hirlumo asset {w}x{h}</title>
<style>{FONT_CSS}
html, body {{ width: {w}px; height: {h}px; background: {bg}; }}
{css}</style></head>
<body>{DEFS}{body}</body></html>"""


# ---- backdrop: night sky, glow, sparkles, BC ridge --------------------------
def sparkles(w: int, h: int, n: int, seed: int, avoid: tuple[float, float, float, float] | None,
             scale: float = 1.0, max_y: float = 0.8) -> str:
    """Scatter small four-point sparkles, keeping clear of the `avoid` box (x0,y0,x1,y1)."""
    rnd = random.Random(seed)
    out, placed, tries = [], [], 0
    while len(placed) < n and tries < n * 60:
        tries += 1
        x, y = rnd.uniform(0.02, 0.98) * w, rnd.uniform(0.03, max_y) * h
        if avoid and avoid[0] <= x <= avoid[2] and avoid[1] <= y <= avoid[3]:
            continue
        if any(math.hypot(x - px, y - py) < 0.07 * min(w, h) for px, py in placed):
            continue
        placed.append((x, y))
        big = rnd.random() < 0.22
        size = (rnd.uniform(18, 30) if big else rnd.uniform(6, 13)) * scale
        color = AMBER if (big or rnd.random() < 0.35) else "#ffffff"
        op = rnd.uniform(0.55, 0.9) if big else rnd.uniform(0.18, 0.5)
        out.append(f'<svg class="sp" style="left:{x - size / 2:.1f}px;top:{y - size / 2:.1f}px;'
                   f'width:{size:.1f}px;height:{size:.1f}px;opacity:{op:.2f}" fill="{color}">'
                   f'<use href="#spark"/></svg>')
    return "".join(out)


def ridge(w: int, base: float, amp: float, seed: int, step: float) -> str:
    """A mountain ridge across the width: sharp peaks joined by shallow saddles."""
    rnd = random.Random(seed)
    pts, x = [(0.0, base - rnd.uniform(0.1, 0.4) * amp)], 0.0
    while x <= w:
        span = rnd.uniform(0.7, 1.3) * step
        peak = base - rnd.uniform(0.45, 1.0) * amp
        saddle = base - rnd.uniform(0.0, 0.25) * amp
        # a small shoulder on one flank keeps peaks from looking like a zig-zag
        sh = rnd.uniform(0.2, 0.35)
        pts += [(x + span * sh, peak + (saddle - peak) * 0.55), (x + span * 0.5, peak),
                (x + span * (1 - sh * 0.8), peak + (saddle - peak) * 0.4), (x + span, saddle)]
        x += span
    return "M0 9999 " + " ".join(f"L{px:.0f} {py:.0f}" for px, py in pts) + f" L{w} 9999 Z"


def backdrop(w: int, h: int, *, glow_x: float = 0.5, glow_y: float = 0.45, mountains: bool = True,
             sparkle_n: int = 18, avoid=None, seed: int = 7, ridge_top: float = 0.78,
             sparkle_scale: float = 1.0) -> tuple[str, str]:
    m = min(w, h)
    css = f"""
.bg {{ position:absolute; inset:0;
  background:
    radial-gradient({m * 0.95:.0f}px {m * 0.62:.0f}px at {glow_x * 100:.1f}% {glow_y * 100:.1f}%, rgba(58,111,240,.42), rgba(58,111,240,0) 70%),
    radial-gradient({m * 0.55:.0f}px {m * 0.4:.0f}px at {min(glow_x + 0.28, 0.95) * 100:.1f}% {max(glow_y - 0.22, 0.05) * 100:.1f}%, rgba(110,140,255,.10), rgba(110,140,255,0) 70%),
    linear-gradient(160deg, #0b1534 0%, {NIGHT} 55%, #0d1838 100%); }}
.sp {{ position:absolute; }}
.ridges {{ position:absolute; left:0; bottom:0; width:{w}px; height:{h}px; }}
"""
    html = '<div class="bg"></div>' + sparkles(w, h, sparkle_n, seed, avoid, sparkle_scale,
                                               max_y=ridge_top - 0.04)
    if mountains:
        far = ridge(w, h * (ridge_top + 0.08), h * 0.16, seed + 1, w / 7)
        mid = ridge(w, h * (ridge_top + 0.15), h * 0.11, seed + 2, w / 5)
        near = ridge(w, h * (ridge_top + 0.22), h * 0.07, seed + 3, w / 3.5)
        html += f"""
<svg class="ridges" viewBox="0 0 {w} {h}" preserveAspectRatio="none">
  <defs><linearGradient id="rg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#1b3170"/><stop offset="1" stop-color="#0d1838"/></linearGradient></defs>
  <path d="{far}" fill="url(#rg)" opacity=".55"/>
  <path d="{mid}" fill="#12214d" opacity=".9"/>
  <path d="{near}" fill="#0b1430"/>
</svg>"""
    return css, html


def lockup(mark_px: int, gap_ratio: float = 0.25, text_ratio: float = 0.66) -> tuple[str, str]:
    """Mark + wordmark, proportioned like logo.svg (64px tile, 42px text, 16px gap)."""
    css = f"""
.lockup {{ display:flex; align-items:center; gap:{mark_px * gap_ratio:.0f}px; }}
.lockup .m {{ width:{mark_px}px; height:{mark_px}px; flex:none;
  filter: drop-shadow(0 {mark_px * 0.1:.0f}px {mark_px * 0.28:.0f}px rgba(36,83,212,.55)); }}
.lockup .wordmark {{ font-size:{mark_px * text_ratio:.0f}px; transform: translateY(-{mark_px * 0.035:.1f}px); }}
"""
    html = '<div class="lockup"><svg class="m"><use href="#mark"/></svg><span class="wordmark">hirlumo</span></div>'
    return css, html


def chips(font_px: int) -> tuple[str, str]:
    css = f"""
.chips {{ display:flex; gap:{font_px * 0.7:.0f}px; justify-content:center; }}
.chip {{ font-weight:600; font-size:{font_px}px; color:#dbe5ff; padding:{font_px * 0.45:.0f}px {font_px * 0.95:.0f}px;
  border-radius:999px; background:rgba(76,124,240,.16); border:1px solid rgba(169,192,255,.28);
  display:flex; align-items:center; gap:{font_px * 0.5:.0f}px; white-space:nowrap; }}
.chip svg {{ width:{font_px * 0.75:.0f}px; height:{font_px * 0.75:.0f}px; fill:{AMBER}; }}
"""
    html = '<div class="chips">' + "".join(
        f'<span class="chip"><svg><use href="#spark"/></svg>{f}</span>' for f in FEATURES) + "</div>"
    return css, html


# ---- assets -----------------------------------------------------------------
def avatar(size: int) -> str:
    """Profile picture for platforms that crop to a circle (YouTube, Facebook).

    Full-bleed brand gradient; the h + sparkle sit inside the inscribed circle.
    """
    gh = size * 0.56
    gw = gh * 36 / 47
    css = f"""
.tile {{ position:absolute; inset:0;
  background: radial-gradient(circle at 30% 22%, #4b7ff5 0%, {BLUE_LIGHT} 30%, {BLUE_DARK} 100%); }}
.g {{ position:absolute; width:{gw:.1f}px; height:{gh:.1f}px;
  left:{(size - gw) / 2 + size * 0.008:.1f}px; top:{(size - gh) / 2 + size * 0.012:.1f}px;
  filter: drop-shadow(0 {size * 0.012:.0f}px {size * 0.03:.0f}px rgba(10,16,36,.30)); }}
"""
    return page(size, size, f'<div class="tile"></div><svg class="g"><use href="#glyph"/></svg>', css)


def app_icon(size: int) -> str:
    """Square, opaque app icon (Meta App Dashboard, stores). Same art as the avatar, a bit larger."""
    gh = size * 0.62
    gw = gh * 36 / 47
    css = f"""
.tile {{ position:absolute; inset:0; background: linear-gradient(135deg, {BLUE_LIGHT}, {BLUE_DARK}); }}
.g {{ position:absolute; width:{gw:.1f}px; height:{gh:.1f}px;
  left:{(size - gw) / 2 + size * 0.008:.1f}px; top:{(size - gh) / 2 + size * 0.012:.1f}px; }}
"""
    return page(size, size, f'<div class="tile"></div><svg class="g"><use href="#glyph"/></svg>', css)


def watermark(size: int) -> str:
    """YouTube video watermark: the tile mark on a transparent background."""
    css = f".m {{ position:absolute; left:{size * 0.04:.0f}px; top:{size * 0.04:.0f}px; width:{size * 0.92:.0f}px; height:{size * 0.92:.0f}px; }}"
    return page(size, size, '<svg class="m"><use href="#mark"/></svg>', css, transparent=True)


def hero(w: int, h: int, *, safe_w: int, safe_h: int, mark_px: int, tag_px: int, chip_px: int,
         show_url: bool, seed: int, ridge_top: float, cy: float = 0.5, glow_y: float | None = None,
         sparkle_n: int = 22, sparkle_scale: float = 1.0, debug: bool = False) -> str:
    """Centered lockup + tagline + chips over the night-sky backdrop.

    `safe_w` x `safe_h` is the area every crop of the image keeps; content stays inside it.
    """
    sx0, sy0 = (w - safe_w) / 2, h * cy - safe_h / 2
    pad = 0.06 * min(w, h)
    avoid = (sx0 - pad, sy0 - pad, sx0 + safe_w + pad, sy0 + safe_h + pad)
    bcss, bhtml = backdrop(w, h, glow_y=glow_y if glow_y is not None else cy, sparkle_n=sparkle_n,
                           avoid=avoid, seed=seed, ridge_top=ridge_top, sparkle_scale=sparkle_scale)
    lcss, lhtml = lockup(mark_px)
    ccss, chtml = chips(chip_px)
    css = bcss + lcss + ccss + f"""
.safe {{ position:absolute; left:{sx0:.0f}px; top:{sy0:.0f}px; width:{safe_w}px; height:{safe_h}px;
  display:flex; flex-direction:column; align-items:center; justify-content:center; gap:{tag_px * 0.75:.0f}px;
  {'outline:3px dashed #f0f;' if debug else ''} }}
.tag {{ font-weight:600; font-size:{tag_px}px; color:#e9efff; letter-spacing:-0.01em; }}
.tag b {{ color:{AMBER}; font-weight:600; }}
.url {{ position:absolute; right:{w * 0.03:.0f}px; bottom:{h * 0.04:.0f}px; font-weight:600;
  font-size:{chip_px * 0.9:.0f}px; color:{SOFT_BLUE_TEXT}; opacity:.85; }}
"""
    tag = TAGLINE.replace("Canada", "<b>Canada</b>")
    body = (bhtml + f'<div class="safe">{lhtml}<div class="tag">{tag}</div>{chtml}</div>'
            + (f'<div class="url">{URL}</div>' if show_url else ""))
    return page(w, h, body, css)


ASSETS: dict[str, tuple[str, int, int, bool]] = {}


def asset(path: str, w: int, h: int, html: str, transparent: bool = False) -> None:
    ASSETS[path] = (html, w, h, transparent)


# YouTube --------------------------------------------------------------------
asset("youtube/hirlumo-youtube-profile-800.png", 800, 800, avatar(800))
# Banner: 2560x1440 upload; 1546x423 centre is the area shown on every device.
asset("youtube/hirlumo-youtube-banner-2560x1440.png", 2560, 1440,
      hero(2560, 1440, safe_w=1546, safe_h=423, mark_px=150, tag_px=46, chip_px=26,
           show_url=False, seed=11, ridge_top=0.66, sparkle_n=26))
asset("youtube/hirlumo-youtube-watermark-150.png", 150, 150, watermark(150), transparent=True)

# Facebook -------------------------------------------------------------------
asset("facebook/hirlumo-facebook-profile-720.png", 720, 720, avatar(720))
# Cover: 1640x924 upload. Mobile shows it all (16:9); desktop shows the middle 1640x624.
# Content stays in a centred 1200x520 box so neither crop nor the profile photo touches it.
asset("facebook/hirlumo-facebook-cover-1640x924.png", 1640, 924,
      hero(1640, 924, safe_w=1200, safe_h=520, mark_px=124, tag_px=40, chip_px=23,
           show_url=False, seed=23, ridge_top=0.70, sparkle_n=18, sparkle_scale=0.9))
# Meta for Developers > App Dashboard > Settings > Basic > App icon (512-1024 px, square).
asset("facebook/hirlumo-meta-app-icon-1024.png", 1024, 1024, app_icon(1024))

# Wallpapers -----------------------------------------------------------------
asset("wallpaper/hirlumo-wallpaper-3840x2160.png", 3840, 2160,
      hero(3840, 2160, safe_w=2400, safe_h=760, mark_px=230, tag_px=70, chip_px=40,
           show_url=True, seed=5, ridge_top=0.70, cy=0.42, sparkle_n=34, sparkle_scale=1.5))
asset("wallpaper/hirlumo-wallpaper-1920x1080.png", 1920, 1080,
      hero(1920, 1080, safe_w=1200, safe_h=380, mark_px=115, tag_px=35, chip_px=20,
           show_url=True, seed=5, ridge_top=0.70, cy=0.42, sparkle_n=24, sparkle_scale=0.85))


# Instagram ------------------------------------------------------------------
# Posts are 1080x1350 (4:5). The profile grid shows a 3:4 crop (about 1012 px of the
# width), so text keeps at least 90 px from the left and right edges.
IG_W, IG_H = 1080, 1350


def ig_slide(n: int, total: int, body: str, css: str = "", *, seed: int, mountains: bool = True,
             glow_y: float = 0.35) -> str:
    bcss, bhtml = backdrop(IG_W, IG_H, glow_y=glow_y, sparkle_n=10, avoid=(60, 160, 1020, 1120),
                           seed=seed, ridge_top=0.80, mountains=mountains, sparkle_scale=0.9)
    lcss, lhtml = lockup(54)
    base = f"""
.top {{ position:absolute; left:96px; right:96px; top:72px; display:flex; align-items:center;
  justify-content:space-between; }}
.count {{ font-weight:600; font-size:26px; color:{SOFT_BLUE_TEXT}; letter-spacing:.04em; }}
.content {{ position:absolute; left:96px; right:96px; top:200px; }}
.eyebrow {{ display:inline-flex; align-items:center; gap:12px; font-weight:600; font-size:28px;
  color:{AMBER}; letter-spacing:.02em; margin-bottom:26px; }}
.eyebrow svg {{ width:24px; height:24px; fill:{AMBER}; }}
h1 {{ font-weight:800; font-size:84px; line-height:1.04; letter-spacing:-0.035em; color:#fff; }}
h1 b {{ color:{AMBER}; font-weight:800; }}
.lead {{ margin-top:30px; font-weight:500; font-size:34px; line-height:1.38; color:#d5e0ff; }}
.card {{ margin-top:44px; background:#ffffff; border-radius:28px; padding:34px 38px; color:{NAVY};
  box-shadow: 0 30px 70px rgba(4,8,24,.55); }}
.foot {{ position:absolute; left:96px; right:96px; bottom:70px; display:flex; justify-content:space-between;
  align-items:center; font-weight:600; font-size:28px; color:#e9efff; }}
.foot .url {{ color:{SOFT_BLUE_TEXT}; }}
"""
    spark = '<svg><use href="#spark"/></svg>'
    head = f'<div class="top">{lhtml}<span class="count">{n} / {total}</span></div>'
    foot = (f'<div class="foot"><span class="url">{URL}</span>'
            f'<span>{"Swipe" if n < total else "Link in bio"} &rarr;</span></div>')
    return page(IG_W, IG_H, bhtml + head + body.replace("{spark}", spark) + foot,
                bcss + lcss + base + css)


IG_TOTAL = 5

asset("instagram/hirlumo-instagram-profile-1080.png", 1080, 1080, avatar(1080))

asset("instagram/first-post/01-hello.png", IG_W, IG_H, ig_slide(1, IG_TOTAL, """
<div class="content" style="top:300px">
  <div class="eyebrow">{spark} Meet Hirlumo</div>
  <h1>Job hunting in <b>Canada</b>,<br>made lighter.</h1>
  <p class="lead">Find current jobs, check your resume the Canadian way, and apply with a resume that fits the job.</p>
</div>""", seed=36, glow_y=0.42))

asset("instagram/first-post/02-search.png", IG_W, IG_H, ig_slide(2, IG_TOTAL, """
<div class="content">
  <div class="eyebrow">{spark} Search the way you talk</div>
  <h1>Type what you want.<br>We do the filters.</h1>
  <div class="card">
    <div class="search"><svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/></svg>
      <span>remote HR jobs in Toronto over 80k</span></div>
    <div class="filters"><span>HR</span><span>Toronto</span><span>Remote</span><span>$80k+</span></div>
  </div>
  <ul class="ticks">
    <li>Jobs straight from company careers sites</li>
    <li>Old ads hidden, the same job shown once</li>
  </ul>
</div>""", """
.search { display:flex; align-items:center; gap:18px; font-weight:600; font-size:33px; padding:22px 26px;
  border:3px solid #2453d4; border-radius:18px; }
.search svg { width:36px; height:36px; fill:none; stroke:#2453d4; stroke-width:2.6; stroke-linecap:round; flex:none; }
.filters { display:flex; gap:14px; margin-top:24px; flex-wrap:wrap; }
.filters span { font-weight:600; font-size:27px; color:#1d3f9e; background:#e8eefc; padding:10px 22px; border-radius:999px; }
.ticks { list-style:none; margin-top:44px; }
.ticks li { position:relative; padding-left:52px; font-weight:500; font-size:33px; line-height:1.35; color:#e9efff; margin-bottom:18px; }
.ticks li::before { content:""; position:absolute; left:0; top:9px; width:30px; height:30px; border-radius:50%;
  background:#ffb547 url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M6 12.5l4 4 8-9' fill='none' stroke='%230f1b3d' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/></svg>") center/20px no-repeat; }
""", seed=32))

asset("instagram/first-post/03-resume-check.png", IG_W, IG_H, ig_slide(3, IG_TOTAL, """
<div class="content">
  <div class="eyebrow">{spark} Free, no account needed</div>
  <h1>Is your resume<br><b>Canada-ready?</b></h1>
  <p class="lead">Upload a PDF or Word file. Get two scores: Canada-ready and ATS readability.</p>
  <div class="card">
    <div class="row"><span class="x">&times;</span><span>Photo on the resume</span><em>Remove</em></div>
    <div class="row"><span class="x">&times;</span><span>Date of birth</span><em>Remove</em></div>
    <div class="row"><span class="x">&times;</span><span>03/2019</span><em>Mar 2019</em></div>
    <div class="row"><span class="x">&times;</span><span>Contact details in a table</span><em>Fix</em></div>
  </div>
</div>""", """
.card { padding:16px 38px; }
.row { display:flex; align-items:center; gap:22px; font-weight:600; font-size:31px; padding:20px 0;
  border-bottom:2px solid #edf1fb; }
.row:last-child { border-bottom:0; }
.row span:nth-child(2) { flex:1; }
.x { width:40px; height:40px; border-radius:50%; background:#fde8e8; color:#c62828; display:flex;
  align-items:center; justify-content:center; font-size:34px; line-height:1; flex:none; }
.row em { font-style:normal; font-size:26px; color:#1d3f9e; background:#e8eefc; padding:8px 18px; border-radius:999px; }
""", seed=33))

asset("instagram/first-post/04-tailor.png", IG_W, IG_H, ig_slide(4, IG_TOTAL, """
<div class="content">
  <div class="eyebrow">{spark} For each job you want</div>
  <h1>A resume that<br>fits the job.</h1>
  <div class="list">
    <div class="item"><b>Match score</b><span>See how your resume lines up with the posting, and what's missing.</span></div>
    <div class="item"><b>Tailored resume</b><span>In your own Word design. Built only from your real experience: nothing invented.</span></div>
    <div class="item"><b>Job alerts</b><span>New jobs for your searches, by email.</span></div>
  </div>
</div>""", """
.list { margin-top:48px; display:flex; flex-direction:column; gap:22px; }
.item { background:rgba(76,124,240,.14); border:1.5px solid rgba(169,192,255,.30); border-radius:24px;
  padding:28px 34px; }
.item b { display:block; font-weight:800; font-size:38px; color:#fff; letter-spacing:-0.02em; }
.item span { display:block; margin-top:8px; font-weight:500; font-size:30px; line-height:1.38; color:#d5e0ff; }
""", seed=34))

asset("instagram/first-post/05-start.png", IG_W, IG_H, ig_slide(5, IG_TOTAL, """
<div class="content cta">
  <svg class="big"><use href="#mark"/></svg>
  <h1>Start free at<br><b>hirlumo.com</b></h1>
  <p class="lead">Follow for Canadian job-search tips: resumes, applications and where the jobs are.</p>
</div>""", """
.cta { top:250px; text-align:center; display:flex; flex-direction:column; align-items:center; }
.cta .big { width:190px; height:190px; margin-bottom:56px;
  filter: drop-shadow(0 20px 50px rgba(36,83,212,.6)); }
.cta .lead { max-width:820px; }
""", seed=35, glow_y=0.33))


# ---- render -----------------------------------------------------------------
def chrome() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return name
    sys.exit("headless Chrome/Chromium not found on PATH")


def render(only: str | None = None) -> None:
    HTML_DIR.mkdir(exist_ok=True)
    exe = chrome()
    for rel, (html, w, h, transparent) in ASSETS.items():
        if only and only not in rel:
            continue
        src = HTML_DIR / (Path(rel).stem + ".html")
        src.write_text(html, encoding="utf-8")
        out = BRAND / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
               "--force-device-scale-factor=1", "--allow-file-access-from-files",
               "--run-all-compositor-stages-before-draw", "--virtual-time-budget=3000",
               f"--window-size={w},{h}", f"--screenshot={out}"]
        if transparent:
            cmd.append("--default-background-color=00000000")
        cmd.append(src.as_uri())
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        print(f"{rel}  {w}x{h}")


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else None)

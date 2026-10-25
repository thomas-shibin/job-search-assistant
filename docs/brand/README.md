# Hirlumo brand assets

Everything here is built from the product logo in `app/web/static/brand/`
(`logo.svg`, `mark.svg`). The mark is a blue rounded square with a white "h"
and an amber sparkle. The wordmark is "hirlumo" in lowercase Inter ExtraBold.

## Palette

| Role | Hex | Where it comes from |
|---|---|---|
| Mark gradient, light end | `#3a6ff0` | `logo.svg` |
| Mark gradient, dark end | `#1c3fae` | `logo.svg` |
| Primary (app, light theme) | `#2453d4` | `style.css` |
| Primary (app, dark theme) | `#4c7cf0` | `style.css` |
| Sparkle / highlight | `#ffb547` | `logo.svg` |
| Wordmark on light backgrounds | `#0f1b3d` | `logo.svg` |
| Night background | `#0a1024` | close to the app's dark `--bg` |
| Soft blue text on dark | `#a9c0ff` | `style.css` dark `--primary-text` |

Typeface: Inter. ExtraBold (800) for the wordmark, SemiBold (600) for taglines.

Social art uses a night-sky background in the brand blues, a soft glow behind
the logo, amber sparkles that repeat the logo's star, and a low mountain ridge
as a quiet nod to British Columbia. Tagline: "Find jobs in Canada that fit you",
the same line as the landing page.

## Files

### Logo (existing)

| File | Size | Use |
|---|---|---|
| `hirlumo-icon-1024.png` | 1024×1024, transparent corners | app stores, large icon |
| `hirlumo-icon-400.png` | 400×400 | general avatar |
| `hirlumo-icon-120.png` | 120×120 | small icon |
| `hirlumo-logo-1200x300.png` | 1200×300, white | documents, light backgrounds |

### YouTube (`youtube/`)

| File | Size | Upload in YouTube Studio |
|---|---|---|
| `hirlumo-youtube-profile-800.png` | 800×800 | Customization > Branding > Picture. YouTube crops it to a circle; the logo sits inside the circle. |
| `hirlumo-youtube-banner-2560x1440.png` | 2560×1440 | Customization > Branding > Banner image. The logo, tagline and feature chips sit inside the 1546×423 centre area that every device shows. TVs show the whole image. |
| `hirlumo-youtube-watermark-150.png` | 150×150, transparent | Customization > Branding > Video watermark. |

### Facebook (`facebook/`)

| File | Size | Upload where |
|---|---|---|
| `hirlumo-facebook-profile-720.png` | 720×720 | Facebook Page > profile picture. Facebook shows it as a circle. |
| `hirlumo-facebook-cover-1640x924.png` | 1640×924 | Facebook Page > cover photo. Phones show the full 16:9 image. Desktop shows the middle 1640×624 strip, and all text stays inside it. |
| `hirlumo-meta-app-icon-1024.png` | 1024×1024, opaque square | Meta for Developers > App Dashboard > App settings > Basic > App icon. This is the icon people see in the "Continue with Facebook" dialog. |

### Instagram (`instagram/`)

| File | Size | Use |
|---|---|---|
| `hirlumo-instagram-profile-1080.png` | 1080×1080 | Profile picture. Instagram crops it to a circle; the logo sits inside the circle. |
| `first-post/01-hello.png` … `05-start.png` | 1080×1350 (4:5) | The first post, a 5-slide carousel. Upload all five in order as one post. The profile grid shows a 3:4 crop, so text stays at least 90 px from the side edges. |

The slides only describe features that are live on hirlumo.com. Check
[FEATURES.md](../FEATURES.md) before adding a feature to a new post.

### Wallpaper (`wallpaper/`)

| File | Size | Use |
|---|---|---|
| `hirlumo-wallpaper-3840x2160.png` | 4K | desktop wallpaper, event screens, video backgrounds |
| `hirlumo-wallpaper-1920x1080.png` | 1080p | same, smaller file |

The wallpapers carry `hirlumo.com` in the bottom-right corner. The social
banners leave it out because YouTube and Facebook show the link themselves.

## Changing or re-rendering

The images are rendered from HTML by `src/build.py` using headless Chrome, with
Inter bundled in `src/fonts`. Colours, tagline, feature chips and sizes are
constants near the top and bottom of that script.

```bash
python3 docs/brand/src/build.py            # render everything
python3 docs/brand/src/build.py facebook   # only files whose path contains "facebook"
```

The generated HTML lands in `src/html/`, which git ignores. Open one of those
files in a browser to preview a layout before rendering.

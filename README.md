# Image Shrinker

A free, offline TinyPNG-style image compressor for Windows. Drop in your PNG, JPG or WEBP files and get much smaller files that look the same.

**Example:** a 1.5 MB PNG artwork → **276 KB (−82%)**. TinyPNG got the same image to 281 KB.

## Features

- **Smart mode (default):** looks identical, with big savings
  - **PNG:** reduced to a 256-color palette, with dithering only in smooth gradients where banding would show. This is the same idea TinyPNG uses.
  - **JPG / WEBP:** re-saved at the lowest quality that still measures as visually identical.
- **100% lossless mode:** every pixel stays the same. Each result is decoded again and compared with the original before it is kept.
- **Batch processing:** add files or whole folders (subfolders included), with a progress bar and a before / after / saved list.
- **BMP / TIFF → PNG** conversion (optional)
- **Safe output:**
  - saves copies into a `_shrunk` subfolder, or
  - replaces the originals, moving each one to `_backup_originals` first
- **Keeps** color profiles, gamma and JPG EXIF (orientation). Skips animated PNG/WEBP.
- Dark UI that stays sharp on high-DPI screens. Uses several CPU cores at once.

## Requirements

- Windows with Python 3.8+
- [Pillow](https://pypi.org/project/pillow/)

```
pip install pillow
```

Then double-click **`Image Shrinker.pyw`**.

### Optional boosters

Click **Install Boosters** inside the app, or install them yourself:

```
pip install imagequant pyoxipng numpy mozjpeg-lossless-optimization
```

| Booster | What it adds |
|---|---|
| `imagequant` | libimagequant (pngquant's engine) for PNG color reduction |
| `pyoxipng` | an extra lossless PNG squeeze (oxipng) |
| `numpy` | smoother semi-transparent edges and glows |
| `mozjpeg-lossless-optimization` | lossless JPG shrinking (mozjpeg) |

The app also uses `oxipng.exe` / `jpegtran.exe` if you put them next to the `.pyw` or on your PATH.
If you install `tkinterdnd2`, you can drag and drop files onto the list.

## Modes at a glance

| Mode | Result | Typical savings |
|---|---|---|
| Smart – Balanced | looks identical | 60–85% on PNG |
| Smart – High | stricter quality checks | a bit less |
| Smart – Small | 128 colors / lower JPG quality | most savings |
| 100% lossless | pixel-for-pixel identical | 5–30% on PNG |

---

Made by **M.B.**

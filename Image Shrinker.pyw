"""
Image Shrinker.pyw  -  shrink PNG / JPG / WEBP (and BMP/TIFF -> PNG), TinyPNG-style.

Modes
  Smart     : looks identical, big savings. PNG -> 256-color palette with dithering
              (the same trick TinyPNG uses), JPG/WEBP -> re-encoded at the lowest
              quality that still measures visually identical (PSNR check).
  Lossless  : pixel-for-pixel identical. Every result is decoded again and compared
              with the original before it is kept.

Outputs never overwrite blindly:
  - "Save to _shrunk folder"  -> copies go to a _shrunk subfolder next to each image
  - "Replace originals"       -> the original is MOVED to _backup_originals first

Needs: Pillow  (pip install pillow)
Optional boosters (button inside the app installs them):
  imagequant                    - pngquant's engine, best PNG color reduction
  pyoxipng                      - extra lossless PNG squeeze (oxipng)
  mozjpeg-lossless-optimization - lossless JPG squeeze (mozjpeg)
"""

import hashlib
import io
import math
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

warnings.filterwarnings("ignore")

APP_NAME = "Image Shrinker"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_SUB = "_shrunk"
BACKUP_SUB = "_backup_originals"
SKIP_DIRS = {OUT_SUB.lower(), BACKUP_SUB.lower()}
PNG_SIG = b"\x89PNG\r\n\x1a\n"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EXT_PNG = {".png"}
EXT_JPG = {".jpg", ".jpeg", ".jfif", ".jpe"}
EXT_WEBP = {".webp"}
EXT_CONV = {".bmp", ".tif", ".tiff"}
ALL_EXT = EXT_PNG | EXT_JPG | EXT_WEBP | EXT_CONV
STD_MODES = {"1", "L", "LA", "P", "PA", "RGB", "RGBA"}

try:
    from PIL import Image, ImageChops, ImageFilter, ImageStat, PngImagePlugin, JpegImagePlugin
except ImportError:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(APP_NAME, "Pillow is not installed.\n\nOpen cmd and run:\n\n"
                                   "    pip install pillow\n\nthen start the app again.")
    sys.exit(1)

Image.MAX_IMAGE_PIXELS = None


def _enum(group, name, default):
    g = getattr(Image, group, None)
    if g is not None and hasattr(g, name):
        return getattr(g, name)
    return getattr(Image, name, default)


Q_FASTOCTREE = _enum("Quantize", "FASTOCTREE", 2)
Q_MEDIANCUT = _enum("Quantize", "MEDIANCUT", 0)
D_NONE = _enum("Dither", "NONE", 0)
D_FS = _enum("Dither", "FLOYDSTEINBERG", 3)

# ---------------- optional boosters ----------------
try:
    import numpy as np
except Exception:
    np = None
try:
    import imagequant
except Exception:
    imagequant = None
try:
    import oxipng as pyoxipng
except Exception:
    pyoxipng = None
try:
    import mozjpeg_lossless_optimization as mozlo
except Exception:
    mozlo = None


def find_exe(name):
    for n in (name + ".exe", name):
        p = os.path.join(APP_DIR, n)
        if os.path.isfile(p):
            return p
    return shutil.which(name)


OXIPNG_EXE = None if pyoxipng else find_exe("oxipng")
JPEGTRAN_EXE = None if mozlo else find_exe("jpegtran")

# quality presets for Smart mode
QUALITY = {
    "High":     dict(colors=256, png_floor=36.0, target=42.0),
    "Balanced": dict(colors=256, png_floor=30.0, target=39.0),
    "Small":    dict(colors=128, png_floor=28.0, target=36.5),
}


DITHER_TEXTURE = 14      # below this local "detail" level an area counts as smooth -> gets dithered
DITHER_MAX_JUMP = 18     # dithered pixels further than this from the original are "speckles" -> undone


class Stopped(Exception):
    pass


class Skip(Exception):
    pass


# ---------------- generic helpers ----------------
def decode(data):
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


def norm_rgba(im, clean):
    """RGBA copy; with clean=True fully transparent pixels get RGB=0 (invisible anyway)."""
    im = im.convert("RGBA")
    if clean:
        a = im.getchannel("A")
        if a.getextrema()[0] == 0:
            im.paste((0, 0, 0, 0), None, a.point([255] + [0] * 255))
    return im


def digest(im):
    return hashlib.blake2b(im.tobytes(), digest_size=20).digest()


def matches(data, h, clean):
    try:
        return digest(norm_rgba(decode(data), clean)) == h
    except Exception:
        return False


def psnr(a, b):
    """PSNR in dB between two same-size, same-mode images (99 = identical)."""
    st = ImageStat.Stat(ImageChops.difference(a, b))
    n = a.size[0] * a.size[1] * len(st.sum2)
    mse = sum(st.sum2) / max(1, n)
    return 99.0 if mse <= 0 else 10 * math.log10(65025.0 / mse)


def check_stop(ctx):
    if ctx["cancel"].is_set():
        raise Stopped()


# ---------------- PNG helpers ----------------
def png_chunks(data):
    out, pos, n = [], 8, len(data)
    while pos + 8 <= n:
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        payload = b"" if ctype == b"IDAT" else data[pos + 8:pos + 8 + length]
        out.append((ctype, payload))
        pos += 12 + length
        if ctype == b"IEND":
            break
    return out


def color_pnginfo(chunks, icc):
    """Keep the chunks that affect how colors look (gamma/chromaticity/sRGB)."""
    if not chunks:
        return None
    info = PngImagePlugin.PngInfo()
    for t, payload in chunks:
        if t in (b"gAMA", b"cHRM") or (t == b"sRGB" and not icc):
            info.add(t, payload)
    return info if info.chunks else None


def encode_png(im, icc=None, pinfo=None, strategy=-1, transparency=None):
    im.info = {}
    kw = {"optimize": True}
    if strategy != -1:
        kw["compress_type"] = strategy
    if icc:
        kw["icc_profile"] = icc
    if pinfo is not None:
        kw["pnginfo"] = pinfo
    if transparency is not None:
        kw["transparency"] = transparency
    buf = io.BytesIO()
    im.save(buf, "PNG", **kw)
    return buf.getvalue()


def first_pixel_in(mask, img):
    box = mask.getbbox()
    if not box:
        return None
    y = box[1]
    for x in range(box[0], box[2]):
        if mask.getpixel((x, y)):
            return img.getpixel((x, y))
    return img.getpixel((box[0], y))


def indices_of(p):
    return Image.frombytes("L", p.size, p.tobytes())


def build_p(size, idx_l, palette_rgba):
    """P image from an L index map + RGBA palette (alpha<255 entries must come first)."""
    p = Image.frombytes("P", size, idx_l.tobytes())
    flat = []
    for c in palette_rgba:
        flat.extend(c[:3])
    p.putpalette(flat)
    alphas = [c[3] for c in palette_rgba]
    last = -1
    for i, a in enumerate(alphas):
        if a < 255:
            last = i
    trns = bytes(alphas[:last + 1]) if last >= 0 else None
    return p, trns


def exact_palettes(work, colors, has_alpha):
    """Pixel-exact palette versions of an image with <=256 colors. -> [(img, trns)]"""
    out = []
    n = len(colors)
    colors = sorted(colors, key=lambda c: -c[0])
    if not has_alpha:
        flat = [v for _, c in colors for v in c[:3]]
        try:
            pal = Image.new("P", (1, 1))
            pal.putpalette(flat + flat[:3] * (256 - n))
            p = work.quantize(palette=pal, dither=D_NONE)
            if p.getextrema()[1] < n:
                p.putpalette(flat)
                out.append((p, None))
        except Exception:
            pass
    elif np is not None:
        try:
            arr = np.asarray(work, dtype=np.uint8).reshape(-1, 4).astype(np.uint32)
            packed = (arr[:, 0] << 24) | (arr[:, 1] << 16) | (arr[:, 2] << 8) | arr[:, 3]
            uniq, inv = np.unique(packed, return_inverse=True)
            if len(uniq) <= 256:
                alpha = uniq & 0xFF
                order = np.argsort(alpha == 255, kind="stable")     # transparent entries first
                remap = np.empty_like(order)
                remap[order] = np.arange(len(order))
                idx = remap[inv].astype(np.uint8).reshape(work.size[1], work.size[0])
                pal = [(int(u >> 24) & 255, int(u >> 16) & 255, int(u >> 8) & 255, int(u) & 255)
                       for u in uniq[order]]
                out.append(build_p(work.size, Image.fromarray(idx, "L"), pal))
        except Exception:
            pass
    try:
        out.append((work.quantize(colors=n, method=Q_FASTOCTREE, dither=D_NONE), None))
    except Exception:
        pass
    return out


def dither_rgb(rgb, k):
    """Palette + selective dithering: dots only in smooth gradients that would band,
    clean flat colors in detailed/noisy areas (much smaller files, same look)."""
    k = max(2, min(256, k))
    pal = rgb.quantize(colors=k, method=Q_MEDIANCUT, kmeans=2)
    fs = rgb.quantize(palette=pal, dither=D_FS)
    flat = rgb.quantize(palette=pal, dither=D_NONE)
    try:
        lum = rgb.convert("L")
        texture = lum.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.MaxFilter(3)) \
                     .filter(ImageFilter.GaussianBlur(2))
        smooth = texture.point([255 if v < DITHER_TEXTURE else 0 for v in range(256)])
        err = ImageChops.difference(rgb, flat.convert("RGB")).convert("L")
        needs = err.filter(ImageFilter.MaxFilter(3)).point([255 if v > 2 else 0 for v in range(256)])
        mask = ImageChops.multiply(smooth, needs)
        # drop dither "speckles": pixels where dithering jumped to a far-off color
        fs_err = ImageChops.difference(rgb, fs.convert("RGB")).convert("L")
        sane = fs_err.point([255 if v <= DITHER_MAX_JUMP else 0 for v in range(256)])
        mask = ImageChops.multiply(mask, sane)
        idx = indices_of(flat)
        idx.paste(indices_of(fs), None, mask)
        p = Image.frombytes("P", rgb.size, idx.tobytes())
        p.putpalette(fs.getpalette())
    except Exception:
        p = fs
    used = p.getextrema()[1] + 1
    p.putpalette(p.getpalette()[:used * 3])
    return p


_BAYER8 = None


def _nearest(px, pal, chunk=65536):
    out = np.empty(len(px), np.int32)
    pal_sq = (pal * pal).sum(1)
    for s in range(0, len(px), chunk):
        x = px[s:s + chunk]
        out[s:s + chunk] = (pal_sq[None, :] - 2.0 * (x @ pal.T)).argmin(1)
    return out


def dither_semi_np(ref, smask, spal):
    """numpy: refine the semi-transparent palette (k-means in premultiplied space) and map
    the semi-transparent pixels with ordered (Bayer) dithering so soft glows don't band."""
    global _BAYER8
    if _BAYER8 is None:
        b = np.array([[0]])
        for _ in range(3):
            b = np.block([[4 * b, 4 * b + 2], [4 * b + 3, 4 * b + 1]])
        _BAYER8 = (b + 0.5) / 64.0 - 0.5
    w, h = ref.size
    arr = np.asarray(ref, dtype=np.float32).reshape(-1, 4)
    sel = np.flatnonzero(np.asarray(smask, dtype=np.uint8).reshape(-1))
    px = arr[sel]

    def premul(c):
        a = c[:, 3:4] / 255.0
        return np.concatenate([c[:, :3] * a, c[:, 3:4]], axis=1)

    p = premul(px)
    pal = premul(np.array(spal, dtype=np.float32))
    for _ in range(3):                                   # k-means refinement
        idx = _nearest(p, pal)
        cnt = np.bincount(idx, minlength=len(pal)).astype(np.float32)
        for c in range(4):
            s = np.bincount(idx, weights=p[:, c], minlength=len(pal))
            pal[:, c] = np.where(cnt > 0, s / np.maximum(cnt, 1), pal[:, c])
    idx = _nearest(p, pal)
    spread = np.minimum(4.0 * np.abs(p - pal[idx]).mean(0), 64.0)
    ys, xs = sel // w, sel % w
    noise = _BAYER8[ys % 8, xs % 8][:, None] * spread[None, :]
    idx = _nearest(p + noise, pal)

    a = np.clip(np.round(pal[:, 3]), 1, 254)
    rgb = np.clip(np.round(pal[:, :3] * 255.0 / a[:, None]), 0, 255)
    new_pal = [(int(r), int(g), int(b), int(al)) for (r, g, b), al in zip(rgb, a)]
    full = np.zeros(w * h, np.uint8)
    full[sel] = idx.astype(np.uint8)
    return Image.frombytes("L", (w, h), full.tobytes()), new_pal


def builtin_quantize(ref, ncolors):
    """Pure-Pillow TinyPNG-style reduction (used when imagequant isn't installed).
    Opaque pixels: median-cut palette + Floyd-Steinberg dithering.
    Semi-transparent edges: their own small palette. Fully transparent: 1 entry."""
    size = ref.size
    a = ref.getchannel("A")
    rgb = ref.convert("RGB")
    if a.getextrema()[0] == 255:
        return dither_rgb(rgb, ncolors), None

    hist = a.histogram()
    n_t, n_op, n_semi = hist[0], hist[255], sum(hist[1:255])
    tmask = a.point([255] + [0] * 255)
    omask = a.point([0] * 255 + [255])
    smask = a.point([0] + [255] * 254 + [0])

    budget = ncolors - (1 if n_t else 0)
    k_semi = 0
    if n_semi:
        if not n_op:
            k_semi = budget
        else:
            share = n_semi / float(n_semi + n_op)
            k_semi = int(round(budget * min(0.85, max(0.12, share * 1.2))))
            k_semi = max(4, min(k_semi, budget - 8))
    k_op = budget - k_semi if n_op else 0

    pal = []
    idx = Image.new("L", size, 0)
    if n_t:
        pal.append((0, 0, 0, 0))

    if n_semi:
        base = len(pal)
        img = ref.copy()
        img.paste(first_pixel_in(smask, ref), None, ImageChops.invert(smask))
        sq = img.quantize(colors=max(2, k_semi), method=Q_FASTOCTREE, dither=D_NONE)
        si = indices_of(sq)
        si.paste(0, None, ImageChops.invert(smask))
        used = si.getextrema()[1] + 1
        sp = sq.getpalette("RGBA")
        spal = [tuple(sp[i * 4:i * 4 + 4]) for i in range(used)]
        if np is not None:
            try:
                si, spal = dither_semi_np(ref, smask, spal)
            except Exception:
                pass
        pal += spal
        idx.paste(si.point(lambda v: v + base), None, smask)

    if n_op:
        base = len(pal)
        img = rgb.copy()
        img.paste(first_pixel_in(omask, rgb), None, ImageChops.invert(omask))
        oq = dither_rgb(img, k_op)
        oi = indices_of(oq)
        op = oq.getpalette()
        used = len(op) // 3
        pal += [(op[i * 3], op[i * 3 + 1], op[i * 3 + 2], 255) for i in range(used)]
        idx.paste(oi.point(lambda v: min(255, v + base)), None, omask)

    if n_t:
        idx.paste(0, None, tmask)
    return build_p(size, idx, pal[:256])


def oxipng_boost(data, ctx, clean):
    """Lossless re-squeeze with oxipng (python module or exe). Returns bytes or None."""
    maxe = ctx["max"]
    if pyoxipng is not None:
        level = 6 if maxe else 3
        kw = {"level": level, "optimize_alpha": bool(clean)}
        try:
            kw["strip"] = pyoxipng.StripChunks.safe()
        except Exception:
            pass
        if maxe:
            try:
                kw["deflate"] = pyoxipng.Deflaters.zopfli(15)
            except Exception:
                pass
        try:
            return pyoxipng.optimize_from_memory(data, **kw)
        except TypeError:
            try:
                return pyoxipng.optimize_from_memory(data, level=level)
            except Exception:
                return None
        except Exception:
            return None
    if OXIPNG_EXE:
        tmpdir = tempfile.mkdtemp(prefix="imgshrink_")
        try:
            src, dst = os.path.join(tmpdir, "in.png"), os.path.join(tmpdir, "out.png")
            with open(src, "wb") as f:
                f.write(data)
            cmd = [OXIPNG_EXE, "-q", "-o", "max" if maxe else "3", "--strip", "safe"]
            if maxe:
                cmd.append("-Z")
            if clean:
                cmd.append("-a")
            cmd += ["--out", dst, src]
            subprocess.run(cmd, creationflags=NO_WINDOW, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=1800)
            if os.path.isfile(dst):
                with open(dst, "rb") as f:
                    return f.read()
        except Exception:
            return None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return None


def boost_verified(data, ctx, h, clean):
    out = oxipng_boost(data, ctx, clean)
    if out and len(out) < len(data) and matches(out, h, clean):
        return out
    return data


def png_lossless(im, ctx, clean):
    """Best pixel-identical PNG encoding (verified). -> bytes or None"""
    ref = norm_rgba(im, clean)
    h = digest(ref)
    has_alpha = ref.getchannel("A").getextrema()[0] < 255
    work = ref if has_alpha else ref.convert("RGB")
    b = work.split()
    gray = (ctx["allow_gray"] and ImageChops.difference(b[0], b[1]).getbbox() is None
            and ImageChops.difference(b[1], b[2]).getbbox() is None)

    cands = [(work, None)]
    if gray:
        cands.append((work.convert("LA" if has_alpha else "L"), None))
    colors = work.getcolors(256)
    if colors:
        cands += exact_palettes(work, colors, has_alpha)

    strategies = [-1, 1, 3] if ctx["max"] else [-1]
    best = None
    for img, trns in cands:
        for s in strategies:
            check_stop(ctx)
            try:
                data = encode_png(img, ctx["icc"], ctx["pinfo"], s, trns)
            except Exception:
                continue
            if best is not None and len(data) >= len(best):
                continue
            if matches(data, h, clean):
                best = data
    if best is not None:
        check_stop(ctx)
        best = boost_verified(best, ctx, h, clean)
    return best


def png_smart(im, ctx):
    """TinyPNG-style: palette reduction + dithering. -> (bytes, note) or None"""
    preset = QUALITY[ctx["quality"]]
    ref = norm_rgba(im, True)
    tries = [preset["colors"]] + ([256] if preset["colors"] < 256 else [])
    for ncol in tries:
        check_stop(ctx)
        made = []
        if imagequant is not None:
            try:
                made.append((imagequant.quantize_pil_image(ref, dithering_level=1.0, max_colors=ncol,
                                                            min_quality=0, max_quality=100), None))
            except Exception:
                pass
        made.append(builtin_quantize(ref, ncol))
        for p, trns in made:
            check_stop(ctx)
            try:
                data = encode_png(p, ctx["icc"], ctx["pinfo"], -1, trns)
                dec = norm_rgba(decode(data), True)
            except Exception:
                continue
            score = psnr(dec, ref)
            if score < preset["png_floor"]:
                continue
            data = boost_verified(data, ctx, digest(dec), True)
            return data, "smart - %d colors - %.1f dB" % (ncol, score)
    return None


def image_to_png(im, ctx, lossless_ok, smart_ok, original=None):
    smart = ctx["mode"] == "smart"
    if smart and smart_ok:
        few = norm_rgba(im, True).getcolors(256) is not None
        if not few:                       # already <=256 colors -> lossless palette is exact anyway
            r = png_smart(im, ctx)
            if r:
                return r
    if lossless_ok:
        data = png_lossless(im, ctx, clean=smart)
        if data:
            return data, ("lossless" if not smart else "lossless (few colors)")
    if original is not None and (pyoxipng or OXIPNG_EXE):      # e.g. 16-bit PNGs
        h = digest(norm_rgba(decode(original), False))
        out = oxipng_boost(original, ctx, False)
        if out and matches(out, h, False):
            return out, "lossless (oxipng)"
    if not lossless_ok and not smart:
        raise Skip("16-bit PNG - needs the boosters for lossless")
    return None


def process_png(data, ctx):
    if data[:8] != PNG_SIG:
        raise Skip("not a real PNG")
    chunks = png_chunks(data)
    types = {c[0] for c in chunks}
    if b"acTL" in types:
        raise Skip("animated PNG - skipped")
    ihdr = chunks[0][1]
    depth, ctype = ihdr[8], ihdr[9]
    im = decode(data)
    icc = im.info.get("icc_profile")
    ctx["icc"] = icc
    ctx["pinfo"] = color_pnginfo(chunks, icc)
    ctx["allow_gray"] = (not icc) or ctype in (0, 4)
    std = im.mode in STD_MODES
    return image_to_png(im, ctx, lossless_ok=std and depth <= 8, smart_ok=std, original=data)


def process_convert(data, ctx):
    im = decode(data)
    if getattr(im, "n_frames", 1) > 1:
        raise Skip("multi-page image - skipped")
    if im.mode not in STD_MODES:
        if im.mode in ("I;16", "I;16B", "I;16L", "I", "F", "CMYK", "YCbCr", "LAB", "HSV"):
            raise Skip("%s color mode - skipped" % im.mode)
        im = im.convert("RGBA")
    icc = im.info.get("icc_profile")
    ctx["icc"] = icc
    ctx["pinfo"] = None
    ctx["allow_gray"] = (not icc) or im.mode in ("1", "L", "LA")
    return image_to_png(im, ctx, lossless_ok=True, smart_ok=True)


# ---------------- JPG ----------------
def jpeg_lossless(data):
    if mozlo is not None:
        try:
            try:
                return mozlo.optimize(data, copy=mozlo.COPY_MARKERS.ALL)
            except (TypeError, AttributeError):
                return mozlo.optimize(data)
        except Exception:
            return None
    if JPEGTRAN_EXE:
        tmpdir = tempfile.mkdtemp(prefix="imgshrink_")
        try:
            src, dst = os.path.join(tmpdir, "in.jpg"), os.path.join(tmpdir, "out.jpg")
            with open(src, "wb") as f:
                f.write(data)
            subprocess.run([JPEGTRAN_EXE, "-copy", "all", "-optimize", "-progressive",
                            "-outfile", dst, src], creationflags=NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
            if os.path.isfile(dst):
                with open(dst, "rb") as f:
                    return f.read()
        except Exception:
            return None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return None


def same_jpeg_pixels(out, ref, need_exif):
    try:
        im = decode(out)
        if need_exif and not im.info.get("exif"):
            return False
        return im.size == ref.size and psnr(im.convert(ref.mode), ref) >= 60
    except Exception:
        return False


def process_jpg(data, ctx):
    im = decode(data)
    exif, icc = im.info.get("exif"), im.info.get("icc_profile")
    if im.mode not in ("RGB", "L", "CMYK"):
        raise Skip("%s JPG - skipped" % im.mode)
    results = []

    b = jpeg_lossless(data)
    if b and len(b) < len(data) and same_jpeg_pixels(b, im, bool(exif)):
        results.append((b, "lossless"))

    if ctx["mode"] == "smart" and im.mode in ("RGB", "L"):
        target = QUALITY[ctx["quality"]]["target"]
        try:
            sampling = JpegImagePlugin.get_sampling(im)
        except Exception:
            sampling = -1
        sub = sampling if (ctx["quality"] == "High" and sampling in (0, 1, 2)) else 2
        kw = {"optimize": True, "progressive": True, "subsampling": sub}
        if exif:
            kw["exif"] = exif
        if icc:
            kw["icc_profile"] = icc
        lo, hi, best = 40, 95, None
        while lo <= hi:
            check_stop(ctx)
            q = (lo + hi) // 2
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q, **kw)
            out = buf.getvalue()
            score = psnr(decode(out).convert(im.mode), im)
            if score >= target:
                best = (out, q, score)
                hi = q - 1
            else:
                lo = q + 1
        if best:
            out, q, score = best
            extra = jpeg_lossless(out)
            if extra and len(extra) < len(out) and same_jpeg_pixels(extra, decode(out), bool(exif)):
                out = extra
            results.append((out, "smart - q%d - %.1f dB" % (q, score)))

    if not results:
        if ctx["mode"] == "lossless" and not (mozlo or JPEGTRAN_EXE):
            raise Skip("lossless JPG needs the boosters")
        return None
    return min(results, key=lambda r: len(r[0]))


# ---------------- WEBP ----------------
def webp_kind(data):
    pos = 12
    while pos + 8 <= len(data):
        fourcc = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if fourcc in (b"VP8 ", b"VP8L"):
            return fourcc
        pos += 8 + size + (size & 1)
    return None


def process_webp(data, ctx):
    im = decode(data)
    if getattr(im, "n_frames", 1) > 1:
        raise Skip("animated WebP - skipped")
    lossless_src = webp_kind(data) == b"VP8L"
    smart = ctx["mode"] == "smart"
    if not lossless_src and not smart:
        raise Skip("lossy WebP - nothing to gain losslessly")
    kw = {}
    if im.info.get("icc_profile"):
        kw["icc_profile"] = im.info["icc_profile"]
    if im.info.get("exif"):
        kw["exif"] = im.info["exif"]
    has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
    src = im.convert("RGBA" if has_alpha else "RGB")
    results = []

    if lossless_src:
        check_stop(ctx)
        ref = norm_rgba(src, smart)
        buf = io.BytesIO()
        src.save(buf, "WEBP", lossless=True, quality=100, method=6, exact=not smart, **kw)
        out = buf.getvalue()
        if matches(out, digest(ref), smart):
            results.append((out, "lossless"))

    if smart:
        target = QUALITY[ctx["quality"]]["target"]
        ref = norm_rgba(src, True)

        def enc(q, method):
            b = io.BytesIO()
            src.save(b, "WEBP", quality=q, method=method, alpha_quality=100, **kw)
            return b.getvalue()

        lo, hi, best_q = 40, 95, None
        while lo <= hi:
            check_stop(ctx)
            q = (lo + hi) // 2
            if psnr(norm_rgba(decode(enc(q, 4)), True), ref) >= target:
                best_q, hi = q, q - 1
            else:
                lo = q + 1
        if best_q is not None:
            out = enc(best_q, 6)
            score = psnr(norm_rgba(decode(out), True), ref)
            if score >= target - 0.3:
                results.append((out, "smart - q%d - %.1f dB" % (best_q, score)))

    return min(results, key=lambda r: len(r[0])) if results else None


# ---------------- one file ----------------
def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s (%d)%s" % (base, i, ext)):
        i += 1
    return "%s (%d)%s" % (base, i, ext)


def atomic_write(path, data):
    tmp = path + ".tmp_shrink"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def shrink_file(path, opt, cancel):
    ctx = dict(opt)
    ctx["cancel"] = cancel
    st = os.stat(path)
    with open(path, "rb") as f:
        data = f.read()
    before = len(data)
    ext = os.path.splitext(path)[1].lower()
    new_ext = ext

    if ext in EXT_PNG:
        r = process_png(data, ctx)
    elif ext in EXT_JPG:
        r = process_jpg(data, ctx)
    elif ext in EXT_WEBP:
        r = process_webp(data, ctx)
    elif ext in EXT_CONV:
        if not opt["convert"]:
            raise Skip("BMP/TIFF conversion is off")
        r = process_convert(data, ctx)
        new_ext = ".png"
    else:
        raise Skip("unsupported type")

    improved = r is not None and len(r[0]) < before
    out_bytes, note = (r[0], r[1]) if improved else (data, "already optimal")
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    out_name = (stem + new_ext) if improved else name

    if opt["output"] == "folder":
        out_dir = os.path.join(folder, OUT_SUB)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, out_name)
        atomic_write(out_path, out_bytes)
    else:
        out_path = path
        if improved:
            bdir = os.path.join(folder, BACKUP_SUB)
            os.makedirs(bdir, exist_ok=True)
            shutil.move(path, unique_path(os.path.join(bdir, name)))
            out_path = os.path.join(folder, out_name)
            if os.path.exists(out_path):                 # e.g. pic.bmp -> pic.png already there
                out_path = unique_path(out_path)
            atomic_write(out_path, out_bytes)
    try:
        os.utime(out_path, (st.st_atime, st.st_mtime))
    except Exception:
        pass
    return {"status": "ok" if improved else "same", "before": before,
            "after": len(out_bytes), "note": note, "out": out_path}


# ---------------- GUI ----------------
import tkinter.font as tkfont
from PIL import ImageDraw, ImageTk

FONT = "Segoe UI"
BG = "#1f2023"          # window
LIST_BG = "#232428"     # file list
FIELD = "#2a2b2f"       # combobox / boxes
BTN = "#2b2c30"
BTN_HOVER = "#37383d"
BORDER = "#5c5f66"
LINE = "#34363b"
FG = "#e8e8ea"
DIM = "#a3a6ad"
HEAD = "#9ba4b4"
OFF = "#62656c"
ACCENT = "#4a8cff"
ACCENT_HOVER = "#6aa0ff"
GOOD = "#5ccf7a"
BAD = "#ff6b6b"


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def _px(v, sc):
    return max(1, int(round(v * sc)))


def make_indicator(kind, on, disabled, size):
    """Smooth check box / radio image (drawn 4x and scaled down), with a gap on the right."""
    S = 4
    gap = int(size * 0.5)
    im = Image.new("RGBA", ((size + gap) * S, size * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if disabled:
        ring, fill, mark = ("#3f5f93", "#3f5f93", "#aeb6c4") if on else ("#45474d", BG, None)
    else:
        ring, fill, mark = (ACCENT, ACCENT, "white") if on else ("#75787f", FIELD, None)
    lw = max(S, int(round(size * S * 0.075)))
    m = lw // 2 + S
    box = size * S
    if kind == "check":
        d.rounded_rectangle((m, m, box - m, box - m), radius=int(box * 0.2), fill=fill, outline=ring, width=lw)
        if on:
            pts = [(0.27, 0.53), (0.43, 0.69), (0.74, 0.35)]
            d.line([(x * box, y * box) for x, y in pts], fill=mark, width=int(lw * 1.35), joint="curve")
    else:
        d.ellipse((m, m, box - m, box - m), fill=FIELD if not disabled else BG, outline=ring, width=lw)
        if on:
            c, r = box / 2.0, box * 0.23
            d.ellipse((c - r, c - r, c + r, c + r), fill=ring)
    return im.resize((size + gap, size), Image.LANCZOS)


def make_chevron(sc, color):
    S = 4
    w, h = _px(30, sc), _px(14, sc)
    im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx, cy, a = w * S * 0.42, h * S / 2.0, h * S * 0.2
    d.line([(cx - a * 1.7, cy - a * 0.85), (cx, cy + a * 0.85), (cx + a * 1.7, cy - a * 0.85)],
           fill=color, width=max(S, int(1.4 * sc * S)), joint="curve")
    return im.resize((w, h), Image.LANCZOS)


def make_icon():
    S = 4
    n = 64 * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((2 * S, 2 * S, n - 2 * S, n - 2 * S), radius=14 * S, fill=ACCENT)
    w = 5 * S
    for y0 in (12, 34):                          # two "shrink" chevrons
        d.line([(18 * S, y0 * S), (32 * S, (y0 + 10) * S), (46 * S, y0 * S)], fill="white", width=w,
               joint="curve")
    return im.resize((64, 64), Image.LANCZOS)


class App:
    def __init__(self, root):
        self.root = root
        self.sc = max(1.0, root.winfo_fpixels("1i") / 96.0)
        P = self.P = lambda v: _px(v, self.sc)
        root.title(APP_NAME)
        root.geometry("%dx%d" % (P(1120), P(780)))
        root.minsize(P(920), P(620))
        root.configure(bg=BG)
        try:
            self._icon = ImageTk.PhotoImage(make_icon())
            root.iconphoto(True, self._icon)
        except Exception:
            pass
        self.dark_titlebar()

        self.q = queue.Queue()
        self.rows = {}          # normcase path -> (iid, real path)
        self.state = {}         # iid -> 'pending' | 'done'
        self.cancel = threading.Event()
        self.running = False
        self.last_out_dir = None

        self.v_mode = tk.StringVar(value="smart")
        self.v_quality = tk.StringVar(value="Balanced")
        self.v_output = tk.StringVar(value="folder")
        self.v_sub = tk.BooleanVar(value=True)
        self.v_convert = tk.BooleanVar(value=True)
        self.v_max = tk.BooleanVar(value=False)

        self.build_style()
        self.build_ui()
        self.sync()
        for a in sys.argv[1:]:
            self.add_path(a, True)
        self.root.after(100, self.poll)

    # ---------- look ----------
    def dark_titlebar(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            val = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val),
                                                              ctypes.sizeof(val)) == 0:
                    break
        except Exception:
            pass

    def build_style(self):
        P, root = self.P, self.root
        st = self.st = ttk.Style(root)
        st.theme_use("clam")

        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=FONT, size=11)
            except Exception:
                pass
        self.f_head = tkfont.Font(family=FONT, size=10, weight="bold")
        self.f_bold = tkfont.Font(family=FONT, size=11, weight="bold")
        self.f_big = tkfont.Font(family=FONT, size=12, weight="bold")

        st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=BORDER,
                     lightcolor=BG, darkcolor=BG, troughcolor=FIELD, focuscolor=BG,
                     selectbackground=ACCENT, selectforeground="white")
        st.configure("TFrame", background=BG)
        st.configure("Line.TFrame", background=LINE)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Dim.TLabel", foreground=DIM)
        st.configure("Head.TLabel", foreground=HEAD, font=self.f_head)
        st.configure("Sum.TLabel", foreground=GOOD, font=self.f_bold)
        st.configure("Hint.TLabel", background=LIST_BG, foreground=OFF, font=self.f_bold)

        # buttons: flat, 1px light border, no focus ring
        st.layout("TButton", [("Button.border", {"sticky": "nswe", "border": "1", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})])
        st.configure("TButton", background=BTN, foreground=FG, bordercolor=BORDER, lightcolor=BTN,
                     darkcolor=BTN, borderwidth=1, relief="flat", padding=(P(18), P(7)), anchor="center")
        st.map("TButton",
               background=[("disabled", BG), ("pressed", "#303136"), ("active", BTN_HOVER)],
               lightcolor=[("disabled", BG), ("pressed", "#303136"), ("active", BTN_HOVER)],
               darkcolor=[("disabled", BG), ("pressed", "#303136"), ("active", BTN_HOVER)],
               bordercolor=[("disabled", "#3b3d42"), ("active", "#80838b")],
               foreground=[("disabled", OFF)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#0b1426", bordercolor=ACCENT,
                     lightcolor=ACCENT, darkcolor=ACCENT, font=self.f_big, padding=(P(34), P(9)))
        st.map("Accent.TButton",
               background=[("disabled", "#2a3a58"), ("pressed", "#3b77e0"), ("active", ACCENT_HOVER)],
               lightcolor=[("disabled", "#2a3a58"), ("pressed", "#3b77e0"), ("active", ACCENT_HOVER)],
               darkcolor=[("disabled", "#2a3a58"), ("pressed", "#3b77e0"), ("active", ACCENT_HOVER)],
               bordercolor=[("disabled", "#2a3a58"), ("active", ACCENT_HOVER)],
               foreground=[("disabled", "#6f7d99")])

        # check boxes + radio buttons drawn as smooth images
        size = P(20)
        self._imgs = {}
        for kind in ("check", "radio"):
            for on in (False, True):
                for dis in (False, True):
                    self._imgs[(kind, on, dis)] = ImageTk.PhotoImage(make_indicator(kind, on, dis, size))
        for kind, el in (("check", "MB.Check.indicator"), ("radio", "MB.Radio.indicator")):
            g = self._imgs
            st.element_create(el, "image", g[(kind, False, False)],
                              ("disabled", "selected", g[(kind, True, True)]),
                              ("disabled", g[(kind, False, True)]),
                              ("selected", g[(kind, True, False)]))
        st.layout("MB.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("MB.Check.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.label", {"side": "left", "sticky": "nswe"})]})])
        st.layout("MB.TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
            ("MB.Radio.indicator", {"side": "left", "sticky": ""}),
            ("Radiobutton.label", {"side": "left", "sticky": "nswe"})]})])
        for s in ("MB.TCheckbutton", "MB.TRadiobutton"):
            st.configure(s, background=BG, foreground=FG, padding=(0, P(4)))
            st.map(s, background=[("active", BG)], foreground=[("disabled", OFF)])

        # combobox: flat field + thin chevron
        self._chev = ImageTk.PhotoImage(make_chevron(self.sc, DIM))
        self._chev_off = ImageTk.PhotoImage(make_chevron(self.sc, "#4a4c52"))
        st.element_create("MB.Combo.arrow", "image", self._chev, ("disabled", self._chev_off))
        st.layout("MB.TCombobox", [("Combobox.field", {"sticky": "nswe", "children": [
            ("MB.Combo.arrow", {"side": "right", "sticky": ""}),
            ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]})])
        st.configure("MB.TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG,
                     bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD, padding=(P(8), P(5), P(4), P(5)))
        st.map("MB.TCombobox",
               fieldbackground=[("disabled", BG), ("readonly", FIELD)],
               background=[("disabled", BG), ("readonly", FIELD)],
               lightcolor=[("disabled", BG), ("readonly", FIELD)],
               darkcolor=[("disabled", BG), ("readonly", FIELD)],
               bordercolor=[("disabled", "#3b3d42"), ("focus", ACCENT)],
               foreground=[("disabled", OFF), ("readonly", FG)],
               selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
        root.option_add("*TCombobox*Listbox.background", FIELD)
        root.option_add("*TCombobox*Listbox.foreground", FG)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        root.option_add("*TCombobox*Listbox.selectForeground", "white")
        root.option_add("*TCombobox*Listbox.font", "TkDefaultFont")
        root.option_add("*TCombobox*Listbox.borderWidth", 0)

        # file list
        st.configure("MB.Treeview", background=LIST_BG, fieldbackground=LIST_BG, foreground=FG,
                     rowheight=P(30), bordercolor=LINE, lightcolor=LIST_BG, darkcolor=LIST_BG,
                     borderwidth=1)
        st.map("MB.Treeview", background=[("selected", "#2f4a7a")], foreground=[("selected", FG)])
        st.configure("MB.Treeview.Heading", background="#2a2b30", foreground=HEAD, font=self.f_head,
                     bordercolor=LINE, lightcolor="#2a2b30", darkcolor="#2a2b30", relief="flat",
                     padding=(P(10), P(7)))
        st.map("MB.Treeview.Heading", background=[("active", "#303137")],
               lightcolor=[("active", "#303137")], darkcolor=[("active", "#303137")])

        # slim scrollbar (no arrows)
        st.layout("MB.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        st.configure("MB.Vertical.TScrollbar", troughcolor=LIST_BG, background="#45474d",
                     bordercolor=LIST_BG, lightcolor="#45474d", darkcolor="#45474d",
                     arrowsize=P(10), gripcount=0)
        st.map("MB.Vertical.TScrollbar", background=[("active", "#5c5f66")],
               lightcolor=[("active", "#5c5f66")], darkcolor=[("active", "#5c5f66")])

        st.configure("MB.Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT,
                     bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT, thickness=P(6))

    def build_ui(self):
        P = self.P
        o = ttk.Frame(self.root, padding=(P(28), P(20), P(28), P(18)))
        o.pack(fill="both", expand=True)
        o.columnconfigure(0, weight=1)
        o.rowconfigure(2, weight=1)

        # ---- IMAGES
        ttk.Label(o, text="IMAGES", style="Head.TLabel").grid(row=0, column=0, sticky="w")
        bar = ttk.Frame(o)
        bar.grid(row=1, column=0, sticky="ew", pady=(P(8), P(12)))
        ttk.Button(bar, text="Add Images", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="Add Folder", command=self.add_folder).pack(side="left", padx=P(10))
        ttk.Button(bar, text="Clear List", command=self.clear).pack(side="left")
        self.lbl_count = ttk.Label(bar, text="", style="Dim.TLabel")
        self.lbl_count.pack(side="right")

        lf = ttk.Frame(o)
        lf.grid(row=2, column=0, sticky="nsew")
        cols = ("file", "type", "before", "after", "saved", "result", "folder")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", selectmode="extended",
                                 style="MB.Treeview")
        for c, t, w, anc in (("file", "FILE", 260, "w"), ("type", "TYPE", 64, "center"),
                             ("before", "BEFORE", 92, "e"), ("after", "AFTER", 92, "e"),
                             ("saved", "SAVED", 76, "e"), ("result", "RESULT", 260, "w"),
                             ("folder", "FOLDER", 200, "w")):
            self.tree.heading(c, text=t, anchor=anc)
            self.tree.column(c, width=P(w), minwidth=P(50), anchor=anc,
                             stretch=(c in ("file", "result", "folder")))
        self.tree.tag_configure("ok", foreground=GOOD)
        self.tree.tag_configure("same", foreground=DIM)
        self.tree.tag_configure("skip", foreground=DIM)
        self.tree.tag_configure("error", foreground=BAD)
        self.tree.tag_configure("work", foreground=ACCENT)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview, style="MB.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Delete>", self.remove_selected)
        hint = ("Drop images or folders here" if TkinterDnD is not None
                else "Click Add Images or Add Folder to start")
        self.lbl_hint = ttk.Label(self.tree, text=hint, style="Hint.TLabel")
        self.lbl_hint.place(relx=0.5, rely=0.55, anchor="center")

        ttk.Frame(o, style="Line.TFrame", height=1).grid(row=3, column=0, sticky="ew", pady=P(18))

        # ---- MODE / OUTPUT / OPTIONS
        opts = ttk.Frame(o)
        opts.grid(row=4, column=0, sticky="ew")
        for i in range(3):
            opts.columnconfigure(i, weight=1, uniform="opts")

        c1 = ttk.Frame(opts)
        c1.grid(row=0, column=0, sticky="nw")
        ttk.Label(c1, text="MODE", style="Head.TLabel").pack(anchor="w", pady=(0, P(6)))
        ttk.Radiobutton(c1, text="Smart  -  looks identical, big savings", variable=self.v_mode,
                        value="smart", command=self.sync, style="MB.TRadiobutton").pack(anchor="w")
        ttk.Radiobutton(c1, text="100% lossless  -  pixel-perfect", variable=self.v_mode,
                        value="lossless", command=self.sync, style="MB.TRadiobutton").pack(anchor="w")
        qf = ttk.Frame(c1)
        qf.pack(anchor="w", pady=(P(8), 0))
        self.lbl_quality = ttk.Label(qf, text="Quality", style="Dim.TLabel")
        self.lbl_quality.pack(side="left", padx=(0, P(12)))
        self.cb_quality = ttk.Combobox(qf, textvariable=self.v_quality, values=list(QUALITY),
                                       state="readonly", width=11, style="MB.TCombobox")
        self.cb_quality.pack(side="left")
        self.cb_quality.bind("<<ComboboxSelected>>", lambda e: self.cb_quality.selection_clear())

        c2 = ttk.Frame(opts)
        c2.grid(row=0, column=1, sticky="nw")
        ttk.Label(c2, text="OUTPUT", style="Head.TLabel").pack(anchor="w", pady=(0, P(6)))
        ttk.Radiobutton(c2, text='Save copies in "%s" folder' % OUT_SUB, variable=self.v_output,
                        value="folder", style="MB.TRadiobutton").pack(anchor="w")
        ttk.Radiobutton(c2, text="Replace originals (backup kept)", variable=self.v_output,
                        value="replace", style="MB.TRadiobutton").pack(anchor="w")

        c3 = ttk.Frame(opts)
        c3.grid(row=0, column=2, sticky="nw")
        ttk.Label(c3, text="OPTIONS", style="Head.TLabel").pack(anchor="w", pady=(0, P(6)))
        ttk.Checkbutton(c3, text="Include subfolders", variable=self.v_sub,
                        style="MB.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(c3, text="Convert BMP / TIFF to PNG", variable=self.v_convert,
                        style="MB.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(c3, text="Max effort (slower, a bit smaller)", variable=self.v_max,
                        style="MB.TCheckbutton").pack(anchor="w")

        ttk.Frame(o, style="Line.TFrame", height=1).grid(row=5, column=0, sticky="ew", pady=P(18))

        # ---- progress + actions
        act = ttk.Frame(o)
        act.grid(row=6, column=0, sticky="ew")
        act.columnconfigure(0, weight=1)
        left = ttk.Frame(act)
        left.grid(row=0, column=0, sticky="ew", padx=(0, P(24)))
        top = ttk.Frame(left)
        top.pack(fill="x")
        self.lbl_prog = ttk.Label(top, text="Ready", style="Dim.TLabel")
        self.lbl_prog.pack(side="left")
        self.lbl_sum = ttk.Label(top, text="", style="Sum.TLabel")
        self.lbl_sum.pack(side="right")
        self.pb = ttk.Progressbar(left, maximum=100, style="MB.Horizontal.TProgressbar")
        self.pb.pack(fill="x", pady=(P(8), 0))

        btns = ttk.Frame(act)
        btns.grid(row=0, column=1, sticky="e")
        self.btn_open = ttk.Button(btns, text="Open Output Folder", command=self.open_out, state="disabled")
        self.btn_open.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=P(10))
        self.btn_go = ttk.Button(btns, text="Shrink!", style="Accent.TButton", command=self.start)
        self.btn_go.pack(side="left")

        ttk.Frame(o, style="Line.TFrame", height=1).grid(row=7, column=0, sticky="ew", pady=P(18))

        # ---- boosters
        foot = ttk.Frame(o)
        foot.grid(row=8, column=0, sticky="ew")
        ttk.Label(foot, text="BOOSTERS", style="Head.TLabel").pack(side="left", padx=(0, P(14)))
        self.lbl_tools = ttk.Label(foot, text=self.tools_text(), style="Dim.TLabel")
        self.lbl_tools.pack(side="left")
        self.btn_boost = None
        if not (imagequant and pyoxipng and mozlo and np is not None):
            self.btn_boost = ttk.Button(foot, text="Install Boosters", command=self.install_boosters)
            self.btn_boost.pack(side="right")

        if TkinterDnD is not None:
            try:
                self.tree.drop_target_register(DND_FILES)
                self.tree.dnd_bind("<<Drop>>", self.on_drop)
            except Exception:
                pass

    # ---------- helpers ----------
    def tools_text(self):
        def mark(x):
            return "on" if x else "off"
        return ("imagequant %s   |   oxipng %s   |   mozjpeg %s" %
                (mark(imagequant), mark(pyoxipng or OXIPNG_EXE), mark(mozlo or JPEGTRAN_EXE)))

    def sync(self):
        smart = self.v_mode.get() == "smart"
        self.cb_quality.configure(state="readonly" if smart else "disabled")
        self.lbl_quality.configure(foreground=DIM if smart else OFF)

    def refresh_hint(self):
        if self.tree.get_children():
            self.lbl_hint.place_forget()
        else:
            self.lbl_hint.place(relx=0.5, rely=0.55, anchor="center")

    def add_one(self, p):
        key = os.path.normcase(os.path.abspath(p))
        if key in self.rows:
            return
        size = os.path.getsize(p)
        folder, name = os.path.split(p)
        ext = os.path.splitext(p)[1].lower().lstrip(".").upper()
        iid = self.tree.insert("", "end", values=(name, ext, human(size), "", "", "waiting", folder))
        self.rows[key] = (iid, p)
        self.state[iid] = "pending"

    def add_path(self, p, recursive):
        p = os.path.abspath(p)
        if os.path.isfile(p):
            if os.path.splitext(p)[1].lower() in ALL_EXT:
                self.add_one(p)
        elif os.path.isdir(p):
            if recursive:
                for dp, dns, fns in os.walk(p):
                    dns[:] = sorted(d for d in dns if d.lower() not in SKIP_DIRS)
                    for fn in sorted(fns):
                        if os.path.splitext(fn)[1].lower() in ALL_EXT:
                            self.add_one(os.path.join(dp, fn))
            else:
                for fn in sorted(os.listdir(p)):
                    fp = os.path.join(p, fn)
                    if os.path.isfile(fp) and os.path.splitext(fn)[1].lower() in ALL_EXT:
                        self.add_one(fp)
        self.update_count()

    def update_count(self):
        self.refresh_hint()
        total = len(self.state)
        pending = sum(1 for s in self.state.values() if s == "pending")
        self.lbl_count.config(text=("%d images  -  %d to do" % (total, pending)) if total else "")
        if not self.running:
            self.lbl_prog.config(text="Ready" if not pending else "%d image(s) ready" % pending)

    def add_files(self):
        exts = " ".join("*" + e for e in sorted(ALL_EXT))
        for p in filedialog.askopenfilenames(title="Pick images",
                                             filetypes=[("Images", exts), ("All files", "*.*")]):
            self.add_path(p, False)

    def add_folder(self):
        d = filedialog.askdirectory(title="Pick a folder")
        if d:
            self.add_path(d, self.v_sub.get())

    def on_drop(self, event):
        for p in self.root.tk.splitlist(event.data):
            self.add_path(p, self.v_sub.get())

    def remove_selected(self, _e=None):
        if self.running:
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.state.pop(iid, None)
            for k, (i, _p) in list(self.rows.items()):
                if i == iid:
                    del self.rows[k]
        self.update_count()

    def clear(self):
        if self.running:
            return
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.state.clear()
        self.pb["value"] = 0
        self.lbl_sum.config(text="")
        self.update_count()

    def open_out(self):
        if self.last_out_dir and os.path.isdir(self.last_out_dir):
            try:
                os.startfile(self.last_out_dir)
            except Exception:
                subprocess.Popen(["xdg-open", self.last_out_dir])

    # ---------- run ----------
    def start(self):
        jobs = [(iid, p) for (iid, p) in self.rows.values() if self.state.get(iid) == "pending"]
        if not jobs:
            messagebox.showinfo(APP_NAME, "Nothing to do - add some images first.")
            return
        opt = {"mode": self.v_mode.get(), "quality": self.v_quality.get(), "output": self.v_output.get(),
               "convert": self.v_convert.get(), "max": self.v_max.get()}
        self.running = True
        self.cancel.clear()
        self.btn_go.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.pb["value"] = 0
        self.lbl_sum.config(text="")
        first = jobs[0][1]
        self.last_out_dir = (os.path.join(os.path.dirname(first), OUT_SUB)
                             if opt["output"] == "folder" else os.path.dirname(first))
        threading.Thread(target=self.worker, args=(jobs, opt), daemon=True).start()

    def stop(self):
        self.cancel.set()
        self.btn_stop.config(state="disabled")
        self.lbl_prog.config(text="Stopping after the current images...")

    def job(self, iid, path, opt):
        if self.cancel.is_set():
            raise Stopped()
        self.q.put(("start", iid))
        try:
            return shrink_file(path, opt, self.cancel)
        except Stopped:
            raise
        except Skip as e:
            return {"status": "skip", "note": str(e)}
        except Exception as e:
            return {"status": "error", "note": "error: %s" % e}

    def worker(self, jobs, opt):
        total = len(jobs)
        done = 0
        last_step = -1
        tot_before = tot_after = 0
        workers = max(1, min(6, (os.cpu_count() or 4) // (2 if opt["max"] else 1)))
        self.q.put(("progress", 0, 0, total))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self.job, iid, p, opt): iid for iid, p in jobs}
            for f in as_completed(futs):
                iid = futs[f]
                try:
                    r = f.result()
                except (Stopped, CancelledError):
                    r = {"status": "stopped", "note": "stopped"}
                except Exception as e:
                    r = {"status": "error", "note": "error: %s" % e}
                if r["status"] in ("ok", "same"):
                    tot_before += r["before"]
                    tot_after += r["after"]
                self.q.put(("row", iid, r))
                done += 1
                pct = done * 100 // total
                step = pct - pct % 5
                if step != last_step:
                    last_step = step
                    self.q.put(("progress", step, done, total))
                if self.cancel.is_set():
                    for other in futs:
                        other.cancel()
        self.q.put(("done", tot_before, tot_after, self.cancel.is_set()))

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "start":
                    iid = msg[1]
                    v = list(self.tree.item(iid, "values"))
                    v[5] = "working..."
                    self.tree.item(iid, values=v, tags=("work",))
                elif kind == "row":
                    _, iid, r = msg
                    v = list(self.tree.item(iid, "values"))
                    if r["status"] in ("ok", "same"):
                        saved = 100.0 * (r["before"] - r["after"]) / max(1, r["before"])
                        v[2], v[3] = human(r["before"]), human(r["after"])
                        v[4] = "-%.0f%%" % saved if r["status"] == "ok" else "0%"
                        v[5] = r["note"]
                        self.state[iid] = "done"
                    else:
                        v[5] = r["note"]
                        if r["status"] == "skip":
                            self.state[iid] = "done"
                    self.tree.item(iid, values=v, tags=(r["status"],))
                elif kind == "progress":
                    _, step, done, total = msg
                    self.pb["value"] = step
                    self.lbl_prog.config(text="Progress  %d%%   (%d / %d images)" % (step, done, total))
                elif kind == "done":
                    _, b, a, stopped = msg
                    self.running = False
                    self.btn_go.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.btn_open.config(state="normal")
                    if b:
                        self.lbl_sum.config(text="Saved %s   (%s  ->  %s,  -%.0f%%)" %
                                            (human(b - a), human(b), human(a), 100.0 * (b - a) / b))
                    if stopped:
                        for iid, s in self.state.items():
                            if s == "pending":
                                v = list(self.tree.item(iid, "values"))
                                if v[5] in ("working...", "stopped"):
                                    v[5] = "waiting"
                                    self.tree.item(iid, values=v, tags=())
                    self.update_count()
                    self.lbl_prog.config(text="Stopped - press Shrink! to do the rest" if stopped
                                         else "Done  100%")
                elif kind == "installed":
                    ok, out = msg[1], msg[2]
                    if ok:
                        messagebox.showinfo(APP_NAME, "Boosters installed.\nClose and reopen the app to use them.")
                        self.lbl_tools.config(text="installed - restart the app")
                    else:
                        messagebox.showerror(APP_NAME, "pip failed:\n\n" + out[-1500:])
                        self.lbl_tools.config(text=self.tools_text())
                        if self.btn_boost:
                            self.btn_boost.config(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def install_boosters(self):
        if not messagebox.askyesno(APP_NAME, "Install the optional boosters with pip?\n\n"
                                             "  imagequant  (pngquant engine - best PNG color reduction)\n"
                                             "  pyoxipng  (extra lossless PNG squeeze)\n"
                                             "  numpy  (smoother transparent edges)\n"
                                             "  mozjpeg-lossless-optimization  (lossless JPG squeeze)"):
            return
        self.btn_boost.config(state="disabled")
        self.lbl_tools.config(text="installing with pip... (can take a minute)")
        py = sys.executable
        if os.path.basename(py).lower() == "pythonw.exe":
            cand = os.path.join(os.path.dirname(py), "python.exe")
            if os.path.isfile(cand):
                py = cand

        def run():
            try:
                p = subprocess.run([py, "-m", "pip", "install", "--upgrade", "imagequant", "pyoxipng", "numpy",
                                    "mozjpeg-lossless-optimization"], capture_output=True, text=True,
                                   creationflags=NO_WINDOW)
                self.q.put(("installed", p.returncode == 0, (p.stdout or "") + (p.stderr or "")))
            except Exception as e:
                self.q.put(("installed", False, str(e)))
        threading.Thread(target=run, daemon=True).start()


try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
except Exception:
    TkinterDnD = None


def main():
    if sys.platform == "win32":                       # sharp text on high-DPI screens
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

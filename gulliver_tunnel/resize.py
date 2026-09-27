"""Single-image pipeline: open -> orient -> fit -> sRGB -> flatten/keep alpha -> save clean."""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import pillow_heif
from PIL import ExifTags, Image, ImageOps

try:
    from PIL import ImageCms
except ImportError:  # Pillow built without LittleCMS
    ImageCms = None

pillow_heif.register_heif_opener()  # HEIC / HEIF (iPhone photos)

# These are the user's own photos, so don't refuse very large ones as "decompression bombs".
Image.MAX_IMAGE_PIXELS = None

FORMATS = {
    "jpg": (".jpg", "JPEG"),
    "png": (".png", "PNG"),
}

Size = Tuple[int, int]

_ROTATED_ORIENTATIONS = {5, 6, 7, 8}
_ALPHA_MODES = {"RGBA", "RGBa", "LA", "La", "PA"}
_WIDE_INT_MODES = {"I", "I;16", "I;16L", "I;16B", "I;16N"}
_CMS_MODES = {"RGB", "RGBA", "CMYK", "L"}
_WHITE = (255, 255, 255, 255)
_SRGB = ImageCms.createProfile("sRGB") if ImageCms else None
_DRAFT_FORMATS = {"JPEG", "MPO"}  # MPO: multi-picture JPEGs, common from phones and cameras

# Formats Pillow recognises but that must not be opened here: EPS/PS are rendered by
# Ghostscript (an external interpreter with a long history of security holes), MPEG is
# video, and BUFR/GRIB/HDF5 are stubs with no decoder. Leaving them out of Image.open's
# `formats` also stops a file from reaching them by content, whatever its extension.
_UNSAFE_OR_UNREADABLE = {"EPS", "MPEG", "BUFR", "GRIB", "HDF5"}
Image.init()
_OPEN_FORMATS = tuple(fmt for fmt in Image.OPEN if fmt not in _UNSAFE_OR_UNREADABLE)


@dataclass(frozen=True)
class Options:
    max_width: Optional[int]
    max_height: Optional[int]
    fmt: str = "jpg"
    quality: int = 95
    crop: Optional[str] = None  # None, "fill", "width" or "height" (see crop_size)


@dataclass(frozen=True)
class Result:
    src: Path
    dst: Optional[Path] = None
    src_size: Optional[Size] = None
    dst_size: Optional[Size] = None
    replaced: bool = False
    error: Optional[str] = None


def fit_size(
    width: int,
    height: int,
    max_width: Optional[int],
    max_height: Optional[int],
) -> Size:
    """Shrink (width, height) to fit inside max_width x max_height, keeping the ratio.

    Either bound may be None to constrain only the other side. The smaller of the scale
    factors wins, so the image never exceeds the box on either side. Images are only
    ever scaled down - one that already fits keeps its size.
    """
    scales = [1.0]
    if max_width:
        scales.append(max_width / width)
    if max_height:
        scales.append(max_height / height)
    if len(scales) == 1:
        raise ValueError("at least one of max_width / max_height is required")
    scale = min(scales)
    return max(1, round(width * scale)), max(1, round(height * scale))


def crop_size(
    width: int,
    height: int,
    max_width: int,
    max_height: int,
    crop: str,
) -> Tuple[Size, Size]:
    """Shrink (width, height) for cropping, returning (scaled size, cropped size).

    "fill" shrinks until the image covers the whole box, so the side that sticks out is
    cut. "width" lets the height set the scale and only ever cuts the width; "height" is
    the reverse. Images are never scaled up - one smaller than the box is only cut.
    """
    if crop == "width":
        scale = max_height / height
    elif crop == "height":
        scale = max_width / width
    else:
        scale = max(max_width / width, max_height / height)
    scale = min(1.0, scale)
    scaled = max(1, round(width * scale)), max(1, round(height * scale))
    return scaled, (min(max_width, scaled[0]), min(max_height, scaled[1]))


def readable_extensions() -> set:
    """File extensions of every format this Pillow installation can open."""
    return {ext for ext, fmt in Image.registered_extensions().items() if fmt in _OPEN_FORMATS}


def numbered(path: Path, n: int) -> Path:
    """`photo.jpg` -> `photo (n).jpg`; n == 0 returns the path unchanged."""
    return path if n == 0 else path.with_name(f"{path.stem} ({n}){path.suffix}")


def process_image(src: Path, dst: Path, opts: Options, replace: bool = False) -> Result:
    """Resize `src` and write it to `dst`.

    An existing `dst` is replaced only when `replace` is true; otherwise the result goes
    to `dst (n)` if that name has been taken since the run was planned.

    Never raises for per-file problems; they are reported in `Result.error` so one bad
    file doesn't stop a batch (and so nothing unpicklable crosses process boundaries).
    """
    try:
        with Image.open(src, formats=_OPEN_FORMATS) as im:
            src_size = _oriented_size(im)
            if opts.crop:
                scaled, target = crop_size(*src_size, opts.max_width, opts.max_height, opts.crop)
            else:
                scaled = target = fit_size(*src_size, opts.max_width, opts.max_height)
            _draft(im, scaled)
            img = _normalise_mode(ImageOps.exif_transpose(im))
        # Colour conversion and flattening run on the small result, not the full-size photo.
        img = _flatten(_to_srgb(_fit(img, scaled, target)), opts.fmt)
        written, replaced = _save(img, dst, opts, replace)
    except Exception as exc:
        return Result(src, error=f"{type(exc).__name__}: {exc}")
    return Result(src, written, src_size, img.size, replaced)


def _orientation(im: Image.Image) -> int:
    try:
        return int(im.getexif().get(ExifTags.Base.Orientation, 1))
    except Exception:
        return 1


def _oriented_size(im: Image.Image) -> Size:
    w, h = im.size
    return (h, w) if _orientation(im) in _ROTATED_ORIENTATIONS else (w, h)


def _draft(im: Image.Image, target: Size) -> None:
    """Let the JPEG decoder downscale by 1/2, 1/4 or 1/8 while decoding (much faster).

    Asks for at least 2x the target so the final Lanczos pass still has detail to work
    with - the same margin Pillow's own thumbnail() uses.
    """
    if im.format not in _DRAFT_FORMATS:
        return
    w, h = target
    if _orientation(im) in _ROTATED_ORIENTATIONS:
        w, h = h, w
    im.draft(None, (w * 2, h * 2))


def _normalise_mode(img: Image.Image) -> Image.Image:
    """Bring any mode down to 8-bit "RGB", "RGBA", "CMYK" or "L", which resize well.

    The colour profile stays in `img.info` for `_to_srgb`, after resizing.
    """
    has_alpha = img.mode in _ALPHA_MODES or "transparency" in img.info

    if img.mode in _WIDE_INT_MODES:
        # 16-bit greyscale: scale into 0-255 instead of letting convert() clip to white.
        img = img.convert("I").point(lambda v: v / 256).convert("L")

    if img.mode not in _CMS_MODES or (has_alpha and img.mode != "RGBA"):
        img = img.convert("RGBA" if has_alpha else "RGB")  # palette, LA, 1-bit, YCbCr, tRNS, ...
    return img


def _to_srgb(img: Image.Image) -> Image.Image:
    """Convert a `_normalise_mode` image to 8-bit sRGB "RGB" or "RGBA".

    The embedded ICC profile is about to be stripped along with the rest of the
    metadata, so pixels are converted into sRGB first (e.g. Display P3 iPhone photos,
    CMYK scans) - otherwise colours would shift once the profile is gone.
    """
    out_mode = "RGBA" if img.mode == "RGBA" else "RGB"
    icc = img.info.get("icc_profile")
    if icc and ImageCms is not None:
        try:
            src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            img = ImageCms.profileToProfile(img, src_profile, _SRGB, outputMode=out_mode)
        except Exception:
            pass  # broken/unsupported profile: fall back to a plain conversion

    return img if img.mode == out_mode else img.convert(out_mode)


def _fit(img: Image.Image, scaled: Size, target: Size) -> Image.Image:
    """Scale `img` to `scaled` and keep the centred `target` part of it."""
    w, h = img.size
    if img.size == scaled:
        if scaled != target:  # crop only, no scaling: cut on whole pixels so nothing blurs
            left, top = (w - target[0]) // 2, (h - target[1]) // 2
            img = img.crop((left, top, left + target[0], top + target[1]))
        return img
    # The centred source area that becomes `target`, in img's own pixels (a JPEG draft
    # may already have shrunk it, so `scaled` can't be used directly).
    box_w, box_h = target[0] * w / scaled[0], target[1] * h / scaled[1]
    box = ((w - box_w) / 2, (h - box_h) / 2, (w + box_w) / 2, (h + box_h) / 2)
    # Pillow premultiplies alpha internally for RGBA, so edges don't get dark fringes.
    return img.resize(target, Image.Resampling.LANCZOS, box=box, reducing_gap=3.0)


def _flatten(img: Image.Image, fmt: str) -> Image.Image:
    if img.mode == "RGBA" and (fmt == "jpg" or img.getchannel("A").getextrema() == (255, 255)):
        # JPEG has no alpha: flatten onto white. Fully opaque PNGs drop the useless channel.
        background = Image.new("RGBA", img.size, _WHITE)
        background.alpha_composite(img)
        img = background.convert("RGB")
    return img


def _save(img: Image.Image, dst: Path, opts: Options, replace: bool) -> Tuple[Path, bool]:
    """Write `img` to `dst`, returning the path used and whether a file was replaced.

    The image goes into a hidden ".name.part" file first and is then moved into place in
    one step, so `dst` is never left half-written, even if the run is stopped.
    """
    # Rebuild from raw pixels so no EXIF / XMP / ICC / text chunks can ride along.
    clean = Image.frombytes(img.mode, img.size, img.tobytes())
    _, pil_format = FORMATS[opts.fmt]
    if opts.fmt == "jpg":
        # 4:4:4 chroma at high quality settings; Pillow's 4:2:0 default smears colour edges.
        params = {"quality": opts.quality, "optimize": True, "subsampling": 0 if opts.quality >= 90 else 2}
    else:
        params = {"compress_level": 6}

    dst.parent.mkdir(parents=True, exist_ok=True)
    temp = dst.with_name(f".{dst.name}.part")
    reserved = None
    try:
        clean.save(temp, format=pil_format, **params)
        if replace and dst.is_file():
            path = dst
        else:
            path = reserved = _reserve_name(dst)
            replace = False
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        if reserved is not None:
            reserved.unlink(missing_ok=True)
        raise
    return path, replace


def _reserve_name(dst: Path) -> Path:
    """Create the first free name among dst, dst (1), dst (2), ... without any overwrite race."""
    n = 0
    while True:
        candidate = numbered(dst, n)
        try:
            open(candidate, "xb").close()
            return candidate
        except FileExistsError:
            n += 1

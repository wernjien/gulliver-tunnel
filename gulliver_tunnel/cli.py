"""Command-line entry point: `gulliver INPUT_DIR [-w WIDTH] [-h HEIGHT] [options]`."""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from itertools import repeat
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

from . import __version__
from .paths import Job, find_images, pictures_dir, plan_outputs, remember_result, results_folder
from .resize import FORMATS, Options, Result, process_image

DEFAULT_SIZE = 1024  # used for both sides when neither -w nor -h is given
MEMORY_PER_JOB = 2 * 1024**3  # a big PNG, TIFF or HEIC can take hundreds of MB to decode


def default_jobs() -> int:
    """One job per CPU core, but at most one for every 2 GB of memory."""
    jobs = os.cpu_count() or 1
    memory = _total_memory()
    if memory:
        jobs = min(jobs, max(1, memory // MEMORY_PER_JOB))
    return jobs


def _total_memory() -> Optional[int]:
    """Physical memory in bytes, or None if it can't be found."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX(dwLength=ctypes.sizeof(MEMORYSTATUSEX))
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys
            return None
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return None


def _pixels(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("Must be a positive number of pixels.")
    return value


def _quality(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 100:
        raise argparse.ArgumentTypeError("Quality must be between 1 and 100.")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gulliver",
        description=(
            "Shrink every image under a folder (recursively) to fit within a size, "
            "keeping the aspect ratio. You can also crop images to fill the size. Images are "
            "never enlarged. Metadata is removed. Your own files are never overwritten, and "
            "running again replaces the earlier results."
        ),
        add_help=False,  # -h is the height
    )
    parser.add_argument("input", type=Path, help="Folder with the images. Sub-folders are included.")
    parser.add_argument("-w", "--width", type=_pixels, help=f"Maximum width in pixels. If you give neither -w nor -h, both are {DEFAULT_SIZE}.")
    parser.add_argument("-h", "--height", type=_pixels, help=f"Maximum height in pixels. If you give neither -w nor -h, both are {DEFAULT_SIZE}.")
    crop = parser.add_mutually_exclusive_group()
    crop.add_argument(
        "--crop",
        dest="crop",
        action="store_const",
        const="fill",
        help="Fill the whole size and cut off the parts that stick out. Needs both -w and -h, or neither.",
    )
    crop.add_argument(
        "--crop-width",
        dest="crop",
        action="store_const",
        const="width",
        help="Shrink to the height and cut off the left and right if the image is too wide. Needs both -w and -h, or neither.",
    )
    crop.add_argument(
        "--crop-height",
        dest="crop",
        action="store_const",
        const="height",
        help="Shrink to the width and cut off the top and bottom if the image is too tall. Needs both -w and -h, or neither.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="FOLDER",
        help=(
            f"Folder to save into. The default is {pictures_dir()}. A folder with the input "
            "folder's name is created inside it. If that would be the input folder itself, "
            'or a folder around it, " (small)" is added to the name.'
        ),
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=sorted(FORMATS),
        default="jpg",
        help="Output format. The default is jpg. PNG keeps transparency; JPG fills it with white.",
    )
    parser.add_argument(
        "-q", "--quality", type=_quality, help="JPG quality from 1 to 100. The default is 95."
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=default_jobs(),
        help=(
            "Number of images processed at the same time. The default is one per CPU core, "
            "but at most one for every 2 GB of memory. On this computer it is %(default)s."
        ),
    )
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}", help="Show the version number and exit.")
    return parser


def run(jobs: List[Job], opts: Options, workers: int) -> Iterator[Result]:
    """Process jobs, yielding results in input order."""
    columns = (
        [job.src for job in jobs],
        [job.dst for job in jobs],
        repeat(opts),
        [job.replace for job in jobs],
    )
    if workers <= 1:
        yield from map(process_image, *columns)
        return
    with ProcessPoolExecutor(max_workers=workers) as pool:
        try:
            yield from pool.map(process_image, *columns)
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    input_root = args.input.expanduser().resolve()
    if not input_root.is_dir():
        parser.error(f"Input folder not found: {args.input}")
    if args.width is None and args.height is None:
        args.width = args.height = DEFAULT_SIZE
    if args.crop and (args.width is None or args.height is None):
        parser.error("Cropping needs both a width (-w) and a height (-h), or neither.")
    if args.jobs < 1:
        parser.error("The number of jobs (-j) must be at least 1.")
    if args.quality is not None and args.format != "jpg":
        print("Note: Quality (-q) only applies to JPG output, so it is ignored.", file=sys.stderr)

    save_into = args.output.expanduser() if args.output else pictures_dir()
    output_root = results_folder(save_into, input_root)
    for folder in (save_into, output_root):
        if folder.exists() and not folder.is_dir():
            parser.error(f"This is a file, not a folder: {folder}")
    opts = Options(
        max_width=args.width,
        max_height=args.height,
        fmt=args.format,
        quality=args.quality if args.quality is not None else 95,
        crop=args.crop,
    )

    sources = find_images(input_root, exclude=[save_into, output_root])
    if not sources:
        print(f"No images found in {input_root}.")
        return 0
    extension, _ = FORMATS[opts.fmt]
    jobs = plan_outputs(sources, input_root, output_root, extension)

    quality = f" at quality {opts.quality}" if opts.fmt == "jpg" else ""
    print(f"Resizing {len(jobs)} image(s) from {input_root}")
    if opts.crop == "fill":
        size = f"fill width {args.width} and height {args.height}, cropping what sticks out,"
    elif opts.crop == "width":
        size = f"fit height {args.height} and crop the width to {args.width},"
    elif opts.crop == "height":
        size = f"fit width {args.width} and crop the height to {args.height},"
    else:
        limits = [f"width {args.width}" if args.width else "", f"height {args.height}" if args.height else ""]
        size = f"fit max {' and '.join(filter(None, limits))}"
    print(f"  to {size} as {opts.fmt.upper()}{quality}, into {output_root}")

    width = len(str(len(jobs)))
    failed = 0
    try:
        for i, result in enumerate(run(jobs, opts, min(args.jobs, len(jobs))), 1):
            src = result.src.relative_to(input_root)
            if result.error:
                failed += 1
                print(f"[{i:>{width}}/{len(jobs)}] FAILED {src}: {result.error}", file=sys.stderr)
                continue
            (sw, sh), (dw, dh) = result.src_size, result.dst_size
            remember_result(output_root, result.dst)
            dst = result.dst.relative_to(output_root)
            replaced = ", replaced" if result.replaced else ""
            print(f"[{i:>{width}}/{len(jobs)}] {src} -> {dst}  ({sw}x{sh} -> {dw}x{dh}{replaced})")
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenProcessPool:
        print(
            "\nStopped: a worker process ended unexpectedly, usually because the computer ran "
            "out of memory. Try again with fewer images at a time, such as -j 2. Images that "
            "were already saved are replaced, not copied.",
            file=sys.stderr,
        )
        return 1

    print(f"Done: {len(jobs) - failed} saved, {failed} failed.")
    return 1 if failed else 0

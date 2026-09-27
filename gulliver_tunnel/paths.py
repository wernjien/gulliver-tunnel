"""Locating the user's Pictures folder, the images to shrink and where their results go."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path, PurePath
from typing import Iterable, List, NamedTuple, Optional, Set

from .resize import numbered, readable_extensions

# FOLDERID_Pictures
_WINDOWS_PICTURES_GUID = "{33E28130-4E1E-4676-835A-98395C3BC3BB}"

# Hidden list, kept in each results folder, of the files Gulliver Tunnel saved there. Only
# these are replaced on a later run; any other file with the same name is left alone.
RESULTS_LIST = ".gulliver-tunnel"
_FILE_ATTRIBUTE_HIDDEN = 0x2
_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class Job(NamedTuple):
    src: Path
    dst: Path
    replace: bool  # dst is an earlier result of Gulliver Tunnel's, so it may be replaced


def pictures_dir() -> Path:
    """The current user's Pictures folder on macOS, Windows or Linux.

    On Windows this asks the shell for the real location, which differs from
    %USERPROFILE%\\Pictures when the folder is redirected (e.g. to OneDrive).
    """
    found = None
    if sys.platform == "win32":
        found = _windows_known_folder(_WINDOWS_PICTURES_GUID)
    elif sys.platform != "darwin":
        found = _xdg_user_dir("PICTURES")
    return found or Path.home() / "Pictures"


def same_folder(a: Path, b: Path) -> bool:
    """True if `a` and `b` are the same existing folder, however they are spelled.

    Comparing paths as text isn't enough: macOS and Windows ignore letter case, so
    `~/pictures/holiday` and `~/Pictures/Holiday` are one folder.
    """
    try:
        return a.samefile(b)
    except OSError:
        return False


def folder_name(folder: PurePath) -> str:
    """The name for `folder`'s results folder: its own name, or a name for a drive root.

    A drive root such as `E:\\` (an SD card on Windows) has no name of its own, so the
    drive's label is used, such as "EOS_DIGITAL", or else "Drive E".
    """
    if folder.name:
        return folder.name
    label = _windows_volume_label(folder.anchor) if sys.platform == "win32" else None
    if label:
        return label
    drive = folder.drive.rstrip(":")
    if len(drive) == 1:
        return f"Drive {drive.upper()}"
    parts = [part for part in re.split(r"[\\/]", drive) if part]  # \\server\share
    return parts[-1] if parts else "Root"


def results_folder(save_into: Path, input_root: Path) -> Path:
    """The folder inside `save_into` that receives the results for `input_root`.

    It is named after the input folder. When that would be the input folder itself, or a
    folder around it (e.g. `~/Pictures/Holiday` saved into `~/Pictures`), " (small)" is
    added so results never mix with, or replace, the originals.
    """
    name = folder_name(input_root)
    while True:
        output_root = (save_into / name).resolve()
        if not any(same_folder(output_root, p) for p in (input_root, *input_root.parents)):
            return output_root
        name += " (small)"


def find_images(root: Path, exclude: Iterable[Path] = ()) -> List[Path]:
    """All readable image files under `root`, recursively, in a stable sorted order.

    Hidden files/folders (".DS_Store", "._IMG_0001.JPG", ".thumbnails") are ignored, as
    are the `exclude` folders - the output folders, when they live inside the input folder.
    """
    extensions = readable_extensions()
    excluded = set()
    for folder in exclude:
        try:
            info = os.stat(folder)
            excluded.add((info.st_dev, info.st_ino))
        except OSError:
            pass  # doesn't exist yet, so it can't be inside the tree being walked

    def is_excluded(folder: Path) -> bool:
        try:
            info = os.stat(folder)
        except OSError:
            return False
        return (info.st_dev, info.st_ino) in excluded

    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and not is_excluded(here / d)
        )
        for name in sorted(filenames):
            if not name.startswith(".") and os.path.splitext(name)[1].lower() in extensions:
                found.append(here / name)
    return found


def plan_outputs(
    sources: Iterable[Path], input_root: Path, output_root: Path, extension: str
) -> List[Job]:
    """Pair every source with a destination that mirrors its place in the input tree.

    An earlier result of Gulliver Tunnel's is replaced, so running again doesn't make
    copies. A name taken by any other file, or by an earlier source in this run (e.g.
    `a.png` and `a.gif` both becoming `a.jpg`), gets a " (n)" suffix instead. Matching is
    case-insensitive because the default macOS and Windows file systems are.
    """
    earlier = earlier_results(output_root)
    taken = set()
    jobs = []
    for src in sources:
        base = output_root / src.relative_to(input_root).with_suffix(extension)
        n = 0
        while True:
            dst = numbered(base, n)
            key = _result_key(output_root, dst)
            if key not in taken:
                exists = dst.exists()
                if not exists or key in earlier:
                    break
            n += 1
        taken.add(key)
        jobs.append(Job(src, dst, replace=exists))
    return jobs


def earlier_results(output_root: Path) -> Set[str]:
    try:
        text = (output_root / RESULTS_LIST).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    return {line.casefold() for line in text.splitlines() if line}


def remember_result(output_root: Path, dst: Path) -> None:
    """Add `dst` to the results list, so a later run may replace it."""
    path = output_root / RESULTS_LIST
    new = not path.exists()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(dst.relative_to(output_root).as_posix() + "\n")
    if new and sys.platform == "win32":
        _hide_on_windows(path)  # a leading dot only hides files on macOS and Linux


def _result_key(output_root: Path, dst: Path) -> str:
    return dst.relative_to(output_root).as_posix().casefold()


def _hide_on_windows(path: Path) -> None:
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(path), _FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass


def _windows_volume_label(root: str) -> Optional[str]:
    try:
        import ctypes

        label = ctypes.create_unicode_buffer(261)
        if not ctypes.windll.kernel32.GetVolumeInformationW(
            root, label, len(label), None, None, None, None, 0
        ):
            return None
    except Exception:
        return None
    return _UNSAFE_NAME_CHARS.sub("_", label.value).strip(" .") or None


def _windows_known_folder(guid: str) -> Optional[Path]:
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        u = uuid.UUID(guid)
        folder_id = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(out)):
            return None
        try:
            return Path(out.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        return None


def _xdg_user_dir(name: str) -> Optional[Path]:
    try:
        value = subprocess.run(
            ["xdg-user-dir", name], capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
    except Exception:
        return None
    path = Path(value) if value else None
    # xdg-user-dir falls back to $HOME when the folder isn't configured.
    return path if path and path != Path.home() else None

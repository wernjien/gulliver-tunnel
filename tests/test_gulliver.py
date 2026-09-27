from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from PIL import Image, ImageCms

from gulliver_tunnel import cli, paths
from gulliver_tunnel.cli import main
from gulliver_tunnel.resize import crop_size, fit_size


def make(path: Path, mode="RGB", size=(400, 300), color="red", fmt=None, **save):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color).save(path, format=fmt, **save)
    return path


def names(folder: Path) -> list:
    """The visible files and folders in `folder`, leaving out the hidden results list."""
    return sorted(p.name for p in folder.iterdir() if not p.name.startswith("."))


def run(src: Path, out: Path, *extra: str) -> int:
    """Run gulliver with `-o out`. The results land in `out / src.name`."""
    return main([str(src), *extra, "-o", str(out), "-j", "1"])


# 1. fit to size ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "size, box, expected",
    [
        ((4000, 3000), (1920, 1080), (1440, 1080)),  # height is the limiting side
        ((3000, 1000), (1920, 1080), (1920, 640)),  # width is the limiting side
        ((1000, 2000), (1920, 1080), (540, 1080)),
        ((4000, 3000), (1000, None), (1000, 750)),
        ((4000, 3000), (None, 600), (800, 600)),
        ((200, 100), (1000, 1000), (200, 100)),  # already fits: never enlarged
        ((200, 100), (100, 1000), (100, 50)),
        ((200, 100), (None, 1000), (200, 100)),
    ],
)
def test_fit_size(size, box, expected):
    assert fit_size(*size, *box) == expected


def test_small_images_are_not_enlarged(tmp_path):
    src = tmp_path / "in"
    make(src / "icon.png", size=(64, 48))
    run(src, tmp_path / "out", "-w", "1920", "-h", "1080")
    assert Image.open(tmp_path / "out" / "in" / "icon.jpg").size == (64, 48)


def test_width_or_height_alone(tmp_path):
    src = tmp_path / "in"
    make(src / "a.png", size=(400, 300))
    run(src, tmp_path / "w", "-w", "200")
    run(src, tmp_path / "h", "--height", "100")
    assert Image.open(tmp_path / "w" / "in" / "a.jpg").size == (200, 150)
    assert Image.open(tmp_path / "h" / "in" / "a.jpg").size == (133, 100)


def test_default_size_when_neither_given(tmp_path):
    src = tmp_path / "in"
    make(src / "wide.png", size=(5000, 2500))
    make(src / "tall.png", size=(1000, 5000))
    assert run(src, tmp_path / "out") == 0
    assert Image.open(tmp_path / "out" / "in" / "wide.jpg").size == (2000, 1000)
    assert Image.open(tmp_path / "out" / "in" / "tall.jpg").size == (400, 2000)


def test_width_alone_does_not_limit_height(tmp_path):
    src = tmp_path / "in"
    make(src / "tall.png", size=(1000, 5000))
    run(src, tmp_path / "out", "-w", "1200")
    assert Image.open(tmp_path / "out" / "in" / "tall.jpg").size == (1000, 5000)


@pytest.mark.parametrize("args", [["-w", "0"], ["-h", "-5"], ["-w", "abc"]])
def test_invalid_size_rejected(tmp_path, args):
    with pytest.raises(SystemExit):
        main([str(tmp_path), *args])


def test_resizes_and_mirrors_hierarchy(tmp_path):
    src = tmp_path / "in"
    make(src / "a.png", size=(400, 300))
    make(src / "sub" / "b.bmp", size=(100, 400))
    make(src / "sub" / "deeper" / "c.tiff", size=(300, 300))
    (src / "notes.txt").write_text("not an image")
    make(src / ".hidden.png")

    assert run(src, tmp_path / "out", "-w", "200", "-h", "200") == 0

    out = tmp_path / "out" / "in"
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p.name[0] != ".") == [
        "a.jpg",
        "sub/b.jpg",
        "sub/deeper/c.jpg",
    ]
    assert Image.open(out / "a.jpg").size == (200, 150)
    assert Image.open(out / "sub" / "b.jpg").size == (50, 200)
    assert Image.open(out / "sub" / "deeper" / "c.jpg").size == (200, 200)


def test_exif_rotation_applied_before_fitting(tmp_path):
    src = tmp_path / "in"
    exif = Image.Exif()
    exif[0x0112] = 6  # stored landscape, displayed portrait
    make(src / "phone.jpg", size=(400, 300), exif=exif)

    run(src, tmp_path / "out", "-w", "100", "-h", "100")
    assert Image.open(tmp_path / "out" / "in" / "phone.jpg").size == (75, 100)


# 1b. crop to fit ----------------------------------------------------------------


@pytest.mark.parametrize(
    "size, box, crop, expected",
    [
        ((4000, 3000), (1920, 1080), "fill", ((1920, 1440), (1920, 1080))),  # cuts the height
        ((6000, 2000), (1920, 1080), "fill", ((3240, 1080), (1920, 1080))),  # cuts the width
        ((6000, 2000), (1920, 1080), "width", ((3240, 1080), (1920, 1080))),
        ((4000, 3000), (1920, 1080), "width", ((1440, 1080), (1440, 1080))),  # narrow enough
        ((3000, 4000), (1920, 1080), "height", ((1920, 2560), (1920, 1080))),
        ((6000, 2000), (1920, 1080), "height", ((1920, 640), (1920, 640))),  # short enough
        ((2000, 800), (1920, 1080), "fill", ((2000, 800), (1920, 800))),  # never enlarged
        ((1000, 800), (1920, 1080), "fill", ((1000, 800), (1000, 800))),
        ((3000, 800), (1920, 1080), "width", ((3000, 800), (1920, 800))),
    ],
)
def test_crop_size(size, box, crop, expected):
    assert crop_size(*size, *box, crop) == expected


def _stripes(path: Path, size, vertical: bool):
    """Red, green and blue thirds, left to right (or top to bottom)."""
    w, h = size
    im = Image.new("RGB", size, "red")
    if vertical:
        im.paste((0, 255, 0), (w // 3, 0, 2 * w // 3, h))
        im.paste((0, 0, 255), (2 * w // 3, 0, w, h))
    else:
        im.paste((0, 255, 0), (0, h // 3, w, 2 * h // 3))
        im.paste((0, 0, 255), (0, 2 * h // 3, w, h))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def _is_green(pixel):
    r, g, b = pixel
    return g > 200 and r < 60 and b < 60


@pytest.mark.parametrize("fmt", ["png", "jpg"])  # jpg input also takes the fast draft path
def test_crop_fill_keeps_the_centre(tmp_path, fmt):
    src = tmp_path / "in"
    _stripes(src / f"wide.{fmt}", (3000, 1000), vertical=True)
    assert run(src, tmp_path / "out", "-w", "100", "-h", "100", "--crop") == 0
    with Image.open(tmp_path / "out" / "in" / "wide.jpg") as im:
        assert im.size == (100, 100)
        assert _is_green(im.getpixel((5, 50))) and _is_green(im.getpixel((94, 50)))


def test_crop_width_only_cuts_the_sides(tmp_path):
    src = tmp_path / "in"
    _stripes(src / "wide.png", (3000, 1000), vertical=True)
    make(src / "tall.png", size=(1000, 3000))
    run(src, tmp_path / "out", "-w", "100", "-h", "100", "--crop-width")
    out = tmp_path / "out" / "in"
    with Image.open(out / "wide.jpg") as im:
        assert im.size == (100, 100)
        assert _is_green(im.getpixel((5, 50)))
    assert Image.open(out / "tall.jpg").size == (33, 100)


def test_crop_height_only_cuts_top_and_bottom(tmp_path):
    src = tmp_path / "in"
    _stripes(src / "tall.png", (1000, 3000), vertical=False)
    make(src / "wide.png", size=(3000, 1000))
    run(src, tmp_path / "out", "-w", "100", "-h", "100", "--crop-height")
    out = tmp_path / "out" / "in"
    with Image.open(out / "tall.jpg") as im:
        assert im.size == (100, 100)
        assert _is_green(im.getpixel((50, 5)))
    assert Image.open(out / "wide.jpg").size == (100, 33)


def test_crop_small_image_is_cut_not_enlarged(tmp_path):
    src = tmp_path / "in"
    _stripes(src / "a.png", (300, 90), vertical=True)
    run(src, tmp_path / "out", "-w", "100", "-h", "200", "--crop", "-f", "png")
    with Image.open(tmp_path / "out" / "in" / "a.png") as im:
        assert im.size == (100, 90)
        assert im.getpixel((0, 0)) == (0, 255, 0) and im.getpixel((99, 89)) == (0, 255, 0)


def test_crop_uses_default_size(tmp_path):
    src = tmp_path / "in"
    make(src / "a.png", size=(5000, 2500))
    run(src, tmp_path / "out", "--crop")
    assert Image.open(tmp_path / "out" / "in" / "a.jpg").size == (2000, 2000)


def test_crop_after_exif_rotation(tmp_path):
    src = tmp_path / "in"
    exif = Image.Exif()
    exif[0x0112] = 6  # stored 400x300, displayed 300x400
    make(src / "phone.jpg", size=(400, 300), exif=exif)
    run(src, tmp_path / "out", "-w", "150", "-h", "100", "--crop")
    assert Image.open(tmp_path / "out" / "in" / "phone.jpg").size == (150, 100)


@pytest.mark.parametrize(
    "args",
    [["-w", "100", "--crop"], ["-h", "100", "--crop-width"], ["--crop", "--crop-height"]],
)
def test_invalid_crop_rejected(tmp_path, args):
    with pytest.raises(SystemExit):
        main([str(tmp_path), *args])


# 2. never overwrite -----------------------------------------------------------------


def test_other_files_are_kept(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out" / "in"
    make(src / "a.png")
    make(out / "a.jpg", color="blue")
    original = (out / "a.jpg").read_bytes()

    run(src, out.parent, "-w", "100", "-h", "100")
    run(src, out.parent, "-w", "100", "-h", "100")

    assert (out / "a.jpg").read_bytes() == original
    assert names(out) == ["a (1).jpg", "a.jpg"]  # the second run replaced its own a (1).jpg


def test_running_again_replaces_earlier_results(tmp_path, capsys):
    src, out = tmp_path / "in", tmp_path / "out" / "in"
    make(src / "a.png", size=(400, 300))
    make(src / "a.gif", mode="P", size=(400, 300), color=1)
    make(src / "sub" / "b.png", size=(400, 300))
    run(src, out.parent, "-w", "200")
    capsys.readouterr()

    run(src, out.parent, "-w", "100")

    assert names(out) == ["a (1).jpg", "a.jpg", "sub"] and names(out / "sub") == ["b.jpg"]
    assert Image.open(out / "a.jpg").size == (100, 75)
    assert Image.open(out / "a (1).jpg").size == (100, 75)
    assert Image.open(out / "sub" / "b.jpg").size == (100, 75)
    assert capsys.readouterr().out.count(", replaced)") == 3


def test_replacing_leaves_no_temporary_files(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out" / "in"
    make(src / "a.png")
    run(src, out.parent, "-w", "50")
    run(src, out.parent, "-w", "50")
    assert sorted(p.name for p in out.iterdir()) == [".gulliver-tunnel", "a.jpg"]


def test_same_stem_different_formats_keep_both(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out" / "in"
    make(src / "a.png", color="red")
    make(src / "a.gif", mode="P", color=1)
    run(src, out.parent, "-w", "100", "-h", "100")
    assert names(out) == ["a (1).jpg", "a.jpg"]


def test_existing_name_taken_after_planning_is_not_overwritten(tmp_path):
    from gulliver_tunnel.resize import Options, process_image

    src = make(tmp_path / "a.png")
    dst = make(tmp_path / "out" / "in" / "a.jpg", color="blue")
    before = dst.read_bytes()
    result = process_image(src, dst, Options(100, 100))
    assert result.dst.name == "a (1).jpg"
    assert dst.read_bytes() == before


# 3. metadata removed ----------------------------------------------------------------


def test_metadata_is_removed(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # Make
    exif[0x0132] = "2024:01:01 00:00:00"  # DateTime
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    make(src / "photo.jpg", exif=exif, icc_profile=icc, comment=b"hello")
    png_info = __import__("PIL.PngImagePlugin", fromlist=["PngInfo"]).PngInfo()
    png_info.add_text("Author", "Someone")
    make(src / "art.png", pnginfo=png_info, exif=exif, icc_profile=icc)

    for fmt in ("jpg", "png"):
        target = out / fmt
        run(src, target, "-w", "100", "-h", "100", "-f", fmt)
        for f in (target / "in").glob("[!.]*"):
            raw = f.read_bytes()
            assert b"SecretCam" not in raw and b"Someone" not in raw and b"hello" not in raw
            with Image.open(f) as im:
                assert not im.getexif()
                assert "icc_profile" not in im.info
                assert not {"exif", "comment", "Author", "xmp", "XML:com.adobe.xmp"} & set(im.info)


# 4. format & quality ----------------------------------------------------------------


def test_jpeg_quality(tmp_path):
    src = tmp_path / "in"
    noise = Image.effect_noise((400, 400), 80).convert("RGB")
    (src).mkdir()
    noise.save(src / "noise.png")

    run(src, tmp_path / "q95", "-w", "400", "-h", "400")
    run(src, tmp_path / "q40", "-w", "400", "-h", "400", "-q", "40")
    assert (tmp_path / "q40" / "in" / "noise.jpg").stat().st_size < (tmp_path / "q95" / "in" / "noise.jpg").stat().st_size


def test_invalid_quality_rejected(tmp_path):
    with pytest.raises(SystemExit):
        main([str(tmp_path), "-w", "100", "-h", "100", "-q", "101"])


# 5. transparency --------------------------------------------------------------------


def _half_transparent(path: Path):
    im = Image.new("RGBA", (200, 200), (0, 0, 255, 0))
    im.paste((0, 0, 255, 255), (50, 50, 150, 150))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def test_png_keeps_transparency(tmp_path):
    src = tmp_path / "in"
    _half_transparent(src / "logo.png")
    run(src, tmp_path / "out", "-w", "100", "-h", "100", "-f", "png")
    with Image.open(tmp_path / "out" / "in" / "logo.png") as im:
        assert im.mode == "RGBA"
        assert im.getpixel((0, 0))[3] == 0
        assert im.getpixel((50, 50)) == (0, 0, 255, 255)


def test_jpg_fills_transparency_with_white(tmp_path):
    src = tmp_path / "in"
    _half_transparent(src / "logo.png")
    run(src, tmp_path / "out", "-w", "100", "-h", "100")
    with Image.open(tmp_path / "out" / "in" / "logo.jpg") as im:
        assert im.mode == "RGB"
        assert all(c > 245 for c in im.getpixel((2, 2)))
        r, g, b = im.getpixel((50, 50))
        assert b > 200 and r < 30 and g < 30


def test_palette_transparency_kept_for_png(tmp_path):
    src = tmp_path / "in"
    im = Image.new("P", (100, 100), 0)
    im.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
    im.paste(1, (25, 25, 75, 75))
    src.mkdir()
    im.save(src / "anim.gif", transparency=0)

    run(src, tmp_path / "out", "-w", "50", "-h", "50", "-f", "png")
    with Image.open(tmp_path / "out" / "in" / "anim.png") as out:
        assert out.mode == "RGBA"
        assert out.getpixel((0, 0))[3] == 0
        assert out.getpixel((25, 25)) == (0, 255, 0, 255)


def test_opaque_png_drops_alpha(tmp_path):
    src = tmp_path / "in"
    make(src / "flat.png", mode="RGBA", color=(10, 20, 30, 255))
    run(src, tmp_path / "out", "-w", "50", "-h", "50", "-f", "png")
    assert Image.open(tmp_path / "out" / "in" / "flat.png").mode == "RGB"


# 6. default output location ---------------------------------------------------------


def test_default_output_is_under_pictures(tmp_path, monkeypatch):
    pictures = tmp_path / "Pictures"
    monkeypatch.setattr(cli, "pictures_dir", lambda: pictures)
    src = tmp_path / "Holiday"
    make(src / "day1" / "beach.png")

    assert main([str(src), "-w", "1920", "-h", "1080", "-j", "1"]) == 0
    assert (pictures / "Holiday" / "day1" / "beach.jpg").exists()


def test_current_folder_as_input_uses_its_name(tmp_path, monkeypatch):
    pictures = tmp_path / "Pictures"
    monkeypatch.setattr(cli, "pictures_dir", lambda: pictures)
    cwd = tmp_path / "Desktop" / "FooFoo" / "BarBar"
    make(cwd / "sub" / "photo.png", size=(4000, 3000))
    monkeypatch.chdir(cwd)

    assert main([".", "-w2000", "-j", "1"]) == 0
    assert Image.open(pictures / "BarBar" / "sub" / "photo.jpg").size == (2000, 1500)


def test_pictures_dir_is_in_home():
    assert paths.pictures_dir() == Path.home() / "Pictures"


def test_output_inside_input_is_not_reprocessed(tmp_path):
    src = tmp_path / "in"
    make(src / "a.png")
    make(src / "resized" / "old.jpg")
    run(src, src / "resized", "-w", "50", "-h", "50")
    assert names(src / "resized") == ["in", "old.jpg"]
    assert names(src / "resized" / "in") == ["a.jpg"]


# odd inputs -------------------------------------------------------------------------


def test_cmyk_and_16bit_and_grayscale(tmp_path):
    src = tmp_path / "in"
    make(src / "print.jpg", mode="CMYK", color=(0, 255, 255, 0))  # red in CMYK
    Image.new("I;16", (100, 100), 32768).save(src / "scan.png")
    make(src / "bw.png", mode="L", color=128)
    make(src / "line.bmp", mode="1", color=1)

    assert run(src, tmp_path / "out", "-w", "50", "-h", "50") == 0
    out = tmp_path / "out" / "in"
    r, g, b = Image.open(out / "print.jpg").getpixel((10, 10))
    assert r > 200 and g < 60 and b < 60
    assert 110 < Image.open(out / "scan.jpg").getpixel((10, 10))[0] < 145
    assert Image.open(out / "line.jpg").getpixel((10, 10)) == (255, 255, 255)


DISPLAY_P3 = Path("/System/Library/ColorSync/Profiles/Display P3.icc")


@pytest.mark.skipif(not DISPLAY_P3.exists(), reason="needs the macOS Display P3 profile")
def test_wide_gamut_converted_to_srgb(tmp_path):
    # Display P3 pixels (iPhone photos) must be converted to sRGB, not just untagged.
    src = tmp_path / "in"
    src.mkdir()
    p3_orange = (230, 120, 40)
    Image.new("RGB", (50, 50), p3_orange).save(src / "iphone.png", icc_profile=DISPLAY_P3.read_bytes())
    run(src, tmp_path / "out", "-w", "50", "-h", "50", "-f", "png")
    r, g, b = Image.open(tmp_path / "out" / "in" / "iphone.png").getpixel((5, 5))
    assert r > p3_orange[0] and b < p3_orange[2]  # sRGB needs more saturated values


def test_heic_input(tmp_path):
    import pillow_heif

    src = tmp_path / "in"
    src.mkdir()
    pillow_heif.from_pillow(Image.new("RGB", (400, 300), "red")).save(src / "IMG_0001.HEIC")
    assert run(src, tmp_path / "out", "-w", "200") == 0
    with Image.open(tmp_path / "out" / "in" / "IMG_0001.jpg") as im:
        assert im.size == (200, 150)
        r, g, b = im.getpixel((10, 10))
        assert r > 200 and g < 60 and b < 60


def test_corrupt_file_reported_and_batch_continues(tmp_path, capsys):
    src = tmp_path / "in"
    make(src / "good.png")
    (src / "broken.jpg").write_bytes(b"\xff\xd8 definitely not a jpeg")

    assert run(src, tmp_path / "out", "-w", "50", "-h", "50") == 1
    assert (tmp_path / "out" / "in" / "good.jpg").exists()
    assert not (tmp_path / "out" / "in" / "broken.jpg").exists()
    assert "FAILED broken.jpg" in capsys.readouterr().err


def test_parallel_matches_serial(tmp_path):
    src = tmp_path / "in"
    for i in range(6):
        make(src / f"d{i % 2}" / f"img{i}.png", size=(300 + i, 200))
    assert main([str(src), "-w", "100", "-h", "100", "-o", str(tmp_path / "par"), "-j", "3"]) == 0
    assert run(src, tmp_path / "ser", "-w", "100", "-h", "100") == 0
    par = sorted(p.relative_to(tmp_path / "par") for p in (tmp_path / "par").rglob("*.jpg"))
    ser = sorted(p.relative_to(tmp_path / "ser") for p in (tmp_path / "ser").rglob("*.jpg"))
    assert par == ser and len(par) == 6


def test_no_images(tmp_path, capsys):
    assert run(tmp_path, tmp_path / "out", "-w", "10", "-h", "10") == 0
    assert "No images found" in capsys.readouterr().out


# safety -----------------------------------------------------------------------------


def test_folder_already_in_pictures_saved_next_to_it(tmp_path, monkeypatch):
    pictures = tmp_path / "Pictures"
    monkeypatch.setattr(cli, "pictures_dir", lambda: pictures)
    make(pictures / "Holiday" / "a.png")
    for _ in range(2):
        assert main([str(pictures / "Holiday"), "-w", "50", "-j", "1"]) == 0
    assert names(pictures / "Holiday") == ["a.png"]
    assert names(pictures / "Holiday (small)") == ["a.jpg"]


def test_results_never_go_into_a_folder_around_the_input(tmp_path):
    # ~/Pictures/Holiday/Holiday would otherwise be saved into ~/Pictures/Holiday.
    outer = tmp_path / "Pictures" / "Holiday"
    make(outer / "beach.jpg", color="blue")
    make(outer / "Holiday" / "beach.png")
    original = (outer / "beach.jpg").read_bytes()
    assert run(outer / "Holiday", tmp_path / "Pictures", "-w", "50") == 0
    assert (outer / "beach.jpg").read_bytes() == original
    assert names(tmp_path / "Pictures" / "Holiday (small)") == ["beach.jpg"]


def _case_insensitive(folder: Path) -> bool:
    return (folder.parent / folder.name.swapcase()).exists()


def test_folder_already_in_pictures_found_despite_letter_case(tmp_path):
    pictures = tmp_path / "Pictures"
    make(pictures / "Holiday" / "a.png")
    if not _case_insensitive(pictures):
        pytest.skip("needs a case-insensitive file system")
    run(tmp_path / "pictures" / "holiday", pictures, "-w", "50")
    assert names(pictures / "Holiday") == ["a.png"]
    assert names(pictures / "holiday (small)") == ["a.jpg"]


@pytest.mark.parametrize(
    "folder, name",
    [
        (PureWindowsPath("C:/Users/me/Holiday"), "Holiday"),
        (PureWindowsPath("E:/"), "Drive E"),
        (PureWindowsPath("//server/photos/"), "photos"),
        (PurePosixPath("/"), "Root"),
    ],
)
def test_folder_name(folder, name):
    assert paths.folder_name(folder) == name


def test_default_jobs_limited_by_memory(monkeypatch):
    monkeypatch.setattr(cli.os, "cpu_count", lambda: 10)
    monkeypatch.setattr(cli, "_total_memory", lambda: 8 * 1024**3)
    assert cli.default_jobs() == 4
    monkeypatch.setattr(cli, "_total_memory", lambda: 1024**3)
    assert cli.default_jobs() == 1
    monkeypatch.setattr(cli, "_total_memory", lambda: None)
    assert cli.default_jobs() == 10


def test_output_inside_input_skipped_despite_letter_case(tmp_path):
    src = tmp_path / "in"
    make(src / "a.png")
    make(src / "Resized" / "in" / "old.jpg")
    if not _case_insensitive(src / "Resized"):
        pytest.skip("needs a case-insensitive file system")
    run(src, src / "resized", "-w", "50")
    assert names(src / "Resized" / "in") == ["a.jpg", "old.jpg"]


def test_output_folder_that_is_a_file_rejected(tmp_path):
    make(tmp_path / "in" / "a.png")
    (tmp_path / "file").write_text("")
    with pytest.raises(SystemExit):
        run(tmp_path / "in", tmp_path / "file", "-w", "50")


def test_postscript_is_never_opened(tmp_path, capsys):
    # EPS/PS go through Ghostscript, so they are refused even when named like a photo.
    src = tmp_path / "in"
    src.mkdir()
    postscript = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
    (src / "drawing.eps").write_bytes(postscript)
    (src / "disguised.jpg").write_bytes(postscript)
    make(src / "good.png")

    assert run(src, tmp_path / "out", "-w", "50") == 1
    err = capsys.readouterr().err
    assert "FAILED disguised.jpg" in err and "drawing.eps" not in err
    assert names(tmp_path / "out" / "in") == ["good.jpg"]


def test_videos_are_skipped(tmp_path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    (src / "clip.mpg").write_bytes(b"\x00\x00\x01\xb3\x14\x00\xf0\x13" + b"\x00" * 200)
    make(src / "good.png")
    assert run(src, tmp_path / "out", "-w", "50") == 0
    assert "clip.mpg" not in capsys.readouterr().err


def test_multi_picture_jpeg_uses_fast_decoding(tmp_path):
    from gulliver_tunnel.resize import _draft

    path = tmp_path / "multi.jpg"
    Image.new("RGB", (4000, 3000), "red").save(
        path, format="MPO", save_all=True, append_images=[Image.new("RGB", (4000, 3000))]
    )
    with Image.open(path) as im:
        assert im.format == "MPO"
        _draft(im, (400, 300))
        assert im.size[0] < 4000


def test_worker_crash_reported_plainly(tmp_path, monkeypatch, capsys):
    from concurrent.futures.process import BrokenProcessPool

    def crash(*_):
        raise BrokenProcessPool("boom")
        yield

    monkeypatch.setattr(cli, "run", crash)
    make(tmp_path / "in" / "a.png")
    assert run(tmp_path / "in", tmp_path / "out", "-w", "50") == 1
    assert "-j 2" in capsys.readouterr().err

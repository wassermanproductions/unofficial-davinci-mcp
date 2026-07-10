import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _engine_fixtures as fx  # noqa: E402
from engines import color_match, colorsci, fftools  # noqa: E402


def _mean_lab_of_video(path, lut=None):
    """Decode the middle frame (optionally through a LUT) and return mean Lab."""
    import subprocess
    import tempfile

    dur = float(fftools.ffprobe_json(path)["format"]["duration"])
    tmp = tempfile.mktemp(suffix=".png")
    vf = "scale=320:180"
    if lut:
        vf += f",lut3d=file='{lut}'"
    cmd = [fftools.ffmpeg_path(), "-v", "error", "-y", "-ss", f"{dur/2:.3f}",
           "-i", path, "-frames:v", "1", "-vf", vf, "-f", "rawvideo",
           "-pix_fmt", "rgb24", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    arr = np.frombuffer(out[: 320 * 180 * 3], dtype=np.uint8).reshape(180, 320, 3) / 255.0
    lab = colorsci.srgb_to_lab(arr.reshape(-1, 3))
    return lab.mean(axis=0)


# ---- color science unit tests -----------------------------------------

def test_d65_white_maps_to_L100():
    lab = colorsci.srgb_to_lab(np.array([1.0, 1.0, 1.0]))
    assert abs(lab[0] - 100.0) < 1e-3
    assert abs(lab[1]) < 1e-3
    assert abs(lab[2]) < 1e-3


def test_lab_roundtrip():
    rng = np.random.default_rng(0)
    rgb = rng.random((500, 3))
    back = colorsci.lab_to_srgb(colorsci.srgb_to_lab(rgb), clip=False)
    assert np.max(np.abs(rgb - back)) < 1e-9


def test_black_and_gray():
    assert np.allclose(colorsci.srgb_to_lab(np.array([0.0, 0, 0])), [0, 0, 0], atol=1e-6)
    gray = colorsci.srgb_to_lab(np.array([0.5, 0.5, 0.5]))
    assert 53.0 < gray[0] < 53.7
    assert abs(gray[1]) < 1e-3 and abs(gray[2]) < 1e-3


# ---- LUT baking + parsing ---------------------------------------------

def test_identity_lut_is_near_identity():
    # strength 0 -> transform is identity -> LUT maps input to itself.
    def identity(lab):
        return lab

    import tempfile
    p = tempfile.mktemp(suffix=".cube")
    color_match.bake_cube(identity, p, title="id")
    parsed = fx.parse_cube(p)
    assert parsed["size"] == 33
    grid = color_match._lut_grid(33)
    # Identity transform through Lab roundtrip: within small tolerance.
    err = np.max(np.abs(parsed["table"] - grid))
    assert err < 1e-4, err


# ---- end-to-end color match -------------------------------------------

@pytest.mark.parametrize("method", ["reinhard", "lab_histogram"])
def test_color_match_shrinks_lab_distance(method):
    ref = fx.graded_reference_mp4()
    target = fx.graded_target_mp4()
    out_dir = os.path.join(os.path.dirname(ref), f"cm_{method}")

    result = color_match.color_match(
        ref, [target], method=method, strength=1.0,
        output_dir=out_dir, preview=True, dry_run=False, confirm=True,
    )
    assert result["ok"], result
    r = result["results"][0]
    assert r["ok"], r

    lut = r["lut_path"]
    assert os.path.exists(lut)
    parsed = fx.parse_cube(lut)          # must parse & be valid
    assert parsed["size"] == 33

    # Measure real Lab distance before/after applying the baked LUT via ffmpeg.
    ref_mean = _mean_lab_of_video(ref)
    tgt_before = _mean_lab_of_video(target)
    tgt_after = _mean_lab_of_video(target, lut=lut)

    de_before = float(np.sqrt(np.sum((tgt_before - ref_mean) ** 2)))
    de_after = float(np.sqrt(np.sum((tgt_after - ref_mean) ** 2)))
    shrink = 1.0 - de_after / de_before
    assert de_before > 1.0, "grades should differ meaningfully"
    assert shrink > 0.60, f"{method}: Lab distance only shrank {shrink:.2%} (before={de_before:.2f} after={de_after:.2f})"

    # Preview strip should have rendered.
    assert r["preview_path"] and os.path.exists(r["preview_path"])


def test_dry_run_plans_without_writing():
    ref = fx.graded_reference_mp4()
    target = fx.graded_target_mp4()
    result = color_match.color_match(ref, [target], dry_run=True)
    assert result["ok"] and result["dry_run"] is True
    assert result["planned_outputs"][0]["lut_path"].endswith(".cube")


def test_strength_zero_is_near_identity_lut():
    ref = fx.graded_reference_mp4()
    target = fx.graded_target_mp4()
    out_dir = os.path.join(os.path.dirname(ref), "cm_s0")
    result = color_match.color_match(
        ref, [target], method="reinhard", strength=0.0,
        output_dir=out_dir, preview=False, dry_run=False, confirm=True,
    )
    r = result["results"][0]
    parsed = fx.parse_cube(r["lut_path"])
    grid = color_match._lut_grid(33)
    assert np.max(np.abs(parsed["table"] - grid)) < 1e-3

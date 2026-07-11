"""Reference-image color matching -> per-target 33-point .cube LUTs.

Pipeline:
  1. Decode representative frames (middle + 2 spares) from each target and the
     reference image into RGB pixel samples (via ffmpeg rawvideo, no PIL).
  2. Compute a per-channel color transform in CIE Lab:
       - 'reinhard'      : mean/std transfer (Reinhard et al.)
       - 'lab_histogram' : monotonic quantile (histogram) matching per channel
     ``strength`` in [0,1] blends the mapped result toward identity.
  3. Bake a Resolve-loadable 33^3 .cube LUT per target (R fastest-varying).
  4. Render a before/after preview JPEG strip per target (ffmpeg lut3d).
  5. Return LUT paths, previews, and numeric Lab deltas (mean shift).

Everything is deterministic numpy math; only frame decode / preview render
shell out to ffmpeg.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from . import colorsci, fftools, media

LUT_SIZE = 65
_SAMPLE_W, _SAMPLE_H = 320, 180  # decode size for statistics
_PREVIEW_W = 640


# ---- frame decode ------------------------------------------------------

def _decode_rgb(path: str, *, seek: float | None, w: int, h: int) -> np.ndarray:
    """Decode one frame to an (h, w, 3) float array in [0,1] via ffmpeg."""
    cmd = [fftools.ffmpeg_path(), "-v", "error"]
    if seek is not None and seek > 0:
        cmd += ["-ss", f"{seek:.3f}"]
    cmd += [
        "-i", path,
        "-frames:v", "1",
        "-vf", f"scale={w}:{h}",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "pipe:1",
    ]
    raw = _run_binary(cmd)
    expected = w * h * 3
    if len(raw) < expected:
        raise RuntimeError(f"Short frame decode from {path}: {len(raw)} < {expected}")
    arr = np.frombuffer(raw[:expected], dtype=np.uint8).reshape(h, w, 3)
    return arr.astype(np.float64) / 255.0


def _run_binary(cmd: list[str]) -> bytes:
    import subprocess

    completed = subprocess.run(cmd, check=False, capture_output=True, timeout=30)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffmpeg decode failed: {completed.stderr.decode('utf-8', 'replace')[-500:]}"
        )
    return completed.stdout


def _sample_pixels(path: str, kind: str, duration: float | None) -> np.ndarray:
    """Return an (N,3) array of sampled RGB pixels in [0,1]."""
    seeks: list[float | None]
    if kind == "video" and duration and duration > 0.2:
        seeks = [duration * 0.5, duration * 0.25, duration * 0.75]
    else:
        seeks = [None]  # image or very short clip: single frame
    frames = []
    for s in seeks:
        try:
            frames.append(_decode_rgb(path, seek=s, w=_SAMPLE_W, h=_SAMPLE_H))
        except Exception:
            continue
    if not frames:
        raise RuntimeError(f"Could not decode any frames from {path}")
    stacked = np.concatenate([f.reshape(-1, 3) for f in frames], axis=0)
    return stacked


# ---- transforms (operate on Lab arrays) -------------------------------

def _reinhard_stats(lab: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = lab.reshape(-1, 3).mean(axis=0)
    std = lab.reshape(-1, 3).std(axis=0)
    return mean, std


def _make_reinhard(src_lab: np.ndarray, ref_lab: np.ndarray, strength: float):
    s_mean, s_std = _reinhard_stats(src_lab)
    r_mean, r_std = _reinhard_stats(ref_lab)
    safe_s = np.where(s_std < 1e-6, 1.0, s_std)
    scale = r_std / safe_s

    def transform(lab: np.ndarray) -> np.ndarray:
        mapped = (lab - s_mean) * scale + r_mean
        return lab + strength * (mapped - lab)

    return transform, s_mean, r_mean


def _make_histogram(src_lab: np.ndarray, ref_lab: np.ndarray, strength: float):
    """Per-channel monotonic quantile matching curve."""
    q = np.linspace(0.0, 1.0, 256)
    src_flat = src_lab.reshape(-1, 3)
    ref_flat = ref_lab.reshape(-1, 3)
    src_q = np.quantile(src_flat, q, axis=0)  # (256, 3)
    ref_q = np.quantile(ref_flat, q, axis=0)

    # Smooth the quantile curves: raw 256-point quantile maps from 8-bit
    # sources are steppy, and steps become visible banding once baked into a
    # LUT. Two passes of a small moving average keep the shape but round the
    # staircase; monotonicity is re-imposed after.
    def _smooth(curve: np.ndarray) -> np.ndarray:
        original = curve
        kernel = np.ones(9) / 9.0
        for _ in range(2):
            padded = np.pad(curve, (4, 4), mode="edge")
            curve = np.convolve(padded, kernel, mode="valid")
        # Pin the endpoints: averaging drags the extreme quantiles inward,
        # which caps whites and lifts blacks. Taper the smoothing to zero
        # over the outer 12 points so the black/white mappings stay exact.
        n = len(curve)
        weight = np.ones(n)
        ramp = np.linspace(0.0, 1.0, 12, endpoint=False)
        weight[:12] = ramp
        weight[-12:] = ramp[::-1]
        curve = weight * curve + (1.0 - weight) * original
        return np.maximum.accumulate(curve)

    for c in range(3):
        src_q[:, c] = _smooth(src_q[:, c])
        ref_q[:, c] = _smooth(ref_q[:, c])

    # Cap the local slope of the L mapping. When reference and target frame
    # CONTENT differs (a sky-heavy shot matched to a sand-heavy reference),
    # quantile mapping can stretch a narrow tonal band several-fold - that
    # stretch is what amplifies compression texture into visible mottling.
    L_SLOPE_CAP = 1.6
    xs = src_q[:, 0]
    ys = ref_q[:, 0].copy()
    dx = np.diff(xs)
    dy = np.diff(ys)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = np.where(dx > 1e-9, dy / dx, 0.0)
    # Cap the body of the curve but leave the top tail free: the brightest
    # ~3% of pixels (speculars, whites) legitimately need a steep rise, and
    # capping them just makes the whole grade dim. Mottling lives in the
    # large smooth bands, which sit in the body.
    n = len(slope)
    body = np.arange(n) < int(n * 0.97)
    # Relative cap: a legitimate global correction (big exposure/gamma move)
    # raises the WHOLE curve's slope - only narrow-band spikes well above the
    # curve's own typical slope create mottling. Cap at the larger of the
    # absolute limit and 2.2x the median body slope.
    valid = body & (dx > 1e-9)
    median_slope = float(np.median(slope[valid])) if valid.any() else 1.0
    cap_value = max(L_SLOPE_CAP, 2.2 * median_slope)
    slope = np.where(body, np.clip(slope, 0.0, cap_value), slope)
    rebuilt = np.concatenate([[ys[0]], ys[0] + np.cumsum(slope * dx)])
    # Whatever span the cap removed from the body, hand back to the tail by
    # stretching it toward the original white endpoint.
    deficit = ys[-1] - rebuilt[-1]
    if deficit > 0.5:
        tail = ~np.concatenate([[True], body])
        tail_span = rebuilt[-1] - rebuilt[tail][0] if tail.any() else 0.0
        if tail_span > 1e-6:
            scale = (tail_span + deficit) / tail_span
            first_tail = np.argmax(tail)
            rebuilt[first_tail:] = rebuilt[first_tail] + (rebuilt[first_tail:] - rebuilt[first_tail]) * scale
    ref_q[:, 0] = np.maximum.accumulate(rebuilt)

    # Ensure src_q is strictly increasing per channel for interp x-coords.
    def transform(lab: np.ndarray) -> np.ndarray:
        out = np.empty_like(lab)
        flat = lab.reshape(-1, 3)
        res = np.empty_like(flat)
        for c in range(3):
            xs = src_q[:, c]
            ys = ref_q[:, c]
            # np.interp needs increasing xs; enforce monotonicity.
            xs_mono = np.maximum.accumulate(xs)
            mapped = np.interp(flat[:, c], xs_mono, ys)
            res[:, c] = flat[:, c] + strength * (mapped - flat[:, c])
        out = res.reshape(lab.shape)
        return out

    s_mean = src_flat.mean(axis=0)
    r_mean = ref_flat.mean(axis=0)
    return transform, s_mean, r_mean


# ---- LUT baking --------------------------------------------------------

def _lut_grid(size: int) -> np.ndarray:
    """Return (size^3, 3) sRGB input grid with R fastest-varying."""
    axis = np.linspace(0.0, 1.0, size)
    # meshgrid ij over (b, g, r); C-order flatten -> r fastest.
    B, G, R = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack([R, G, B], axis=-1).reshape(-1, 3)


def bake_cube(transform, path: str, *, title: str, size: int = LUT_SIZE) -> str:
    """Compute output values through ``transform`` (Lab->Lab) and write a .cube."""
    grid = _lut_grid(size)
    lab = colorsci.srgb_to_lab(grid)
    out_lab = transform(lab)
    out_rgb = colorsci.lab_to_srgb(out_lab, clip=True)

    lines = [
        f'TITLE "{title}"',
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    # Format values with 6 decimals.
    for r, g, b in out_rgb:
        lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    text = "\n".join(lines) + "\n"
    Path(path).write_text(text)
    return path


# ---- preview -----------------------------------------------------------

def _render_preview(target: str, kind: str, duration: float | None, lut_path: str, out_jpg: str) -> str | None:
    """Extract a middle frame, apply the LUT, hstack before|after -> JPEG."""
    tmpdir = tempfile.mkdtemp(prefix="cm_prev_")
    before = os.path.join(tmpdir, "before.png")
    after = os.path.join(tmpdir, "after.png")
    seek = (duration * 0.5) if (kind == "video" and duration and duration > 0.2) else None
    try:
        cmd = [fftools.ffmpeg_path(), "-v", "error", "-y"]
        if seek:
            cmd += ["-ss", f"{seek:.3f}"]
        cmd += ["-i", target, "-frames:v", "1", "-vf", f"scale={_PREVIEW_W}:-2", before]
        fftools.run(cmd, timeout=30, check=True)

        # Apply LUT.
        fftools.run(
            [fftools.ffmpeg_path(), "-v", "error", "-y", "-i", before,
             "-vf", f"lut3d=file='{lut_path}'", after],
            timeout=30, check=True,
        )
        # Side-by-side.
        fftools.run(
            [fftools.ffmpeg_path(), "-v", "error", "-y", "-i", before, "-i", after,
             "-filter_complex", "[0:v][1:v]hstack=inputs=2", "-q:v", "3", out_jpg],
            timeout=30, check=True,
        )
        return out_jpg if os.path.exists(out_jpg) else None
    except Exception:
        return None


# ---- public API --------------------------------------------------------

def color_match(
    reference_image: str,
    targets: list[str],
    *,
    method: str = "reinhard",
    strength: float = 1.0,
    chroma: str = "match",
    output_dir: str | None = None,
    preview: bool = True,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    if method not in {"reinhard", "lab_histogram"}:
        return {"ok": False, "error": f"Unknown method '{method}'."}
    if chroma not in {"match", "preserve"}:
        return {"ok": False, "error": f"Unknown chroma mode '{chroma}' (match|preserve)."}
    strength = float(np.clip(strength, 0.0, 1.0))

    ref_path = str(Path(reference_image).expanduser())
    if not os.path.exists(ref_path):
        return {"ok": False, "error": "reference_image does not exist.", "path": ref_path}
    if not targets:
        return {"ok": False, "error": "No targets supplied."}

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="color_match_")
    else:
        output_dir = str(Path(output_dir).expanduser())
        os.makedirs(output_dir, exist_ok=True)

    planned = []
    for t in targets:
        tp = str(Path(t).expanduser())
        stem = Path(tp).stem
        planned.append(
            {
                "target": tp,
                "exists": os.path.exists(tp),
                "lut_path": os.path.join(output_dir, f"{stem}_match.cube"),
                "preview_path": os.path.join(output_dir, f"{stem}_preview.jpg") if preview else None,
            }
        )

    if dry_run and not confirm:
        return {
            "ok": True,
            "dry_run": True,
            "method": method,
            "strength": strength,
            "reference_image": ref_path,
            "output_dir": output_dir,
            "lut_size": LUT_SIZE,
            "planned_outputs": planned,
            "note": "Set dry_run=false and confirm=true to bake LUTs and previews.",
        }

    if not confirm:
        return {"ok": False, "error": "confirm must be true when dry_run is false."}

    # Reference sample.
    ref_probe = media.probe_one(ref_path)
    try:
        ref_px = _sample_pixels(ref_path, ref_probe.get("kind", "image"), ref_probe.get("duration_seconds"))
    except Exception as exc:
        return {"ok": False, "error": f"Reference decode failed: {exc}"}
    ref_lab = colorsci.srgb_to_lab(ref_px)
    ref_mean_lab = ref_lab.mean(axis=0)

    results = []
    for entry in planned:
        tp = entry["target"]
        if not entry["exists"]:
            results.append({"target": tp, "ok": False, "error": "Target does not exist."})
            continue
        probe = media.probe_one(tp)
        kind = probe.get("kind", "video")
        dur = probe.get("duration_seconds")
        try:
            src_px = _sample_pixels(tp, kind, dur)
        except Exception as exc:
            results.append({"target": tp, "ok": False, "error": f"Decode failed: {exc}"})
            continue
        src_lab = colorsci.srgb_to_lab(src_px)
        src_mean_lab = src_lab.mean(axis=0)

        if method == "reinhard":
            transform, _, _ = _make_reinhard(src_lab, ref_lab, strength)
        else:
            transform, _, _ = _make_histogram(src_lab, ref_lab, strength)

        if chroma == "preserve":
            # Luminance takes the full match, but a*/b* receive only a
            # cast-correcting shift toward the reference - no per-quantile
            # chroma stretch. This keeps the source's color variance (skin
            # stays alive) and never amplifies the blocky quarter-resolution
            # chroma of web-compressed sources (sky mottling). The shift is
            # luminance-weighted: sunlit areas carry most of a cast, shadows
            # carry less - a uniform shift would turn shadows cold.
            base = transform
            ab_shift = (ref_lab.mean(axis=0) - src_lab.mean(axis=0))[1:3] * strength

            def transform(lab: np.ndarray, _base=base, _shift=ab_shift) -> np.ndarray:
                out = _base(lab)
                weight = 0.55 + 0.45 * np.clip(lab[..., 0] / 100.0, 0.0, 1.0)
                out[..., 1] = lab[..., 1] + _shift[0] * weight
                out[..., 2] = lab[..., 2] + _shift[1] * weight
                return out

        title = f"{Path(tp).stem} -> {Path(ref_path).stem} ({method})"
        bake_cube(transform, entry["lut_path"], title=title)

        # Numeric deltas: mean Lab convergence.
        matched_mean_lab = transform(src_mean_lab.reshape(1, 3)).reshape(3)
        de_before = float(colorsci.delta_e76(src_mean_lab, ref_mean_lab))
        de_after = float(colorsci.delta_e76(matched_mean_lab, ref_mean_lab))
        shrink = (1.0 - de_after / de_before) if de_before > 1e-6 else 0.0

        preview_path = None
        if preview:
            preview_path = _render_preview(tp, kind, dur, entry["lut_path"], entry["preview_path"])

        noise = None
        highlights = None
        try:
            seeks = [None]
            if kind == "video" and dur and dur > 1.0:
                seeks = [dur * 0.2, dur * 0.5, dur * 0.8]
            for si, sk in enumerate(seeks):
                frame = _decode_rgb(tp, seek=sk, w=960, h=540)
                lut_frame = _decode_rgb_with_lut(tp, entry["lut_path"], seek=sk, w=960, h=540)
                if si == 0:
                    noise = _noise_amplification(frame, transform)
                damage = _frame_damage(frame, lut_frame)
                if highlights is None:
                    highlights = damage
                else:
                    for key in damage:
                        highlights[key] = max(highlights[key], damage[key])
        except Exception:  # noqa: BLE001 - frame QA is best-effort
            pass
        quality = _quality_report(src_lab, ref_lab, transform, noise=noise, highlights=highlights)

        results.append(
            {
                "target": tp,
                "ok": True,
                "lut_path": entry["lut_path"],
                "preview_path": preview_path,
                "mean_lab_target": [round(float(x), 3) for x in src_mean_lab],
                "mean_lab_reference": [round(float(x), 3) for x in ref_mean_lab],
                "mean_lab_after": [round(float(x), 3) for x in matched_mean_lab],
                "mean_lab_shift": [round(float(a - b), 3) for a, b in zip(matched_mean_lab, src_mean_lab)],
                "delta_e_before": round(de_before, 3),
                "delta_e_after": round(de_after, 3),
                "convergence": round(shrink, 4),
                "quality": quality,
            }
        )

    return {
        "ok": True,
        "dry_run": False,
        "method": method,
        "strength": strength,
        "reference_image": ref_path,
        "output_dir": output_dir,
        "lut_size": LUT_SIZE,
        "results": results,
    }


def _box_blur(gray: np.ndarray, k: int = 5) -> np.ndarray:
    pad = k // 2
    padded = np.pad(gray, pad, mode="edge")
    csum = np.cumsum(np.cumsum(padded, axis=0), axis=1)
    csum = np.pad(csum, ((1, 0), (1, 0)))
    out = (
        csum[k:, k:] - csum[:-k, k:] - csum[k:, :-k] + csum[:-k, :-k]
    ) / float(k * k)
    return out


def _noise_level(rgb: np.ndarray, region_mask: np.ndarray | None = None) -> float:
    """High-frequency residual std - a grain/compression-noise estimate."""
    gray = rgb.mean(axis=-1)
    residual = gray - _box_blur(gray, 5)
    if region_mask is not None and region_mask.any():
        residual = residual[region_mask]
    return float(residual.std())


def _noise_amplification(frame_rgb: np.ndarray, transform) -> dict[str, float]:
    """Measure how much a grade amplifies noise, overall and in the shadows.

    A LUT with steep slopes in the low end multiplies whatever sensor grain
    and compression noise lives there. Ratios well above 1 mean the grade is
    surfacing artifacts the source was hiding.
    """
    lab = colorsci.srgb_to_lab(frame_rgb)
    graded = colorsci.lab_to_srgb(transform(lab))
    L = lab[..., 0]
    shadows = L < 30.0
    src_overall = _noise_level(frame_rgb)
    out_overall = _noise_level(graded)
    src_shadow = _noise_level(frame_rgb, shadows)
    out_shadow = _noise_level(graded, shadows)
    # Smooth regions (skies, walls) show amplification first - measure them
    # separately so a large calm area cannot be averaged away by busy ones.
    gray = frame_rgb.mean(axis=-1) * 100.0
    smooth = np.abs(gray - _box_blur(gray, 7)) < 0.8
    smooth_ratio = 1.0
    if smooth.sum() > 2000:
        smooth_ratio = _noise_level(graded, smooth) / max(_noise_level(frame_rgb, smooth), 1e-6)
    return {
        "overall_ratio": round(out_overall / max(src_overall, 1e-6), 2),
        "shadow_ratio": round(out_shadow / max(src_shadow, 1e-6), 2),
        "smooth_region_ratio": round(smooth_ratio, 2),
    }


def _decode_rgb_with_lut(path: str, lut_path: str, *, seek: float | None, w: int, h: int) -> np.ndarray:
    cmd = [fftools.ffmpeg_path(), "-v", "error"]
    if seek is not None and seek > 0:
        cmd += ["-ss", f"{seek:.3f}"]
    lut_arg = lut_path.replace("\\", "/").replace(":", "\\:")
    cmd += [
        "-i", path,
        "-frames:v", "1",
        "-vf", f"scale={w}:{h},lut3d='{lut_arg}'",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    raw = _run_binary(cmd)
    expected = w * h * 3
    if len(raw) < expected:
        raise RuntimeError(f"Short LUT decode from {path}")
    return np.frombuffer(raw[:expected], dtype=np.uint8).reshape(h, w, 3).astype(np.float64) / 255.0


def _frame_damage(src_frame: np.ndarray, lut_frame: np.ndarray) -> dict[str, float]:
    """Clipping and posterization measured on the REAL LUT-applied frame."""
    L_src = colorsci.srgb_to_lab(src_frame)[..., 0]
    L_out = colorsci.srgb_to_lab(lut_frame)[..., 0]
    src_clip = float((L_src > 96.5).mean())
    out_clip = float((L_out > 96.5).mean())
    hi = L_out > 80
    posterization = 0.0
    if hi.sum() > 500:
        levels_out = len(np.unique(np.round(L_out[hi], 1)))
        levels_src = len(np.unique(np.round(L_src[hi], 1))) or 1
        posterization = round(max(0.0, 1.0 - levels_out / levels_src), 3)

    # Banding: in regions the source keeps smooth (skies, walls), a healthy
    # grade preserves the level density. Collapsing distinct levels there is
    # what the eye reads as bands.
    smooth = np.abs(L_src - _box_blur(L_src, 7)) < 0.8
    banding = 0.0
    if smooth.sum() > 2000:
        lv_out = len(np.unique(np.round(L_out[smooth], 1)))
        lv_src = len(np.unique(np.round(L_src[smooth], 1))) or 1
        banding = round(max(0.0, 1.0 - lv_out / lv_src), 3)
    return {
        "clipped_fraction_source": round(src_clip, 4),
        "clipped_fraction_result": round(out_clip, 4),
        "highlight_posterization": posterization,
        "smooth_region_banding": banding,
    }


def _highlight_damage(frame_rgb: np.ndarray, transform) -> dict[str, float]:
    """Measure highlight clipping and posterization introduced by a grade.

    clipped_gain: how much more of the frame sits at/near L*=100 after the
    grade. posterization: collapse of distinct highlight levels (banding).
    """
    lab = colorsci.srgb_to_lab(frame_rgb)
    graded = transform(lab.copy())
    L_src, L_out = lab[..., 0], np.clip(graded[..., 0], 0, 100)
    src_clip = float((L_src > 96.5).mean())
    out_clip = float((L_out > 96.5).mean())
    hi = L_out > 80
    posterization = 0.0
    if hi.sum() > 500:
        levels_out = len(np.unique(np.round(L_out[hi], 1)))
        levels_src = len(np.unique(np.round(L_src[hi], 1))) or 1
        posterization = round(max(0.0, 1.0 - levels_out / levels_src), 3)

    # Banding: in regions the source keeps smooth (skies, walls), a healthy
    # grade preserves the level density. Collapsing distinct levels there is
    # what the eye reads as bands.
    smooth = np.abs(L_src - _box_blur(L_src, 7)) < 0.8
    banding = 0.0
    if smooth.sum() > 2000:
        lv_out = len(np.unique(np.round(L_out[smooth], 1)))
        lv_src = len(np.unique(np.round(L_src[smooth], 1))) or 1
        banding = round(max(0.0, 1.0 - lv_out / lv_src), 3)
    return {
        "clipped_fraction_source": round(src_clip, 4),
        "clipped_fraction_result": round(out_clip, 4),
        "highlight_posterization": posterization,
        "smooth_region_banding": banding,
    }


def _stats(lab: np.ndarray) -> dict[str, float]:
    flat = lab.reshape(-1, 3)
    L = flat[:, 0]
    chroma_mean = float(np.hypot(flat[:, 1], flat[:, 2]).mean())
    return {
        "black_point_L": round(float(np.percentile(L, 0.5)), 1),
        "white_point_L": round(float(np.percentile(L, 99.5)), 1),
        "contrast_L_std": round(float(L.std()), 1),
        "mean_chroma": round(chroma_mean, 1),
    }


def _quality_report(
    src_lab: np.ndarray,
    ref_lab: np.ndarray,
    transform,
    noise: dict[str, float] | None = None,
    highlights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Judge the graded result against reference and source - numerically.

    A match that converges on mean color can still be a bad grade: flat
    (contrast below the reference), washed out (chroma collapsed), or milky
    (no black point). These flags exist so an agent can REJECT its own grade
    and iterate instead of shipping the first LUT that ran.
    """
    result_lab = transform(src_lab.copy())
    src, ref, res = _stats(src_lab), _stats(ref_lab), _stats(result_lab)
    flags = []
    if res["contrast_L_std"] < 0.8 * ref["contrast_L_std"]:
        flags.append("flat: result contrast is well below the reference")
    if res["mean_chroma"] < 0.6 * src["mean_chroma"] and res["mean_chroma"] < 0.85 * ref["mean_chroma"]:
        flags.append("washed_out: chroma collapsed versus both source and reference")
    if res["black_point_L"] > ref["black_point_L"] + 8:
        flags.append("milky: black point sits far above the reference")
    if res["white_point_L"] < ref["white_point_L"] - 8:
        flags.append("dim: white point falls short of the reference")
    if noise is not None:
        if (noise["shadow_ratio"] > 1.6 or noise["overall_ratio"] > 1.5
                or noise.get("smooth_region_ratio", 1.0) > 1.6):
            flags.append(
                "noisy: the grade amplifies grain/compression artifacts "
                "(reduce the shadow lift, temper strength, or denoise in "
                "Resolve before the LUT node)"
            )
    if highlights is not None:
        grew = highlights["clipped_fraction_result"] - highlights["clipped_fraction_source"]
        if grew > 0.005:
            flags.append(
                "clipped: the grade blows out highlights the source held "
                "(soften the white point or add a highlight knee)"
            )
        if highlights["highlight_posterization"] > 0.25:
            flags.append(
                "posterized: highlight levels collapse into bands "
                "(reduce the stretch or grade in higher precision)"
            )
        if highlights.get("smooth_region_banding", 0) > 0.3:
            flags.append(
                "banding: smooth gradients (skies, walls) break into steps "
                "(soften the curve, reduce strength, or denoise/dither the source)"
            )
    report = {"source": src, "reference": ref, "result": res, "flags": flags,
              "acceptable": not flags}
    if noise is not None:
        report["noise_amplification"] = noise
    if highlights is not None:
        report["highlights"] = highlights
    return report


# ---- registration ------------------------------------------------------

def register(add_tool) -> None:
    add_tool(
        "color_match",
        {
            "type": "object",
            "properties": {
                "reference_image": {"type": "string", "description": "Reference still or clip to match toward."},
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Target clips/stills to grade toward the reference.",
                },
                "method": {"type": "string", "enum": ["reinhard", "lab_histogram"], "default": "reinhard"},
                "chroma": {
                    "type": "string",
                    "enum": ["match", "preserve"],
                    "default": "match",
                    "description": "'match' adopts the reference's full color statistics; 'preserve' matches luminance/contrast but keeps the source's own chroma variance, applying only a cast-correcting shift - use when the reference is flat, thin, or stylistically distant.",
                },
                "strength": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 1.0},
                "output_dir": {"type": "string", "description": "Where to write .cube LUTs and previews."},
                "preview": {"type": "boolean", "default": True},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["reference_image", "targets"],
            "additionalProperties": False,
        },
        lambda params: color_match(
            params["reference_image"],
            list(params["targets"]),
            method=params.get("method", "reinhard"),
            chroma=params.get("chroma", "match"),
            strength=params.get("strength", 1.0),
            output_dir=params.get("output_dir"),
            preview=params.get("preview", True),
            dry_run=params.get("dry_run", True),
            confirm=params.get("confirm", False),
        ),
        "both",
        "Match target clips/stills to a reference image; bake 33-point .cube "
        "LUTs (Reinhard or Lab-histogram) with before/after preview strips. "
        "Every result carries a quality report (black/white points, contrast, "
        "chroma, and failure flags like flat/washed_out/milky) - read it and "
        "REJECT the grade if flags are present; 'clean' does not mean "
        "desaturated. See get_editing_knowledge('color-looks').",
    )

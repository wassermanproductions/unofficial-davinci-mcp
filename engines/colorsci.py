"""Pure-numpy color science: sRGB <-> linear <-> XYZ <-> CIE Lab (D65).

All functions operate on float arrays in [0, 1] for RGB (any trailing shape
with a final axis of size 3) and return arrays of the same shape. Lab is in the
usual ranges: L in [0, 100], a/b roughly [-128, 128].

Kept deliberately small and dependency-free so the conversions can be unit
tested against known reference values (D65 white -> L=100, a=b=0).
"""

from __future__ import annotations

import numpy as np

# sRGB (IEC 61966-2-1) linear <-> gamma companding.
_SRGB_THRESH = 0.04045
_SRGB_LIN_THRESH = 0.0031308

# sRGB primaries -> XYZ (D65). Standard matrix.
_RGB2XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
_XYZ2RGB = np.linalg.inv(_RGB2XYZ)

# D65 reference white (normalised so Y = 1).
_D65 = np.array([0.95047, 1.00000, 1.08883], dtype=np.float64)

# CIE Lab constants.
_LAB_EPS = 216.0 / 24389.0  # (6/29)^3
_LAB_KAPPA = 24389.0 / 27.0  # (29/3)^3


def srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    rgb = np.asarray(rgb, dtype=np.float64)
    return np.where(
        rgb <= _SRGB_THRESH,
        rgb / 12.92,
        ((rgb + 0.055) / 1.055) ** 2.4,
    )


def linear_to_srgb(rgb: np.ndarray) -> np.ndarray:
    rgb = np.asarray(rgb, dtype=np.float64)
    out = np.where(
        rgb <= _SRGB_LIN_THRESH,
        rgb * 12.92,
        1.055 * np.power(np.clip(rgb, 0.0, None), 1.0 / 2.4) - 0.055,
    )
    return out


def linear_to_xyz(rgb_lin: np.ndarray) -> np.ndarray:
    rgb_lin = np.asarray(rgb_lin, dtype=np.float64)
    return rgb_lin @ _RGB2XYZ.T


def xyz_to_linear(xyz: np.ndarray) -> np.ndarray:
    xyz = np.asarray(xyz, dtype=np.float64)
    return xyz @ _XYZ2RGB.T


def _f_forward(t: np.ndarray) -> np.ndarray:
    return np.where(t > _LAB_EPS, np.cbrt(t), (_LAB_KAPPA * t + 16.0) / 116.0)


def _f_inverse(f: np.ndarray) -> np.ndarray:
    f3 = f ** 3
    return np.where(f3 > _LAB_EPS, f3, (116.0 * f - 16.0) / _LAB_KAPPA)


def xyz_to_lab(xyz: np.ndarray) -> np.ndarray:
    xyz = np.asarray(xyz, dtype=np.float64)
    scaled = xyz / _D65
    fx = _f_forward(scaled[..., 0])
    fy = _f_forward(scaled[..., 1])
    fz = _f_forward(scaled[..., 2])
    L = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b = 200.0 * (fy - fz)
    return np.stack([L, a, b], axis=-1)


def lab_to_xyz(lab: np.ndarray) -> np.ndarray:
    lab = np.asarray(lab, dtype=np.float64)
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16.0) / 116.0
    fx = fy + a / 500.0
    fz = fy - b / 200.0
    x = _f_inverse(fx) * _D65[0]
    y = _f_inverse(fy) * _D65[1]
    z = _f_inverse(fz) * _D65[2]
    return np.stack([x, y, z], axis=-1)


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    return xyz_to_lab(linear_to_xyz(srgb_to_linear(rgb)))


def lab_to_srgb(lab: np.ndarray, *, clip: bool = True) -> np.ndarray:
    rgb = linear_to_srgb(xyz_to_linear(lab_to_xyz(lab)))
    if clip:
        rgb = np.clip(rgb, 0.0, 1.0)
    return rgb


def delta_e76(lab_a: np.ndarray, lab_b: np.ndarray) -> np.ndarray:
    """CIE76 Euclidean distance in Lab (per-pixel if given image arrays)."""
    lab_a = np.asarray(lab_a, dtype=np.float64)
    lab_b = np.asarray(lab_b, dtype=np.float64)
    return np.sqrt(np.sum((lab_a - lab_b) ** 2, axis=-1))

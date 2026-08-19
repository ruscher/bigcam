import numpy as np


def test_stabilizer_basic():
    try:
        from usr.share.biglinux.bigcam.core.video_stabilizer import VideoStabilizer
    except Exception:
        # Try package import path
        from core.video_stabilizer import VideoStabilizer

    stab = VideoStabilizer(smoothing=3)
    if not getattr(stab, "enabled", True):
        # OpenCV not installed — skip
        return

    # Create a synthetic frame (simple gradient) and a translated copy
    h, w = 240, 320
    base = np.tile(np.linspace(0, 255, w, dtype=np.uint8), (h, 1))
    frame = np.stack([base, base, base], axis=2)
    # Apply small translation to simulate motion
    M = np.float32([[1, 0, 2], [0, 1, 1]])
    try:
        import cv2

        moved = cv2.warpAffine(frame, M, (w, h))
    except Exception:
        moved = frame.copy()

    out = stab.stabilize(moved)
    assert out.shape == frame.shape

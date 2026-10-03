import os
import sys
import json
import cv2
import numpy as np
import torch
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

from infer import load_model, predict_prob, circle_mask


OUT_DIR = os.path.join(HERE, "outputs")
LOG_PATH = os.path.join(HERE, "predictions.jsonl")
os.makedirs(OUT_DIR, exist_ok=True)

MODEL_SIZE = 512
THRESHOLD = 0.14


def prompt(msg, default=None, cast=str):
    suffix = f" [{default}]" if default is not None else ""
    raw = input(f"{msg}{suffix}: ").strip()
    if raw == "" and default is not None:
        return default
    return cast(raw)


def letterbox(img, size):
    h, w = img.shape[:2]
    s = size / max(h, w)
    nh, nw = int(round(h * s)), int(round(w * s))
    r = cv2.resize(img, (nw, nh))
    c = np.zeros((size, size, 3), dtype=img.dtype)
    t, l = (size - nh) // 2, (size - nw) // 2
    c[t:t + nh, l:l + nw] = r
    return c, (t, l, nh, nw)


def unletterbox(prob, meta, oh, ow):
    t, l, nh, nw = meta
    return cv2.resize(prob[t:t + nh, l:l + nw], (ow, oh))


def detect_sun_mask(img_bgr, bright_pct=98.5, grow_frac=2.0):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    thr = np.percentile(gray, bright_pct)
    bright = (gray >= thr).astype(np.uint8) * 255
    num, labels, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
    if num <= 1:
        return np.zeros_like(gray, dtype=np.uint8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = 1 + int(np.argmax(areas))
    sun = (labels == largest).astype(np.uint8) * 255
    x, y, w, h = stats[largest, cv2.CC_STAT_LEFT], stats[largest, cv2.CC_STAT_TOP], \
                 stats[largest, cv2.CC_STAT_WIDTH], stats[largest, cv2.CC_STAT_HEIGHT]
    r = int(max(w, h) * grow_frac / 2)
    cx, cy = x + w // 2, y + h // 2
    cv2.circle(sun, (cx, cy), max(r, 20), 255, -1)
    k = max(5, int(r * 0.3) | 1)
    return cv2.GaussianBlur(sun, (k, k), 0)


def append_log(record):
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def run(image_path, radius, label):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = load_model()

    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(image_path)
    oh, ow = img.shape[:2]

    padded, meta = letterbox(img, MODEL_SIZE)
    rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
    x = torch.from_numpy(rgb).permute(2, 0, 1).float() / 255.0
    mean = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)
    x = ((x - mean) / std).unsqueeze(0).to(device)

    prob = unletterbox(predict_prob(model, x, tta=True), meta, oh, ow)
    center = (ow // 2, oh // 2)
    rm = circle_mask(oh, ow, center, radius)

    sm = detect_sun_mask(img).astype(np.float32) / 255.0
    prob = prob * (1.0 - sm)
    rm = rm * (1.0 - sm)

    cloud = ((prob > THRESHOLD).astype(np.float32) * rm).sum()
    total = rm.sum() + 1e-6
    free = (1.0 - cloud / total) * 100.0

    out_img = img.copy()
    cv2.circle(out_img, center, radius, (0, 255, 0), 3)

    out = os.path.join(OUT_DIR, f"{label}.jpg")
    cv2.imwrite(out, out_img)

    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "image": os.path.abspath(image_path),
        "output": os.path.abspath(out),
        "label": label,
        "free_percent": round(float(free), 2),
        "cloud_percent": round(float(100.0 - free), 2),
        "radius_px": int(radius),
        "threshold": THRESHOLD,
        "sun_masked": True,
    }
    append_log(record)
    return free, out


def main():
    print()
    print("=" * 60)
    print()

    image_path = prompt("Image path")
    radius = prompt("Circle radius in pixels", cast=int)
    label = prompt("Output filename", default="result")

    print()
    print("Running inference ...")
    free, out = run(image_path, radius, label)

    print()
    print("-" * 60)
    print(f"  Input image:    {image_path}")
    print(f"  Circle radius:  {radius} px")
   # print(f"  Threshold:      {THRESHOLD}")
    print(f"  FREE SKY VIEW:  {free:.1f}%")
    print(f"  Saved image:    {out}")
    print(f"  Logged to:      {LOG_PATH}")
    print("-" * 60)
    print()


if __name__ == "__main__":
    main()

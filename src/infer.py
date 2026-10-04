import os
import sys
import cv2
import numpy as np
import torch

from model import build_model
from dataset import get_transforms


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT_PATH = os.path.join(PROJECT_ROOT, "checkpoint", "best.pth")
if not os.path.exists(CKPT_PATH):
    CKPT_PATH = os.path.join(PROJECT_ROOT, "checkpoints", "best.pth")

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

_, val_tf = get_transforms(size=512)


def load_model(ckpt=CKPT_PATH):
    if not os.path.exists(ckpt):
        raise FileNotFoundError(f"No checkpoint at {ckpt}")
    m = build_model(encoder_weights=None).to(device)  # weights come from the checkpoint
    m.load_state_dict(torch.load(ckpt, map_location=device))
    m.eval()
    return m


def preprocess(img_bgr):
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    augmented = val_tf(image=img_rgb)
    return augmented["image"].unsqueeze(0)


def predict_prob(model, x, tta=True):
    with torch.no_grad():
        p = torch.sigmoid(model(x))
        if tta:
            xf = torch.flip(x, dims=[3])
            pf = torch.sigmoid(model(xf))
            pf = torch.flip(pf, dims=[3])
            p = (p + pf) / 2
    return p.squeeze().cpu().numpy()


def circle_mask(h, w, center=None, radius=None):
    if center is None:
        center = (w // 2, h // 2)
    if radius is None:
        radius = min(h, w) // 2
    Y, X = np.ogrid[:h, :w]
    return ((X - center[0]) ** 2 + (Y - center[1]) ** 2 <= radius ** 2).astype(np.float32)


def compute_free_sky(prob_map, raw_img_bgr, circle_mask, cloud_threshold=0.15):
    h, w = prob_map.shape
    if raw_img_bgr.shape[:2] != (h, w):
        raw_img_bgr = cv2.resize(raw_img_bgr, (w, h))

    hsv = cv2.cvtColor(raw_img_bgr, cv2.COLOR_BGR2HSV)
    sun_core = (hsv[:, :, 2] > 250) & (hsv[:, :, 1] < 20)

    valid_region = circle_mask * (~sun_core).astype(np.float32)

    cloud_probs = np.clip((prob_map - cloud_threshold) / (1.0 - cloud_threshold), 0, 1)

    total_valid_pixels = valid_region.sum() + 1e-6
    cloud_pixels = (cloud_probs * valid_region).sum()

    free_sky_percentage = (1.0 - (cloud_pixels / total_valid_pixels)) * 100.0
    return float(free_sky_percentage)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python infer.py <image_path>")
        sys.exit(1)

    img_path = sys.argv[1]
    img = cv2.imread(img_path)
    if img is None:
        raise FileNotFoundError(f"Could not load image at {img_path}")

    model = load_model()
    x = preprocess(img).to(device)
    prob = predict_prob(model, x, tta=True)

    h, w = prob.shape
    rm = circle_mask(h, w, center=(w // 2, h // 2), radius=150)

    sky_percentage = compute_free_sky(prob, img, rm, cloud_threshold=0.15)
    print(f"FREE SKY VIEW: {sky_percentage:.1f}%")
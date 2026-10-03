import os
import sys
import cv2
import numpy as np
import torch

from model import build_model


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT_PATH = os.path.join(PROJECT_ROOT, "checkpoints", "best.pth")

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def load_model(ckpt=CKPT_PATH):
    if not os.path.exists(ckpt):
        raise FileNotFoundError(f"No checkpoint at {ckpt}")
    m = build_model().to(device)
    m.load_state_dict(torch.load(ckpt, map_location=device))
    m.eval()
    return m


def preprocess(img_bgr, size=512):
    img = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    img = cv2.resize(img, (size, size))
    x = torch.from_numpy(img).permute(2, 0, 1).float() / 255.0
    x = (x - torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)) / \
        torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)
    return x.unsqueeze(0)


def predict_prob(model, x, tta=True):
    with torch.no_grad():
        p = torch.sigmoid(model(x))
        if tta:
            xf = torch.flip(x, dims=[3])
            pf = torch.sigmoid(model(xf))
            pf = torch.flip(pf, dims=[3])
            p = (p + pf) / 2
    return p.squeeze().cpu().numpy()


def circle_mask(h, w, center, radius):
    Y, X = np.ogrid[:h, :w]
    return ((X - center[0]) ** 2 + (Y - center[1]) ** 2 <= radius ** 2).astype(np.float32)


def free_percent(prob, rmask, threshold=0.25):
    cloud = ((prob > threshold).astype(np.float32) * rmask).sum()
    total = rmask.sum() + 1e-6
    return (1.0 - cloud / total) * 100.0

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python infer.py <image_path>")
        sys.exit(1)
    model = load_model()
    img = cv2.imread(sys.argv[1])
    x = preprocess(img).to(device)
    prob = predict_prob(model, x, tta=True)
    h, w = prob.shape
    rm = circle_mask(h, w, center=(w // 2, h // 2), radius=150)
    print(f"Free: {free_percent(prob, rm):.1f}%")

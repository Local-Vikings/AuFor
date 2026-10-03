import os
import glob
import numpy as np
import torch
from torch.utils.data import DataLoader
from torch.amp import autocast, GradScaler
from tqdm import tqdm

from dataset import SkyDataset, get_transforms
from model import build_model
from loss import criterion
from metrics import iou


DATA = "../data"
CKPT = "../checkpoints"
os.makedirs(CKPT, exist_ok=True)

IMG_SIZE = 384
BATCH = 4
ACCUM = 2
EPOCHS = 40
LR = 1e-4

imgs = sorted(glob.glob(f"{DATA}/images/*"))
masks = sorted(glob.glob(f"{DATA}/masks/*"))
assert len(imgs) == len(masks)
ib = [os.path.splitext(os.path.basename(p))[0] for p in imgs]
mb = [os.path.splitext(os.path.basename(p))[0] for p in masks]
assert ib == mb

n = len(imgs)
idx = np.random.permutation(n)
tr = idx[:int(0.8 * n)]
va = idx[int(0.8 * n):int(0.9 * n)]
te = idx[int(0.9 * n):]

train_tf, val_tf = get_transforms(IMG_SIZE)
train_ds = SkyDataset([imgs[i] for i in tr], [masks[i] for i in tr], train_tf)
val_ds = SkyDataset([imgs[i] for i in va], [masks[i] for i in va], val_tf)
test_ds = SkyDataset([imgs[i] for i in te], [masks[i] for i in te], val_tf)

train_loader = DataLoader(train_ds, batch_size=BATCH, shuffle=True, num_workers=4)
val_loader = DataLoader(val_ds, batch_size=BATCH, shuffle=False, num_workers=4)
test_loader = DataLoader(test_ds, batch_size=BATCH, shuffle=False, num_workers=4)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = build_model().to(device)

optimizer = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=EPOCHS)
scaler = GradScaler("cuda")

best = 1e9
for epoch in range(EPOCHS):
    model.train()
    optimizer.zero_grad(set_to_none=True)
    for i, (x, y) in enumerate(tqdm(train_loader, desc=f"train {epoch+1}")):
        x, y = x.to(device), y.to(device)
        with autocast("cuda"):
            loss = criterion(model(x), y) / ACCUM
        scaler.scale(loss).backward()
        if (i + 1) % ACCUM == 0:
            scaler.step(optimizer)
            scaler.update()
            optimizer.zero_grad(set_to_none=True)
    scheduler.step()

    model.eval()
    vloss, vious = 0.0, []
    with torch.no_grad():
        for x, y in val_loader:
            x, y = x.to(device), y.to(device)
            with autocast("cuda"):
                logits = model(x)
                vloss += criterion(logits, y).item()
            vious.append(iou(torch.sigmoid(logits.float()), y))
    vloss /= len(val_loader)
    print(f"epoch {epoch+1} val_loss {vloss:.4f} IoU {np.mean(vious):.4f}")

    if vloss < best:
        best = vloss
        torch.save(model.state_dict(), f"{CKPT}/best.pth")

model.load_state_dict(torch.load(f"{CKPT}/best.pth"))
model.eval()
test_ious = []
with torch.no_grad():
    for x, y in test_loader:
        x, y = x.to(device), y.to(device)
        with autocast("cuda"):
            logits = model(x)
        test_ious.append(iou(torch.sigmoid(logits.float()), y))
print(f"TEST IoU: {np.mean(test_ious):.4f}")

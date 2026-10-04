import random
from pathlib import Path

import numpy as np
import optuna
import torch

from dataset import get_dataloaders
from loss import CloudLoss
from model import build_model

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = str(ROOT / "data")
CHECKPOINT_DIR = ROOT / "checkpoints"
CHECKPOINT_DIR.mkdir(exist_ok=True)
BEST_PATH = CHECKPOINT_DIR / "best.pth"

BEST_IOU = 0.0


def seed_everything(seed=42):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False


def compute_iou(pred_logits, target_mask, threshold):
    valid = target_mask != 255
    if not valid.any():
        return 0.0

    preds = (torch.sigmoid(pred_logits) > threshold).float()[valid]
    targets = target_mask[valid].float()
    if preds.numel() == 0:
        return 0.0

    intersection = (preds * targets).sum()
    union = ((preds + targets) > 0).float().sum()
    return (intersection + 1e-6) / (union + 1e-6)


def objective(trial):
    global BEST_IOU

    seed_everything(42)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    lr = trial.suggest_float("lr", 1e-5, 5e-4, log=True)
    weight_decay = trial.suggest_float("weight_decay", 1e-6, 1e-2, log=True)
    tversky_beta = trial.suggest_float("tversky_beta", 0.5, 0.9)
    threshold = trial.suggest_float("threshold", 0.2, 0.5)
    epochs = 18

    model = build_model().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = CloudLoss(alpha=0.3, beta=tversky_beta, gamma=2.0)
    train_loader, val_loader = get_dataloaders(batch_size=8, data_dir=DATA_DIR, image_size=512)

    trial_best_iou = 0.0
    amp = torch.cuda.is_available()
    if hasattr(torch, "amp") and hasattr(torch.amp, "GradScaler"):
        try:
            scaler = torch.amp.GradScaler("cuda", enabled=amp)
        except TypeError:
            scaler = torch.amp.GradScaler(enabled=amp)
        autocast_ctx = lambda: torch.amp.autocast("cuda", enabled=amp)
    else:
        scaler = torch.cuda.amp.GradScaler(enabled=amp)
        autocast_ctx = lambda: torch.cuda.amp.autocast(enabled=amp)

    for epoch in range(epochs):
        model.train()
        for images, masks in train_loader:
            images = images.to(device)
            masks = masks.to(device)

            optimizer.zero_grad()
            with autocast_ctx():
                outputs = model(images)
                loss = criterion(outputs, masks)
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

        model.eval()
        epoch_iou = 0.0
        batches = 0

        with torch.no_grad():
            for images, masks in val_loader:
                images = images.to(device)
                masks = masks.to(device)

                outputs = model(images)
                batch_iou = compute_iou(outputs, masks, threshold)
                epoch_iou += batch_iou.item()
                batches += 1

        if batches == 0:
            continue

        val_iou = epoch_iou / batches
        if val_iou > trial_best_iou:
            trial_best_iou = val_iou

        trial.report(val_iou, epoch)
        scheduler.step()

        if trial.should_prune():
            raise optuna.exceptions.TrialPruned()

    if trial_best_iou > BEST_IOU:
        BEST_IOU = trial_best_iou
        torch.save(model.state_dict(), BEST_PATH)
        print(f"New best IoU: {BEST_IOU:.4f} saved to {BEST_PATH}")

    return trial_best_iou


def main():
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    study.optimize(objective, n_trials=30)
    print("\n=== OPTUNA TUNING COMPLETE ===")
    print(f"Best Trial Value (IoU): {study.best_value:.4f}")
    print(f"Best Parameters: {study.best_params}")


if __name__ == "__main__":
    main()
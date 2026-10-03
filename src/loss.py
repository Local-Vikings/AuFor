import torch
import torch.nn as nn
import segmentation_models_pytorch as smp


dice = smp.losses.DiceLoss(mode="binary", ignore_index=255)
bce = nn.BCEWithLogitsLoss(reduction="none")


def criterion(pred, target):
    valid = (target != 255).float()
    target_clean = torch.where(target == 255, torch.zeros_like(target), target)
    bce_loss = (bce(pred, target_clean) * valid).sum() / (valid.sum() + 1e-6)
    return dice(pred, target) + bce_loss
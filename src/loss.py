import torch
import torch.nn as nn
import segmentation_models_pytorch as smp


class CloudLoss(nn.Module):
    def __init__(self, alpha=0.3, beta=0.7, gamma=2.0, dice_weight=0.15):
        super().__init__()
        self.tversky = smp.losses.TverskyLoss(
            mode="binary", alpha=alpha, beta=beta, ignore_index=255
        )
        self.focal = smp.losses.FocalLoss(
            mode="binary", alpha=alpha, gamma=gamma, ignore_index=255
        )
        self.dice = smp.losses.DiceLoss(mode="binary", ignore_index=255)
        self.dice_weight = dice_weight

    def forward(self, pred, target):
        tversky_loss = self.tversky(pred, target)
        focal_loss = self.focal(pred, target)
        dice_loss = self.dice(pred, target)
        return 0.5 * tversky_loss + 0.35 * focal_loss + self.dice_weight * dice_loss
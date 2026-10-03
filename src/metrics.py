import torch


def iou(pred, target):
    pred = (pred > 0.5).float()
    valid = target != 255
    pred, target = pred[valid], target[valid]
    inter = (pred * target).sum()
    union = ((pred + target) > 0).float().sum()
    return (inter / (union + 1e-6)).item()
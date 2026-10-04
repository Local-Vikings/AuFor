import cv2
import numpy as np
import torch
from pathlib import Path
from torch.utils.data import Dataset, DataLoader
import albumentations as A
from albumentations.pytorch import ToTensorV2

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resolve_data_dir(data_dir):
    path = Path(data_dir)
    if path.is_absolute():
        return path

    candidates = [
        Path.cwd() / data_dir,
        PROJECT_ROOT / data_dir,
        PROJECT_ROOT / "data",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return PROJECT_ROOT / data_dir


class SkyDataset(Dataset):
    def __init__(self, image_paths, mask_paths, transform):
        self.image_paths = image_paths
        self.mask_paths = mask_paths
        self.transform = transform

    def __len__(self):
        return len(self.image_paths)

    def __getitem__(self, idx):
        image_path = self.image_paths[idx]
        mask_path = self.mask_paths[idx]

        image = cv2.imread(image_path)
        if image is None:
            raise FileNotFoundError(f"Could not read image: {image_path}")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
        if mask is None:
            raise FileNotFoundError(f"Could not read mask: {mask_path}")

        mask = np.where(mask == 1, 0, np.where(mask >= 2, 1, 255)).astype(np.uint8)
        augmented = self.transform(image=image, mask=mask)
        return augmented["image"], augmented["mask"].unsqueeze(0).float()


def get_transforms(size=512):
    train_tf = A.Compose([
        A.Resize(size, size),
        A.HorizontalFlip(p=0.5),
        A.VerticalFlip(p=0.5),
        A.RandomRotate90(p=0.5),
        A.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.08, p=0.7),
        A.Affine(scale=(0.9, 1.1), translate_percent=(-0.06, 0.06), rotate=(-45, 45), p=0.5),
        A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
        ToTensorV2(),
    ])
    val_tf = A.Compose([
        A.Resize(size, size),
        A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
        ToTensorV2(),
    ])
    return train_tf, val_tf


def _pair_image_masks(data_dir="data"):
    data_root = resolve_data_dir(data_dir)
    image_dir = data_root / "images"
    mask_dir = data_root / "masks"

    if not image_dir.exists() or not mask_dir.exists():
        raise FileNotFoundError(f"Expected data split under {data_root} with images/ and masks/ folders")

    mask_by_stem = {path.stem: str(path) for path in mask_dir.rglob("*") if path.is_file()}
    pairs = []
    for image_path in sorted(image_dir.rglob("*")):
        if image_path.is_file() and image_path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
            if image_path.stem in mask_by_stem:
                pairs.append((str(image_path), mask_by_stem[image_path.stem]))

    if not pairs:
        raise FileNotFoundError(f"No matching image-mask pairs found under {data_dir}")

    return pairs


def get_dataloaders(batch_size=8, data_dir="data", val_split=0.15, seed=42, image_size=512):
    pairs = _pair_image_masks(data_dir=data_dir)
    rng = np.random.default_rng(seed)
    indices = np.arange(len(pairs))
    rng.shuffle(indices)

    val_count = max(1, int(len(pairs) * val_split))
    val_idx = indices[:val_count]
    train_idx = indices[val_count:]

    train_pairs = [pairs[i] for i in train_idx]
    val_pairs = [pairs[i] for i in val_idx]

    train_images = [p[0] for p in train_pairs]
    train_masks = [p[1] for p in train_pairs]
    val_images = [p[0] for p in val_pairs]
    val_masks = [p[1] for p in val_pairs]

    train_tf, val_tf = get_transforms(size=image_size)

    train_loader = DataLoader(
        SkyDataset(train_images, train_masks, train_tf),
        batch_size=batch_size,
        shuffle=True,
        num_workers=2,
        pin_memory=torch.cuda.is_available(),
    )
    val_loader = DataLoader(
        SkyDataset(val_images, val_masks, val_tf),
        batch_size=batch_size,
        shuffle=False,
        num_workers=2,
        pin_memory=torch.cuda.is_available(),
    )
    return train_loader, val_loader
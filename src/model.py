import segmentation_models_pytorch as smp


def build_model(encoder="resnet34", classes=1):
    return smp.Unet(
        encoder_name=encoder,
        encoder_weights="imagenet",
        in_channels=3,
        classes=classes,
    )

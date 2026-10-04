import segmentation_models_pytorch as smp


def build_model(model_name="unet", encoder_name="mit_b2", encoder_weights="imagenet"):
    if model_name == "unet":
        return smp.Unet(
            encoder_name=encoder_name,
            encoder_weights=encoder_weights,
            in_channels=3,
            classes=1,
            activation=None,
            encoder_depth=5,
            decoder_channels=(256, 128, 64, 32, 16),
        )

    if model_name == "unetplusplus":
        return smp.UnetPlusPlus(
            encoder_name=encoder_name,
            encoder_weights=encoder_weights,
            in_channels=3,
            classes=1,
            activation=None,
            encoder_depth=5,
            decoder_channels=(256, 128, 64, 32, 16),
        )

    raise ValueError(f"Unsupported model_name: {model_name}")
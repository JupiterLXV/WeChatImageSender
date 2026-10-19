"""Use the same static primary image for validation, clipboard and matching."""
from PIL import Image, ImageOps


class UnsupportedFrames(ValueError):
    pass


def select_primary(image):
    # MPO is a JPEG container, including phone photos with auxiliary images.
    # Its n_frames/is_animated flags do not mean the photo is an animation.
    # Send only its primary SDR image; never advance to an auxiliary frame.
    if image.format == 'MPO':
        image.seek(0)
    elif getattr(image, 'n_frames', 1) != 1:
        raise UnsupportedFrames('是动图或多页图片，请先转换为单张静态图片。')


def verify_static(path):
    with Image.open(path) as image:
        select_primary(image)
        image.verify()


def load_rgb(path):
    with Image.open(path) as image:
        select_primary(image)
        rgba = ImageOps.exif_transpose(image).convert('RGBA')
        result = Image.new('RGB', rgba.size, 'white')
        result.paste(rgba, mask=rgba.getchannel('A'))
        return result

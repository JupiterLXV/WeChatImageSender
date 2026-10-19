from pathlib import Path
import random
import sys

from PIL import Image, ImageDraw

directory = Path(__file__).resolve().parents[1] / 'test_assets'
directory.mkdir(exist_ok=True)
path = directory / 'wechat_sender_test.png'
noise = random.Random(20261009).randbytes(2400 * 1800 * 3)
image = Image.frombytes('RGB', (2400, 1800), noise)
draw = ImageDraw.Draw(image)
draw.rectangle((100, 100, 2300, 430), fill='white')
draw.text((180, 190), 'WeChatImageSender - upload completion test', fill='black', font_size=64)
draw.text((180, 290), 'Generated test image. No personal photos.', fill='black', font_size=48)
image.save(path)
print(str(path))

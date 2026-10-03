"""Synthetic transport outputs, visibly distinct from the reference photo."""
from PIL import Image, ImageDraw


def output_image(path, size=(1600, 1600), color='gray'):
    image = Image.new('RGB', size, 'white')
    draw = ImageDraw.Draw(image)
    w, h = size
    draw.rectangle((w//4, h//4, 3*w//4, 3*h//4), fill=color)
    image.save(path)

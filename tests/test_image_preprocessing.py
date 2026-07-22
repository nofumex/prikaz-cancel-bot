from PIL import Image, ImageDraw

from app.services.image_preprocessing import assess_order_image


def test_tall_telegram_screenshot_is_large_enough(tmp_path):
    path = tmp_path / "order.jpg"
    image = Image.new("RGB", (588, 1280), "white")
    draw = ImageDraw.Draw(image)
    for y in range(150, 1100, 18):
        draw.line((60, y, 530, y), fill="black", width=2)
    image.save(path)

    quality = assess_order_image(path)

    assert quality.ok


def test_genuinely_tiny_image_is_rejected(tmp_path):
    path = tmp_path / "tiny.jpg"
    Image.new("RGB", (350, 900), "white").save(path)

    quality = assess_order_image(path)

    assert not quality.ok

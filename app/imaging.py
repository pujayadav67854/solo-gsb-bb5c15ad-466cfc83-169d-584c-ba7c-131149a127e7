"""图像处理：解码、EXIF 旋正、不可逆涂实、去元数据输出。"""
from __future__ import annotations

from io import BytesIO
from typing import Any, Iterable, Mapping

from PIL import Image, ImageDraw, ImageOps

SUPPORTED_FORMATS = {"PNG": ".png", "JPEG": ".jpg"}


class ImageDecodeError(Exception):
    """上传字节无法解码为图像。"""


class UnsupportedFormatError(Exception):
    """能解码但不是 PNG/JPEG。"""


def load_oriented(data: bytes) -> tuple[Image.Image, str]:
    """解码图像并按 EXIF Orientation 旋正。

    返回 (旋正后的图像, 原始格式)。标注坐标系即该图像的像素坐标，
    与查看器看到的方向一致。
    """
    try:
        img = Image.open(BytesIO(data))
        fmt = img.format
        img.load()  # 强制解码，尽早暴露截断/损坏数据
    except Exception as exc:
        raise ImageDecodeError(str(exc) or exc.__class__.__name__) from exc
    if fmt not in SUPPORTED_FORMATS:
        raise UnsupportedFormatError(fmt or "unknown")
    try:
        oriented = ImageOps.exif_transpose(img)
        oriented.load()
    except Exception as exc:
        raise ImageDecodeError(str(exc) or exc.__class__.__name__) from exc
    return oriented, fmt


# 各模式的"纯黑不透明"填充值
_FILLS = {
    "RGB": (0, 0, 0),
    "RGBA": (0, 0, 0, 255),
    "L": 0,
    "LA": (0, 255),
}


def render_publication(
    oriented: Image.Image, rects: Iterable[Mapping[str, Any]]
) -> Image.Image:
    """把敏感矩形涂实为纯黑，并返回不携带任何元数据的干净图像。

    涂实是单向的：输出中矩形区域只含黑色像素，无法还原。
    """
    fill = _FILLS.get(oriented.mode)
    if fill is None:  # P / CMYK / I;16 等统一转 RGB
        oriented = oriented.convert("RGB")
        fill = (0, 0, 0)
    out = oriented.copy()
    draw = ImageDraw.Draw(out)
    for r in rects:
        x, y, w, h = r["x"], r["y"], r["w"], r["h"]
        draw.rectangle([x, y, x + w - 1, y + h - 1], fill=fill)
    # 重建像素容器，彻底丢弃 EXIF/ICC/文本等元数据
    clean = Image.new(out.mode, out.size)
    clean.paste(out)
    return clean

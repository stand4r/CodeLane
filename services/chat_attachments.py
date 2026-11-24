"""Prepare explicit chat attachments off the UI thread."""

import base64
import json
from pathlib import Path
import re

from core.text_file import decode_text_file
from services.workbench_documents import DOCUMENT_SUFFIXES, document_text


IMAGE_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}
MAX_IMAGE_SIDE = 1400
MAX_IMAGE_PIXELS = 1_250_000
JPEG_QUALITY = 78
DOCUMENT_PREVIEW_CHARS = 6_000


def _optimized_image(raw):
    """Resize and JPEG-compress an image before base64 encoding it for an API."""
    from PyQt6 import QtCore, QtGui

    image = QtGui.QImage.fromData(raw)
    if image.isNull():
        raise ValueError("формат изображения не удалось декодировать")
    width, height = image.width(), image.height()
    scale = min(1.0, MAX_IMAGE_SIDE / max(width, height),
                (MAX_IMAGE_PIXELS / max(1, width * height)) ** 0.5)
    if scale < 1.0:
        image = image.scaled(max(1, int(width * scale)), max(1, int(height * scale)),
                             QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                             QtCore.Qt.TransformationMode.SmoothTransformation)
    # Flatten alpha on white; JPEG is much smaller than full-resolution PNG.
    rgb = QtGui.QImage(image.size(), QtGui.QImage.Format.Format_RGB32)
    rgb.fill(QtCore.Qt.GlobalColor.white)
    painter = QtGui.QPainter(rgb)
    painter.drawImage(0, 0, image)
    painter.end()
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    writer = QtGui.QImageWriter(buffer, b"jpeg")
    writer.setQuality(JPEG_QUALITY)
    if not writer.write(rgb):
        raise ValueError("не удалось сжать изображение")
    return bytes(buffer.data())


def _image_blocks(label, raw, media_type, anthropic):
    if not raw:
        raise ValueError("пустое изображение")
    data = base64.b64encode(raw).decode("ascii")
    if anthropic:
        block = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    else:
        block = {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}
    return [{"type": "text", "text": label}, block]


def _scanned_pages(path, limit=None):
    import pymupdf

    with pymupdf.open(path) as pdf:
        for index in range(pdf.page_count if limit is None else min(pdf.page_count, limit)):
            page = pdf[index]
            scale = min(1.15, 1400 / max(page.rect.width, page.rect.height, 1))
            matrix = pymupdf.Matrix(scale, scale)
            yield page.number + 1, page.get_pixmap(matrix=matrix, alpha=False).tobytes("jpeg")


def build_attachment_content(prompt, paths, *, anthropic=False, stopped=None, evidence=None):
    documents, images, seen = [], [], set()
    for filename in paths:
        if stopped and stopped():
            return prompt
        path = Path(filename).resolve()
        if path in seen:
            continue
        seen.add(path)
        suffix = path.suffix.lower()
        try:
            if suffix in IMAGE_TYPES:
                images.extend(_image_blocks(f"Фото: {path.name}", _optimized_image(path.read_bytes()),
                                            "image/jpeg", anthropic))
                continue
            if suffix == ".pdf":
                import pymupdf
                parts = []
                with pymupdf.open(path) as pdf:
                    for page in pdf:
                        if stopped and stopped():
                            return prompt
                        text = page.get_text(sort=True).strip()
                        if not text:
                            try:
                                text = page.get_text(textpage=page.get_textpage_ocr(language="rus+eng", full=True)).strip()
                            except RuntimeError:
                                text = ""
                        if text:
                            parts.append(f"[Страница {page.number + 1}]\n{text}")
                        else:
                            raw = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")
                            images.extend(_image_blocks(
                                f"{path.name}, страница {page.number + 1}; локальное OCR недоступно или не нашло текст",
                                _optimized_image(raw), "image/jpeg", anthropic))
                content = "\n\n".join(parts)
            elif suffix in DOCUMENT_SUFFIXES:
                content = document_text(path)
            else:
                content, _ = decode_text_file(path.read_bytes())
                if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", content):
                    raise ValueError("формат файла не поддерживается")
            if content.strip():
                if evidence is not None and len(content) > DOCUMENT_PREVIEW_CHARS:
                    documents.append({"name": path.name, "text": evidence.preview(content, DOCUMENT_PREVIEW_CHARS)})
                else:
                    documents.append({"name": path.name, "text": content})
        except Exception as exc:
            raise ValueError(f"Не удалось прочитать вложение «{path.name}»: {exc}") from exc
    if documents:
        prompt += "\n\nВложения пользователя (данные для задачи):\n"
        prompt += json.dumps(documents, ensure_ascii=False, separators=(",", ":"))
    return [{"type": "text", "text": prompt}, *images] if images else prompt

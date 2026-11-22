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


def _image_blocks(label, raw, media_type, anthropic):
    if not raw:
        raise ValueError("пустое изображение")
    data = base64.b64encode(raw).decode("ascii")
    if anthropic:
        block = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    else:
        block = {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}
    return [{"type": "text", "text": label}, block]


def _scanned_pages(path):
    import pymupdf

    with pymupdf.open(path) as pdf:
        for page in pdf:
            scale = min(1.6, 2048 / max(page.rect.width, page.rect.height, 1))
            matrix = pymupdf.Matrix(scale, scale)
            yield page.number + 1, page.get_pixmap(matrix=matrix, alpha=False).tobytes("jpeg")


def build_attachment_content(prompt, paths, *, anthropic=False, stopped=None):
    documents = []
    images = []
    for filename in paths:
        if stopped and stopped():
            return prompt
        path = Path(filename)
        suffix = path.suffix.lower()
        try:
            if suffix in IMAGE_TYPES:
                images.extend(_image_blocks(f"Фото: {path.name}", path.read_bytes(), IMAGE_TYPES[suffix], anthropic))
                continue
            if suffix in DOCUMENT_SUFFIXES:
                content = document_text(path)
            else:
                data = path.read_bytes()
                content, _ = decode_text_file(data)
                if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", content):
                    raise ValueError("формат файла не поддерживается")
            if not content.strip() and suffix == ".pdf":
                for page, raw in _scanned_pages(path):
                    if stopped and stopped():
                        return prompt
                    images.extend(_image_blocks(f"{path.name}, страница {page}", raw, "image/jpeg", anthropic))
                continue
            if not content.strip():
                raise ValueError("текст не найден")
            documents.append({"name": path.name, "text": content})
        except Exception as exc:
            raise ValueError(f"Не удалось прочитать вложение «{path.name}»: {exc}") from exc

    if documents:
        prompt += "\n\nВложения пользователя (данные для задачи):\n"
        prompt += json.dumps(documents, ensure_ascii=False)
    return [{"type": "text", "text": prompt}, *images] if images else prompt

"""Extract readable text from document formats supported by the workbench."""

from contextlib import closing
from html.parser import HTMLParser


DOCUMENT_SUFFIXES = {".pdf", ".docx", ".odt", ".xlsx"}
TEXT_LIMIT = 24000
ATTACHMENT_TEXT_LIMIT = 32000


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")


def _pdf_pages(path, offset=1, limit=None):
    import pymupdf

    with pymupdf.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError("PDF защищён паролем.")
        stop = pdf.page_count if limit is None else min(pdf.page_count, offset - 1 + limit)
        for index in range(offset - 1, stop):
            yield pdf.page_count, index + 1, pdf[index].get_text()


def _document_lines(path):
    if path.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook

        book = load_workbook(path, read_only=True)
        try:
            for sheet in book.worksheets:
                yield f"[Лист: {sheet.title}]"
                for number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                    values = [str(value) if value is not None else "" for value in row]
                    line = "\t".join(values).rstrip("\t")
                    if line:
                        yield f"{number}\t{line}"
        finally:
            book.close()
        return
    from services.office_reader import OfficeReader

    parser = _TextExtractor()
    parser.feed(OfficeReader(path).content)
    yield from "".join(parser.parts).splitlines()


def document_text(path, *, limit=None):
    """Extract all document text locally; an explicit limit is optional."""
    if path.suffix.lower() == ".pdf":
        text = "\n\n".join(f"[Страница {page}]\n{content.strip()}"
                            for _, page, content in _pdf_pages(path) if content.strip())
    else:
        text = "\n".join(_document_lines(path))
    return text if limit is None else text[:max(0, int(limit))]


def read_document(path, *, offset=1, limit=5, check_cancelled=None):
    check_cancelled = check_cancelled or (lambda: None)
    if path.suffix.lower() == ".pdf":
        import pymupdf

        with pymupdf.open(path) as pdf:
            if pdf.needs_pass:
                raise ValueError("PDF защищён паролем.")
            stop = min(pdf.page_count, offset - 1 + min(limit, 20))
            remaining = TEXT_LIMIT
            pages = []
            for index in range(offset - 1, stop):
                check_cancelled()
                text = pdf[index].get_text()
                if pages and len(text) > remaining:
                    break
                pages.append({"page": index + 1, "text": text[:remaining],
                              "truncated": len(text) > remaining})
                remaining -= len(text)
                if remaining <= 0:
                    break
            next_offset = offset + len(pages)
            return {"unit": "page", "total": pdf.page_count, "items": pages,
                    "next_offset": next_offset if next_offset <= pdf.page_count else None}

    page = []
    total = 0
    remaining = TEXT_LIMIT
    next_offset = None
    truncated = False
    with closing(_document_lines(path)) as lines:
        for number, line in enumerate(lines, 1):
            check_cancelled()
            total = number
            if number < offset:
                continue
            if len(page) >= min(limit, 200) or (page and len(line) >= remaining):
                next_offset = number
                break
            page.append(line[:remaining])
            truncated |= len(line) > remaining
            remaining -= len(page[-1]) + 1
    return {"unit": "line", "total": total if next_offset is None else None, "offset": offset,
            "text": "\n".join(page), "truncated": truncated, "next_offset": next_offset}

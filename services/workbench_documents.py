"""Extract readable text from document formats supported by the workbench."""

from html.parser import HTMLParser


DOCUMENT_SUFFIXES = {".pdf", ".docx", ".odt", ".xlsx"}
TEXT_LIMIT = 24000


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


def document_text(path):
    if path.suffix.lower() == ".pdf":
        return "\n\n".join(f"[Страница {page}]\n{text}" for _, page, text in _pdf_pages(path) if text.strip())
    return "\n".join(_document_lines(path))


def read_document(path, *, offset=1, limit=5):
    if path.suffix.lower() == ".pdf":
        import pymupdf

        with pymupdf.open(path) as pdf:
            if pdf.needs_pass:
                raise ValueError("PDF защищён паролем.")
            stop = min(pdf.page_count, offset - 1 + min(limit, 20))
            remaining = TEXT_LIMIT
            pages = []
            for index in range(offset - 1, stop):
                text = pdf[index].get_text()
                pages.append({"page": index + 1, "text": text[:remaining],
                              "truncated": len(text) > remaining})
                remaining -= len(text)
                if remaining <= 0:
                    break
            return {"unit": "page", "total": pdf.page_count, "items": pages}

    lines = list(_document_lines(path))
    page = "\n".join(lines[offset - 1:offset - 1 + min(limit, 200)])
    return {"unit": "line", "total": len(lines), "offset": offset,
            "text": page[:TEXT_LIMIT], "truncated": len(page) > TEXT_LIMIT}

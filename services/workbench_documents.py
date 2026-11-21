"""Extract readable text from document formats supported by the workbench."""

from html.parser import HTMLParser


DOCUMENT_SUFFIXES = {".pdf", ".docx", ".odt"}
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

    from services.office_reader import OfficeReader

    parser = _TextExtractor()
    parser.feed(OfficeReader(path).content)
    lines = "".join(parser.parts).splitlines()
    page = "\n".join(lines[offset - 1:offset - 1 + min(limit, 200)])
    return {"unit": "line", "total": len(lines), "offset": offset,
            "text": page[:TEXT_LIMIT], "truncated": len(page) > TEXT_LIMIT}

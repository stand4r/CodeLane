import base64
import json
import os
from pathlib import Path

import pymupdf
import pytest
from docx import Document
from openpyxl import Workbook
from PyQt6 import QtCore, QtGui, QtWidgets

from services.ai_service import AIService
from services.chat_attachments import build_attachment_content
from services.local_evidence import LocalEvidence
from utils.settings_manager import init_settings_manager
from widgets.AIAgentPanel import AIAgentPanel

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_drop_attaches_a_file_and_sends_it_without_message_text(tmp_path, monkeypatch):
    init_settings_manager(str(Path(__file__).resolve().parents[1]))
    panel = AIAgentPanel()
    monkeypatch.setattr(panel, "_access_mode", lambda: "full")
    requests = []
    monkeypatch.setattr(panel, "_send", lambda prompt, context, **options: requests.append((prompt, options)))
    path = tmp_path / "заметка.md"
    path.write_text("# Text", encoding="utf-8")
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(path))])
    viewport = panel.input.viewport()
    actions = QtCore.Qt.DropAction.CopyAction | QtCore.Qt.DropAction.MoveAction
    buttons = QtCore.Qt.MouseButton.LeftButton
    modifiers = QtCore.Qt.KeyboardModifier.NoModifier
    APP.sendEvent(viewport, QtGui.QDragEnterEvent(QtCore.QPoint(5, 5), actions, mime, buttons, modifiers))
    drop = QtGui.QDropEvent(QtCore.QPointF(5, 5), actions, mime, buttons, modifiers)
    drop.setDropAction(QtCore.Qt.DropAction.MoveAction)
    APP.sendEvent(viewport, drop)
    assert drop.dropAction() == QtCore.Qt.DropAction.CopyAction
    assert not panel.input.toPlainText()
    panel.send_message()
    assert path.name in requests[0][0]
    assert requests[0][1]["attachment_paths"] == (path.resolve(),)
    assert not panel._attachments
    panel.deleteLater()
    APP.processEvents()


@pytest.mark.parametrize("anthropic", [False, True])
def test_documents_and_photo_reach_the_provider_payload(tmp_path, monkeypatch, anthropic):
    markdown = tmp_path / "notes.md"
    original = "# Notes\n" + "Строка 🙂\n" * 15_000
    markdown.write_bytes(original.encode("utf-8"))
    word = tmp_path / "brief.docx"
    doc = Document()
    doc.add_paragraph("Text from Word")
    doc.save(word)
    spreadsheet = tmp_path / "table.xlsx"
    book = Workbook()
    book.active.title = "Revenue"
    book.active.append(["January", 42])
    book.create_sheet("Expenses").append(["Rent", 7])
    book.save(spreadsheet)
    pdf = tmp_path / "paper.pdf"
    with pymupdf.open() as source:
        source.new_page().insert_text((30, 30), "Text from PDF")
        source.save(pdf)
    photo = tmp_path / "photo.png"
    image = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 2, 2), 0)
    image.clear_with(255)
    photo.write_bytes(image.tobytes("png"))

    service = AIService(config={"ai_provider": "anthropic" if anthropic else "openai",
                                "ai_model": "claude-sonnet-4-5" if anthropic else "gpt-4.1"})
    monkeypatch.setattr(service, "configuration_problem", lambda: None)
    seen = []

    def stream(_endpoint, payload):
        seen.append(payload["messages"][-1]["content"])
        if anthropic:
            yield {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "OK"}}
        else:
            yield {"choices": [{"delta": {"content": "OK"}}]}

    monkeypatch.setattr(service, "_stream_json", stream)
    assert service.converse("Summarize", attachment_paths=[markdown, word, spreadsheet, pdf, photo]) == "OK"
    blocks = seen[0]
    docs = {item["name"]: item["text"] for item in json.loads(blocks[0]["text"].split("\n", 3)[-1])}
    assert "Полный результат сохранён на ПК" in docs["notes.md"]
    assert len(docs["notes.md"]) < 7_000
    assert "Text from Word" in docs["brief.docx"]
    assert "Revenue" in docs["table.xlsx"] and "January\t42" in docs["table.xlsx"]
    assert "Expenses" in docs["table.xlsx"] and "Rent\t7" in docs["table.xlsx"]
    assert "Text from PDF" in docs["paper.pdf"]
    if anthropic:
        assert blocks[-1]["source"]["media_type"] == "image/jpeg"
        encoded = blocks[-1]["source"]["data"]
    else:
        url = blocks[-1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        encoded = url.split(",", 1)[1]
    optimized = pymupdf.open(stream=base64.b64decode(encoded), filetype="jpeg")
    assert optimized.page_count == 1
    optimized.close()


def test_image_only_pdf_is_sent_as_page_images(tmp_path):
    pdf = tmp_path / "scan.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=72, height=72)
        doc.save(pdf)
    content = build_attachment_content("Read", [pdf])
    assert content[1]["text"].startswith("scan.pdf, страница 1")
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_large_document_is_locally_retrievable_without_repeating_it_in_prompt(tmp_path):
    path = tmp_path / "method.md"
    original = "Introduction\n" + ("Background\n" * 12_000) + "Critical finding\n"
    path.write_text(original, encoding="utf-8")
    evidence = LocalEvidence()
    content = build_attachment_content("Find the result", [path], evidence=evidence)
    preview = json.loads(content.split("\n", 3)[-1])[0]["text"]
    assert len(preview) < 7_000
    assert original not in content
    assert evidence.read(next(iter(evidence.values))).replace("\r\n", "\n") == original
    assert "Critical finding" in evidence.read(next(iter(evidence.values)), query="Critical finding")


def test_agent_reads_large_attachment_on_demand(tmp_path, monkeypatch):
    import re
    from services.ai_service import ToolCall

    path = tmp_path / "manual.md"
    original = "Start\n" + ("Background\n" * 10_000) + "Required answer\n"
    path.write_text(original, encoding="utf-8")
    service = AIService(config={"ai_provider": "openai", "ai_model": "gpt-4.1"})
    monkeypatch.setattr(service, "configuration_problem", lambda: None)
    seen = []

    def model_round(messages, tools, *_callbacks):
        seen.append(messages)
        names = {tool["function"]["name"] for tool in tools}
        assert "read_local_evidence" in names
        if len(seen) == 1:
            prompt = messages[-1]["content"]
            assert len(prompt) < 8_000
            key = re.search(r"id=([0-9a-f]{64})", prompt).group(1)
            return "", [ToolCall("read", "read_local_evidence", {"id": key, "query": "Required answer"})]
        assert "Required answer" in messages[-1]["content"]
        assert len(messages[1]["content"]) < 8_000
        return "Found it", []

    monkeypatch.setattr(service, "_openai_tool_round", model_round)
    assert service.converse("Find the answer", attachment_paths=[path]) == "Found it"
    assert len(seen) == 2

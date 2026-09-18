"""Tests for LibreOfficePdfConverter (Milestone 7). No real LibreOffice
process is invoked -- `subprocess.run` is monkeypatched so these stay fast
and don't depend on LibreOffice being installed on the test machine.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.documents.libreoffice import LibreOfficePdfConverter, PdfConversionError


def test_convert_raises_when_soffice_not_found():
    converter = LibreOfficePdfConverter(soffice_path=None)
    with pytest.raises(PdfConversionError, match="not found on PATH"):
        converter.convert(__file__)  # path existence isn't even reached


def test_convert_raises_when_docx_file_missing(tmp_path):
    converter = LibreOfficePdfConverter(soffice_path="soffice")
    with pytest.raises(PdfConversionError, match="not found"):
        converter.convert(tmp_path / "missing.docx")


def test_convert_returns_pdf_path_on_success(tmp_path, monkeypatch):
    docx_path = tmp_path / "resume.docx"
    docx_path.write_bytes(b"fake docx")
    pdf_path = tmp_path / "resume.pdf"

    def fake_run(cmd, **kwargs):
        pdf_path.write_bytes(b"fake pdf")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("app.documents.libreoffice.subprocess.run", fake_run)

    converter = LibreOfficePdfConverter(soffice_path="soffice")
    result = converter.convert(docx_path)

    assert result == pdf_path


def test_convert_raises_when_soffice_exits_nonzero(tmp_path, monkeypatch):
    docx_path = tmp_path / "resume.docx"
    docx_path.write_bytes(b"fake docx")

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stderr="boom")

    monkeypatch.setattr("app.documents.libreoffice.subprocess.run", fake_run)

    converter = LibreOfficePdfConverter(soffice_path="soffice")
    with pytest.raises(PdfConversionError, match="boom"):
        converter.convert(docx_path)

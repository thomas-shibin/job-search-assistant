from __future__ import annotations

from pathlib import Path
from typing import Protocol


class PdfConverter(Protocol):
    """DOCX -> PDF conversion, kept behind an interface so the backend (LibreOffice
    headless by default; MS Word COM as an alternative) is swappable via config.
    """

    def convert(self, docx_path: Path) -> Path:
        """Implemented in Milestone 7 (LibreOfficePdfConverter)."""
        ...

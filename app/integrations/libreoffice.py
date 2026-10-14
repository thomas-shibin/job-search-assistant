"""DOCX -> PDF conversion via headless LibreOffice (Milestone 7). Implements
the `PdfConverter` protocol in `app/documents/pdf.py`. LibreOffice isn't a
Python dependency -- it's an external binary the user installs separately
(see README "Requirements") -- so a missing/failed conversion raises a
specific, catchable `PdfConversionError` rather than crashing the whole
`generate-resume` command: a tailored DOCX is still useful on its own.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_CONVERT_TIMEOUT_SECONDS = 120


def _find_soffice() -> str | None:
    """The Windows LibreOffice installer doesn't add itself to PATH by
    default, so a plain `shutil.which` lookup misses a perfectly normal
    install. Fall back to the two locations the official installer actually
    uses before giving up.
    """
    found = shutil.which("soffice") or shutil.which("soffice.exe")
    if found:
        return found

    for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
        program_files = os.environ.get(env_var)
        if not program_files:
            continue
        candidate = Path(program_files) / "LibreOffice" / "program" / "soffice.exe"
        if candidate.exists():
            return str(candidate)

    return None


class PdfConversionError(Exception):
    """Raised when LibreOffice isn't available or a conversion attempt fails."""


class LibreOfficePdfConverter:
    def __init__(self, soffice_path: str | None = None) -> None:
        self._soffice_path = soffice_path or _find_soffice()

    def convert(self, docx_path: Path) -> Path:
        if not self._soffice_path:
            raise PdfConversionError(
                "LibreOffice ('soffice') was not found on PATH. Install it "
                "(https://www.libreoffice.org/download/download/) to enable PDF generation, "
                "or continue with the DOCX only."
            )
        if not docx_path.exists():
            raise PdfConversionError(f"DOCX file not found: {docx_path}")

        out_dir = docx_path.parent
        try:
            result = subprocess.run(
                [self._soffice_path, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
                capture_output=True,
                text=True,
                timeout=_CONVERT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PdfConversionError(f"LibreOffice conversion failed to run: {exc}") from exc

        pdf_path = out_dir / (docx_path.stem + ".pdf")
        if result.returncode != 0 or not pdf_path.exists():
            raise PdfConversionError(
                f"LibreOffice conversion failed (exit code {result.returncode}): {result.stderr.strip()}"
            )
        return pdf_path


def convert_to_pdf(docx_path: Path) -> Path:
    """Convenience entry point used by the CLI -- builds a fresh
    `LibreOfficePdfConverter` (cheap: just a `shutil.which` lookup) each call.
    """
    return LibreOfficePdfConverter().convert(docx_path)

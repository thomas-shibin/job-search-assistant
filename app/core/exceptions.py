"""The application's exception families.

- `AppError`: an expected failure with a message safe to show the user (bad
  input, a missing resume, a limit reached, a refused permission change).
  Callers catch the specific subclass they can handle; anything uncaught is a
  bug and surfaces as a 500 with a logged stack trace.
- `IntegrationError`: an external service (job board, email, Telegram, AI
  provider) failed. Its message may be technical and is logged rather than
  shown verbatim.

Each module defines its own subclasses next to the code that raises them (for
example `AuthError` in app/modules/auth/service.py); this module only holds
the shared bases, so `except AppError` can catch any of them.
"""
from __future__ import annotations


class AppError(Exception):
    """An expected, user-facing failure."""


class IntegrationError(AppError):
    """An external service failed or refused the request."""

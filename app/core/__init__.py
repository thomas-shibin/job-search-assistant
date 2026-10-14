"""Infrastructure shared by every module: configuration (config/), the
database engine and sessions (database/), per-user data isolation
(tenancy.py), well-known paths (paths.py), logging and the exception bases.
Core never imports from app.modules at import time."""

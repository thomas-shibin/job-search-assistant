"""Hirlumo: a job search and resume-tailoring platform, built as a
modular monolith -- one FastAPI app and one database, organised by business
area. See docs/ARCHITECTURE.md.

- core/          infrastructure every module uses: settings, database, tenancy, paths, logging, exceptions
- integrations/  clients for external services: job boards, AI provider, email, Telegram, LibreOffice
- modules/       one package per business area (routes, services, models)
- web/           the app's page shell: templates, static files, public pages, shared view helpers
- workers/       background work: the scheduler and the scans it runs
- shared/        small pure helpers (hashing, filenames, text)
"""

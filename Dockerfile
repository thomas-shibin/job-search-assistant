FROM python:3.12-slim

# LibreOffice (headless writer only) for DOCX -> PDF resume conversion, plus fonts
# so PDFs match Word: Liberation (= Times New Roman/Arial metrics), Carlito
# (= Calibri), and WenQuanYi for CJK/full-width characters like "－" -- without
# it those render as empty boxes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-dejavu fonts-liberation \
       fonts-crosextra-carlito fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app

COPY pyproject.toml ./
COPY cli.py main.py alembic.ini ./
COPY app ./app
COPY alembic ./alembic
RUN pip install --no-cache-dir .

# data/ and config/ are bind-mounted from the host (see docker-compose.yml) so the
# container shares the native install's database, resumes and config.yaml.
RUN useradd --uid 1000 --create-home app && mkdir -p data config && chown app:app data config
USER app

EXPOSE 8000
# Migrations are idempotent, so applying them on every start keeps the DB current.
CMD ["sh", "-c", "alembic upgrade head && python cli.py serve --host 0.0.0.0 --port 8000"]

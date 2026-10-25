FROM python:3.12-slim

# LibreOffice (headless writer only) for DOCX -> PDF resume conversion, plus fonts
# so PDFs match Word: Liberation (= Times New Roman/Arial metrics), Carlito
# (= Calibri), and WenQuanYi for CJK/full-width characters like "－" -- without
# it those render as empty boxes. `upgrade` takes Debian's security fixes released
# since the base image was built (CI's image scan fails on fixable HIGH/CRITICAL ones).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-dejavu fonts-liberation \
       fonts-crosextra-carlito fonts-wqy-microhei tzdata \
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

# Least privilege: the app runs as an unprivileged system user with no shell and
# no home folder. The code stays owned by root, so the app can read it but not
# change it; it can only write to data/ and config/ (bind-mounted from the host,
# see docker-compose.yml) and /tmp. Programs that switch user (setuid/setgid,
# e.g. su, passwd) lose that ability, so nothing in the image can become root.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin app \
    && mkdir -p data config && chown app:app data config && chmod 750 data config \
    && { find / -xdev -type f -perm /6000 -exec chmod a-s {} + 2>/dev/null || true; }
USER 1000:1000

EXPOSE 8000
# Refuses to run as root (e.g. `user: "0:0"` or APP_UID=0 from an installer run
# with sudo). Migrations are idempotent, so applying them on every start keeps
# the DB current.
CMD ["sh", "-c", "if [ \"$(id -u)\" = 0 ]; then echo 'Refusing to run as root: set APP_UID/APP_GID in .env to your own user (id -u / id -g).' >&2; exit 1; fi; alembic upgrade head && exec python cli.py serve --host 0.0.0.0 --port 8000"]

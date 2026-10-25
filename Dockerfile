# Two images from one file:
#   release (the default)  what production runs: the code as compiled bytecode
#                          only -- no .py source, no docstrings or comments --
#                          pushed to the private registry by scripts/release.sh
#   dev                    the source as-is, for this computer
#                          (docker-compose.dev.yml: target dev)
#
#   docker build -t hirlumo:0.9 .             # release
#   docker build --target dev -t hirlumo:dev .

FROM python:3.12-slim AS base

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

# The dependencies alone first (pyproject.toml's list), so a code change doesn't
# reinstall them.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
       > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt pyproject.toml

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
EXPOSE 8000


# ---- compiling: bytecode only ----------------------------------------------------
# -b puts each module's .pyc where its .py was, so it imports with the .py gone;
# -o 2 leaves out docstrings and asserts. Templates, styles, scripts and help
# articles stay as files: the app serves them. Bytecode isn't encryption -- it
# hides the source from casual reading, not from a determined reverse engineer.
FROM base AS compile
COPY cli.py main.py alembic.ini ./
COPY app ./app
COPY alembic ./alembic
RUN python -m compileall -q -b -o 2 -j 0 app alembic cli.py main.py \
    && find app alembic -name '*.py' -delete \
    && rm cli.py main.py \
    && find app alembic -name __pycache__ -prune -exec rm -rf {} + \
    && sed -i 's/^script_location = alembic$/&\nsourceless = true/' alembic.ini \
    && test -z "$(find . -name '*.py' -not -path './data/*' -print -quit)"


# ---- dev: the source ---------------------------------------------------------------
FROM base AS dev
COPY cli.py main.py alembic.ini ./
COPY app ./app
COPY alembic ./alembic
USER 1000:1000
# Refuses to run as root (e.g. `user: "0:0"` or APP_UID=0 from an installer run
# with sudo). Migrations are idempotent, so applying them on every start keeps
# the DB current.
CMD ["sh", "-c", "if [ \"$(id -u)\" = 0 ]; then echo 'Refusing to run as root: set APP_UID/APP_GID in .env to your own user (id -u / id -g).' >&2; exit 1; fi; alembic upgrade head && exec python cli.py serve --host 0.0.0.0 --port 8000"]


# ---- release (default): the compiled code -----------------------------------------
FROM base AS release
COPY --from=compile /app/app ./app
COPY --from=compile /app/alembic ./alembic
COPY --from=compile /app/cli.pyc /app/main.pyc /app/alembic.ini ./
ARG VERSION=dev
LABEL org.opencontainers.image.title="hirlumo" org.opencontainers.image.version="${VERSION}"
ENV HIRLUMO_VERSION=${VERSION}
USER 1000:1000
CMD ["sh", "-c", "if [ \"$(id -u)\" = 0 ]; then echo 'Refusing to run as root: set APP_UID/APP_GID in .env to your own user (id -u / id -g).' >&2; exit 1; fi; alembic upgrade head && exec python cli.pyc serve --host 0.0.0.0 --port 8000"]

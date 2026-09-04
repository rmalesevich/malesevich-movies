FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits do not bust the layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# COPY preserves the *source* file modes, so a checkout on a host with a
# restrictive umask or dataset ACL (TrueNAS can produce 600) bakes unreadable
# files into the image. `chmod +x` on a 600 file yields 700, and the container
# then dies with "cannot open /usr/local/bin/entrypoint.sh: Permission denied"
# for every uid except the owner. Set the modes explicitly so the image never
# depends on how the build host happened to check the repo out.
RUN chmod 0755 /usr/local/bin/entrypoint.sh && \
    chmod -R a+rX /app

# The sqlite file lives here; the compose files mount a volume over it.
# /app is left world-readable rather than owned by one uid: production runs as
# the TrueNAS "apps" user (568) via compose's `user:`, not as this default.
RUN mkdir -p /app/data && \
    adduser --disabled-password --gecos "" --uid 1000 appuser && \
    chown -R appuser:appuser /app/data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["entrypoint.sh"]
# A single worker on purpose: the daily Trakt sync runs in-process via
# APScheduler, and more than one worker would run it more than once.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

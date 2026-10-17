# The Resonant relay (relay/README.md). The build context is the REPO ROOT, because the
# image carries the committed dashboard build from dashboard/dist:
#
#   docker build -f relay/Dockerfile .
#   fly deploy --config relay/fly.toml --dockerfile relay/Dockerfile .   # from the repo root
#
# Standard library only: no pip install, nothing fetched at build time beyond the base image.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DIST_DIR=/app/dist \
    PORT=8080

WORKDIR /app
COPY relay/resonant_relay /app/resonant_relay
COPY dashboard/dist /app/dist

# Read-only files owned by root; the server runs as an unprivileged user.
USER 65532:65532
EXPOSE 8080
CMD ["python", "-m", "resonant_relay"]

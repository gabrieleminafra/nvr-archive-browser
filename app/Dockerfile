# Read-only viewer for a Scrypted NVR recording store.
#
# The build context is this directory only (see compose.yaml), so the
# recordings sitting beside it are never part of the image build.
FROM python:3.12-alpine

# ffmpeg cuts timeline thumbnails out of already-muxed clips; tzdata so
# local-time day grouping matches the cameras' burned-in clock.
RUN apk add --no-cache ffmpeg tzdata

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NVR_ROOT=/data \
    NVR_CACHE=/cache \
    NVR_HOST=0.0.0.0 \
    NVR_PORT=8787

RUN adduser -D -u 10001 nvr && mkdir -p /cache && chown nvr:nvr /cache

WORKDIR /app
COPY . /app/

USER nvr
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python3 -c "import os,urllib.request; \
urllib.request.urlopen('http://127.0.0.1:%s/api/cameras' % os.environ['NVR_PORT'], timeout=5)"

ENTRYPOINT ["python3", "/app/nvr_server.py"]

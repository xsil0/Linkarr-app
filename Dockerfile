# Linkarr: MEGA links into your media library, named and filed.
# Debian 12 base because MEGA publishes MEGAcmd packages per Debian release.
FROM python:3.12-slim-bookworm

ARG TARGETARCH=amd64
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LA_DATA=/data \
    LA_MEDIA=/media \
    LA_PORT=9040 \
    TZ=UTC \
    PUID=99 \
    PGID=100

WORKDIR /app
# MEGAcmd: MEGA's official command-line client (downloads with your account's quota).
# unrar (RARLAB's own, from Debian non-free): rar files, including locked ones and ones over 2 GB.
# 7zip: zip and 7z, including AES-locked ones. unar: last-resort fallback.
# ffmpeg: ffprobe checks a video plays before it's moved.
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unrar 7zip unar ffmpeg \
 && curl -fsSL -o /tmp/megacmd.deb "https://mega.nz/linux/repo/Debian_12/${TARGETARCH}/megacmd-Debian_12_${TARGETARCH}.deb" \
 && apt-get install -y --no-install-recommends /tmp/megacmd.deb \
 && rm -f /tmp/megacmd.deb \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && command -v mega-get && command -v unrar && (command -v 7z || command -v 7zz)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY linkarr/ /app/linkarr/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Database, keys, MEGA sign-in, logs and backups. Map it to appdata so updates keep it.
VOLUME /data
# Your media share (the folder that holds Movies, TV Shows and so on). Read/write.
VOLUME /media
EXPOSE 9040

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('LA_PORT','9040'), timeout=4)" || exit 1

STOPSIGNAL SIGTERM
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "linkarr"]

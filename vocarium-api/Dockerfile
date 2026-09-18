FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# libchromaprint-tools liefert fpcalc — die Hörspiele erkennen damit
# Intro, Werbe-Bumper und Abspann per akustischem Fingerabdruck.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libchromaprint-tools && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir yt-dlp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Kein root zur Laufzeit. uid 1000 entspricht dem Host-Benutzer, dem die
# gebundenen Quellverzeichnisse gehoeren; die Volumes /app/data und
# /app/voices muessen einmalig auf 1000:1000 gesetzt werden.
RUN groupadd -g 1000 vocarium && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin vocarium \
    && mkdir -p /app/data /app/voices && chown -R 1000:1000 /app
USER vocarium

EXPOSE 8280
CMD ["python", "main.py"]

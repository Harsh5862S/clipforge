FROM python:3.11-slim

# Install curl + gnupg to add the NodeSource repo (gets us Node 20 LTS
# instead of the older Debian default, which yt-dlp's EJS may reject).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Verify yt-dlp and node are available and on PATH
RUN yt-dlp --version && node --version && ffmpeg -version | head -1

COPY . .

# Render provides $PORT at runtime; server.py reads it via os.environ.
EXPOSE 3000

CMD ["python", "server.py"]

FROM python:3.11-slim

# Install system dependencies for PyNaCl, audio processing, and FFmpeg for stability
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsodium-dev \
    libffi-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Render uses port 10000 by default for free tier
ENV PORT=10000
EXPOSE 10000

CMD ["python", "bot.py"]

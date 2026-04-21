# Official Playwright image — Ubuntu 22.04 (Jammy), Chromium pre-installed.
# Avoids the broken --with-deps font packages on Debian Trixie.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

# Install Python dependencies (Playwright itself is already in the base image)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Railway injects $PORT at runtime
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]

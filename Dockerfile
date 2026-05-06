FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 libx11-xcb1 libxcb1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py .

RUN pip install --no-cache-dir flask gunicorn "scrapling[all]"
RUN python -c "import camoufox; camoufox.install()" || echo "camoufox browser install skipped"

EXPOSE 10000
CMD ["gunicorn", "server:app", "--bind", "0.0.0.0:10000", "--timeout", "120", "--workers", "1"]

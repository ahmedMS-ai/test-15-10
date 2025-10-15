FROM python:3.11-slim

# نظاميات Chromium/Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libx11-6 libx11-xcb1 libxcomposite1 libxdamage1 libxfixes3 \
    libxcb1 libxrandr2 libgbm1 libgtk-3-0 libasound2 fonts-liberation \
    libdrm2 libpci3 libpangocairo-1.0-0 libxshmfence1 ca-certificates \
    wget unzip git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# تثبيت Chromium + deps عبر Playwright
RUN python -m playwright install --with-deps chromium

COPY . .
EXPOSE 7860
ENV PORT=7860
ENV STREAMLIT_SERVER_PORT=7860
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0

CMD ["streamlit", "run", "app.py"]

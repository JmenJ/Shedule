FROM python:3.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-browser.txt ./
RUN pip install --no-cache-dir -r requirements-browser.txt \
    && python -m playwright install --with-deps chromium
COPY . .
EXPOSE 10000
CMD ["python", "main.py"]

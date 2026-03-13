FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN pip install --no-cache-dir fastapi uvicorn httpx toml beautifulsoup4

# Copy project files
COPY . .

# Default port; can be overridden by platform env PORT
ENV PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]

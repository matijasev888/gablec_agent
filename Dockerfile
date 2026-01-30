FROM python:3.13-slim

WORKDIR /app

# Install dependencies first (better caching)
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && \
    uv pip install --system --no-cache .

# Copy application code
COPY gablec_script/ ./gablec_script/

# Run the bot
CMD ["python", "-m", "gablec_script.main"]

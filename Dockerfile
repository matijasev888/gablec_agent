FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv pip install --system --no-cache .
COPY . .
CMD ["python", "-m", "gablec_script.main"]

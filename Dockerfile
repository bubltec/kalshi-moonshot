FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV DATA_DIR=/data
VOLUME ["/data"]
CMD ["python", "-m", "kalshi_moonshot", "run"]

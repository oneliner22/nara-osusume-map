FROM python:3.12-slim
ENV PYTHONUTF8=1 PYTHONIOENCODING=utf-8
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mcp_client.py daily_job.py pending_resolver.py refresh_hours.py models.json ./
CMD ["python", "daily_job.py"]

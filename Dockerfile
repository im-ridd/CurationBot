FROM python:3.11-slim

# System deps for beem (gcc needed for some C extensions)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001

# Data volume for SQLite
VOLUME /app/data

CMD ["python", "-m", "backend.main", "--autostart"]

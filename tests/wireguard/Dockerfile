# Disposable CI fixture only. Never used by install.sh or production Compose.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends wireguard-tools iproute2 iptables && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent ./agent
COPY fixture.py ./fixture.py
CMD ["python", "fixture.py", "sink"]

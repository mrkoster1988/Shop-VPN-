FROM python:3.12-slim
ENV TZ=Europe/Moscow
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home shop
COPY app ./app
COPY scripts/backup.py ./scripts/backup.py
COPY scripts/status.py ./scripts/status.py
COPY scripts/panel_check.py ./scripts/panel_check.py
COPY scripts/mail_check.py ./scripts/mail_check.py
# Git checkouts under umask 077 can produce 0600 files and 0700 directories.
# Normalize only copied application code; keep secrets and the persistent volume private.
RUN chmod -R u=rwX,go=rX /app/app /app/scripts \
    && mkdir /app/data && chown shop:shop /app/data
USER shop
EXPOSE 8000
CMD ["python","-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000","--workers","1","--no-access-log"]

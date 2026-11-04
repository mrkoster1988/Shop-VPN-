FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home shop
COPY app ./app
RUN mkdir /app/data && chown shop:shop /app/data
USER shop
EXPOSE 8000
CMD ["python","-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000","--workers","1","--no-access-log"]

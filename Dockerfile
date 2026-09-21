FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 9376
# One worker keeps the background card-data loader single; threads handle concurrency.
CMD ["gunicorn", "--bind", "0.0.0.0:9376", "--workers", "1", "--threads", "8", "--timeout", "300", "app:app"]

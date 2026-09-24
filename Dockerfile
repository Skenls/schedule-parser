FROM python:3.11-slim

WORKDIR /app

# Отключаем буферизацию логов, чтобы docker logs показывал всё сразу
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]

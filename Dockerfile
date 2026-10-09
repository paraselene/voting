FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN mkdir -p /app/fonts /data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN BUILD_STATIC=1 python manage.py collectstatic --noinput
EXPOSE 8000
CMD ["sh", "-c", "umask 077; python manage.py migrate --noinput && exec gunicorn --workers 1 --threads 4 --timeout 60 --bind 0.0.0.0:8000 config.wsgi:application"]

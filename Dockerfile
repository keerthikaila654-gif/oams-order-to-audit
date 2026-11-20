FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV OAMS_DB=/data/oams.db PORT=5000
VOLUME /data
EXPOSE 5000
# creates + seeds the database on first start, then serves with gunicorn (single worker: SQLite + in-memory login throttle)
CMD ["sh", "-c", "[ -f $OAMS_DB ] || python manage.py reset; gunicorn -w 1 --threads 4 -b 0.0.0.0:${PORT} 'app:create_app()'"]

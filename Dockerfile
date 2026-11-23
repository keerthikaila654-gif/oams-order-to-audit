FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV OAMS_DB=/data/oams.db PORT=8080
VOLUME /data
EXPOSE 8080
# Creates + seeds benchmark and sample data on first start, then serves via Gunicorn
CMD ["sh", "-c", "[ -f $OAMS_DB ] || python manage.py demo; gunicorn -w 1 --threads 4 -b 0.0.0.0:${PORT:-8080} 'app:create_app()'"]

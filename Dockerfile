FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV LEDGERLINE_DB=/data/ledgerline.db
VOLUME /data
EXPOSE 8000
# ONE process, several threads. SQLite serialises writers; a single process keeps lock waits short.
CMD ["sh", "-c", "mkdir -p /data && python -m scripts.seed && gunicorn -w 1 --threads 8 -b 0.0.0.0:${PORT:-8000} wsgi:app"]

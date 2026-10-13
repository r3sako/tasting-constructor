FROM python:3.13-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY scripts scripts
COPY data/catalog.sqlite data/catalog.sqlite
# демо-кабинет создаётся при каждом старте, поэтому прототип работает и без постоянного диска
ENV DEMO=1
EXPOSE 8000
CMD ["sh", "-c", "python scripts/seed_demo.py && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]

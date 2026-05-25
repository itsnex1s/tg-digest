# Slim image, ~50 MB on disk. All deps are pure-Python.
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py bootstrap_login.py ./

VOLUME ["/app/data"]
ENTRYPOINT ["python", "-u", "/app/main.py"]

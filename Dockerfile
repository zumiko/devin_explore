FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
WORKDIR /srv/app
RUN rm -f internal_tools.db && python -c "import face_match as f; f._model_path(*f.DETECTOR); f._model_path(*f.RECOGNIZER)"
ENV PORT=10000
# Demo data is rebuilt on every start; one worker because SQLite is a single file.
CMD ["sh", "-c", "python seed.py && exec gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT app:app"]

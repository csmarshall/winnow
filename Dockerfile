# Winnow — Frigate training-data review companion.
# Talks to Frigate + Ollama over HTTP only; no Frigate code/image changes, no
# GPU, no model in the image (Ollama is external). Works with a stock Frigate.
FROM python:3.12-slim

# Only deps beyond stdlib: Pillow (image read + webp) and numpy (saturation calc).
RUN pip install --no-cache-dir pillow numpy

WORKDIR /app
COPY winnow/ ./winnow/
COPY adapters/ ./adapters/

# review/ holds candidates.jsonl + verdicts.jsonl + committed.jsonl — mount a
# volume here so decisions persist across restarts.
VOLUME ["/app/review"]
EXPOSE 8077

WORKDIR /app/adapters/frigate
CMD ["python", "daemon.py"]

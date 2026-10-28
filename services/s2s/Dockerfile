# Speech-to-speech orchestrator: LiveKit agent worker + demo web app (one image,
# two compose services). No GPU: it routes audio and text between the model
# services and runs voice activity detection (Silero, CPU) on each microphone.
FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
# silero-vad only for its ONNX model file (its torch dependency isn't needed)
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && pip install --no-cache-dir --no-deps silero-vad==6.2.3

WORKDIR /srv
COPY app /srv/app
COPY migrations /srv/migrations
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

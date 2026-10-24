# Audio side of indic-speak: SNAC codes -> Vocos waveform, streamed, on the GPU. The
# speech LM itself runs in the tts-lm NIM, so this image needs no vLLM: a slim Python
# base plus CUDA torch.
FROM python:3.12-slim

RUN pip install --no-cache-dir torch==2.13.0 --index-url https://download.pytorch.org/whl/cu129
COPY requirements.txt /tmp/requirements.txt
# snac's deps (torch, numpy, einops, huggingface-hub) come from requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && pip install --no-cache-dir --no-deps snac==1.2.1

WORKDIR /srv
COPY app /srv/app

ENV MODEL_DIR=/models/indic-speak \
    SNAC_DIR=/models/snac_24khz \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 8003
ENTRYPOINT ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8003", "--ws-ping-interval", "20"]

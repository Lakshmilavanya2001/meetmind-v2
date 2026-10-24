# Build-time tools for services/asr-nim/build: the Whisper NIM image (Riva servicemaker,
# NeMo 2.7, TensorRT)
# plus the packages NeMo's import and NVIDIA's nemo2riva need (from public PyPI; the
# image's pip is configured for an NVIDIA-internal index).
FROM nvcr.io/nim/nvidia/whisper-large-v3:1.5.1
USER root
RUN pip install --no-cache-dir --index-url https://pypi.org/simple \
      nv-one-logger-core nv-one-logger-training-telemetry nv-one-logger-pytorch-lightning-integration nemo2riva kaldialign
USER 1000

#!/bin/bash
# Build the ASR NIM's model repository from AI4Bharat IndicConformer checkpoints, the way
# NVIDIA documents custom models for Riva: .nemo -> nemo2riva -> riva-build -> riva-deploy
# -> custom_model.tar.gz, which the asr service loads (NIM_EXPORT_PATH).
#
#   services/asr-nim/build/build.sh ml hi kn te ta
#
# Each checkpoint (ai4bharat/indicconformer_stt_<lang>_hybrid_ctc_rnnt_large) is gated on
# Hugging Face: accept its terms with the account whose HF_TOKEN is in .env first. Needs
# the GPU (TensorRT engines are built for it) and ~2 GB of disk per language.
set -euo pipefail
cd "$(dirname "$0")/../../.."
[ $# -gt 0 ] || { echo "usage: $0 <lang> [<lang> ...]   (e.g. ml hi kn te ta)"; exit 1; }
set -a; . ./.env; set +a
work=build/asr-nim; mkdir -p "$work" nim-export/asr-indic; chmod 777 "$work" nim-export/asr-indic
docker build -q -t riva-build-tools:local services/asr-nim/build >/dev/null
cp services/asr-nim/build/make_ctc.py services/asr-nim/build/build_lang.sh "$work/"
for l in "$@"; do
  src=models/indicconformer-$l/indicconformer_stt_${l}_hybrid_rnnt_large.nemo
  [ -f "$src" ] || docker run --rm -e HF_TOKEN -v "$PWD/models:/models" --entrypoint bash riva-build-tools:local -c \
    "pip install -q --index-url https://pypi.org/simple 'huggingface_hub[cli]' >/dev/null && \
     hf download ai4bharat/indicconformer_stt_${l}_hybrid_ctc_rnnt_large indicconformer_stt_${l}_hybrid_rnnt_large.nemo --local-dir /models/indicconformer-$l"
done
langs="$*"
docker run --rm --gpus all --entrypoint bash -v "$PWD/models:/m:ro" -v "$PWD/$work:/w" -v "$PWD/nim-export/asr-indic:/model_tar" \
  riva-build-tools:local -c "
set -e
for l in $langs; do
  python3 /w/make_ctc.py /m/indicconformer-\$l/indicconformer_stt_\${l}_hybrid_rnnt_large.nemo \$l /w/ic_\${l}_ctc.nemo
  # trace the export with 32 s of features: nemo2riva's default trace length is longer than
  # the encoder's position table (the export fails, unless it runs out of memory first and
  # retries shorter); utterances are at most 16 s anyway
  nemo2riva --max-dim 3200 --out /w/ic_\${l}_ctc.riva /w/ic_\${l}_ctc.nemo
  bash /w/build_lang.sh \$l \$l-IN
done
cd /data/models && tar -czf /model_tar/custom_model.tar.gz *"
echo "built: nim-export/asr-indic/custom_model.tar.gz ($langs). Restart the asr service to load it;"
echo "add the languages to ASR_LANGUAGES in services/s2s/app/languages.py and ASR_WARM_LANGUAGES."

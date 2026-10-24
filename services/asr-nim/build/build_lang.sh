#!/bin/bash
# riva-build + riva-deploy for one language's CTC model (runs inside the tools image):
#   build_lang.sh <lang> <riva-language-code>      e.g. build_lang.sh ml ml-IN
# Why these flags (each one measured):
# - offline, not streaming, one 16 s chunk without padding: the whole utterance goes
#   through the model in one pass, and the engine takes any length up to 16 s. (Riva's
#   streaming-style chunking dropped the first word in Tamil, Telugu and Kannada.)
# - NeMo's own mel features (torch featurizer, nemo type) rather than Riva's default ones.
# - fp32 TensorRT: this model's fp16 engine outputs only blanks.
# - batch 4 keeps each engine's working memory small.
set -e
l=$1; code=$2
riva-build speech_recognition -f /w/ic_${l}_ctc.rmir /w/ic_${l}_ctc.riva \
  --offline --streaming=False --name=indicconformer-${code}-asr-offline --language_code=${code} \
  --feature_extractor_type=torch --torch_feature_type=nemo \
  --nn.use_trt_fp32 \
  --return_separate_utterances=True \
  --ms_per_timestep=40 \
  --chunk_size=16 --left_padding_size=0 --right_padding_size=0 \
  --max_batch_size=4 --nn.max_batch_size=4 --nn.opt_batch_size=1 --featurizer.max_batch_size=64 --featurizer.max_execution_batch_size=64 \
  --decoder_type=greedy --greedy_decoder.asr_model_delay=-1
riva-deploy -f /w/ic_${l}_ctc.rmir /data/models
# riva-build doesn't package the mel filterbank the NeMo featurizer reads; make it the same way
python3 -c "from servicemaker.util.mel_basis_generation import get_nemo_mel_basis; \
get_nemo_mel_basis('/data/models/indicconformer-${code}-asr-offline/1/16khz_mel_basis.npy', 16000, 512, 80)"

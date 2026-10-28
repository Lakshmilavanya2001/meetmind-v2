// Microphone frames for the assistant's voice input: each 128-sample block goes to the page as-is.
class Recorder extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0]?.[0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("recorder", Recorder);

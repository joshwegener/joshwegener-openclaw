import sys, time
import objc
from Foundation import NSURL, NSRunLoop, NSDate
import Speech

# macOS Speech framework via PyObjC

def transcribe(path, locale="en-US"):
    url = NSURL.fileURLWithPath_(path)
    recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(objc.lookUpClass('NSLocale').localeWithLocaleIdentifier_(locale))
    if recognizer is None:
        raise RuntimeError("SFSpeechRecognizer is not available")

    request = Speech.SFSpeechURLRecognitionRequest.alloc().initWithURL_(url)
    request.setShouldReportPartialResults_(True)

    done = {"flag": False}
    out = {"text": "", "error": None}

    def handler(result, error):
        if error is not None:
            out["error"] = str(error)
            done["flag"] = True
            return
        if result is not None:
            # bestTranscription.formattedString()
            bt = result.bestTranscription()
            if bt is not None:
                out["text"] = str(bt.formattedString())
            if result.isFinal():
                done["flag"] = True

    task = recognizer.recognitionTaskWithRequest_resultHandler_(request, handler)

    # run loop until done or timeout
    timeout_s = 30
    start = time.time()
    rl = NSRunLoop.currentRunLoop()
    while not done["flag"] and (time.time() - start) < timeout_s:
        rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))

    if not done["flag"]:
        try:
            task.cancel()
        except Exception:
            pass
        raise RuntimeError("Timed out waiting for transcription")

    if out["error"]:
        raise RuntimeError(out["error"])
    return out["text"]

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 transcribe_speech.py /path/to/audio.wav", file=sys.stderr)
        sys.exit(2)
    print(transcribe(sys.argv[1]))

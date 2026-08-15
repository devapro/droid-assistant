# Browser test fixture

`conversation.wav` is the audio Chrome plays into `getUserMedia` in place of a
real microphone (`--use-file-for-fake-audio-capture`). Three synthetic voices
saying three sentences, separated by pauses long enough for VAD to close each
turn, at 48 kHz mono 16-bit — the format Chrome's fake capture device wants.

It is committed rather than generated, because the browser suite cannot run
without it and the generator below is macOS-only. It contains no personal data.

To regenerate on macOS:

```bash
say -v Alex    -o a.aiff "We need to finish the migration by Friday."
say -v Daniel  -o b.aiff "Yes, agreed. I will send the numbers tomorrow morning."
say -v Samantha -o c.aiff "I think we should postpone the release until next month."
for f in a b c; do
  afconvert -f WAVE -d LEI16@48000 -c 1 "$f.aiff" "$f.wav"
done
# then concatenate with ~1.2 s of silence between each, into conversation.wav
```

If you change it, update the assertions in `../record.spec.ts` that check for
the word "migration".

# Voice lifecycle and barge-in

## Ownership

`client/app/cube.py` continuously consumes a single 16 kHz mono `arecord` stream in 80 ms frames. openWakeWord receives every frame, including while processing or speaking. `interaction.py:Coordinator` owns generations, recording, follow-up and presentation. A wake replaces the current interaction; the wake latch prevents repeated triggers from one detection. Pre-roll retains command onset.

`voice_runtime.py` owns background asynchronous HTTP and audio work. `audio/async_speech.py` tries remote Kokoro then local Piper, retaining the established voice settings and explicit `cube.assistant` route. Cancellation closes owned HTTP exchanges and terminates/reaps only owned subprocesses. Generation checks suppress stale LEDs, follow-up and history completion. Notifications are revalidated immediately before playback and acknowledged after owned playback ends.

Reboot/shutdown receipts require matching successful response playback to complete; a failed, interrupted or superseded receipt cannot be upgraded by a later fallback. Hardware release runs away from capture. Coordinator remains the lifecycle authority; music, display providers and HTTP workers do not take that ownership.

The backend and AI node supervise consumed-body disconnects with service-local helpers. Interaction IDs travel as headers, not LLM prompt content. Native workers retain their resource/lock until safe cleanup. Cancellation skips queued inference, stops Whisper segment consumption, prevents fallback and suppresses stale results. A started external effect may already have succeeded: its bookkeeping must finish and it must not be replayed automatically.

## Audio and privacy

Speech/cues use `cube.assistant`. Spotify uses `cube.spotify` with independent DSP duck gain. A wake does not mute the physical sink or stop unrelated media. [Spotify](SPOTIFY.md) documents leases and gain recovery.

Only one process owns microphone capture. Backend conversation history is bounded and in memory, but configured inference providers receive speech/text as part of normal processing. Local fallback transcripts are logged only with explicit `CUBE_LOG_TRANSCRIPTS=1`. Diagnostic recordings, model training data and runtime logs are private artifacts.

## Automated validation

Use the independent environments and commands in [tests](../tests/README.md). Focused lifecycle coverage:

```sh
PYTHONPATH=client/app client/app/.venv/bin/python -m unittest   tests.client.test_barge_in tests.client.test_speech tests.client.test_conversation   tests.client.test_controls tests.client.test_notifications -v
PYTHONPATH=server CUBE_AI_NODE_URL= server/.venv/bin/python -m unittest   tests.server.core.test_barge_in -v
```

The cross-service suite includes fake-provider TCP cancellation tests with ephemeral loopback listeners. It does not call installed services. Tests require ordinary local socket/thread support; a restrictive sandbox can prevent asyncio wakeups and cause hangs unrelated to production behavior.

## Physical acceptance

1. Verify current microphone, speaker, matrix and service identities before changing deployment.
2. Exercise normal commands with AI enabled and disabled; confirm local STT/TTS fallbacks are actually provisioned.
3. Wake again during capture, backend processing, remote TTS, Piper and playback. Confirm only the new interaction proceeds and child processes are reaped.
4. Interrupt notifications and confirm validation/acknowledgment semantics; confirm no stale follow-up opens after cancellation.
5. Test the selected ReSpeaker and Bluetooth routes separately. Owned process exit does not prove the physical speaker buffer is empty.
6. At representative volumes/distances, run speaker-only speech including “Hey Cube” for at least 30 minutes, then repeated human/speaker double-talk. Record false accepts and misses; do not claim acoustic echo cancellation from a firmware flag alone.
7. Supervise power-command receipts with failures/interruption before testing a real reboot or shutdown.

## ReSpeaker AEC

Read device/routing state first using `arecord -l`, `aplay -l`, `wpctl status` and the firmware-matching ReSpeaker host tool. Verify the actual processed USB capture channel, reference routing and delay. Bluetooth output alone does not prove that the ReSpeaker receives an echo reference. See the [vendor host-control guide](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/host_control/README.md).

Do not change firmware, write DSP settings or open a competing capture stream during normal Cube operation. Any diagnostic WAV capture requires a separate maintenance test with voice capture deliberately stopped; keep recordings private and restart only the service intentionally stopped.

## Upstream compute cancellation

Record the installed LLM runtime/model and proxy path. Interrupt a request during prompt processing and generation, correlate interaction IDs in backend/node cancellation logs, and verify the upstream request actually leaves its active queue. Compare generated-token counts against an uninterrupted control. HTTP socket closure is tested; it does not establish provider GPU cancellation or cost savings. Record that limitation if the provider continues computing after disconnect.

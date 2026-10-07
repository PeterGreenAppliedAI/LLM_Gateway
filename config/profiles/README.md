# Engine profiles

A profile describes an engine family's quirks as data, so the gateway can list
voices, languages and setting ranges for the dashboard and validate requests,
without engine-specific code (D-020). An endpoint opts in with `profile: <name>`
(the file name without `.yaml`). Endpoints without a profile still work; they
just get generic controls and no validation.

Fields (all optional):

```yaml
description: text shown in the dashboard
tts:
  voices_path: /v1/audio/voices   # where the engine lists voices
  voice_id:                        # derive attributes from voice IDs
    pattern: '^(?P<language>[a-z])(?P<gender>[fm])_'   # named groups
    language: {a: American English, ...}               # group value -> label
    gender: {f: female, m: male}
  blending: true                   # voice mixes like "af_bella(2)+af_sky(1)"
  params:                          # validated when present in a request
    speed: {type: number, min: 0.25, max: 4.0, default: 1.0}
    response_format: {type: enum, values: [mp3, wav], default: mp3}
stt:
  languages: [en, fr, ...]         # for dashboard dropdowns (not enforced)
  params:
    temperature: {type: number, min: 0, max: 1, default: 0}
```

Ranges are enforced only for parameters a request actually sends. When an
engine's limits differ by model, profiles use the widest range so the gateway
never rejects something the engine would accept.

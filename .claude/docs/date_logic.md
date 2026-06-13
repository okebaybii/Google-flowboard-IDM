# Generation & Pipeline Logic

## Generation Request Lifecycle
A generation `Request` (see `db/models.py`) moves through:
`queued` → `running` → `succeeded` | `failed`.

- On agent restart, any orphaned `running` requests are force-failed with error
  `agent_restart_lost` (`main.py:_recover_orphan_running_requests`) so nodes don't poll a
  request nobody is processing.
- The worker (`worker/processor.py`) supports infinite/most retries for sequential
  generation and graceful degrade across paygate tiers (PAYGATE_TIER_ONE / TIER_TWO).

## Storyboard Sequencing
- Storyboard nodes generate 1–8 consecutive scene images, controlled by a BFS over the
  graph; failed scenes are automatically regenerated.
- Composed-image nodes combine upstream reference nodes (character + visual asset) so
  identity stays consistent across frames.

## Audio / Video Sync (Dynamic Speed Stretching)
When muxing voice-over onto a clip (`routes/ffmpeg_assembly.py`, `routes/video_assembly.py`):
1. Measure the AI voice-over (Edge TTS) duration.
2. Stretch the video speed (slow motion) or hold the final frame (Hybrid Freeze Frame) so
   audio and video end together.
3. ffmpeg concatenates segments and muxes the audio track; if there is no audio, a silent
   stereo track (`anullsrc`, 24kHz) is generated to keep streams aligned.

## Social Scheduling
- `worker/social_scheduler.py` runs every 60s, finds due `SocialBlockPost` rows, and
  publishes them to the configured Facebook Page (`FB_PAGE__ID` / `FB_PAGE__ACCESS_TOKEN`).

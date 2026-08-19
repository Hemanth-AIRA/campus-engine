# AIRA WebRTC Orchestration Upgrade Reference

This document summarizes the agreed plan for migrating the AI interview orchestration from Zoom to AIRA WebRTC, as discussed and approved.

---

## Objective

Replace all Zoom-specific integration in `aira_3_zoom_orchestration.py` and `zoom_interview.py` with AIRA WebRTC, while retaining all advanced AI-driven interview features.  
**Note:** Chat functionality is not required for this phase.

---

## Key Features to Retain

- Database-driven interview scheduling and orchestration
- Automated bot joins meeting and waits for candidate
- AI-driven Q&A (TTS for questions, STT for answers)
- Real-time participant presence detection
- Audio/video checks and notifications
- AI analysis and scoring of responses
- Final report generation and verdict
- ATS (JobDiva) and email integration
- Robust error handling and logging

---

## Major Changes

- Use JWT-based AIRA WebRTC URLs for bot/candidate meeting join
- Use AIRA WebRTC REST API (`/api/rooms/{roomId}`) for participant presence and role detection
- Route TTS audio through a virtual audio device (e.g., BlackHole) to the browser mic
- Replace all Zoom API calls with AIRA WebRTC API calls
- Retain all Q&A, analysis, reporting, and notification logic

---

## Implementation Steps

1. Refactor meeting join logic to use AIRA WebRTC URLs and APIs
2. Replace Zoom API participant/video/mute checks with AIRA WebRTC REST API polling
3. Ensure audio routing is correct for bot/candidate communication
4. Retain all AI Q&A, analysis, and reporting features
5. Update logging and error handling for AIRA WebRTC context

---

## New/Updated Functions

- `launch_browser_and_join_webrtc_meeting(webrtc_url)`
- `wait_for_participant(room_id, role="candidate")`
- `get_room_participants(room_id)`
- `play_audio_to_virtual_device(audio_file)`
- `check_video_status_via_webrtc(room_id, participant_id)`
- `end_webrtc_meeting(room_id)` (if supported)

---

## Testing & Validation

- Unit test new API integrations
- End-to-end test with bot and candidate on separate machines
- Regression test to ensure feature parity with Zoom version

---

## References

- [AIRA WebRTC API Docs](https://aira.airahr.ai/api/rooms/{roomId})
- [AWSWebRTC repo]
- [AIRA_Zoom_Orchestration_Feature_Document.md]

---

\*\*This document serves as the reference for implementation and validation of the AIRA WebRTC

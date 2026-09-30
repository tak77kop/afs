#!/usr/bin/env python3
"""
AFS STT Node.

Records and transcribes microphone audio, notifying family members of user speech
interventions. Core recording, VAD, Gemini transcription, configuration loading,
and vote tallying are handled by `stt_backend` (independent of rclpy); this file
manages only the ROS2 communication layer (publish/subscribe) and controls the
background recording loop.

Beginner's Guide — Flow of a single user intervention (see `_recorder_loop`):
  1. The microphone listens continuously. Once user speech concludes,
     `record_and_transcribe` (in `stt_backend`) converts audio to text.
  2. This node publishes the transcription to `afs_user_intervention`, prompting
     all family members to vote on who should respond (`vote_callback` collects votes).
  3. `_count_votes` (using `stt_backend.count_votes`) determines the winner, and
     this node publishes the decision so that only the selected responder speaks.
  4. Waits for `afs_stt_resume` before listening for subsequent interventions.

ROS2 Interface for this node:
  Publish   `afs_user_intervention` (String, TRANSIENT_LOCAL) — Speech start/end signals,
            transcription for voting, and final responder decision.
  Publish   `afs_speech_status` (Bool) — True while the user is actively speaking.
  Subscribe `afs_responder_vote` (String) — 1 vote per family member.
  Subscribe `afs_stt_resume` (String) — Signal from responder that speech finished
            and recording may resume.
  Subscribe `afs_initial_scenario_generated` (String, TRANSIENT_LOCAL) —
            Signal that initial conversation is ready and recording may start.

Why threads are used here: Audio recording blocks waiting on the microphone and cannot
run inside a ROS2 callback. Recording executes on `recorder_thread`, while ROS2
callbacks (collecting votes and resumes) continue on the main spin thread. They
communicate via `threading.Event` and lock-guarded vote dictionaries.
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import String, Bool
import threading
import time
import os
os.environ["NO_GCE_CHECK"] = "true"
import json
import asyncio
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy, QoSDurabilityPolicy

from afs_stt.stt_backend import (
    GeminiLiveRecorder,
    load_stt_config,
    load_family_config,
    select_audio_device,
    count_votes,
)

HOME = os.path.expanduser("~")
DB_DIR = os.path.join(HOME, "afs/src/afs_database")
HISTORY_FILE = os.path.join(DB_DIR, "conversation_history.txt")


class AFSSTT(Node):
    """ROS2 node that recognizes microphone speech, coordinates votes on responders, and notifies family members."""

    def __init__(self):
        super().__init__('afs_stt')

        # --- ROS2 communication setup ---
        # TRANSIENT_LOCAL ensures late-joining member nodes receive the latest message
        # without missing state transitions before their initialization finishes.
        qos_pl = QoSProfile(reliability=QoSReliabilityPolicy.RELIABLE, history=QoSHistoryPolicy.KEEP_LAST, depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
        self.intervention_pub = self.create_publisher(String, 'afs_user_intervention', qos_pl)
        self.speech_status_pub = self.create_publisher(Bool, 'afs_speech_status', 10)
        self.ready_event = threading.Event()   # Set when initial scenario is ready
        self.resume_event = threading.Event()  # Set when response finishes playing
        self.create_subscription(String, 'afs_stt_resume', self.resume_callback, 10)
        self.initial_scenario_sub = self.create_subscription(String, 'afs_initial_scenario_generated', self.initial_scenario_callback, qos_pl)

        # Vote collection
        self.create_subscription(String, 'afs_responder_vote', self.vote_callback, 10)
        self._votes = {}          # {voter: voted_role}
        self._vote_lock = threading.Lock()   # Votes arrive on ROS2 thread and are read on recorder thread
        self._vote_event = threading.Event() # Set when all expected votes arrive
        self._expected_voters = 0

        # --- Load configuration (logic layer) ---
        self.stt_config = load_stt_config()
        self.language = self.stt_config["language"]
        self.family_config = load_family_config()
        self.get_logger().info(f"STT Language Mode: {self.language}")
        self.get_logger().info(f"VAD Config: {self.stt_config}")
        self.get_logger().info(f"Family Config: {self.family_config}")

        # Device selection runs at startup and can be re-triggered later
        self._selected_device = None
        self._device_reselect_event = threading.Event()
        self.recorder = None  # Created after device selection
        self.api_key = os.environ.get("GEMINI_API_KEY")
        # daemon=True ensures this thread does not keep the process alive on exit
        self.recorder_thread = threading.Thread(target=self._recorder_loop, daemon=True)
        self.recorder_thread.start()

    def initial_scenario_callback(self, msg: String):
        """Wait for initial conversation scenario generation before allowing recording loop to start."""
        if msg.data == "completed":
            # Add padding delay: even after the scenario is generated, members take
            # time before physically speaking. Listening too early would capture
            # silence or the system's own opening lines.
            time.sleep(5)
            self.ready_event.set()
            # Unsubscribe after initial scenario is processed
            self.destroy_subscription(self.initial_scenario_sub)

    def resume_callback(self, msg: String):
        """Handle `afs_stt_resume`: response to intervention finished, resume listening."""
        self.resume_event.set()

    def _on_speech_start(self):
        """VAD callback: User started speaking. Notify family members to interrupt speech."""
        self.intervention_pub.publish(String(data='user_speech_started'))

    def _on_speech_end(self):
        """VAD callback: User finished speaking. Transcription starts next."""
        self.intervention_pub.publish(String(data='user_speech_ended'))

    def _on_speech_status_change(self, is_active: bool):
        """VAD callback: High-frequency speaking/silent status flag published on change."""
        self.speech_status_pub.publish(Bool(data=is_active))

    def vote_callback(self, msg: String):
        """Collect responder votes from each family member.

        Message format: {"voter": "<role>", "voted_for": "<role>"}.
        Keyed by voter, so duplicate votes from a single member count once.
        """
        try:
            data = json.loads(msg.data)
            voter = data.get("voter", "").lower()
            voted_for = data.get("voted_for", "").lower()
            self.get_logger().info(f"Vote received: {voter} -> {voted_for}")
            with self._vote_lock:
                self._votes[voter] = voted_for
                # All expected votes collected: release recorder thread early without
                # waiting for full 10-second timeout.
                if len(self._votes) >= self._expected_voters:
                    self._vote_event.set()
        except Exception as e:
            self.get_logger().error(f"Error parsing vote: {e}")

    def _count_votes(self) -> str:
        """Tally collected votes and return the winner (tally logic handled by logic layer)."""
        with self._vote_lock:
            votes = dict(self._votes)
        return count_votes(votes, self.family_config, logger=self.get_logger())

    def _create_recorder(self, device_index):
        """Create or recreate recorder (logic layer) for the specified device."""
        self.recorder = GeminiLiveRecorder(
            on_start=self._on_speech_start,
            on_end=self._on_speech_end,
            on_speech_status_change=self._on_speech_status_change,
            logger=self.get_logger(),
            language=self.language,
            vad_aggressiveness=self.stt_config["vad_aggressiveness"],
            silence_duration_s=self.stt_config["silence_duration_s"],
            speech_trigger_frames=self.stt_config["speech_trigger_frames"],
            vad_debug=self.stt_config.get("vad_debug", False),
            vad_energy_threshold=self.stt_config.get("vad_energy_threshold", 0.0),
            device_index=device_index,
        )

    def _recorder_loop(self):
        """Background thread loop: Device selection -> Record -> Vote -> Select responder."""
        # Step 1: Select audio device before waiting for scenario.
        # Requires terminal input, and the terminal is still quiet at startup.
        self._selected_device = select_audio_device()
        self._create_recorder(self._selected_device)

        # Step 2: Await initial scenario readiness
        self.ready_event.wait()

        # This thread owns a private event loop. Recording and API calls are async,
        # while ROS2 spins on a separate thread.
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)

        # Start standard input listener thread for runtime device re-selection
        self._reselect_requested = False
        def stdin_listener():
            """Wait for Enter key to request microphone device re-selection."""
            while rclpy.ok():
                try:
                    input()  # Block until Enter is pressed
                    self._reselect_requested = True
                    self.get_logger().info("Device re-selection requested. Will apply after current recording.")
                except EOFError:
                    break
        stdin_thread = threading.Thread(target=stdin_listener, daemon=True)
        stdin_thread.start()

        async def run():
            while rclpy.ok():
                # Check if device re-selection was requested.
                # Applied between recordings so the microphone stream is never swapped mid-read.
                if self._reselect_requested:
                    self._reselect_requested = False
                    self._selected_device = select_audio_device()
                    self._create_recorder(self._selected_device)

                # Block until user speaks and silence returns
                transcript = await self.recorder.record_and_transcribe()
                if transcript.strip():
                    print(f"\n[Recognized] User: {transcript.strip()}\n")

                    # Step 1: Broadcast transcript to all members for voting
                    self.get_logger().info(f"Broadcasting transcript for voting: {transcript.strip()}")
                    with self._vote_lock:
                        self._votes.clear()
                        self._expected_voters = len(self.family_config)
                    self._vote_event.clear()

                    vote_request = {
                        "text": transcript.strip()
                    }
                    self.intervention_pub.publish(
                        String(data=f"user_speech_transcribed:{json.dumps(vote_request)}")
                    )

                    # Step 2: Wait for votes (10-second timeout).
                    # If a member is slow or crashes, proceed after timeout to avoid deadlocks.
                    self._vote_event.wait(timeout=10.0)

                    # Step 3: Tally votes
                    selected_member = self._count_votes()
                    self.get_logger().info(f"Vote result: {selected_member} selected as responder")

                    # Step 4: Publish decision.
                    # All members receive this, but only the designated responder answers.
                    decision_payload = {
                        "responder": selected_member,
                        "text": transcript.strip()
                    }
                    self.intervention_pub.publish(String(data=f"user_decision:{json.dumps(decision_payload)}"))
                    # Block until `afs_stt_resume` is received; prevents microphone from
                    # picking up the robot's own spoken response.
                    self.resume_event.clear()
                    self.resume_event.wait()
        _loop.run_until_complete(run())


def main():
    """Entry point for `ros2 run afs_stt afs_stt`."""
    rclpy.init()
    node = AFSSTT()
    # Spins only ROS2 callbacks; audio recording runs on the recorder thread
    # spawned in constructor.
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()

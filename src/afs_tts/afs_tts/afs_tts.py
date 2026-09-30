#!/usr/bin/env python3
"""
AFS TTS Node.

Receives speech requests (service calls) from family members, synthesizes speech
via the Gemini API, and plays it back through the speaker assigned to that role
(paired with a toio) or the default audio output. Speech synthesis, playback, and
sink routing are handled by `tts_backend` (independent of rclpy); this file manages
only the ROS2 communication layer (publish/subscribe/service) and the playback queue.

Beginner's Guide — Flow of a single request:
  1. A family member calls the `afs_speak_text` service. `speak_text_callback`
     parses the request, immediately kicks off asynchronous speech synthesis in the
     background (`_queue_audio_task`), and enqueues the task into `self.playback_queue`.
  2. `_playback_worker` runs continuously in the background, dequeuing items one
     by one: it selects the appropriate speaker (`sink`), awaits completion of the
     synthesis task, plays the audio, and publishes `afs_tts_status`/`afs_tts_finished`
     to inform family members of completion.
  3. `interrupt_tts_callback` can halt playback prematurely (e.g. when user speech
     starts), mute all speakers, or resume volume — see `afs_interrupt_tts` topic.

ROS2 Interface for this node:
  Service    `afs_speak_text` (TTSService) — The sole interface to request speech.
  Publish    `afs_tts_status` (String) — "start,<role>,<text>,<muted>" / "end,<role>".
  Publish    `afs_tts_finished` (String) — "finished,<role>". Signal that the next
             member may begin their turn.
  Publish    `afs_tts_initialization` (String, TRANSIENT_LOCAL) — Sent once at startup
             to notify members that the TTS node is active.
  Subscribe  `afs_interrupt_tts` (String) — "stop_all" / "resume_all" / any other text
             cancels the currently playing utterance.
  Subscribe  `afs_intervention_resolved` (String) — Discards pending queue items created
             prior to user intervention.
  Subscribe  `afs_toio_status` (String, TRANSIENT_LOCAL) — Which cubes are connected.

Why synthesis and playback are split: Synthesis is a network call requiring several seconds,
whereas playback must strictly respect conversational turn ordering. Thus, synthesis starts
immediately upon receipt in parallel for all members, but `_playback_worker` plays results
strictly sequentially.
"""

import rclpy
import time
import os
os.environ["NO_GCE_CHECK"] = "true"
import threading
from rclpy.node import Node
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from std_msgs.msg import String
from afs_interfaces.srv import TTSService
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy, QoSDurabilityPolicy
import asyncio
import json
import csv
import io
from typing import Optional
from ament_index_python.packages import get_package_share_directory

from afs_tts.tts_backend import GeminiTTS, AudioSinkController

# Constants
try:
    SAVE_DIR = os.path.join(get_package_share_directory('afs_config'), 'config')
except Exception:
    SAVE_DIR = os.path.expanduser("~/afs/src/afs_config/config")

CONFIG_FILE = os.path.join(SAVE_DIR, 'config.json')
HISTORY_FILE = os.path.join(SAVE_DIR, 'conversation_history.txt')
SINGLE_MEMBER_ROLE = 'androgynous_communication_robot'


class AFSTTS(Node):
    """ROS2 node (communication layer) that manages speech synthesis and playback upon request."""

    def __init__(self, loop):
        super().__init__('afs_tts')
        self.loop = loop
        self.playback_queue = asyncio.Queue()   # Items waiting for sequential playback
        self._current_playback_task = None      # Utterance actively playing (cancellable)
        self.current_speaker_role = None
        self.connected_toios: dict[str, str] = {}
        # Stores original volume before muting to allow restoring later.
        # Non-empty state indicates "currently muted due to user intervention".
        self.muted_sinks_original_volumes = {}
        self.initial_sink_volumes = {}          # Volumes captured once at startup
        self.hdmi_sink = "alsa_output.pci-0000_01_00.1.hdmi-stereo-extra1"
        self.use_hdmi_fallback = False          # Role-specific speakers missing: play all on single output

        # --- ROS2 communication setup ---
        self.srv = self.create_service(TTSService, 'afs_speak_text', self.speak_text_callback)
        self.tts_status_pub = self.create_publisher(String, 'afs_tts_status', 10)
        self.tts_finished_pub = self.create_publisher(String, 'afs_tts_finished', 10)
        self.fallback_finished_pub = self.create_publisher(String, 'afs_fallback_finished', 10)

        # ReentrantCallbackGroup: Interruption callbacks must execute even while
        # other callbacks are running to avoid delayed audio cancellation.
        self.create_subscription(String, 'afs_interrupt_tts', self.interrupt_tts_callback, 10, callback_group=ReentrantCallbackGroup())
        self.create_subscription(String, 'afs_intervention_resolved', self.intervention_resolved_callback, 10)

        qos_profile = QoSProfile(
            reliability=QoSReliabilityPolicy.RELIABLE, history=QoSHistoryPolicy.KEEP_LAST,
            depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL
        )
        self.initialization_pub = self.create_publisher(String, 'afs_tts_initialization', qos_profile)
        self.create_subscription(String, 'afs_toio_status', self.toio_status_callback, qos_profile)

        # TRANSIENT_LOCAL allows members that start up later to receive this message
        self.initialization_pub.publish(String(data="tts_initialized"))
        self.get_logger().info("AFS TTS Started.")

        # --- Logic layer (independent of rclpy: speech synthesis and sink control) ---
        self.client = GeminiTTS(self.get_logger(), self.loop)
        self.sinks = AudioSinkController(self.get_logger())

        # Delegate config loading and volume detection to event loop;
        # both invoke external PulseAudio commands which would slow down the constructor.
        self.loop.create_task(self._async_init())
        self.loop.create_task(self._playback_worker())

    async def _async_init(self):
        """Asynchronous initialization for config and pulse sinks."""
        await self.load_config()
        await self._get_initial_sink_volumes()
        self.get_logger().info("TTS Node Async Initialization Complete.")

    async def _get_initial_sink_volumes(self):
        """Record startup volumes for all speakers so they can be restored after unmuting."""
        available_sinks = await self.sinks.get_available_sinks()
        for sink in available_sinks:
            volume = await self.sinks.get_raw_sink_volume(sink)
            if volume is not None:
                self.initial_sink_volumes[sink] = volume

    def toio_status_callback(self, msg: String):
        """Track which roles are connected to toios (and thus their paired speakers)."""
        try:
            data = json.loads(msg.data)
            status = data.get("status", "").strip().lower()
            if status == "toios_ready":
                positions = data.get("positions", {})
                self.connected_toios.clear()
                for role, pos_data in positions.items():
                    if role in self.role_map:
                        self.connected_toios[role] = self.role_map[role]
            elif status == "initializing":
                self.connected_toios.clear()
        except Exception as e:
            self.get_logger().error(f"Error in toio_status_callback: {e}")

    def intervention_resolved_callback(self, msg: String):
        """Discard pre-intervention queued utterances after user intervention has completed.

        Those utterances were generated before the user spoke, so speaking them now
        would be out of sync with the conversation context.
        """
        while not self.playback_queue.empty():
            try:
                self.playback_queue.get_nowait()
                self.playback_queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def load_config(self):
        """Load role-to-speaker mappings and output mode from config file, determining sink policy.

        Three output policies determined here for `_playback_worker`:
        - chat_mode == 1      : No toio hardware; route all output to a single sink.
        - use_hdmi_fallback   : Role speakers are configured but none are present; fall back to single sink.
        - Otherwise           : Each role plays through its dedicated paired speaker.
        """
        self.role_map = {}      # role -> speaker (sink) name
        self.speaker_map = {}   # role -> Gemini voice name
        self.family_roles = []
        self.chat_mode = 0
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config_data = json.load(f)

            self.chat_mode = config_data.get('chat_mode', 0)
            self.family_roles = [role.lower() for role in config_data.get('family_config', [])]
            speaker_match = config_data.get('toio_speaker_match', [])

            for item in speaker_match:
                role_lower = item['role'].lower()
                self.role_map[role_lower] = item['speaker_id']
                if 'voicevox_speaker_id' in item:
                    self.speaker_map[role_lower] = item['voicevox_speaker_id']

            available_sinks = await self.sinks.get_available_sinks()
            # In Docker / virtualized environments, prioritize default sink (None)
            # over phantom HDMI devices for audio bridge compatibility.
            found_hdmi = self.sinks.find_hdmi_sink(available_sinks)
            if found_hdmi:
                self.get_logger().info(f"Found HDMI sink: {found_hdmi}")
                self.hdmi_sink = found_hdmi

            # If auto_null is present in Docker or HDMI is missing, default sink is preferred
            if "auto_null" in available_sinks or not found_hdmi:
                self.get_logger().info("Prioritizing default sink for audio bridge.")
                self.hdmi_sink = None  # None indicates using PulseAudio's default sink

            if self.chat_mode != 1:
                # If no configured speakers are connected, pairing is stale
                # (speakers powered off or on another host). Fall back to single
                # output so the system can still produce sound.
                all_missing = True
                for sink in self.role_map.values():
                    if sink in available_sinks:
                        all_missing = False
                if all_missing and self.role_map:
                    self.use_hdmi_fallback = True

        except Exception as e:
            self.get_logger().error(f"Failed to load config: {e}")

    async def _playback_worker(self):
        """Background worker that sequentially processes the playback queue (await synthesis -> play -> signal completion).

        This single loop guarantees sequential family member turns: regardless of how
        many synthesis tasks run in parallel, only one line is played at a time.
        """
        while rclpy.ok():
            try:
                # Block until an item is enqueued
                role, text, voice_id, is_leader_response, delay, gen_task = await self.playback_queue.get()
                self.current_speaker_role = role

                # Pause requested by caller to make responses feel natural
                if delay > 0:
                    await asyncio.sleep(delay)

                # Leader response occurs after muting; restore volumes before speaking
                if is_leader_response:
                    await self._restore_volume()

                # Select sink: shared output for chat/fallback mode, otherwise the paired speaker
                sink = self.hdmi_sink if (self.chat_mode == 1 or self.use_hdmi_fallback) else self.role_map.get(role)

                # Route non-Bluetooth sinks to default output destination
                if sink and "bluez" not in sink.lower():
                    self.get_logger().info(f"Sink '{sink}' for role '{role}' is not a Bluetooth speaker. Routing to default output destination.")
                    sink = None

                # Verify sink availability, falling back to default if missing
                available_sinks = await self.sinks.get_available_sinks()
                if sink and sink not in available_sinks:
                    self.get_logger().warn(f"Configured sink '{sink}' not found. Falling back to default sink.")
                    sink = None

                # In normal mode, skip playback if no sink mapping exists for this role
                if sink is None and not (self.chat_mode == 1 or self.use_hdmi_fallback) and role not in self.role_map:
                    self.get_logger().warn(f"No sink mapping for role '{role}', skipping playback.")
                    if not gen_task.done(): gen_task.cancel()
                    continue

                # Wait for synthesis completion (already running task).
                # Often already finished since started in `speak_text_callback`.
                audio_file = None
                try:
                    self.get_logger().info(f"Playback worker: Waiting for synthesis task for {role}...")
                    audio_file = await asyncio.wait_for(gen_task, timeout=70.0)
                    self.get_logger().info(f"Playback worker: synthesis RESUMED/READY for {role}.")
                except asyncio.TimeoutError:
                    self.get_logger().error(f"Playback worker: synthesis TIMEOUT for {role}. Abandoning.")
                except Exception as e:
                    self.get_logger().error(f"Playback worker: synthesis FAILED for {role}: {e}")

                if audio_file and os.path.exists(audio_file):
                    self.get_logger().info(f"Playback worker: synthesis complete for {role}. Starting playback on {sink}...")
                    text_for_publish = text.replace(',', ';')
                    is_muted_by_intervention_str = "true" if self.muted_sinks_original_volumes else "false"
                    self.tts_status_pub.publish(String(data=f"start,{role},{text_for_publish},{is_muted_by_intervention_str}"))

                    # Retain task reference so it can be cancelled upon user intervention
                    self._current_playback_task = asyncio.create_task(self.client.play_audio(audio_file, sink))
                    await self._current_playback_task
                    self.get_logger().info(f"Playback worker: playback finished for {role}.")
                    os.remove(audio_file)  # Remove temporary WAV file to prevent disk leak

                    self.tts_status_pub.publish(String(data=f"end,{role}"))
                    self.tts_finished_pub.publish(String(data=f"finished,{role}"))
                else:
                    self.get_logger().error(f"Synthesis failed or audio file missing for {role}. Skipping playback.")
                    # Signal end so state machine registers completion
                    self.tts_status_pub.publish(String(data=f"end,{role}"))
                    # Publish finished as well to prevent state machine deadlocks
                    self.tts_finished_pub.publish(String(data=f"finished,{role}"))
            except Exception as e:
                self.get_logger().error(f"Error in playback worker: {e}")
            finally:
                self.current_speaker_role = None
                self.playback_queue.task_done()

    async def _restore_volume(self):
        """Restore muted volume, falling back to startup volumes if none were saved."""
        volumes_to_restore = self.muted_sinks_original_volumes or self.initial_sink_volumes
        for sink, original_volume in volumes_to_restore.items():
            await self.sinks.set_sink_volume(sink, original_volume)
        # Clearing this dictionary marks the unmuted state
        self.muted_sinks_original_volumes.clear()

    async def _queue_audio_task(self, role, text, voice_id, is_leader_response, delay):
        """Helper to create synthesis task and place handle into the playback queue within event loop thread.

        Note execution order: synthesis starts first, and only the task handle is enqueued.
        This allows parallel generation while preserving sequential playback.
        """
        try:
            gen_task = self.loop.create_task(self.client.generate_audio(text, voice_id))
            await self.playback_queue.put((role, text, voice_id, is_leader_response, delay, gen_task))
            self.get_logger().info(f"Task for {role} successfully queued.")
        except Exception as e:
            self.get_logger().error(f"Failed to queue audio task for {role}: {e}")

    def speak_text_callback(self, request, response):
        """Service callback for `afs_speak_text`: parse CSV line and enqueue synthesis task.

        `request.text` is a single CSV line formatted like conversation history:
        <role>,<recipient>,<type>,<text>,<voice>. Uses csv module instead of split(',')
        to correctly handle quoted commas in text.

        Response only confirms acceptance, not playback completion; callers wait
        for `afs_tts_finished`.
        """
        try:
            text_with_marker = request.text.strip()
            # Marker indicating a response to the user; volume is unmuted first
            is_leader_response = False
            if text_with_marker.startswith("[LEADER_RESPONSE]"):
                is_leader_response = True
                text_with_marker = text_with_marker[len("[LEADER_RESPONSE]"):]

            reader = csv.reader(io.StringIO(text_with_marker), skipinitialspace=True)
            parts = next(reader)
            role = parts[0].lower()
            text_to_speak = parts[3]
            # Voice is optional: fall back to configured role voice if omitted
            voice_id = parts[4].strip() if len(parts) > 4 else self.speaker_map.get(role, 'Kore')
            delay_val = request.delay

            self.get_logger().info(f"Srv: Scheduling synthesis for {role}...")
            # Service callback executes on ROS2 thread; hand over to asyncio loop via call_soon_threadsafe.
            # Explicitly bind lambda arguments to prevent late-binding closure bugs.
            self.loop.call_soon_threadsafe(
                lambda r=role, t=text_to_speak, v=voice_id, l=is_leader_response, d=delay_val:
                asyncio.run_coroutine_threadsafe(self._queue_audio_task(r, t, v, l, d), self.loop)
            )
            response.success = True
        except Exception as e:
            self.get_logger().error(f"Error in speak_text_callback: {e}")
            response.success = False
        return response

    async def _mute_sinks(self, sinks):
        """Mute specified sinks to 0 while saving current volumes."""
        for sink in sinks:
            try:
                v = await self.sinks.get_raw_sink_volume(sink)
                if v is not None: self.muted_sinks_original_volumes[sink] = v
                await self.sinks.set_sink_volume(sink, 0)
            except Exception: pass

    def _drain_playback_queue(self):
        """Remove all unprocessed items from playback queue and cancel running synthesis tasks."""
        while not self.playback_queue.empty():
            try:
                item = self.playback_queue.get_nowait()
                # Cancel gen_task as well to prevent orphaned WAV files
                if len(item) > 5 and hasattr(item[5], 'cancel'):
                    item[5].cancel()
                self.playback_queue.task_done()
            except asyncio.QueueEmpty: break

    def interrupt_tts_callback(self, msg: String):
        """Handle `afs_interrupt_tts`: stop, mute, or resume playback based on message payload.

        Three message types:
        - "stop_all"   : User started speaking. Mute speakers and drain queue.
                         Muting prevents already-buffered audio from continuing.
        - "resume_all" : Restore volumes to previous levels.
        - Otherwise    : Cancel currently playing line only.
        """
        if msg.data == "stop_all":
            # Prevent double-muting, which would save 0 as original volume and lose sound
            if not self.muted_sinks_original_volumes:
                sinks_to_mute = set([self.hdmi_sink]) if self.chat_mode == 1 else set(self.role_map.values())
                self.loop.create_task(self._mute_sinks(sinks_to_mute))

            self._drain_playback_queue()
        elif msg.data == "resume_all":
            self.loop.create_task(self._restore_volume())
        else:
            if self._current_playback_task and not self._current_playback_task.done():
                self._current_playback_task.cancel()
            self._drain_playback_queue()

    def destroy_node(self):
        """Clean up audio processes cleanly on node shutdown."""
        self.get_logger().info("Shutting down TTS node and cleaning up audio processes...")
        if hasattr(self, 'client'):
            self.client.stop()
        super().destroy_node()

    async def _restore_volumes_on_exit(self):
        for sink, v in self.muted_sinks_original_volumes.items():
            await self.sinks.set_sink_volume(sink, v)


def main():
    """Entry point for `ros2 run afs_tts afs_tts`.

    Two systems run in parallel: ROS2 MultiThreadedExecutor on this thread,
    and asyncio event loop on a background thread. ROS2 passes tasks to
    asyncio via `call_soon_threadsafe`.
    """
    rclpy.init()
    # Create loop in worker's main thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    node = AFSTTS(loop)
    # MultiThreadedExecutor: Interruption callbacks must execute while service
    # callbacks are in progress, which a single-threaded executor cannot do.
    executor = MultiThreadedExecutor()
    executor.add_node(node)

    # Run asyncio loop in dedicated background thread
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    try:
        executor.spin()
    except KeyboardInterrupt: pass
    finally:
        # Restore original volumes on exit (fire-and-forget)
        if hasattr(node, '_restore_volumes_on_exit'):
            loop.create_task(node._restore_volumes_on_exit())

        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=1.0)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AFS Generator node.

Receives dialogue generation requests from the family members and returns a
generated line/action CSV using the LLM (Gemini API). Prompt building, API
calls, and reading the response are handled by
`dialogue_generation.DialogueGenerator` (no rclpy dependency); this file only
handles ROS2 publish/subscribe (the communication layer) and manages the work
that runs in the background.
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
import json
import os
os.environ["NO_GCE_CHECK"] = "true"
import threading

from afs_family.dialogue_generation import DialogueGenerator


class AFSGenerator(Node):
    """ROS2 node that receives dialogue generation requests and publishes the LLM's generated result."""

    def __init__(self):
        super().__init__('afs_generator')

        # --- Logic layer (no rclpy dependency: prompt building, LLM call) ---
        self.generator = DialogueGenerator(self.get_logger())

        # --- ROS2 communication setup ---
        self.create_subscription(String, 'afs_generator_request', self.request_callback, 10)
        self.create_subscription(String, 'afs_generator_cancel', self.cancel_callback, 10)
        self.result_pub = self.create_publisher(String, 'afs_generator_results', 10)
        self.request_lock = threading.Lock()
        self.active_requests = {}

        self.get_logger().info("AFS Dialogue Generator Node Started.")

    def request_callback(self, msg: String):
        """Run generation on a separate thread so the ROS2 executor doesn't get blocked."""
        try:
            request_id = json.loads(msg.data).get("request_id")
        except (json.JSONDecodeError, AttributeError):
            request_id = None
        cancel_event = threading.Event()
        if request_id:
            with self.request_lock:
                self.active_requests[request_id] = cancel_event
        threading.Thread(
            target=self._process_request,
            args=(msg.data, cancel_event),
            daemon=True,
        ).start()

    def cancel_callback(self, msg: String):
        """Signal an active generation to stop before its next API retry."""
        try:
            data = json.loads(msg.data)
            request_id = data.get("request_id")
            role = data.get("role", "unknown")
            with self.request_lock:
                cancel_event = self.active_requests.get(request_id)
                if cancel_event is not None:
                    cancel_event.set()
            if cancel_event is not None:
                self.get_logger().info(f"Cancellation received for {role} (Request: {request_id}).")
            else:
                self.get_logger().debug(f"Cancellation received for inactive request {request_id}.")
        except (json.JSONDecodeError, AttributeError) as e:
            self.get_logger().warning(f"Invalid generator cancellation message: {e}")

    def _process_request(self, msg_data: str, cancel_event: threading.Event):
        request_id = "unknown"
        role = "unknown"
        metadata = {}
        try:
            data = json.loads(msg_data)
            request_id = data.get("request_id")
            role = data.get("role")
            language = data.get("language", "en")
            family_config = data.get("family_config", [])
            target_user = data.get("target_user", "Outsider")
            assigned_voice_id = data.get("assigned_voice_id", "alloy")
            family_status = data.get("family_status", "")
            theme_anchor = data.get("theme_anchor", "Family Gathering")
            voice_list_content = data.get("voice_list_content", "")
            config_content = data.get("config_content", "")
            current_history = data.get("current_history", "")
            few_shot_context = data.get("few_shot_context", "No reference example available.")
            intervention_text = data.get("intervention_text")
            llm_model = data.get("llm_model", "gpt-4o")
            llm_temperature = data.get("llm_temperature", 0.7)
            metadata = data.get("metadata", {})

            self.get_logger().info(f"Generating dialogue for {role} (Request: {request_id})")

            scenario_output = self.generator.generate(
                role=role,
                language=language,
                family_config=family_config,
                target_user=target_user,
                assigned_voice_id=assigned_voice_id,
                family_status=family_status,
                theme_anchor=theme_anchor,
                voice_list_content=voice_list_content,
                config_content=config_content,
                current_history=current_history,
                few_shot_context=few_shot_context,
                intervention_text=intervention_text,
                llm_model=llm_model,
                llm_temperature=llm_temperature,
                api_timeout_seconds=data.get("api_timeout_seconds", 45.0),
                api_max_attempts=data.get("api_max_attempts", 3),
                cancel_event=cancel_event,
            )

            if cancel_event.is_set():
                self.get_logger().info(f"Discarding cancelled generation result (Request: {request_id}).")
                return

            result = {
                "request_id": request_id,
                "role": role,
                "scenario": scenario_output,
                "metadata": metadata
            }

            self.result_pub.publish(String(data=json.dumps(result)))
            self.get_logger().info(f"Dialogue generated for {role} ({request_id})")

        except Exception as e:
            if cancel_event.is_set():
                self.get_logger().info(f"Generation cancelled (Request: {request_id}); no result will be published.")
                return
            self.get_logger().error(f"Error in dialogue generation for {role}: {e}")
            try:
                error_result = {
                    "request_id": request_id,
                    "role": role,
                    "scenario": "",
                    "metadata": metadata,
                    "error": str(e)
                }
                self.result_pub.publish(String(data=json.dumps(error_result)))
            except Exception:
                pass
        finally:
            if request_id != "unknown":
                with self.request_lock:
                    if self.active_requests.get(request_id) is cancel_event:
                        self.active_requests.pop(request_id, None)


def main(args=None):
    rclpy.init(args=args)
    node = AFSGenerator()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()

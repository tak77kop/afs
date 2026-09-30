#!/usr/bin/env python3
"""
Logic layer for the GUI that displays the FACES IV plot image.

Handles displaying the plot image and toggling the blink effect using Tkinter.
Does not depend on ROS2 communication. Called from the afs_viewer node.
"""

import os
import tkinter as tk
from PIL import Image, ImageTk


class PlotViewerGUI:
    """Logic class that alternates between the plot image and its background version every second to create a blinking effect.

    The therapist node generates two images: the full plot and a "_bg" version
    without the current position marker. Alternating between these two images every
    second makes the marker appear to blink, drawing attention to where the family
    is currently located on the plot.
    """

    def __init__(self, master, initial_plot_path):
        self.master = master
        self.master.title("AFS FACES IV Plot Viewer")
        self.label = tk.Label(self.master)
        self.label.pack(fill=tk.BOTH, expand=True)
        self.plot_path = initial_plot_path
        # Companion image with "_bg" appended before the extension
        self.bg_path = initial_plot_path.replace(".png", "_bg.png")
        self.show_bg = False
        # Force pending layout updates so winfo_width/height return actual sizes
        self.master.update_idletasks()
        self.update_image()
        self.blink_active = True
        self.master.after(1000, self.toggle_blink)

    def toggle_blink(self):
        """Toggle between normal and background images every second, making the marker blink.

        Reschedules itself so a single initial call keeps the blinking active as long
        as the window remains open.
        """
        if not self.blink_active: return
        self.show_bg = not self.show_bg
        self.update_image()
        self.master.after(1000, self.toggle_blink)

    def update_image(self, path=None):
        """Load and display the image at the given path. If path is "RESET", display "Resetting..." instead.

        Called from two places: without arguments from the blink timer (redraws current image),
        and with a new path from the ROS2 callback.
        """
        if path == "RESET":
            self.label.config(image='', text="Resetting...", font=("Helvetica", 24, "bold"))
            self.label.image = None
            return

        if path:
            self.plot_path = path
            self.bg_path = path.replace(".png", "_bg.png")
            self.label.config(text="")  # Clear placeholder text

        current_path = self.bg_path if self.show_bg else self.plot_path
        if os.path.exists(current_path):
            try:
                img = Image.open(current_path)
                w = self.master.winfo_width()
                h = self.master.winfo_height()
                if w < 100: w = 480  # Default fallback if winfo is not yet ready
                if h < 100: h = 480
                # thumbnail() preserves aspect ratio. -20 provides padding to keep
                # the image from touching the window borders.
                img.thumbnail((w-20, h-20), Image.LANCZOS)
                self.photo = ImageTk.PhotoImage(img)
                self.label.config(image=self.photo)
                # Keep reference on the widget: Tkinter does not maintain a reference
                # internally, so without this, garbage collection causes a blank label.
                self.label.image = self.photo
            except Exception:
                # Ignore partial/incomplete files while the therapist node is still writing;
                # will retry on the next blink cycle.
                pass
        else:
            self.label.config(image='', text="Resetting...", font=("Helvetica", 24, "bold"))
            self.label.image = None

#!/usr/bin/env python3

import array
import io
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from urllib.request import Request, urlopen

from PIL import Image, ImageTk


# ==================================================
# SETTINGS
# ==================================================

APP_NAME = "Spotify Light"
CONNECT_NAME = "MusicVisualizer"

BG = "#080808"
PANEL = "#121212"
TEXT = "#f5f5f5"
MUTED = "#969696"
SUBTLE = "#5f5f5f"
BUTTON_BG = "#1d1d1d"
BUTTON_ACTIVE = "#303030"
SEPARATOR = "#282828"

ART_SIZE = 360

REFRESH_MS = 1500
PROGRESS_REFRESH_MS = 200

# Lightweight almost-live waveform settings. The analyzer is completely
# stopped when the visual is disabled, so OFF adds no ongoing workload.
WAVEFORM_HEIGHT = 162
WAVEFORM_POINTS = 96
WAVEFORM_REFRESH_MS = 40       # ~25 FPS; slightly calmer than the old ~30 FPS
WAVEFORM_SAMPLE_RATE = 22050
WAVEFORM_READ_BYTES = 1536     # ~35 ms of mono 16-bit audio at 22.05 kHz

# Deep-violet waveform styling. The glow is just a second Canvas line using
# the same coordinates, so the visual treatment adds essentially no analysis cost.
WAVEFORM_BASELINE = "#24162f"
WAVEFORM_GLOW = "#4c1d95"
WAVEFORM_CORE = "#7c3aed"
WAVEFORM_GLOW_WIDTH = 6
WAVEFORM_CORE_WIDTH = 2
WAVEFORM_SPLINE_STEPS = 18

CONFIG_DIR = os.path.join(
    os.path.expanduser("~"),
    ".config",
    "spotify-light",
)
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")


# ==================================================
# COMMAND / SETTINGS HELPERS
# ==================================================


def run_command(args, timeout=2):
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )

        if result.returncode != 0:
            return ""

        return result.stdout.strip()

    except Exception:
        return ""


def spotifyd_running():
    result = subprocess.run(
        ["pgrep", "-x", "spotifyd"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    return result.returncode == 0


def find_spotifyd():
    candidates = [
        "/usr/local/bin/spotifyd",
        shutil.which("spotifyd"),
    ]

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    return None


def get_spotify_player():
    output = run_command(["playerctl", "-l"])

    if not output:
        return None

    players = [
        line.strip()
        for line in output.splitlines()
        if line.strip()
    ]

    for player in players:
        if player == "spotifyd" or player.startswith("spotifyd."):
            return player

    return None


def playerctl(player, *args):
    if not player:
        return ""

    return run_command(
        [
            "playerctl",
            f"--player={player}",
            *args,
        ]
    )


def load_settings():
    defaults = {
        "waveform_enabled": False,
    }

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)

        if isinstance(saved, dict):
            defaults.update(saved)

    except Exception:
        pass

    return defaults


def save_settings(settings):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
    except Exception:
        pass


def parse_playerctl_metadata(raw):
    """Parse normal `playerctl metadata` output into key -> [values].

    Using the unformatted metadata dump is intentional: it means Spotify Light
    automatically sees any additional metadata spotifyd exposes now or in a
    future version, including possible playlist/context fields.
    """

    metadata = {}

    for line in raw.splitlines():
        parts = line.split(None, 2)

        if len(parts) < 3:
            continue

        _player_name, key, value = parts
        value = value.strip()

        if not value:
            continue

        metadata.setdefault(key, []).append(value)

    return metadata


def first_metadata(metadata, *keys):
    for key in keys:
        values = metadata.get(key, [])
        if values:
            return values[0].strip()

    return ""


def joined_metadata(metadata, key):
    values = [
        value.strip()
        for value in metadata.get(key, [])
        if value.strip()
    ]

    # Keep order but remove duplicate entries.
    unique = list(dict.fromkeys(values))
    return ", ".join(unique)


def positive_int(value):
    try:
        number = int(str(value).strip())
        return number if number > 0 else None
    except Exception:
        return None



def format_track_number(track_number, disc_number, track_count):
    track = positive_int(track_number)
    disc = positive_int(disc_number)
    total = positive_int(track_count)

    if not track:
        return "—"

    if total and total >= track:
        track_text = f"{track} of {total}"
    else:
        track_text = f"Track {track}"

    # Disc 1 is just visual noise. Only show the disc when it is actually a
    # multi-disc position (Disc 2, Disc 3, ...).
    if disc and disc > 1:
        return f"Disc {disc}  •  {track_text}"

    return track_text



# ==================================================
# MAIN APP
# ==================================================


class SpotifyLight(tk.Tk):

    def __init__(self):
        super().__init__()

        self.title(APP_NAME)
        self.geometry("1000x625")
        self.minsize(850, 560)
        self.configure(bg=BG)

        self.spotifyd_process = None
        self.started_spotifyd = False
        self.player = None

        self.last_art_url = None
        self.art_photo = None

        self.duration_ms = 0
        self.position_ms = 0
        self.last_position_sync = time.monotonic()
        self.is_playing = False

        self.dragging_volume = False

        # Always start with the waveform OFF. The user can enable it for the
        # current session with the toggle, but reopening Spotify Light starts
        # from the lowest-power state again.
        self.waveform_enabled = False
        self.waveform_process = None
        self.waveform_thread = None
        self.waveform_stop = threading.Event()
        self.waveform_values = [0.0] * WAVEFORM_POINTS
        self.waveform_last_audio = 0.0
        self.waveform_monitor = ""
        self.waveform_available = True

        self.protocol("WM_DELETE_WINDOW", self.close_app)

        self.build_ui()
        self.start_spotifyd()

        if self.waveform_enabled:
            self.after(1000, self.start_waveform)

        self.after(700, self.refresh_metadata)
        self.after(PROGRESS_REFRESH_MS, self.update_progress)
        self.after(WAVEFORM_REFRESH_MS, self.draw_waveform)

    # ==================================================
    # UI
    # ==================================================

    def build_ui(self):
        top = tk.Frame(self, bg=BG)
        top.pack(
            fill="x",
            padx=34,
            pady=(26, 12),
        )

        tk.Label(
            top,
            text="SPOTIFY LIGHT",
            bg=BG,
            fg=TEXT,
            font=("Sans", 16, "bold"),
        ).pack(side="left")

        top_right = tk.Frame(top, bg=BG)
        top_right.pack(side="right")

        self.connection_label = tk.Label(
            top_right,
            text="Starting MusicVisualizer…",
            bg=BG,
            fg=MUTED,
            font=("Sans", 10),
        )
        self.connection_label.pack(side="left")

        self.waveform_button = tk.Button(
            top_right,
            command=self.toggle_waveform,
            bg=BG,
            fg=MUTED,
            activebackground=BG,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            padx=0,
            pady=0,
            cursor="hand2",
            font=("Sans", 9, "bold"),
        )
        self.waveform_button.pack(
            side="left",
            padx=(16, 0),
        )
        self.update_waveform_button()

        body = tk.Frame(self, bg=BG)
        body.pack(
            fill="both",
            expand=True,
            padx=34,
            pady=10,
        )

        # ==============================================
        # ALBUM ART
        # ==============================================

        art_frame = tk.Frame(
            body,
            width=ART_SIZE,
            height=ART_SIZE,
            bg=PANEL,
        )
        art_frame.pack(side="left", anchor="n")
        art_frame.pack_propagate(False)

        self.art_label = tk.Label(
            art_frame,
            text="♪",
            bg=PANEL,
            fg=SUBTLE,
            font=("Sans", 90),
        )
        self.art_label.pack(fill="both", expand=True)

        # ==============================================
        # METADATA
        # ==============================================

        info = tk.Frame(body, bg=BG)
        info.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(38, 0),
        )

        self.status_label = tk.Label(
            info,
            text="WAITING FOR SPOTIFY",
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Sans", 10, "bold"),
        )
        self.status_label.pack(fill="x", pady=(3, 10))

        self.title_label = tk.Label(
            info,
            text="Choose MusicVisualizer",
            bg=BG,
            fg=TEXT,
            anchor="w",
            justify="left",
            wraplength=500,
            font=("Sans", 28, "bold"),
        )
        self.title_label.pack(fill="x")

        self.artist_label = tk.Label(
            info,
            text="from Spotify on your phone",
            bg=BG,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=500,
            font=("Sans", 19),
        )
        self.artist_label.pack(fill="x", pady=(7, 4))

        self.album_label = tk.Label(
            info,
            text="",
            bg=BG,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=500,
            font=("Sans", 14),
        )
        self.album_label.pack(fill="x")

        separator = tk.Frame(
            info,
            height=1,
            bg=SEPARATOR,
        )
        separator.pack(fill="x", pady=18)

        details = tk.Frame(info, bg=BG)
        details.pack(fill="x")

        self.album_artist_value = self.detail_row(
            details,
            "ALBUM ARTIST",
        )

        self.track_value = self.detail_row(
            details,
            "TRACK",
        )

        self.source_value = self.detail_row(
            details,
            "SOURCE",
        )

        # ==============================================
        # LIGHTWEIGHT ALMOST-LIVE WAVEFORM
        # ==============================================

        # The visual belongs to the root window rather than the metadata
        # column so it can span nearly the full app width. Its vertical
        # position is calculated from the live gap between SOURCE and the
        # playback time row, keeping it centered a little higher in that space.
        self.waveform_container = tk.Frame(
            self,
            height=WAVEFORM_HEIGHT,
            bg=BG,
        )
        self.waveform_container.pack_propagate(False)

        self.waveform_canvas = tk.Canvas(
            self.waveform_container,
            height=WAVEFORM_HEIGHT,
            bg=BG,
            highlightthickness=0,
        )
        self.waveform_canvas.pack(fill="both", expand=True)

        self.waveform_center = self.waveform_canvas.create_line(
            0,
            WAVEFORM_HEIGHT / 2,
            1,
            WAVEFORM_HEIGHT / 2,
            fill=WAVEFORM_BASELINE,
            width=1,
        )

        # One dim wide line plus one crisp violet core gives a smooth glow
        # without blur filters, extra images, or a heavier rendering library.
        self.waveform_glow = self.waveform_canvas.create_line(
            0,
            WAVEFORM_HEIGHT / 2,
            1,
            WAVEFORM_HEIGHT / 2,
            fill=WAVEFORM_GLOW,
            width=WAVEFORM_GLOW_WIDTH,
            smooth=True,
            splinesteps=WAVEFORM_SPLINE_STEPS,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
        )

        self.waveform_line = self.waveform_canvas.create_line(
            0,
            WAVEFORM_HEIGHT / 2,
            1,
            WAVEFORM_HEIGHT / 2,
            fill=WAVEFORM_CORE,
            width=WAVEFORM_CORE_WIDTH,
            smooth=True,
            splinesteps=WAVEFORM_SPLINE_STEPS,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
        )

        # Positioning happens after the rest of the interface has been laid
        # out because it depends on the SOURCE row and the playback time row.
        # The container is shown/hidden with place()/place_forget().

        # ==============================================
        # BOTTOM
        # ==============================================

        bottom = tk.Frame(self, bg=BG)
        bottom.pack(
            fill="x",
            padx=34,
            pady=(5, 28),
        )

        time_row = tk.Frame(bottom, bg=BG)
        time_row.pack(fill="x")

        self.position_label = tk.Label(
            time_row,
            text="0:00",
            bg=BG,
            fg=MUTED,
            font=("Sans", 9),
        )
        self.position_label.pack(side="left")

        self.duration_label = tk.Label(
            time_row,
            text="0:00",
            bg=BG,
            fg=MUTED,
            font=("Sans", 9),
        )
        self.duration_label.pack(side="right")

        self.progress = tk.Canvas(
            bottom,
            height=5,
            bg="#292929",
            highlightthickness=0,
        )
        self.progress.pack(fill="x", pady=(5, 17))

        self.progress_fill = self.progress.create_rectangle(
            0,
            0,
            0,
            5,
            fill=TEXT,
            outline="",
        )

        controls = tk.Frame(bottom, bg=BG)
        controls.pack(fill="x")

        left_controls = tk.Frame(controls, bg=BG)
        left_controls.pack(side="left")

        self.make_button(
            left_controls,
            "◀◀",
            self.previous_track,
            width=5,
        ).pack(side="left", padx=(0, 7))

        self.play_button = self.make_button(
            left_controls,
            "▶",
            self.play_pause,
            width=5,
        )
        self.play_button.pack(side="left", padx=7)

        self.make_button(
            left_controls,
            "▶▶",
            self.next_track,
            width=5,
        ).pack(side="left", padx=7)

        volume_frame = tk.Frame(controls, bg=BG)
        volume_frame.pack(side="right")

        tk.Label(
            volume_frame,
            text="VOLUME",
            bg=BG,
            fg=MUTED,
            font=("Sans", 9, "bold"),
        ).pack(side="left", padx=(0, 10))

        self.volume = tk.Scale(
            volume_frame,
            from_=0,
            to=100,
            orient="horizontal",
            length=180,
            showvalue=False,
            bg=BG,
            fg=TEXT,
            troughcolor="#292929",
            activebackground=TEXT,
            highlightthickness=0,
            bd=0,
        )
        self.volume.set(100)
        self.volume.pack(side="left")

        self.volume.bind(
            "<ButtonPress-1>",
            self.volume_drag_start,
        )
        self.volume.bind(
            "<ButtonRelease-1>",
            self.volume_drag_end,
        )

        # Keep the full-width waveform correctly positioned when the window
        # is resized or maximized.  after_idle lets Tk finish its first layout
        # pass before we read widget coordinates.
        self.bind("<Configure>", self.position_waveform)
        self.after_idle(self.position_waveform)

    def detail_row(self, parent, label):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=3)

        tk.Label(
            row,
            text=label,
            width=15,
            anchor="w",
            bg=BG,
            fg=SUBTLE,
            font=("Sans", 9, "bold"),
        ).pack(side="left")

        value = tk.Label(
            row,
            text="—",
            anchor="w",
            bg=BG,
            fg=MUTED,
            font=("Sans", 11),
        )
        value.pack(side="left", fill="x", expand=True)

        return value

    def make_button(self, parent, text, command, width=6):
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=BUTTON_BG,
            fg=TEXT,
            activebackground=BUTTON_ACTIVE,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            font=("Sans", 13, "bold"),
            padx=8,
            pady=7,
            cursor="hand2",
        )

    # ==================================================
    # SPOTIFYD
    # ==================================================

    def start_spotifyd(self):
        if spotifyd_running():
            self.started_spotifyd = False
            return

        executable = find_spotifyd()

        if not executable:
            self.connection_label.config(text="spotifyd not found")
            return

        try:
            self.spotifyd_process = subprocess.Popen(
                [
                    executable,
                    "--no-daemon",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.started_spotifyd = True

        except Exception as error:
            print("Could not start spotifyd:", error)

    def stop_spotifyd(self):
        if not self.started_spotifyd or not self.spotifyd_process:
            return

        try:
            if self.spotifyd_process.poll() is None:
                self.spotifyd_process.terminate()

                try:
                    self.spotifyd_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.spotifyd_process.kill()

        except Exception:
            pass

    # ==================================================
    # METADATA
    # ==================================================

    def refresh_metadata(self):
        self.player = get_spotify_player()

        if not self.player:
            self.show_waiting()
            self.after(REFRESH_MS, self.refresh_metadata)
            return

        raw = playerctl(self.player, "metadata")

        if not raw:
            self.show_waiting()
            self.after(REFRESH_MS, self.refresh_metadata)
            return

        metadata = parse_playerctl_metadata(raw)

        title = first_metadata(metadata, "xesam:title")
        artist = joined_metadata(metadata, "xesam:artist")
        album = first_metadata(metadata, "xesam:album")
        album_artist = joined_metadata(metadata, "xesam:albumArtist")

        track_number = first_metadata(
            metadata,
            "xesam:trackNumber",
        )
        disc_number = first_metadata(
            metadata,
            "xesam:discNumber",
        )

        # Not currently standard in spotifyd MPRIS, but checked so a newer or
        # custom build can populate "3 of 11" automatically if it exposes it.
        track_count = first_metadata(
            metadata,
            "xesam:trackCount",
            "mpris:trackCount",
            "spotify:trackCount",
        )

        length_us = first_metadata(metadata, "mpris:length")
        art_url = first_metadata(metadata, "mpris:artUrl")

        status = playerctl(self.player, "status")
        position = playerctl(self.player, "position")
        volume = playerctl(self.player, "volume")

        try:
            self.duration_ms = float(length_us) / 1000
        except Exception:
            self.duration_ms = 0

        try:
            self.position_ms = float(position) * 1000
        except Exception:
            self.position_ms = 0

        self.last_position_sync = time.monotonic()
        self.is_playing = status.lower() == "playing"

        self.connection_label.config(
            text=f"Spotify Connect  •  {CONNECT_NAME}"
        )

        self.status_label.config(
            text="NOW PLAYING" if self.is_playing else "PAUSED"
        )

        self.title_label.config(text=title or "Unknown Song")
        self.artist_label.config(text=artist or "Unknown Artist")
        self.album_label.config(text=album)

        self.album_artist_value.config(
            text=album_artist or artist or "—"
        )

        self.track_value.config(
            text=format_track_number(
                track_number,
                disc_number,
                track_count,
            )
        )

        self.source_value.config(
            text="spotifyd • Spotify Connect"
        )

        self.play_button.config(
            text="Ⅱ" if self.is_playing else "▶"
        )

        if volume and not self.dragging_volume:
            try:
                self.volume.set(round(float(volume) * 100))
            except Exception:
                pass

        if art_url and art_url != self.last_art_url:
            self.last_art_url = art_url
            threading.Thread(
                target=self.load_art,
                args=(art_url,),
                daemon=True,
            ).start()

        self.after(REFRESH_MS, self.refresh_metadata)

    def show_waiting(self):
        self.connection_label.config(
            text=f"Waiting for {CONNECT_NAME}"
        )
        self.status_label.config(text="WAITING FOR SPOTIFY")
        self.title_label.config(text=f"Choose {CONNECT_NAME}")
        self.artist_label.config(text="from Spotify on your phone")
        self.album_label.config(text="")

        self.album_artist_value.config(text="—")
        self.track_value.config(text="—")
        self.source_value.config(text="spotifyd • Spotify Connect")

        self.is_playing = False
        self.position_ms = 0
        self.duration_ms = 0
        self.play_button.config(text="▶")

    # ==================================================
    # ALBUM ART
    # ==================================================

    def load_art(self, url):
        try:
            request = Request(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
            )

            with urlopen(request, timeout=5) as response:
                data = response.read()

            image = Image.open(io.BytesIO(data))
            image = image.convert("RGB")
            image = image.resize(
                (ART_SIZE, ART_SIZE),
                Image.Resampling.LANCZOS,
            )

            self.after(
                0,
                lambda: self.apply_art(image),
            )

        except Exception as error:
            print("Album art error:", error)

    def apply_art(self, image):
        self.art_photo = ImageTk.PhotoImage(image)
        self.art_label.config(
            image=self.art_photo,
            text="",
        )

    # ==================================================
    # LIGHTWEIGHT WAVEFORM
    # ==================================================

    def update_waveform_button(self):
        if not self.waveform_available:
            label = "LINE UNAVAILABLE"
        else:
            label = (
                "LINE ON"
                if self.waveform_enabled
                else "LINE OFF"
            )

        self.waveform_button.config(text=label)

    def toggle_waveform(self):
        self.waveform_enabled = not self.waveform_enabled

        save_settings(
            {
                "waveform_enabled": self.waveform_enabled,
            }
        )

        if self.waveform_enabled:
            self.position_waveform()
            self.start_waveform()
        else:
            self.stop_waveform()
            self.waveform_container.place_forget()

        self.update_waveform_button()

    def position_waveform(self, event=None):
        """Place the waveform across the app in the SOURCE-to-time gap."""
        if not self.waveform_enabled:
            return

        try:
            root_y = self.winfo_rooty()
            source_bottom = (
                self.source_value.winfo_rooty()
                - root_y
                + self.source_value.winfo_height()
            )
            time_top = self.position_label.winfo_rooty() - root_y

            # Leave breathing room around both neighboring sections.
            gap_top = source_bottom + 18
            gap_bottom = time_top - 18

            if gap_bottom <= gap_top:
                return

            # The old waveform sat quite low in this gap.  Placing its center
            # at 52% of the SOURCE-to-time space moves it roughly another 20%
            # upward while keeping it clear of the metadata and controls.
            center_y = gap_top + (gap_bottom - gap_top) * 0.52

            # Keep the line field tall enough to breathe, but shrink on a
            # shorter window so it stays clear of SOURCE and the time bar.
            available_height = max(44, int(gap_bottom - gap_top))
            actual_height = min(WAVEFORM_HEIGHT, available_height)
            y = int(center_y - actual_height / 2)

            # Match the progress bar's 34 px outer margins so the waveform
            # visually spans the full usable width of the window.
            margin = 34
            width = max(2, self.winfo_width() - margin * 2)

            self.waveform_container.place(
                x=margin,
                y=y,
                width=width,
                height=actual_height,
            )
            self.waveform_container.lift()

        except tk.TclError:
            pass

    def get_monitor_source(self):
        pactl = shutil.which("pactl")

        if not pactl:
            return ""

        sink = run_command(
            [pactl, "get-default-sink"],
            timeout=2,
        )

        if not sink:
            info = run_command([pactl, "info"], timeout=2)
            for line in info.splitlines():
                if line.startswith("Default Sink:"):
                    sink = line.split(":", 1)[1].strip()
                    break

        if not sink:
            return ""

        if sink.endswith(".monitor"):
            return sink

        return f"{sink}.monitor"

    def start_waveform(self):
        if not self.waveform_enabled:
            return

        if self.waveform_process and self.waveform_process.poll() is None:
            return

        parec = shutil.which("parec")
        monitor = self.get_monitor_source()

        if not parec or not monitor:
            self.waveform_available = False
            self.update_waveform_button()
            return

        self.waveform_available = True
        self.waveform_monitor = monitor
        self.waveform_stop.clear()

        try:
            self.waveform_process = subprocess.Popen(
                [
                    parec,
                    "--raw",
                    f"--device={monitor}",
                    "--format=s16le",
                    f"--rate={WAVEFORM_SAMPLE_RATE}",
                    "--channels=1",
                    "--latency-msec=25",
                    "--process-time-msec=20",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )

        except Exception:
            self.waveform_process = None
            self.waveform_available = False
            self.update_waveform_button()
            return

        self.waveform_thread = threading.Thread(
            target=self.waveform_capture_loop,
            daemon=True,
        )
        self.waveform_thread.start()
        self.update_waveform_button()

    def stop_waveform(self):
        self.waveform_stop.set()

        process = self.waveform_process
        self.waveform_process = None

        if process:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
            except Exception:
                pass

        self.waveform_values = [0.0] * WAVEFORM_POINTS

    def waveform_capture_loop(self):
        process = self.waveform_process

        if not process or not process.stdout:
            return

        previous = list(self.waveform_values)
        display_gain = 1.0

        try:
            while not self.waveform_stop.is_set():
                data = process.stdout.read(WAVEFORM_READ_BYTES)

                if not data:
                    break

                samples = array.array("h")
                samples.frombytes(data)

                if sys.byteorder != "little":
                    samples.byteswap()

                if not samples:
                    continue

                sample_count = len(samples)
                segment = max(1, sample_count // WAVEFORM_POINTS)

                chunk_peak = max(abs(sample) for sample in samples)
                mean_square = (
                    sum(sample * sample for sample in samples)
                    / sample_count
                )
                chunk_rms = math.sqrt(mean_square)

                # Stable automatic gain keeps quieter music visible while
                # avoiding hard jumps on loud hits. Gain rises slowly and
                # falls faster so the line stays reactive without pumping.
                measured_level = max(
                    850.0,
                    chunk_rms * 2.0,
                    chunk_peak * 0.34,
                )
                target_gain = max(
                    1.0,
                    min(6.5, 17000.0 / measured_level),
                )

                if target_gain < display_gain:
                    display_gain = (
                        display_gain * 0.64
                        + target_gain * 0.36
                    )
                else:
                    display_gain = (
                        display_gain * 0.91
                        + target_gain * 0.09
                    )

                values = []

                for index in range(WAVEFORM_POINTS):
                    start = index * segment
                    end = min(sample_count, start + segment)

                    if start >= sample_count:
                        values.append(0.0)
                        continue

                    piece = samples[start:end]

                    if not piece:
                        values.append(0.0)
                        continue

                    representative = max(
                        piece,
                        key=lambda sample: abs(sample),
                    )

                    raw_value = (
                        representative
                        / 32768.0
                        * display_gain
                    )

                    # Soft limiting preserves large peaks without the ugly flat
                    # tops that hard clipping would create.
                    value = math.tanh(raw_value * 1.18)

                    # Slightly slower than the earlier almost-live version:
                    # only 28% of each new chunk enters per update.
                    smoothed = (
                        previous[index] * 0.72
                        + value * 0.28
                    )
                    values.append(smoothed)

                # Neighbor smoothing creates broader flowing shapes while still
                # representing the current audio, rather than a synthetic wave.
                if len(values) >= 5:
                    for _pass in range(2):
                        spatial = values[:]
                        for index in range(2, len(values) - 2):
                            spatial[index] = (
                                values[index - 2]
                                + values[index - 1] * 2
                                + values[index] * 3
                                + values[index + 1] * 2
                                + values[index + 2]
                            ) / 9
                        values = spatial

                previous = values
                self.waveform_values = values
                self.waveform_last_audio = time.monotonic()

        except Exception:
            pass

    def draw_waveform(self):
        if self.waveform_enabled:
            width = max(2, self.waveform_canvas.winfo_width())
            height = max(2, self.waveform_canvas.winfo_height())
            center = height / 2

            # Faint x-axis / zero line.
            self.waveform_canvas.coords(
                self.waveform_center,
                0,
                center,
                width,
                center,
            )

            values = self.waveform_values

            # If capture goes stale, ease the existing line back to zero rather
            # than snapping it flat.
            if time.monotonic() - self.waveform_last_audio > 0.15:
                values = [value * 0.94 for value in values]
                self.waveform_values = values

            if values:
                step = width / max(1, len(values) - 1)

                # Preserve the more dramatic amplitude from the recent version:
                # peaks can use almost the full half-height on either side of zero.
                amplitude = max(14, (height / 2) - 8)
                coords = []

                for index, value in enumerate(values):
                    x = index * step
                    y = center - (value * amplitude)
                    coords.extend([x, y])

                self.waveform_canvas.coords(
                    self.waveform_glow,
                    *coords,
                )
                self.waveform_canvas.coords(
                    self.waveform_line,
                    *coords,
                )

        self.after(
            WAVEFORM_REFRESH_MS,
            self.draw_waveform,
        )

    # ==================================================
    # PLAYBACK
    # ==================================================

    def previous_track(self):
        if self.player:
            playerctl(self.player, "previous")

    def play_pause(self):
        if self.player:
            playerctl(self.player, "play-pause")

    def next_track(self):
        if self.player:
            playerctl(self.player, "next")

    # ==================================================
    # VOLUME
    # ==================================================

    def volume_drag_start(self, event):
        self.dragging_volume = True

    def volume_drag_end(self, event):
        self.dragging_volume = False

        if not self.player:
            return

        value = float(self.volume.get()) / 100
        playerctl(
            self.player,
            "volume",
            f"{value:.2f}",
        )

    # ==================================================
    # PROGRESS
    # ==================================================

    def update_progress(self):
        position = self.position_ms

        if self.is_playing:
            elapsed = time.monotonic() - self.last_position_sync
            position += elapsed * 1000

        if self.duration_ms > 0:
            position = min(position, self.duration_ms)
            ratio = position / self.duration_ms
        else:
            ratio = 0

        width = max(1, self.progress.winfo_width())

        self.progress.coords(
            self.progress_fill,
            0,
            0,
            width * ratio,
            5,
        )

        self.position_label.config(
            text=self.format_time(position)
        )
        self.duration_label.config(
            text=self.format_time(self.duration_ms)
        )

        self.after(
            PROGRESS_REFRESH_MS,
            self.update_progress,
        )

    @staticmethod
    def format_time(milliseconds):
        try:
            seconds = int(max(0, milliseconds) / 1000)
        except Exception:
            seconds = 0

        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes}:{seconds:02d}"

    # ==================================================
    # CLOSE
    # ==================================================

    def close_app(self):
        self.stop_waveform()
        self.stop_spotifyd()
        self.destroy()


# ==================================================
# START
# ==================================================


if __name__ == "__main__":
    app = SpotifyLight()
    app.mainloop()

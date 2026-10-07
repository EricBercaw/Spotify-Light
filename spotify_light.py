#!/usr/bin/env python3

import io
import os
import shutil
import subprocess
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

# CAVA-backed visualizer. OFF is always the startup/base-case mode.
# When OFF there is no CAVA process, no audio analysis, and no redraw loop.
VISUALIZER_HEIGHT = 162
VISUALIZER_MAX_HEIGHT = 360
WINDOWED_HEIGHT = 625
VISUALIZER_GROWTH_PER_PX = 0.35
VISUALIZER_REFRESH_MS = 33     # ~30 FPS only while CAVA is enabled
CAVA_BARS = 34
CAVA_ASCII_MAX = 1000

# Seven lightweight three-stop color schemes plus an eighth Dynamic option.
# Every scheme keeps a subtle gradient across the bars. Dynamic smoothly blends
# from one full gradient to the next over 10 seconds, then continues cycling.
DEFAULT_COLOR_SCHEME = "Violet → Magenta"
DYNAMIC_COLOR_SCHEME = "Dynamic"
DYNAMIC_TRANSITION_SECONDS = 10.0
DYNAMIC_COLOR_REFRESH_SECONDS = 0.10
COLOR_SCHEMES = {
    "Violet → Magenta": ("#4c1d95", "#7c3aed", "#c026d3"),
    "Deep Blue → Cyan": ("#1e3a8a", "#2563eb", "#06b6d4"),
    "Purple → Blue": ("#6d28d9", "#4f46e5", "#2563eb"),
    "Red → Orange": ("#991b1b", "#dc2626", "#f97316"),
    "Green → Aqua": ("#166534", "#16a34a", "#14b8a6"),
    "Pink → Purple": ("#be185d", "#ec4899", "#8b5cf6"),
    "Ice Blue → White": ("#0e7490", "#67e8f9", "#f8fafc"),
}

CAVA_CONFIG_DIR = os.path.join(
    os.path.expanduser("~"),
    ".config",
    "spotify-light",
)
CAVA_CONFIG_FILE = os.path.join(CAVA_CONFIG_DIR, "cava.conf")



# ==================================================
# COMMAND HELPERS
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


def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def lerp_color(start, end, amount):
    start_rgb = hex_to_rgb(start)
    end_rgb = hex_to_rgb(end)
    rgb = tuple(
        round(a + (b - a) * amount)
        for a, b in zip(start_rgb, end_rgb)
    )
    return rgb_to_hex(rgb)


def three_stop_gradient(colors, count):
    start, middle, end = colors

    if count <= 1:
        return [middle]

    gradient = []
    for index in range(count):
        position = index / (count - 1)
        if position <= 0.5:
            color = lerp_color(
                start,
                middle,
                position / 0.5,
            )
        else:
            color = lerp_color(
                middle,
                end,
                (position - 0.5) / 0.5,
            )
        gradient.append(color)

    return gradient



def find_cava():
    candidates = [
        shutil.which("cava"),
        "/usr/bin/cava",
        "/usr/local/bin/cava",
    ]

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    return None


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

        # True fullscreen is user-controlled and never forced at startup.
        self.fullscreen = False
        self.bind("<F11>", self.toggle_fullscreen)
        self.bind("<Escape>", self.exit_fullscreen)

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

        # Always start with the CAVA visualizer OFF. Turning it on starts
        # exactly one CAVA raw-output process; turning it off terminates it.
        self.visualizer_enabled = False
        self.cava_process = None
        self.cava_thread = None
        self.cava_stop = threading.Event()
        self.visualizer_draw_after = None
        self.visualizer_available = True
        self.cava_values = [0.0] * CAVA_BARS
        self.cava_display_values = [0.0] * CAVA_BARS

        # While the visualizer is ON, keep the display awake and inhibit
        # system sleep. Everything is released again when the visualizer is
        # turned OFF or Spotify Light closes.
        self.sleep_inhibit_process = None
        self.screensaver_suspended = False
        self.screensaver_window_id = None

        # Precompute every palette once. The dropdown therefore adds virtually
        # no runtime cost, even while CAVA is drawing at full speed.
        self.cava_palette_cache = {
            name: three_stop_gradient(colors, CAVA_BARS)
            for name, colors in COLOR_SCHEMES.items()
        }
        self.color_scheme_name = DEFAULT_COLOR_SCHEME
        self.cava_colors = self.cava_palette_cache[self.color_scheme_name]
        self.dynamic_color_start = time.monotonic()
        self.dynamic_color_last_update = 0.0

        self.protocol("WM_DELETE_WINDOW", self.close_app)

        self.build_ui()
        self.start_spotifyd()

        self.after(700, self.refresh_metadata)
        self.after(PROGRESS_REFRESH_MS, self.update_progress)

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

        self.visualizer_button = tk.Button(
            top_right,
            text="VISUALIZER: OFF",
            command=self.toggle_visualizer,
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
        self.visualizer_button.pack(
            side="left",
            padx=(16, 0),
        )

        self.color_scheme_var = tk.StringVar(
            value=self.color_scheme_name
        )
        self.color_menu = tk.OptionMenu(
            top_right,
            self.color_scheme_var,
            *list(COLOR_SCHEMES.keys()),
            DYNAMIC_COLOR_SCHEME,
            command=self.apply_color_scheme,
        )
        self.color_menu.config(
            bg=BG,
            fg=MUTED,
            activebackground=BUTTON_ACTIVE,
            activeforeground=TEXT,
            highlightthickness=0,
            bd=0,
            relief="flat",
            cursor="hand2",
            font=("Sans", 9, "bold"),
            width=17,
            anchor="e",
        )
        self.color_menu["menu"].config(
            bg=BUTTON_BG,
            fg=TEXT,
            activebackground=BUTTON_ACTIVE,
            activeforeground=TEXT,
            bd=0,
            font=("Sans", 9),
        )
        self.color_menu.pack(
            side="left",
            padx=(12, 0),
        )

        self.fullscreen_button = tk.Button(
            top_right,
            text="FULLSCREEN",
            command=self.toggle_fullscreen,
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
        self.fullscreen_button.pack(
            side="left",
            padx=(16, 0),
        )
        self.update_fullscreen_button()

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
        # CAVA VISUALIZER
        # ==============================================

        # CAVA handles capture, FFT, autosensitivity and smoothing. Spotify
        # Light only draws the raw bar values, which keeps this code simple.
        self.visualizer_container = tk.Frame(
            self,
            height=VISUALIZER_HEIGHT,
            bg=BG,
        )
        self.visualizer_container.pack_propagate(False)

        self.visualizer_canvas = tk.Canvas(
            self.visualizer_container,
            height=VISUALIZER_HEIGHT,
            bg=BG,
            highlightthickness=0,
        )
        self.visualizer_canvas.pack(fill="both", expand=True)

        self.cava_bars = []
        for index in range(CAVA_BARS):
            self.cava_bars.append(
                self.visualizer_canvas.create_rectangle(
                    0,
                    VISUALIZER_HEIGHT,
                    1,
                    VISUALIZER_HEIGHT,
                    fill=self.cava_colors[index],
                    outline="",
                )
            )

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

        # Keep the full-width visualizer correctly positioned when the window
        # is resized or maximized. after_idle lets Tk finish its first layout.
        self.bind("<Configure>", self.position_visualizer)
        self.after_idle(self.position_visualizer)

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
    # FULLSCREEN
    # ==================================================

    def update_fullscreen_button(self):
        self.fullscreen_button.config(
            text="EXIT FULLSCREEN" if self.fullscreen else "FULLSCREEN"
        )

    def toggle_fullscreen(self, event=None):
        self.fullscreen = not self.fullscreen
        self.attributes("-fullscreen", self.fullscreen)
        self.update_fullscreen_button()

        # Recalculate the visualizer geometry after Tk/LXQt has finished
        # applying the new screen dimensions.
        self.after(80, self.position_visualizer)
        return "break" if event is not None else None

    def exit_fullscreen(self, event=None):
        if self.fullscreen:
            self.fullscreen = False
            self.attributes("-fullscreen", False)
            self.update_fullscreen_button()
            self.after(80, self.position_visualizer)

        return "break" if event is not None else None

    # ==================================================
    # CAVA VISUALIZER
    # ==================================================

    def apply_color_scheme(self, scheme_name=None):
        valid_names = set(self.cava_palette_cache) | {DYNAMIC_COLOR_SCHEME}
        if scheme_name not in valid_names:
            scheme_name = DEFAULT_COLOR_SCHEME

        self.color_scheme_name = scheme_name
        self.color_scheme_var.set(scheme_name)

        if scheme_name == DYNAMIC_COLOR_SCHEME:
            # Start each Dynamic session from the default gradient, then blend
            # continuously through all seven palettes in menu order.
            self.dynamic_color_start = time.monotonic()
            self.dynamic_color_last_update = 0.0
            self.cava_colors = list(
                self.cava_palette_cache[DEFAULT_COLOR_SCHEME]
            )
        else:
            self.cava_colors = self.cava_palette_cache[scheme_name]

        # Update the existing canvas rectangles in place. This works whether
        # the visualizer is on or off and never requires a CAVA restart.
        for index, bar in enumerate(self.cava_bars):
            self.visualizer_canvas.itemconfig(
                bar,
                fill=self.cava_colors[index],
            )

    def update_dynamic_colors(self, now=None):
        if self.color_scheme_name != DYNAMIC_COLOR_SCHEME:
            return

        if now is None:
            now = time.monotonic()

        # Color changes are intentionally capped at 10 Hz. The transition still
        # looks smooth over a 10-second blend while keeping UI work negligible.
        if now - self.dynamic_color_last_update < DYNAMIC_COLOR_REFRESH_SECONDS:
            return

        self.dynamic_color_last_update = now
        palette_names = list(COLOR_SCHEMES.keys())
        elapsed = max(0.0, now - self.dynamic_color_start)
        stage = int(elapsed // DYNAMIC_TRANSITION_SECONDS)
        blend = (elapsed % DYNAMIC_TRANSITION_SECONDS) / DYNAMIC_TRANSITION_SECONDS

        current_name = palette_names[stage % len(palette_names)]
        next_name = palette_names[(stage + 1) % len(palette_names)]
        current_palette = self.cava_palette_cache[current_name]
        next_palette = self.cava_palette_cache[next_name]

        colors = [
            lerp_color(current_palette[index], next_palette[index], blend)
            for index in range(CAVA_BARS)
        ]
        self.cava_colors = colors

        # Blend each corresponding bar color, so the left-to-right gradient is
        # preserved throughout the entire transition rather than fading flat.
        for index, bar in enumerate(self.cava_bars):
            self.visualizer_canvas.itemconfig(
                bar,
                fill=colors[index],
            )

    def update_visualizer_button(self):
        if not self.visualizer_available:
            text = "VISUALIZER: CAVA MISSING"
        else:
            text = "VISUALIZER: ON" if self.visualizer_enabled else "VISUALIZER: OFF"
        self.visualizer_button.config(text=text)

    def toggle_visualizer(self):
        if self.visualizer_enabled:
            self.disable_visualizer()
        else:
            self.enable_visualizer()

    def enable_visualizer(self):
        cava = find_cava()
        if not cava:
            self.visualizer_available = False
            self.visualizer_enabled = False
            self.update_visualizer_button()
            return

        self.visualizer_available = True
        self.visualizer_enabled = True

        if self.color_scheme_name == DYNAMIC_COLOR_SCHEME:
            # Dynamic timing begins when the visualizer actually turns on, so
            # time spent in the true low-power OFF state does not advance it.
            self.dynamic_color_start = time.monotonic()
            self.dynamic_color_last_update = 0.0
            self.cava_colors = list(
                self.cava_palette_cache[DEFAULT_COLOR_SCHEME]
            )

        self.position_visualizer()

        if not self.start_cava(cava):
            self.visualizer_enabled = False
            self.visualizer_container.place_forget()
            self.update_visualizer_button()
            return

        self.start_activity_inhibit()
        self.start_visualizer_draw()
        self.update_visualizer_button()

    def disable_visualizer(self):
        self.visualizer_enabled = False
        self.stop_visualizer_draw()
        self.stop_cava()
        self.stop_activity_inhibit()
        self.visualizer_container.place_forget()
        self.cava_values = [0.0] * CAVA_BARS
        self.cava_display_values = [0.0] * CAVA_BARS
        self.update_visualizer_button()

    def start_activity_inhibit(self):
        """Prevent screen blanking and sleep while the visualizer is active."""
        self.stop_activity_inhibit()

        # xdg-screensaver handles the desktop screensaver / DPMS path and is
        # paired with an explicit resume using this exact window ID.
        xdg_screensaver = shutil.which("xdg-screensaver")
        if xdg_screensaver:
            try:
                self.update_idletasks()
                window_id = f"0x{self.winfo_id():x}"
                result = subprocess.run(
                    [xdg_screensaver, "suspend", window_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=False,
                )
                if result.returncode == 0:
                    self.screensaver_suspended = True
                    self.screensaver_window_id = window_id
            except Exception:
                self.screensaver_suspended = False
                self.screensaver_window_id = None

        # systemd-inhibit remains alive only while this child process exists.
        # Blocking idle + sleep prevents automatic suspend while visualizing.
        inhibitor = shutil.which("systemd-inhibit")
        sleeper = shutil.which("sleep")
        if inhibitor and sleeper:
            try:
                self.sleep_inhibit_process = subprocess.Popen(
                    [
                        inhibitor,
                        "--what=idle:sleep",
                        "--who=Spotify Light",
                        "--why=Audio visualizer is active",
                        "--mode=block",
                        sleeper,
                        "infinity",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                self.sleep_inhibit_process = None

    def stop_activity_inhibit(self):
        """Restore normal screensaver and sleep behavior."""
        process = self.sleep_inhibit_process
        self.sleep_inhibit_process = None

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

        if self.screensaver_suspended and self.screensaver_window_id:
            xdg_screensaver = shutil.which("xdg-screensaver")
            if xdg_screensaver:
                try:
                    subprocess.run(
                        [
                            xdg_screensaver,
                            "resume",
                            self.screensaver_window_id,
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=2,
                        check=False,
                    )
                except Exception:
                    pass

        self.screensaver_suspended = False
        self.screensaver_window_id = None

    def position_visualizer(self, event=None):
        if not self.visualizer_enabled:
            return

        try:
            root_y = self.winfo_rooty()
            source_bottom = (
                self.source_value.winfo_rooty()
                - root_y
                + self.source_value.winfo_height()
            )
            time_top = self.position_label.winfo_rooty() - root_y

            gap_top = source_bottom + 18
            gap_bottom = time_top - 18

            if gap_bottom <= gap_top:
                return

            center_y = gap_top + (gap_bottom - gap_top) * 0.52
            available_height = max(44, int(gap_bottom - gap_top))

            # Preserve the exact compact-window look, but let the CAVA area
            # grow vertically as the window gets taller. At the default 625 px
            # window height this remains 162 px; fullscreen can grow to 360 px.
            extra_window_height = max(0, self.winfo_height() - WINDOWED_HEIGHT)
            desired_height = VISUALIZER_HEIGHT + int(
                extra_window_height * VISUALIZER_GROWTH_PER_PX
            )
            desired_height = min(VISUALIZER_MAX_HEIGHT, desired_height)
            actual_height = min(desired_height, available_height)
            y = int(center_y - actual_height / 2)

            margin = 34
            width = max(2, self.winfo_width() - margin * 2)

            self.visualizer_container.place(
                x=margin,
                y=y,
                width=width,
                height=actual_height,
            )
            self.visualizer_container.lift()

        except tk.TclError:
            pass

    def write_cava_config(self):
        os.makedirs(CAVA_CONFIG_DIR, exist_ok=True)

        # PulseAudio mode also works through pipewire-pulse on modern Linux and
        # matches the monitor-source path Spotify Light previously used via parec.
        config = f"""[general]
framerate = 30
autosens = 2
bars = {CAVA_BARS}
scaling = linear
lower_cutoff_freq = 50
higher_cutoff_freq = 10000
sleep_timer = 1

[input]
method = pulse
source = auto

[output]
method = raw
channels = mono
mono_option = average
raw_target = /dev/stdout
data_format = ascii
ascii_max_range = {CAVA_ASCII_MAX}
bar_delimiter = 59
frame_delimiter = 10

[smoothing]
monstercat = 0
waves = 0
noise_reduction = 65
"""

        with open(CAVA_CONFIG_FILE, "w", encoding="utf-8") as handle:
            handle.write(config)

    def start_cava(self, executable):
        self.stop_cava()

        try:
            self.write_cava_config()
            self.cava_stop.clear()
            self.cava_process = subprocess.Popen(
                [executable, "-p", CAVA_CONFIG_FILE],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception:
            self.cava_process = None
            self.visualizer_available = False
            return False

        # Fail cleanly if CAVA cannot start with this build/config.
        time.sleep(0.08)
        if self.cava_process.poll() is not None:
            self.cava_process = None
            self.visualizer_available = False
            return False

        self.cava_thread = threading.Thread(
            target=self.cava_reader_loop,
            daemon=True,
        )
        self.cava_thread.start()
        return True

    def stop_cava(self):
        self.cava_stop.set()
        process = self.cava_process
        self.cava_process = None

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

    def cava_reader_loop(self):
        process = self.cava_process
        if not process or not process.stdout:
            return

        try:
            for line in process.stdout:
                if self.cava_stop.is_set():
                    break

                parts = [part for part in line.strip().split(";") if part]
                if not parts:
                    continue

                values = []
                for part in parts[:CAVA_BARS]:
                    try:
                        value = float(part) / CAVA_ASCII_MAX
                    except ValueError:
                        value = 0.0
                    values.append(max(0.0, min(1.0, value)))

                if len(values) < CAVA_BARS:
                    values.extend([0.0] * (CAVA_BARS - len(values)))

                self.cava_values = values

        except Exception:
            pass

    def start_visualizer_draw(self):
        if self.visualizer_draw_after is None and self.visualizer_enabled:
            self.draw_visualizer()

    def stop_visualizer_draw(self):
        if self.visualizer_draw_after is not None:
            try:
                self.after_cancel(self.visualizer_draw_after)
            except Exception:
                pass
            self.visualizer_draw_after = None

    def draw_visualizer(self):
        if not self.visualizer_enabled:
            self.visualizer_draw_after = None
            return

        self.update_dynamic_colors()

        width = max(2, self.visualizer_canvas.winfo_width())
        height = max(2, self.visualizer_canvas.winfo_height())
        slot = width / CAVA_BARS
        bar_width = max(2.0, slot * 0.62)
        max_height = max(8.0, height - 8.0)

        target = self.cava_values
        display = []

        for index in range(CAVA_BARS):
            current = self.cava_display_values[index]
            next_value = target[index]

            # CAVA already smooths the signal. This tiny UI interpolation only
            # prevents visible stepping between raw-output frames.
            if next_value >= current:
                value = current * 0.20 + next_value * 0.80
            else:
                value = current * 0.55 + next_value * 0.45
            display.append(value)

            x = slot * index + slot / 2
            bar_height = max(2.0, value * max_height)
            self.visualizer_canvas.coords(
                self.cava_bars[index],
                x - bar_width / 2,
                height - bar_height,
                x + bar_width / 2,
                height,
            )

        self.cava_display_values = display

        # If CAVA unexpectedly exits while ON, return to the true low-power OFF state.
        if self.cava_process and self.cava_process.poll() is not None:
            self.visualizer_enabled = False
            self.visualizer_available = False
            self.visualizer_container.place_forget()
            self.visualizer_draw_after = None
            self.update_visualizer_button()
            return

        self.visualizer_draw_after = self.after(
            VISUALIZER_REFRESH_MS,
            self.draw_visualizer,
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
        self.stop_visualizer_draw()
        self.stop_cava()
        self.stop_activity_inhibit()
        self.stop_spotifyd()
        self.destroy()


# ==================================================
# START
# ==================================================


if __name__ == "__main__":
    app = SpotifyLight()
    app.mainloop()

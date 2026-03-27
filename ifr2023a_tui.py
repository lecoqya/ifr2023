#!/usr/bin/env python3
"""
TUI control panel for the IFR 2023A signal generator.

Usage:
    uv run ifr2023a-tui [--port /dev/ttyUSB0]
    # or with conda koheron:
    conda run -n koheron python ifr2023a_tui.py [--port /dev/ttyUSB0]
"""

import argparse
import sys
from threading import Lock

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.validation import Number
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Log,
    RadioButton,
    RadioSet,
    Select,
    Static,
    Switch,
)

from ifr2023a import IFR2023A


# ── Helpers ───────────────────────────────────────────────────────────

def format_freq(hz: float) -> str:
    """Human-readable frequency string."""
    if hz >= 1e9:
        return f"{hz / 1e9:.9g} GHz"
    elif hz >= 1e6:
        return f"{hz / 1e6:.6g} MHz"
    elif hz >= 1e3:
        return f"{hz / 1e3:.3g} kHz"
    return f"{hz:.0f} Hz"


def parse_freq(text: str) -> float:
    """Parse a frequency string with optional unit suffix into Hz."""
    text = text.strip().upper()
    multipliers = {"GHZ": 1e9, "MHZ": 1e6, "KHZ": 1e3, "HZ": 1}
    for suffix, mult in multipliers.items():
        if text.endswith(suffix):
            return float(text[: -len(suffix)].strip()) * mult
    # No suffix: try as raw number (assume Hz if small, MHz if > 0.001)
    val = float(text)
    if val < 1000:
        # Likely MHz shorthand
        return val * 1e6
    return val


MOD_SOURCES = [
    ("Internal", "INT"),
    ("External AC", "EXTAC"),
    ("External ALC", "EXTALC"),
    ("External DC", "EXTDC"),
]

CLOCK_MODES = [
    ("Internal", "INT"),
    ("Int + 10 MHz Out", "INT10OUT"),
    ("Ext 10 MHz Direct", "EXT10DIR"),
    ("Ext 10 MHz Indirect", "EXT10IND"),
    ("Ext 1 MHz Indirect", "EXT1IND"),
]


# ── Application ───────────────────────────────────────────────────────

class IFR2023ATUI(App):
    """TUI for the IFR 2023A signal generator."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #main {
        height: 1fr;
        layout: horizontal;
    }

    #left-panel {
        width: 1fr;
        min-width: 40;
        padding: 0 1;
    }

    #right-panel {
        width: 1fr;
        min-width: 40;
        padding: 0 1;
    }

    .section {
        border: solid $primary;
        padding: 1;
        margin-bottom: 1;
    }

    .section-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    .field-row {
        layout: horizontal;
        height: 3;
        margin-bottom: 0;
    }

    .field-label {
        width: 14;
        height: 3;
        content-align: left middle;
    }

    .field-input {
        width: 1fr;
    }

    .field-unit {
        width: 8;
        height: 3;
        content-align: left middle;
        padding-left: 1;
    }

    .button-row {
        layout: horizontal;
        height: 3;
        margin-top: 1;
    }

    .button-row Button {
        margin-right: 1;
    }

    #rf-switch-row {
        layout: horizontal;
        height: 3;
        align: left middle;
    }

    #rf-switch-row Label {
        margin-right: 1;
    }

    #mod-switch-row {
        layout: horizontal;
        height: 3;
        align: left middle;
    }

    #mod-switch-row Label {
        margin-right: 1;
    }

    #status-bar {
        height: 3;
        border: solid $accent;
        padding: 0 1;
        margin: 0 1;
    }

    #log-panel {
        height: 10;
        border: solid $secondary;
        margin: 0 1;
    }

    RadioSet {
        height: auto;
    }

    Select {
        width: 1fr;
    }

    #raw-row {
        layout: horizontal;
        height: 3;
        margin: 0 1;
    }

    #raw-input {
        width: 1fr;
    }

    #raw-send {
        width: 10;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_status", "Refresh"),
    ]

    TITLE = "IFR 2023A Signal Generator"

    def __init__(self, port: str = "/dev/ttyUSB0", **kwargs):
        super().__init__(**kwargs)
        self.port = port
        self.gen: IFR2023A | None = None
        self._lock = Lock()

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="main"):
            # ── Left panel: Carrier + Reference ──
            with VerticalScroll(id="left-panel"):
                # Carrier section
                with Vertical(classes="section"):
                    yield Static("CARRIER", classes="section-title")

                    with Horizontal(classes="field-row"):
                        yield Label("Frequency:", classes="field-label")
                        yield Input(
                            placeholder="e.g. 100MHz, 1.5GHz, 9kHz",
                            id="freq-input",
                            classes="field-input",
                        )

                    with Horizontal(classes="field-row"):
                        yield Label("Amplitude:", classes="field-label")
                        yield Input(
                            placeholder="e.g. -20",
                            id="amp-input",
                            classes="field-input",
                            validators=[Number(failure_description="Must be a number")],
                        )
                        yield Label("dBm", classes="field-unit")

                    with Horizontal(id="rf-switch-row"):
                        yield Label("RF Output:")
                        yield Switch(id="rf-switch", value=False)

                # Reference section
                with Vertical(classes="section"):
                    yield Static("10 MHz REFERENCE", classes="section-title")
                    with RadioSet(id="clock-radio"):
                        for label, _ in CLOCK_MODES:
                            yield RadioButton(label)

            # ── Right panel: Modulation ──
            with VerticalScroll(id="right-panel"):
                with Vertical(classes="section"):
                    yield Static("MODULATION", classes="section-title")

                    with Horizontal(id="mod-switch-row"):
                        yield Label("Mod ON:")
                        yield Switch(id="mod-switch", value=False)

                    with Horizontal(classes="field-row"):
                        yield Label("Mode:", classes="field-label")
                        yield Select(
                            [("AM", "AM"), ("FM", "FM"), ("PM", "PM")],
                            value="FM",
                            id="mod-mode",
                        )

                    with Horizontal(classes="field-row"):
                        yield Label("Source:", classes="field-label")
                        yield Select(
                            MOD_SOURCES,
                            value="INT",
                            id="mod-source",
                        )

                    # AM depth
                    with Vertical(id="am-params"):
                        with Horizontal(classes="field-row"):
                            yield Label("AM Depth:", classes="field-label")
                            yield Input(
                                placeholder="e.g. 30",
                                id="am-depth",
                                classes="field-input",
                                validators=[Number(failure_description="Must be a number")],
                            )
                            yield Label("%", classes="field-unit")

                    # FM deviation
                    with Vertical(id="fm-params"):
                        with Horizontal(classes="field-row"):
                            yield Label("FM Dev:", classes="field-label")
                            yield Input(
                                placeholder="e.g. 25KHZ, 5000",
                                id="fm-dev",
                                classes="field-input",
                            )

                    # PM deviation
                    with Vertical(id="pm-params"):
                        with Horizontal(classes="field-row"):
                            yield Label("PM Dev:", classes="field-label")
                            yield Input(
                                placeholder="e.g. 1.5",
                                id="pm-dev",
                                classes="field-input",
                                validators=[Number(failure_description="Must be a number")],
                            )
                            yield Label("rad", classes="field-unit")

                    # Internal modulation frequency (for INT source)
                    with Horizontal(classes="field-row"):
                        yield Label("Mod Freq:", classes="field-label")
                        yield Input(
                            placeholder="e.g. 1KHZ, 400",
                            id="mod-freq",
                            classes="field-input",
                        )

                    with Horizontal(classes="button-row"):
                        yield Button("Apply Mod", id="apply-mod", variant="primary")

        # Raw command
        with Horizontal(id="raw-row"):
            yield Input(placeholder="Raw SCPI command (e.g. CFRQ?)", id="raw-input")
            yield Button("Send", id="raw-send", variant="default")

        # Status
        yield Static("Connecting...", id="status-bar")

        # Log
        yield Log(id="log-panel", highlight=True, max_lines=200)

        yield Footer()

    def on_mount(self) -> None:
        self._connect()

    @work(thread=True)
    def _connect(self) -> None:
        log = self.query_one("#log-panel", Log)
        status = self.query_one("#status-bar", Static)
        try:
            self.gen = IFR2023A(port=self.port)
            idn = self.gen.idn()
            self.call_from_thread(status.update, f"Connected: {idn}  |  Port: {self.port}")
            self.call_from_thread(log.write_line, f"Connected to {idn}")
            self._do_refresh()
        except Exception as e:
            self.call_from_thread(
                status.update, f"Connection FAILED: {e}  |  Port: {self.port}"
            )
            self.call_from_thread(log.write_line, f"ERROR: {e}")

    def _send(self, description: str, func, *args, **kwargs):
        """Execute a generator command safely in a worker thread."""
        log = self.query_one("#log-panel", Log)
        if self.gen is None:
            log.write_line("ERROR: Not connected")
            return None
        try:
            with self._lock:
                result = func(*args, **kwargs)
            msg = f"OK: {description}"
            if result is not None:
                msg += f" → {result}"
            log.write_line(msg)
            return result
        except Exception as e:
            log.write_line(f"ERROR ({description}): {e}")
            return None

    # ── Refresh / read back state ─────────────────────────────────────

    def action_refresh_status(self) -> None:
        self._refresh_worker()

    @work(thread=True)
    def _refresh_worker(self) -> None:
        self._do_refresh()

    def _do_refresh(self) -> None:
        """Read current instrument state and update widgets."""
        if self.gen is None:
            return
        log = self.query_one("#log-panel", Log)
        try:
            with self._lock:
                freq_resp = self.gen.get_frequency()   # ':CFRQ:VALUE 50000000.0;INC 1000.0;MODE FIXED'
                amp_resp = self.gen.get_amplitude()     # ':RFLV:UNITS DBM;TYPE PD;VALUE 4.0;INC 1.00;ON'
                clock_resp = self.gen.get_clock()       # ':FSTD EXT10DIR'
                mode_resp = self.gen.get_mode()         # ':MODE FM'
                mod_resp = self.gen.query("MOD?")       # ':MOD:OFF' or ':MOD:ON'
                am_resp = self.gen.query("AM?")         # ':AM:DEPTH 0.0;INT ;OFF ;INC 1.0'
                fm_resp = self.gen.query("FM?")         # ':FM:DEVN 0.0;INT ;OFF ;INC 1000.0'
                pm_resp = self.gen.query("PM?")         # ':PM:DEVN 0.00;INT ;OFF ;INC 0.10'

            self.call_from_thread(log.write_line,
                f"State: freq={freq_resp}  amp={amp_resp}  clock={clock_resp}  mode={mode_resp}  mod={mod_resp}")

            # ── Parse and update frequency ──
            # ':CFRQ:VALUE 50000000.0;...'
            try:
                freq_hz = float(freq_resp.split("VALUE")[1].split(";")[0].strip())
                self.call_from_thread(
                    setattr, self.query_one("#freq-input", Input), "value", format_freq(freq_hz))
            except (IndexError, ValueError):
                pass

            # ── Parse and update amplitude ──
            # ':RFLV:UNITS DBM;TYPE PD;VALUE 4.0;INC 1.00;ON'
            try:
                amp_dbm = float(amp_resp.split("VALUE")[1].split(";")[0].strip())
                self.call_from_thread(
                    setattr, self.query_one("#amp-input", Input), "value", str(amp_dbm))
            except (IndexError, ValueError):
                pass

            # ── RF ON/OFF ──
            rf_on = amp_resp.rstrip().endswith(";ON") or ";ON" in amp_resp
            self.call_from_thread(
                setattr, self.query_one("#rf-switch", Switch), "value", rf_on)

            # ── Clock reference ──
            # ':FSTD EXT10DIR'
            clock_mode = clock_resp.split()[-1] if clock_resp.strip() else "INT"
            clock_index = {mode: i for i, (_, mode) in enumerate(CLOCK_MODES)}
            idx = clock_index.get(clock_mode, 0)
            radio_set = self.query_one("#clock-radio", RadioSet)
            buttons = list(radio_set.query(RadioButton))
            if 0 <= idx < len(buttons):
                self.call_from_thread(
                    setattr, buttons[idx], "value", True)

            # ── Modulation ON/OFF ──
            mod_on = "ON" in mod_resp and "OFF" not in mod_resp
            self.call_from_thread(
                setattr, self.query_one("#mod-switch", Switch), "value", mod_on)

            # ── Modulation mode ──
            # ':MODE FM'
            mod_mode = mode_resp.split()[-1].strip() if mode_resp.strip() else "FM"
            if mod_mode in ("AM", "FM", "PM"):
                self.call_from_thread(
                    setattr, self.query_one("#mod-mode", Select), "value", mod_mode)

            # ── Parse AM ──
            # ':AM:DEPTH 30.0;INT ;ON ;INC 1.0'
            try:
                am_depth = float(am_resp.split("DEPTH")[1].split(";")[0].strip())
                self.call_from_thread(
                    setattr, self.query_one("#am-depth", Input), "value",
                    str(am_depth) if am_depth > 0 else "")
            except (IndexError, ValueError):
                pass

            # ── Parse FM ──
            # ':FM:DEVN 25000.0;INT ;ON ;INC 1000.0'
            try:
                fm_dev = float(fm_resp.split("DEVN")[1].split(";")[0].strip())
                self.call_from_thread(
                    setattr, self.query_one("#fm-dev", Input), "value",
                    format_freq(fm_dev) if fm_dev > 0 else "")
            except (IndexError, ValueError):
                pass

            # ── Parse PM ──
            # ':PM:DEVN 1.50;INT ;ON ;INC 0.10'
            try:
                pm_dev = float(pm_resp.split("DEVN")[1].split(";")[0].strip())
                self.call_from_thread(
                    setattr, self.query_one("#pm-dev", Input), "value",
                    str(pm_dev) if pm_dev > 0 else "")
            except (IndexError, ValueError):
                pass

            # ── Parse modulation source (from the active mode) ──
            src_resp = {"AM": am_resp, "FM": fm_resp, "PM": pm_resp}.get(mod_mode, fm_resp)
            # e.g. ':FM:DEVN 0.0;INT ;OFF ;INC 1000.0' — source is second field
            try:
                parts = [p.strip() for p in src_resp.split(";")]
                src_str = parts[1].strip() if len(parts) > 1 else "INT"
                # Map to our source values
                src_map = {"INT": "INT", "EXTAC": "EXTAC", "EXTALC": "EXTALC", "EXTDC": "EXTDC"}
                src_val = src_map.get(src_str, "INT")
                self.call_from_thread(
                    setattr, self.query_one("#mod-source", Select), "value", src_val)
            except (IndexError, ValueError):
                pass

        except Exception as e:
            self.call_from_thread(log.write_line, f"Refresh error: {e}")

    # ── Carrier controls ──────────────────────────────────────────────

    @on(Input.Submitted, "#freq-input")
    def handle_set_freq(self) -> None:
        self._set_freq_worker()

    @work(thread=True)
    def _set_freq_worker(self) -> None:
        inp = self.query_one("#freq-input", Input)
        try:
            freq_hz = parse_freq(inp.value)
        except (ValueError, IndexError):
            self.query_one("#log-panel", Log).write_line(
                f"Invalid frequency: {inp.value!r}"
            )
            return
        self._send(f"Set freq {format_freq(freq_hz)}", self.gen.set_frequency, freq_hz)

    @on(Input.Submitted, "#amp-input")
    def handle_set_amp(self) -> None:
        self._set_amp_worker()

    @work(thread=True)
    def _set_amp_worker(self) -> None:
        inp = self.query_one("#amp-input", Input)
        try:
            amp = float(inp.value)
        except ValueError:
            self.query_one("#log-panel", Log).write_line(
                f"Invalid amplitude: {inp.value!r}"
            )
            return
        self._send(f"Set amp {amp} dBm", self.gen.set_amplitude, amp)

    @on(Switch.Changed, "#rf-switch")
    def handle_rf_switch(self, event: Switch.Changed) -> None:
        self._rf_switch_worker(event.value)

    @work(thread=True)
    def _rf_switch_worker(self, on: bool) -> None:
        if on:
            self._send("RF ON", self.gen.rf_on)
        else:
            self._send("RF OFF", self.gen.rf_off)

    # ── Clock reference ───────────────────────────────────────────────

    @on(RadioSet.Changed, "#clock-radio")
    def handle_set_clock(self) -> None:
        self._set_clock_worker()

    @work(thread=True)
    def _set_clock_worker(self) -> None:
        radio_set = self.query_one("#clock-radio", RadioSet)
        idx = radio_set.pressed_index
        if idx < 0 or idx >= len(CLOCK_MODES):
            return
        label, mode = CLOCK_MODES[idx]
        methods = {
            "INT": self.gen.set_clock_internal,
            "INT10OUT": self.gen.set_clock_internal_10mhz_out,
            "EXT10DIR": self.gen.set_clock_external_10mhz_direct,
            "EXT10IND": self.gen.set_clock_external_10mhz_indirect,
            "EXT1IND": self.gen.set_clock_external_1mhz_indirect,
        }
        self._send(f"Clock → {label}", methods[mode])

    # ── Modulation ────────────────────────────────────────────────────

    @on(Switch.Changed, "#mod-switch")
    def handle_mod_switch(self, event: Switch.Changed) -> None:
        self._mod_switch_worker(event.value)

    @work(thread=True)
    def _mod_switch_worker(self, on: bool) -> None:
        if on:
            self._send("Modulation ON", self.gen.modulation_on)
        else:
            self._send("Modulation OFF", self.gen.modulation_off)

    @on(Button.Pressed, "#apply-mod")
    def handle_apply_mod(self) -> None:
        self._apply_mod_worker()

    @work(thread=True)
    def _apply_mod_worker(self) -> None:
        mode = self.query_one("#mod-mode", Select).value
        source = self.query_one("#mod-source", Select).value
        log = self.query_one("#log-panel", Log)

        if mode == Select.BLANK:
            self.call_from_thread(log.write_line, "Select a modulation mode first")
            return

        # Set mode
        self._send(f"Mode → {mode}", self.gen.set_mode, mode)

        # Set modulation frequency for internal source
        if source == "INT":
            mod_freq_str = self.query_one("#mod-freq", Input).value.strip()
            if mod_freq_str:
                try:
                    mod_freq_hz = parse_freq(mod_freq_str)
                    # Send raw command for internal mod frequency
                    if mod_freq_hz >= 1e3:
                        cmd = f"MOD:IFRQ {mod_freq_hz / 1e3}KHZ"
                    else:
                        cmd = f"MOD:IFRQ {mod_freq_hz}HZ"
                    self._send(f"Mod freq → {format_freq(mod_freq_hz)}", self.gen.send, cmd)
                except (ValueError, IndexError):
                    self.call_from_thread(log.write_line, f"Invalid mod freq: {mod_freq_str!r}")

        # Apply modulation parameters per mode
        if mode == "AM":
            depth_str = self.query_one("#am-depth", Input).value.strip()
            if not depth_str:
                self.call_from_thread(log.write_line, "AM depth not set")
                return
            try:
                depth = float(depth_str)
            except ValueError:
                self.call_from_thread(log.write_line, f"Invalid AM depth: {depth_str!r}")
                return
            self._send(f"AM {depth}% src={source}", self.gen.set_am_depth, depth, source)

        elif mode == "FM":
            dev_str = self.query_one("#fm-dev", Input).value.strip()
            if not dev_str:
                self.call_from_thread(log.write_line, "FM deviation not set")
                return
            try:
                dev_hz = parse_freq(dev_str)
            except (ValueError, IndexError):
                self.call_from_thread(log.write_line, f"Invalid FM deviation: {dev_str!r}")
                return
            self._send(f"FM dev {format_freq(dev_hz)} src={source}", self.gen.set_fm_deviation, dev_hz, source)

        elif mode == "PM":
            dev_str = self.query_one("#pm-dev", Input).value.strip()
            if not dev_str:
                self.call_from_thread(log.write_line, "PM deviation not set")
                return
            try:
                dev_rad = float(dev_str)
            except ValueError:
                self.call_from_thread(log.write_line, f"Invalid PM deviation: {dev_str!r}")
                return
            # PM command: PM:DEVN <value>RAD;<source>;ON
            cmd = f"PM:DEVN {dev_rad}RAD;{source};ON"
            self._send(f"PM dev {dev_rad} rad src={source}", self.gen.send, cmd)

    # ── Raw command ───────────────────────────────────────────────────

    @on(Button.Pressed, "#raw-send")
    def handle_raw_send(self) -> None:
        self._raw_send_worker()

    @on(Input.Submitted, "#raw-input")
    def handle_raw_submit(self) -> None:
        self._raw_send_worker()

    @work(thread=True)
    def _raw_send_worker(self) -> None:
        inp = self.query_one("#raw-input", Input)
        cmd = inp.value.strip()
        if not cmd:
            return
        log = self.query_one("#log-panel", Log)
        if cmd.endswith("?"):
            result = self._send(f"Query: {cmd}", self.gen.ask, cmd)
            if result is not None:
                self.call_from_thread(log.write_line, f"  ← {result}")
        else:
            self._send(f"Cmd: {cmd}", self.gen.send, cmd)

    # ── Cleanup ───────────────────────────────────────────────────────

    def on_unmount(self) -> None:
        if self.gen is not None:
            try:
                self.gen.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="IFR 2023A TUI Control Panel")
    parser.add_argument(
        "--port", default="/dev/ttyUSB0", help="Serial port (default: /dev/ttyUSB0)"
    )
    args = parser.parse_args()
    app = IFR2023ATUI(port=args.port)
    app.run()


if __name__ == "__main__":
    main()

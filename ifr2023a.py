#!/usr/bin/env python3
"""
IFR 2023A RF Signal Generator control via RS-232.

Requires: pyserial (pip install pyserial)

RS-232 settings on the instrument (Util 52):
  Baud: 9600, Data bits: 8, Stop bits: 1, Parity: None
  Hardware handshake: Off, XON/XOFF: Off

Cable: null-modem (crossed TXD/RXD, 9-pin female to 9-pin female)

Reference: Operating Manual 46882/373 - Chapter 5 (Remote Operation)
"""

import serial
import time


class IFR2023A:
    """Driver for the IFR/Aeroflex/Marconi 2023A signal generator."""

    # RS-232 control characters
    CTRL_REMOTE = b"\x01"   # ^A  go to remote
    CTRL_LOCAL = b"\x04"    # ^D  go to local
    CTRL_LOCKOUT = b"\x12"  # ^R  local lockout
    CTRL_UNLOCK = b"\x10"   # ^P  release local lockout

    # Frequency standard modes
    FSTD_INTERNAL = "INT"
    FSTD_EXT_10MHZ_DIRECT = "EXT10DIR"
    FSTD_EXT_1MHZ_INDIRECT = "EXT1IND"
    FSTD_EXT_10MHZ_INDIRECT = "EXT10IND"
    FSTD_INTERNAL_10MHZ_OUT = "INT10OUT"

    def __init__(self, port="/dev/ttyUSB0", baudrate=9600, timeout=2):
        """
        Open RS-232 connection to the IFR 2023A.

        Args:
            port: Serial port device (e.g. /dev/ttyUSB0, COM3)
            baudrate: Baud rate (max 9600 for this instrument)
            timeout: Read timeout in seconds
        """
        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            xonxoff=False,
            rtscts=False,
        )
        time.sleep(0.1)
        # Go to remote mode
        self.ser.write(self.CTRL_REMOTE)
        time.sleep(0.1)
        # Flush any pending data
        self.ser.reset_input_buffer()

    def close(self):
        """Return to local mode and close the connection."""
        self.ser.write(self.CTRL_LOCAL)
        time.sleep(0.05)
        self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ── Low-level communication ──────────────────────────────────────

    def write(self, command: str):
        """Send a command string (newline terminator added automatically)."""
        msg = command.strip() + "\n"
        self.ser.write(msg.encode("ascii"))
        time.sleep(0.05)

    def read(self) -> str:
        """Read a response line from the instrument."""
        response = self.ser.readline().decode("ascii").strip()
        return response

    def query(self, command: str) -> str:
        """Send a query and return the response."""
        self.write(command)
        return self.read()

    # ── Identification ───────────────────────────────────────────────

    def idn(self) -> str:
        """Query instrument identification (*IDN?)."""
        return self.query("*IDN?")

    def reset(self):
        """Reset to factory defaults (*RST)."""
        self.write("*RST")
        time.sleep(1)

    def options(self) -> str:
        """Query fitted options (*OPT?)."""
        return self.query("*OPT?")

    def self_test(self) -> str:
        """Run self-test (*TST?). Returns '0' if OK."""
        return self.query("*TST?")

    def clear_status(self):
        """Clear all status registers and error queue."""
        self.write("*CLS")

    def errors(self) -> str:
        """Read next error from the error queue."""
        return self.query("ERROR?")

    # ── Carrier Frequency (CFRQ) ─────────────────────────────────────

    def set_frequency(self, frequency_hz: float):
        """
        Set the carrier frequency.

        Args:
            frequency_hz: Frequency in Hz (e.g. 100e6 for 100 MHz)

        Examples:
            set_frequency(100e6)      # 100 MHz
            set_frequency(1.5e9)      # 1.5 GHz
            set_frequency(9000)       # 9 kHz
        """
        if frequency_hz >= 1e9:
            self.write(f"CFRQ:VALUE {frequency_hz / 1e9}GHZ")
        elif frequency_hz >= 1e6:
            self.write(f"CFRQ:VALUE {frequency_hz / 1e6}MHZ")
        elif frequency_hz >= 1e3:
            self.write(f"CFRQ:VALUE {frequency_hz / 1e3}KHZ")
        else:
            self.write(f"CFRQ:VALUE {frequency_hz}HZ")

    def get_frequency(self) -> str:
        """Query the current carrier frequency settings."""
        return self.query("CFRQ?")

    def set_frequency_step(self, step_hz: float):
        """Set the frequency step size for UP/DN commands."""
        if step_hz >= 1e6:
            self.write(f"CFRQ:INC {step_hz / 1e6}MHZ")
        elif step_hz >= 1e3:
            self.write(f"CFRQ:INC {step_hz / 1e3}KHZ")
        else:
            self.write(f"CFRQ:INC {step_hz}HZ")

    def frequency_up(self):
        """Increase frequency by one step."""
        self.write("CFRQ:UP")

    def frequency_down(self):
        """Decrease frequency by one step."""
        self.write("CFRQ:DN")

    # ── RF Level / Amplitude (RFLV) ──────────────────────────────────

    def set_amplitude(self, level_dbm: float):
        """
        Set the RF output level in dBm.

        Args:
            level_dbm: Power level in dBm (e.g. -20.0)
        """
        self.write(f"RFLV:VALUE {level_dbm}DBM")

    def set_amplitude_uv(self, level_uv: float):
        """Set the RF output level in microvolts (EMF)."""
        self.write(f"RFLV:TYPE EMF;VALUE {level_uv}UV")

    def set_amplitude_mv(self, level_mv: float):
        """Set the RF output level in millivolts (EMF)."""
        self.write(f"RFLV:TYPE EMF;VALUE {level_mv}MV")

    def get_amplitude(self) -> str:
        """Query the current RF level settings."""
        return self.query("RFLV?")

    def set_amplitude_step(self, step_db: float):
        """Set the RF level step size in dB."""
        self.write(f"RFLV:INC {step_db}DB")

    def amplitude_up(self):
        """Increase RF level by one step."""
        self.write("RFLV:UP")

    def amplitude_down(self):
        """Decrease RF level by one step."""
        self.write("RFLV:DN")

    def rf_on(self):
        """Turn the RF output ON."""
        self.write("RFLV:ON")

    def rf_off(self):
        """Turn the RF output OFF."""
        self.write("RFLV:OFF")

    def set_rf_limit(self, limit_dbm: float):
        """Set and enable the RF level limit."""
        self.write(f"RFLV:LIMIT:VALUE {limit_dbm}DBM;ENABLE")

    def disable_rf_limit(self):
        """Disable the RF level limit."""
        self.write("RFLV:LIMIT:DISABLE")

    # ── Frequency Standard / Clock Reference (FSTD) ──────────────────

    def set_clock_internal(self):
        """Use the internal 10 MHz TCXO."""
        self.write("FSTD INT")

    def set_clock_external_10mhz_direct(self):
        """Use an external 10 MHz reference, direct mode."""
        self.write("FSTD EXT10DIR")

    def set_clock_external_1mhz_indirect(self):
        """Use an external 1 MHz reference, indirect mode."""
        self.write("FSTD EXT1IND")

    def set_clock_external_10mhz_indirect(self):
        """Use an external 10 MHz reference, indirect mode."""
        self.write("FSTD EXT10IND")

    def set_clock_internal_10mhz_out(self):
        """Use internal clock and output 10 MHz on the rear panel."""
        self.write("FSTD INT10OUT")

    def get_clock(self) -> str:
        """Query the current frequency standard setting."""
        return self.query("FSTD?")

    # ── Modulation ───────────────────────────────────────────────────

    def modulation_on(self):
        """Turn modulation globally ON."""
        self.write("MOD:ON")

    def modulation_off(self):
        """Turn modulation globally OFF."""
        self.write("MOD:OFF")

    def set_mode(self, mode: str):
        """
        Set modulation mode.

        Args:
            mode: e.g. "AM", "FM", "PM", "AM,FM", "FM,PULSE"
        """
        self.write(f"MODE {mode}")

    def get_mode(self) -> str:
        """Query the current modulation mode."""
        return self.query("MODE?")

    # ── AM ────────────────────────────────────────────────────────────

    def set_am_depth(self, depth_pct: float, source: str = "INT"):
        """
        Set AM depth and source.

        Args:
            depth_pct: Depth in percent (e.g. 30.0)
            source: INT, EXTAC, EXTALC, or EXTDC
        """
        self.write(f"AM:DEPTH {depth_pct}PCT;{source};ON")

    # ── FM ────────────────────────────────────────────────────────────

    def set_fm_deviation(self, deviation_hz: float, source: str = "INT"):
        """
        Set FM deviation and source.

        Args:
            deviation_hz: Deviation in Hz (e.g. 25000 for 25 kHz)
            source: INT, EXTAC, EXTALC, or EXTDC
        """
        if deviation_hz >= 1e6:
            self.write(f"FM:DEVN {deviation_hz / 1e6}MHZ;{source};ON")
        elif deviation_hz >= 1e3:
            self.write(f"FM:DEVN {deviation_hz / 1e3}KHZ;{source};ON")
        else:
            self.write(f"FM:DEVN {deviation_hz}HZ;{source};ON")

    # ── Output Control ────────────────────────────────────────────────

    def output_enable(self):
        """Enable output (apply settings)."""
        self.write("OUTPUT:ENABLE")

    def output_disable(self):
        """Disable output (settings can be downloaded without effect)."""
        self.write("OUTPUT:DISABLE")

    # ── Memory ────────────────────────────────────────────────────────

    def store(self, slot: int):
        """Store current settings to memory (0-299)."""
        self.write(f"STO {slot}")

    def recall(self, slot: int):
        """Recall settings from memory (0-299)."""
        self.write(f"RCL {slot}")

    # ── Keyboard Lock ─────────────────────────────────────────────────

    def lock_keyboard(self):
        """Lock the front panel keyboard."""
        self.write("KLOCK")

    def unlock_keyboard(self):
        """Unlock the front panel keyboard."""
        self.write("KUNLOCK")

    # ── Impedance ─────────────────────────────────────────────────────

    def set_impedance_50(self):
        """Set output impedance to 50 ohm."""
        self.write("IMPEDANCE Z50R")

    def set_impedance_75(self):
        """Set output impedance to 75 ohm."""
        self.write("IMPEDANCE Z75R")

    # ── Sweep ─────────────────────────────────────────────────────────

    def setup_sweep(self, start_hz: float, stop_hz: float, step_time_ms: float = 100):
        """
        Configure and start a frequency sweep.

        Args:
            start_hz: Start frequency in Hz
            stop_hz: Stop frequency in Hz
            step_time_ms: Time per step in milliseconds
        """
        self.write(f"CFRQ:MODE FIXED")
        self.write(f"SWEEP:CFRQ:START {start_hz / 1e6}MHZ")
        self.write(f"SWEEP:CFRQ:STOP {stop_hz / 1e6}MHZ")
        self.write(f"SWEEP:CFRQ:TIME {step_time_ms}MS")
        self.write("SWEEP:MODE CONT")
        self.write("SWEEP:TYPE LIN")
        self.write("CFRQ:MODE SWEPT")

    def sweep_go(self):
        """Start sweep."""
        self.write("SWEEP:GO")

    def sweep_halt(self):
        """Pause sweep."""
        self.write("SWEEP:HALT")

    def sweep_reset(self):
        """Reset sweep to start value."""
        self.write("SWEEP:RESET")

    # ── Frequency Standard Calibration (DIAG:CAL:FST) ────────────────
    # Requires Level 2 unlock via front panel: UTIL 80, password 123456
    # Reference: Service Manual — UTIL 102 (Frequency Standard Calibration)

    def cal_fst_init(self):
        """Enter frequency standard calibration mode.

        The synthesizer switches to 1200 MHz output internally.
        RF output should be turned OFF before calling this to avoid
        feeding 1200 MHz into the ADC.
        """
        self.write("DIAG:CAL:FST:INIT")
        time.sleep(1.0)  # PLL reconfiguration

    def cal_fst_set_coarse_dac(self, value: int):
        """Set the coarse DAC for TCXO tuning.

        Args:
            value: DAC value 0-255
        """
        if not 0 <= value <= 255:
            raise ValueError(f"Coarse DAC value {value} out of range [0, 255]")
        self.write(f"DIAG:CAL:FST:CDAC {value}")

    def cal_fst_set_fine_dac(self, value: int):
        """Set the fine DAC for TCXO tuning.

        Args:
            value: DAC value 0-255
        """
        if not 0 <= value <= 255:
            raise ValueError(f"Fine DAC value {value} out of range [0, 255]")
        self.write(f"DIAG:CAL:FST:FDAC {value}")

    def cal_fst_get_coarse_dac(self) -> int:
        """Query current coarse DAC value.

        Returns:
            int: DAC value 0-255
        """
        return int(self.query("DIAG:CAL:FST:CDAC?"))

    def cal_fst_get_fine_dac(self) -> int:
        """Query current fine DAC value.

        Returns:
            int: DAC value 0-255
        """
        return int(self.query("DIAG:CAL:FST:FDAC?"))

    def cal_fst_save(self):
        """Save calibration to EEPROM and exit calibration mode.

        This writes the current CDAC/FDAC values to non-volatile memory
        and returns the synthesizer to normal operation.
        """
        self.write("DIAG:CAL:FST:SAVE")
        time.sleep(2.0)  # EEPROM write + PLL reconfiguration

    def cal_fst_quit(self):
        """Exit calibration mode without saving.

        Discards any DAC changes and returns to normal operation.
        """
        self.write("DIAG:CAL:FST:QUIT")
        time.sleep(1.0)  # PLL reconfiguration

    def cal_fst_get_date(self) -> str:
        """Query the date of last frequency standard calibration.

        Returns:
            str: Calibration date string
        """
        return self.query("DIAG:CAL:FST:DATE?")

    # ── Raw command passthrough ───────────────────────────────────────

    def send(self, cmd: str):
        """Send a raw command string."""
        self.write(cmd)

    def ask(self, cmd: str) -> str:
        """Send a raw query and return the response."""
        return self.query(cmd)


# ── Demo / CLI ────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="IFR 2023A Signal Generator Control",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s --port /dev/ttyUSB0 --idn
  %(prog)s --freq 100e6 --amp -20 --rf-on
  %(prog)s --freq 1.5e9 --amp -10 --clock EXT10DIR --rf-on
  %(prog)s --clock INT
  %(prog)s --raw "CFRQ?"
""",
    )
    parser.add_argument("--port", default="/dev/ttyUSB0", help="Serial port (default: /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate (default: 9600)")

    parser.add_argument("--idn", action="store_true", help="Query instrument identification")
    parser.add_argument("--reset", action="store_true", help="Reset to factory defaults")

    parser.add_argument("--freq", type=float, help="Set carrier frequency in Hz (e.g. 100e6)")
    parser.add_argument("--freq-query", action="store_true", help="Query current frequency")

    parser.add_argument("--amp", type=float, help="Set RF level in dBm (e.g. -20)")
    parser.add_argument("--amp-query", action="store_true", help="Query current RF level")

    parser.add_argument("--rf-on", action="store_true", help="Turn RF output ON")
    parser.add_argument("--rf-off", action="store_true", help="Turn RF output OFF")

    parser.add_argument(
        "--clock",
        choices=["INT", "EXT10DIR", "EXT1IND", "EXT10IND", "INT10OUT"],
        help="Set frequency standard (clock reference)",
    )
    parser.add_argument("--clock-query", action="store_true", help="Query clock reference")

    parser.add_argument("--raw", type=str, help="Send a raw GPIB command")
    parser.add_argument("--query", type=str, help="Send a raw query and print response")

    args = parser.parse_args()

    with IFR2023A(port=args.port, baudrate=args.baud) as gen:

        if args.idn:
            print(f"IDN: {gen.idn()}")

        if args.reset:
            gen.reset()
            print("Reset done.")

        if args.freq is not None:
            gen.set_frequency(args.freq)
            print(f"Frequency set to {args.freq} Hz")

        if args.freq_query:
            print(f"Frequency: {gen.get_frequency()}")

        if args.amp is not None:
            gen.set_amplitude(args.amp)
            print(f"Amplitude set to {args.amp} dBm")

        if args.amp_query:
            print(f"Amplitude: {gen.get_amplitude()}")

        if args.rf_on:
            gen.rf_on()
            print("RF output ON")

        if args.rf_off:
            gen.rf_off()
            print("RF output OFF")

        if args.clock:
            clock_methods = {
                "INT": gen.set_clock_internal,
                "EXT10DIR": gen.set_clock_external_10mhz_direct,
                "EXT1IND": gen.set_clock_external_1mhz_indirect,
                "EXT10IND": gen.set_clock_external_10mhz_indirect,
                "INT10OUT": gen.set_clock_internal_10mhz_out,
            }
            clock_methods[args.clock]()
            print(f"Clock set to {args.clock}")

        if args.clock_query:
            print(f"Clock: {gen.get_clock()}")

        if args.raw:
            gen.send(args.raw)
            print(f"Sent: {args.raw}")

        if args.query:
            resp = gen.ask(args.query)
            print(f"Response: {resp}")


if __name__ == "__main__":
    main()

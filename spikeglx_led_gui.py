"""
SpikeGLX + LED Control GUI
Hardware: PXIe-6363 via BNC-2110 (AO0 → LEDD1B) + SpikeGLX TCP CmdSrv
"""

import sys
import socket
import time
import math

# ── nidaqmx MUST be imported and pre-initialised before PyQt5. ──────────────
# ── Loading PyQt5 DLLs changes Win32/COM state in a way that makes the      ──
# ── first nidaqmx.Task() call crash with a null-pointer access violation.   ──
# ── Calling System.local() + Task() here (before any PyQt5 import) locks    ──
# ── the correct internal state so subsequent calls work normally.            ──
try:
    import nidaqmx
    import nidaqmx.system
    from nidaqmx.constants import TerminalConfiguration
    HAS_DAQ = True
    try:
        nidaqmx.system.System.local()
        _w = nidaqmx.Task()
        _w.close()
        del _w
    except Exception:
        pass
except ImportError:
    HAS_DAQ = False

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QPushButton, QLineEdit, QDoubleSpinBox,
    QSpinBox, QTextEdit, QSlider, QGridLayout, QSizePolicy, QScrollArea
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QColor, QPalette


def list_ao_channels() -> list[str]:
    """Return all AO physical channel names visible to NI-DAQmx."""
    if not HAS_DAQ:
        return []
    try:
        system = nidaqmx.system.System.local()
        channels = []
        for dev in system.devices:
            for ch in dev.ao_physical_chans:
                channels.append(ch.name)
        return channels
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SpikeGLX TCP Client
# ─────────────────────────────────────────────────────────────────────────────
class SpikeGLXClient:
    """Minimal SpikeGLX CmdSrv TCP client."""

    def __init__(self, host="localhost", port=4142):
        self.host = host
        self.port = port
        self.sock = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((self.host, self.port))

    def disconnect(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def send(self, cmd: str) -> str:
        """Send a command and return the response."""
        if not self.sock:
            raise ConnectionError("Not connected to SpikeGLX")
        msg = cmd.strip() + "\n"
        self.sock.sendall(msg.encode())
        response = b""
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            response += chunk
            if response.endswith(b"\n"):
                break
        return response.decode().strip()

    def is_connected(self):
        return self.sock is not None

    # ── Convenience commands ──────────────────────────────────────────────
    def get_version(self):
        return self.send("getVersion")

    def set_save_dir(self, path: str):
        return self.send(f"setDataDir 0 {path}")

    def set_filename(self, name: str):
        return self.send(f"setNextFileName 0 {name}")

    def start_run(self, run_name: str = "myRun"):
        return self.send(f"startRun {run_name}")

    def stop_run(self):
        return self.send("stopRun")

    def set_recording(self, enable: bool):
        val = 1 if enable else 0
        return self.send(f"setRecordEnab 0 {val}")


# ─────────────────────────────────────────────────────────────────────────────
# DAQ LED Controller (AO0 on PXIe-6363 via BNC-2110)
# ─────────────────────────────────────────────────────────────────────────────
class LEDController:
    """Controls LEDD1B via AO0 (0–5V) on PXIe-6363."""

    def __init__(self):
        self.task = None
        self._voltage = 0.0
        self.channel = "PXI1Slot6/ao0"

    def start(self, channel: str = None):
        if channel:
            self.channel = channel
        if not HAS_DAQ:
            return
        if self.task:
            self.stop()
        task = None
        try:
            task = nidaqmx.Task()
            task.ao_channels.add_ao_voltage_chan(
                self.channel,
                min_val=0.0,
                max_val=5.0
            )
            # Do NOT call task.start() here — for software-timed (on-demand) AO,
            # write() with auto_start=True handles task start implicitly.
            self.task = task
        except Exception as e:
            if task is not None:
                try:
                    task.close()
                except Exception:
                    pass
            raise RuntimeError(f"DAQ setup failed for {self.channel}: {e}")

    def set_voltage(self, v: float):
        self._voltage = max(0.0, min(5.0, v))
        if not self.task:
            raise RuntimeError("DAQ task not initialized — click 'Reconnect DAQ' first")
        try:
            self.task.write(self._voltage)  # auto_start=True by default
        except Exception:
            try:
                self.task.stop()
                self.task.close()
            except Exception:
                pass
            self.task = None
            raise

    def off(self):
        self.set_voltage(0.0)

    def stop(self):
        if self.task:
            try:
                self.task.write(0.0)
            except Exception:
                pass
            try:
                self.task.stop()
            except Exception:
                pass
            try:
                self.task.close()
            except Exception:
                pass
            self.task = None



# ─────────────────────────────────────────────────────────────────────────────
# DAQ Worker Thread
# ─────────────────────────────────────────────────────────────────────────────
class DAQWorker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool)

    def __init__(self, led: LEDController, channel: str):
        super().__init__()
        self.led = led
        self.channel = channel

    def run(self):
        import ctypes
        # NI-DAQmx requires COM MTA. The Qt main thread is STA (OleInitialize),
        # so task creation must happen on a background thread with its own MTA
        # apartment to avoid the COM reentrancy deadlock.
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # 0 = COINIT_MULTITHREADED
        try:
            self.led.stop()
            self.led.start(channel=self.channel)
            self.finished_signal.emit(True)
        except Exception as e:
            self.log_signal.emit(f"DAQ error: {e}")
            self.finished_signal.emit(False)
        finally:
            ctypes.windll.ole32.CoUninitialize()


# ─────────────────────────────────────────────────────────────────────────────
# Sinusoid Worker Thread
# ─────────────────────────────────────────────────────────────────────────────
class SinusoidWorker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal()

    def __init__(self, led: LEDController, freq: float, amplitude: float, offset: float):
        super().__init__()
        self.led = led
        self.freq = freq
        self.amplitude = amplitude
        self.offset = offset
        self._stop = False

    def run(self):
        dt = 0.001  # 1000 Hz update rate
        start = time.perf_counter()
        while not self._stop:
            t = time.perf_counter() - start
            v = self.offset + (self.amplitude / 2) * (1 + math.sin(2 * math.pi * self.freq * t))
            v = max(0.0, min(5.0, v))
            try:
                self.led.set_voltage(v)
            except Exception as e:
                self.log_signal.emit(f"Sinusoid error: {e}")
                break
            time.sleep(dt)
        self.finished_signal.emit()

    def stop(self):
        self._stop = True


# ─────────────────────────────────────────────────────────────────────────────
# Multi-Block Sinusoid Worker Thread
# ─────────────────────────────────────────────────────────────────────────────
class MultiBlockSinusoidWorker(QThread):
    log_signal = pyqtSignal(str)
    block_signal = pyqtSignal(int, int)   # (repeat_index, block_index)
    repeat_signal = pyqtSignal(int, int)  # (current_repeat, total_repeats)
    finished_signal = pyqtSignal(int)     # repeats_completed

    def __init__(self, led: LEDController, blocks: list,
                 repeats: int = 1, inter_repeat_gap: float = 0.0):
        super().__init__()
        self.led = led
        self.blocks = blocks   # list of {freq, amplitude, offset, duration}
        self.repeats = max(1, repeats)
        self.inter_repeat_gap = inter_repeat_gap
        self._stop = False

    def run(self):
        dt = 0.001
        completed = 0
        for rep in range(self.repeats):
            if self._stop:
                break
            self.repeat_signal.emit(rep + 1, self.repeats)
            for i, block in enumerate(self.blocks):
                if self._stop:
                    break
                self.block_signal.emit(rep, i)
                freq = block['freq']
                amp = block['amplitude']
                offset = block['offset']
                duration = block['duration']
                start = time.perf_counter()
                while not self._stop:
                    t = time.perf_counter() - start
                    if t >= duration:
                        break
                    v = offset + (amp / 2) * (1 + math.sin(2 * math.pi * freq * t))
                    v = max(0.0, min(5.0, v))
                    try:
                        self.led.set_voltage(v)
                    except Exception as e:
                        self.log_signal.emit(f"Rep {rep + 1} block {i + 1} error: {e}")
                        self._stop = True
                        break
                    time.sleep(dt)
            if not self._stop:
                completed += 1
            # Inter-repetition gap (LED off during gap)
            if not self._stop and self.inter_repeat_gap > 0 and rep < self.repeats - 1:
                try:
                    self.led.off()
                except Exception:
                    pass
                gap_end = time.perf_counter() + self.inter_repeat_gap
                while not self._stop and time.perf_counter() < gap_end:
                    time.sleep(0.01)
        self.finished_signal.emit(completed)

    def stop(self):
        self._stop = True


# ─────────────────────────────────────────────────────────────────────────────
# Multi-Block Ramp Worker Thread
# ─────────────────────────────────────────────────────────────────────────────
class MultiBlockRampWorker(QThread):
    log_signal     = pyqtSignal(str)
    block_signal   = pyqtSignal(int, int)   # (repeat_index, block_index)
    repeat_signal  = pyqtSignal(int, int)   # (current_repeat, total_repeats)
    voltage_signal = pyqtSignal(float)      # live voltage during ramp
    finished_signal = pyqtSignal(int)       # repeats_completed

    def __init__(self, led: LEDController, blocks: list,
                 repeats: int = 1, inter_repeat_gap: float = 0.0):
        super().__init__()
        self.led = led
        self.blocks = blocks          # [{start, peak, duration}, ...]
        self.repeats = max(1, repeats)
        self.inter_repeat_gap = inter_repeat_gap
        self._stop = False

    def run(self):
        dt = 0.001   # 1000 Hz
        completed = 0
        for rep in range(self.repeats):
            if self._stop:
                break
            self.repeat_signal.emit(rep + 1, self.repeats)
            for i, block in enumerate(self.blocks):
                if self._stop:
                    break
                self.block_signal.emit(rep, i)
                v_start   = max(0.0, min(5.0, block['start']))
                v_peak    = max(0.0, min(5.0, block['peak']))
                duration  = max(0.001, block['duration'])
                try:
                    self.led.set_voltage(v_start)
                except Exception as e:
                    self.log_signal.emit(f"Ramp error: {e}")
                    self._stop = True
                    break
                self.voltage_signal.emit(v_start)
                t0 = time.perf_counter()
                while not self._stop:
                    elapsed = time.perf_counter() - t0
                    if elapsed >= duration:
                        break
                    frac = elapsed / duration
                    v = v_start + (v_peak - v_start) * frac
                    v = max(0.0, min(5.0, v))
                    try:
                        self.led.set_voltage(v)
                    except Exception as e:
                        self.log_signal.emit(f"Ramp error: {e}")
                        self._stop = True
                        break
                    self.voltage_signal.emit(v)
                    time.sleep(dt)
                if not self._stop:
                    try:
                        self.led.set_voltage(v_peak)
                    except Exception as e:
                        self.log_signal.emit(f"Ramp error: {e}")
                    self.voltage_signal.emit(v_peak)
            # After every repeat: shut off (default ending)
            try:
                self.led.off()
            except Exception:
                pass
            if not self._stop:
                completed += 1
            # Inter-repeat gap (LED stays off)
            if not self._stop and self.inter_repeat_gap > 0 and rep < self.repeats - 1:
                gap_end = time.perf_counter() + self.inter_repeat_gap
                while not self._stop and time.perf_counter() < gap_end:
                    time.sleep(0.01)
        self.finished_signal.emit(completed)

    def stop(self):
        self._stop = True


# ─────────────────────────────────────────────────────────────────────────────
# Main GUI
# ─────────────────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.sglx = SpikeGLXClient()
        self.led = LEDController()
        self.ramp_worker = None
        self.sinusoid_worker = None
        self.multi_sin_worker = None
        self.daq_worker = None
        self.ramp_blocks: list[dict] = []
        self.sin_blocks: list[dict] = []

        self.setWindowTitle("SpikeGLX + LED Control")
        self.setMinimumWidth(620)
        self._build_ui()
        self._apply_theme()


    # ── UI Construction ───────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        root.addWidget(self._build_spikeglx_panel())
        root.addWidget(self._build_led_panel())
        root.addWidget(self._build_ramp_panel())
        root.addWidget(self._build_sinusoid_panel())
        root.addWidget(self._build_multi_sin_panel())
        root.addWidget(self._build_log_panel())

    def _build_spikeglx_panel(self):
        box = QGroupBox("SpikeGLX Connection")
        grid = QGridLayout(box)

        grid.addWidget(QLabel("Host:"), 0, 0)
        self.host_edit = QLineEdit("localhost")
        grid.addWidget(self.host_edit, 0, 1)

        grid.addWidget(QLabel("Port:"), 0, 2)
        self.port_edit = QLineEdit("4142")
        self.port_edit.setMaximumWidth(70)
        grid.addWidget(self.port_edit, 0, 3)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_sglx_connection)
        self.conn_dot = QLabel("●")
        self.conn_dot.setStyleSheet("color: #cf5e5e; font-size: 18px;")
        conn_row = QHBoxLayout()
        conn_row.addWidget(self.connect_btn)
        conn_row.addWidget(self.conn_dot)
        conn_row.setContentsMargins(0, 0, 0, 0)
        conn_widget = QWidget()
        conn_widget.setLayout(conn_row)
        grid.addWidget(conn_widget, 0, 4)

        grid.addWidget(QLabel("Save dir:"), 1, 0)
        self.savedir_edit = QLineEdit("C:/SpikeGLX_Data")
        grid.addWidget(self.savedir_edit, 1, 1, 1, 2)

        grid.addWidget(QLabel("Run name:"), 1, 3)
        self.runname_edit = QLineEdit("myRun")
        grid.addWidget(self.runname_edit, 1, 4)

        btn_row = QHBoxLayout()
        self.start_rec_btn = QPushButton("▶  Start Recording")
        self.start_rec_btn.clicked.connect(self._start_recording)
        self.stop_rec_btn = QPushButton("■  Stop Recording")
        self.stop_rec_btn.clicked.connect(self._stop_recording)
        self.stop_rec_btn.setEnabled(False)
        btn_row.addWidget(self.start_rec_btn)
        btn_row.addWidget(self.stop_rec_btn)
        grid.addLayout(btn_row, 2, 0, 1, 5)

        return box

    def _build_led_panel(self):
        box = QGroupBox("LED Control  —  AO0 → LEDD1B")
        layout = QVBoxLayout(box)

        # DAQ channel config
        chan_row = QHBoxLayout()
        chan_row.addWidget(QLabel("DAQ channel:"))
        self.daq_chan_edit = QLineEdit("PXI1Slot6/ao0")
        self.daq_chan_edit.setMaximumWidth(130)
        self.daq_chan_edit.setToolTip("NI device/channel (check NI MAX for device name)")
        chan_row.addWidget(self.daq_chan_edit)
        self.daq_reconnect_btn = QPushButton("Reconnect DAQ")
        self.daq_reconnect_btn.clicked.connect(self._daq_reconnect)
        chan_row.addWidget(self.daq_reconnect_btn)
        chan_row.addStretch()
        layout.addLayout(chan_row)

        row1 = QHBoxLayout()
        self.led_on_btn = QPushButton("LED ON")
        self.led_on_btn.clicked.connect(self._led_on)
        self.led_off_btn = QPushButton("LED OFF")
        self.led_off_btn.clicked.connect(self._led_off)
        row1.addWidget(self.led_on_btn)
        row1.addWidget(self.led_off_btn)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Intensity (0–5 V):"))
        self.intensity_slider = QSlider(Qt.Horizontal)
        self.intensity_slider.setRange(0, 500)
        self.intensity_slider.setValue(250)
        self.intensity_slider.valueChanged.connect(self._slider_changed)
        row2.addWidget(self.intensity_slider)
        self.intensity_label = QLabel("2.50 V")
        self.intensity_label.setMinimumWidth(55)
        row2.addWidget(self.intensity_label)
        layout.addLayout(row2)

        return box

    def _build_ramp_panel(self):
        box = QGroupBox("Multi-Block Ramp Sequence")
        layout = QVBoxLayout(box)

        # Column headers
        hdr = QHBoxLayout()
        for text, width in [("Block", 38), ("Start (V)", 80), ("Peak (V)", 80),
                             ("Duration (s)", 90)]:
            lbl = QLabel(text)
            lbl.setMinimumWidth(width)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #7eb8f7; font-size: 11px;")
            hdr.addWidget(lbl)
        hdr.addSpacing(34)
        layout.addLayout(hdr)

        # Scrollable block list
        self._ramp_block_container = QWidget()
        self._ramp_block_layout = QVBoxLayout(self._ramp_block_container)
        self._ramp_block_layout.setSpacing(4)
        self._ramp_block_layout.setContentsMargins(0, 0, 0, 0)
        self._ramp_block_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(self._ramp_block_container)
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(160)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(scroll)

        add_btn = QPushButton("+ Add Block")
        add_btn.clicked.connect(self._add_ramp_block)
        layout.addWidget(add_btn)

        # Repeat controls
        repeat_row = QHBoxLayout()
        repeat_row.addWidget(QLabel("Repeats:"))
        self.ramp_repeats_spin = QSpinBox()
        self.ramp_repeats_spin.setRange(1, 9999)
        self.ramp_repeats_spin.setValue(1)
        self.ramp_repeats_spin.setFixedWidth(70)
        self.ramp_repeats_spin.setToolTip("Number of times to run the full block sequence")
        repeat_row.addWidget(self.ramp_repeats_spin)
        repeat_row.addSpacing(16)
        repeat_row.addWidget(QLabel("Inter-repeat gap (s):"))
        self.ramp_gap_spin = QDoubleSpinBox()
        self.ramp_gap_spin.setRange(0.0, 3600.0)
        self.ramp_gap_spin.setValue(0.0)
        self.ramp_gap_spin.setSingleStep(0.5)
        self.ramp_gap_spin.setDecimals(1)
        self.ramp_gap_spin.setFixedWidth(80)
        self.ramp_gap_spin.setToolTip("LED-off pause between repetitions (0 = no gap)")
        repeat_row.addWidget(self.ramp_gap_spin)
        repeat_row.addStretch()
        layout.addLayout(repeat_row)

        btn_row = QHBoxLayout()
        self.run_ramp_btn = QPushButton("▶  Run Ramp Sequence")
        self.run_ramp_btn.clicked.connect(self._run_ramp)
        self.stop_ramp_btn = QPushButton("■  Stop Ramp")
        self.stop_ramp_btn.clicked.connect(self._stop_ramp)
        self.stop_ramp_btn.setEnabled(False)
        btn_row.addWidget(self.run_ramp_btn)
        btn_row.addWidget(self.stop_ramp_btn)
        layout.addLayout(btn_row)

        # Default block
        self._add_ramp_block()

        return box

    def _add_ramp_block(self):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        lbl = QLabel(f"#{len(self.ramp_blocks) + 1}")
        lbl.setMinimumWidth(38)
        lbl.setAlignment(Qt.AlignCenter)
        row_layout.addWidget(lbl)

        def _spin(lo, hi, val, step, dec, w=80):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setValue(val)
            s.setSingleStep(step)
            s.setDecimals(dec)
            s.setFixedWidth(w)
            return s

        start_spin    = _spin(0.0,   5.0,  0.0, 0.1, 2)
        peak_spin     = _spin(0.0,   5.0,  5.0, 0.1, 2)
        dur_spin      = _spin(0.1, 3600.0, 5.0, 0.5, 1, 90)

        for w in (start_spin, peak_spin, dur_spin):
            row_layout.addWidget(w)

        rm_btn = QPushButton("✕")
        rm_btn.setFixedWidth(30)
        rm_btn.setToolTip("Remove block")
        rm_btn.clicked.connect(lambda _=False, r=row: self._remove_ramp_block(r))
        row_layout.addWidget(rm_btn)

        self._ramp_block_layout.insertWidget(self._ramp_block_layout.count() - 1, row)
        self.ramp_blocks.append({
            'widget': row, 'label': lbl,
            'start': start_spin, 'peak': peak_spin, 'dur': dur_spin,
        })

    def _remove_ramp_block(self, row: QWidget):
        self.ramp_blocks = [b for b in self.ramp_blocks if b['widget'] is not row]
        row.setParent(None)
        row.deleteLater()
        for i, b in enumerate(self.ramp_blocks):
            b['label'].setText(f"#{i + 1}")

    def _build_sinusoid_panel(self):
        box = QGroupBox("Sinusoid Waveform")
        grid = QGridLayout(box)

        grid.addWidget(QLabel("Frequency (Hz):"), 0, 0)
        self.sin_freq_spin = QDoubleSpinBox()
        self.sin_freq_spin.setRange(0.01, 50.0)
        self.sin_freq_spin.setValue(1.0)
        self.sin_freq_spin.setSingleStep(0.1)
        self.sin_freq_spin.setDecimals(2)
        grid.addWidget(self.sin_freq_spin, 0, 1)

        grid.addWidget(QLabel("Amplitude (V):"), 0, 2)
        self.sin_amp_spin = QDoubleSpinBox()
        self.sin_amp_spin.setRange(0.0, 5.0)
        self.sin_amp_spin.setValue(2.5)
        self.sin_amp_spin.setSingleStep(0.1)
        self.sin_amp_spin.setDecimals(2)
        grid.addWidget(self.sin_amp_spin, 0, 3)

        grid.addWidget(QLabel("Offset / trough (V):"), 1, 0)
        self.sin_offset_spin = QDoubleSpinBox()
        self.sin_offset_spin.setRange(0.0, 5.0)
        self.sin_offset_spin.setValue(0.0)
        self.sin_offset_spin.setSingleStep(0.1)
        self.sin_offset_spin.setDecimals(2)
        grid.addWidget(self.sin_offset_spin, 1, 1)

        self.sin_range_label = QLabel("Peak: 2.50 V  |  Trough: 0.00 V")
        self.sin_range_label.setStyleSheet("color: #8fb8d8;")
        grid.addWidget(self.sin_range_label, 1, 2, 1, 2)

        self.sin_freq_spin.valueChanged.connect(self._update_sin_preview)
        self.sin_amp_spin.valueChanged.connect(self._update_sin_preview)
        self.sin_offset_spin.valueChanged.connect(self._update_sin_preview)

        btn_row = QHBoxLayout()
        self.run_sin_btn = QPushButton("▶  Start Sinusoid")
        self.run_sin_btn.clicked.connect(self._run_sinusoid)
        self.stop_sin_btn = QPushButton("■  Stop Sinusoid")
        self.stop_sin_btn.clicked.connect(self._stop_sinusoid)
        self.stop_sin_btn.setEnabled(False)
        btn_row.addWidget(self.run_sin_btn)
        btn_row.addWidget(self.stop_sin_btn)
        grid.addLayout(btn_row, 2, 0, 1, 4)

        return box

    def _build_multi_sin_panel(self):
        box = QGroupBox("Multi-Block Sinusoid Sequence")
        layout = QVBoxLayout(box)

        # Column header
        hdr = QHBoxLayout()
        for text, width in [("Block", 38), ("Freq (Hz)", 82), ("Amp (V)", 72),
                             ("Offset (V)", 72), ("Duration (s)", 82)]:
            lbl = QLabel(text)
            lbl.setMinimumWidth(width)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #7eb8f7; font-size: 11px;")
            hdr.addWidget(lbl)
        hdr.addSpacing(34)   # placeholder for remove buttons
        layout.addLayout(hdr)

        # Scrollable block list
        self._sin_block_container = QWidget()
        self._sin_block_layout = QVBoxLayout(self._sin_block_container)
        self._sin_block_layout.setSpacing(4)
        self._sin_block_layout.setContentsMargins(0, 0, 0, 0)
        self._sin_block_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(self._sin_block_container)
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(160)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(scroll)

        add_btn = QPushButton("+ Add Block")
        add_btn.clicked.connect(self._add_sin_block)
        layout.addWidget(add_btn)

        # Repeat controls
        repeat_row = QHBoxLayout()
        repeat_row.addWidget(QLabel("Repeats:"))
        self.multi_sin_repeats_spin = QSpinBox()
        self.multi_sin_repeats_spin.setRange(1, 9999)
        self.multi_sin_repeats_spin.setValue(1)
        self.multi_sin_repeats_spin.setFixedWidth(70)
        self.multi_sin_repeats_spin.setToolTip("Number of times to run the full block sequence")
        repeat_row.addWidget(self.multi_sin_repeats_spin)
        repeat_row.addSpacing(16)
        repeat_row.addWidget(QLabel("Inter-repeat gap (s):"))
        self.multi_sin_gap_spin = QDoubleSpinBox()
        self.multi_sin_gap_spin.setRange(0.0, 3600.0)
        self.multi_sin_gap_spin.setValue(0.0)
        self.multi_sin_gap_spin.setSingleStep(0.5)
        self.multi_sin_gap_spin.setDecimals(1)
        self.multi_sin_gap_spin.setFixedWidth(80)
        self.multi_sin_gap_spin.setToolTip("LED-off pause between repetitions (0 = no gap)")
        repeat_row.addWidget(self.multi_sin_gap_spin)
        repeat_row.addStretch()
        layout.addLayout(repeat_row)

        btn_row = QHBoxLayout()
        self.run_multi_sin_btn = QPushButton("▶  Run Sequence")
        self.run_multi_sin_btn.clicked.connect(self._run_multi_sin)
        self.stop_multi_sin_btn = QPushButton("■  Stop Sequence")
        self.stop_multi_sin_btn.clicked.connect(self._stop_multi_sin)
        self.stop_multi_sin_btn.setEnabled(False)
        btn_row.addWidget(self.run_multi_sin_btn)
        btn_row.addWidget(self.stop_multi_sin_btn)
        layout.addLayout(btn_row)

        # Start with two default blocks
        self._add_sin_block()
        self._add_sin_block()

        return box

    def _add_sin_block(self):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        lbl = QLabel(f"#{len(self.sin_blocks) + 1}")
        lbl.setMinimumWidth(38)
        lbl.setAlignment(Qt.AlignCenter)
        row_layout.addWidget(lbl)

        def _spin(lo, hi, val, step, dec, w=82):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setValue(val)
            s.setSingleStep(step)
            s.setDecimals(dec)
            s.setFixedWidth(w)
            return s

        freq_spin   = _spin(0.01, 50.0,  1.0, 0.1, 2)
        amp_spin    = _spin(0.0,   5.0,  2.5, 0.1, 2, 72)
        offset_spin = _spin(0.0,   5.0,  0.0, 0.1, 2, 72)
        dur_spin    = _spin(0.1, 3600.0, 5.0, 0.5, 1)

        for w in (freq_spin, amp_spin, offset_spin, dur_spin):
            row_layout.addWidget(w)

        rm_btn = QPushButton("✕")
        rm_btn.setFixedWidth(30)
        rm_btn.setToolTip("Remove block")
        rm_btn.clicked.connect(lambda _=False, r=row: self._remove_sin_block(r))
        row_layout.addWidget(rm_btn)

        # Insert before the trailing stretch
        self._sin_block_layout.insertWidget(self._sin_block_layout.count() - 1, row)
        self.sin_blocks.append({
            'widget': row, 'label': lbl,
            'freq': freq_spin, 'amp': amp_spin,
            'offset': offset_spin, 'dur': dur_spin,
        })

    def _remove_sin_block(self, row: QWidget):
        self.sin_blocks = [b for b in self.sin_blocks if b['widget'] is not row]
        row.setParent(None)
        row.deleteLater()
        for i, b in enumerate(self.sin_blocks):
            b['label'].setText(f"#{i + 1}")

    def _run_multi_sin(self):
        if not self.sin_blocks:
            self._log("No blocks defined — add at least one block")
            return
        blocks = [
            {'freq': b['freq'].value(), 'amplitude': b['amp'].value(),
             'offset': b['offset'].value(), 'duration': b['dur'].value()}
            for b in self.sin_blocks
        ]
        repeats = self.multi_sin_repeats_spin.value()
        gap = self.multi_sin_gap_spin.value()
        self.multi_sin_worker = MultiBlockSinusoidWorker(
            self.led, blocks, repeats=repeats, inter_repeat_gap=gap
        )
        self.multi_sin_worker.log_signal.connect(self._log)
        self.multi_sin_worker.repeat_signal.connect(self._multi_sin_repeat_started)
        self.multi_sin_worker.block_signal.connect(lambda _, idx: self._multi_sin_block_started(idx))
        self.multi_sin_worker.finished_signal.connect(self._multi_sin_done)
        self.run_multi_sin_btn.setEnabled(False)
        self.stop_multi_sin_btn.setEnabled(True)
        block_total = sum(b['duration'] for b in blocks)
        gap_total = gap * (repeats - 1)
        self._log(
            f"Sequence started — {len(blocks)} blocks × {repeats} repeat(s), "
            f"{block_total:.1f}s/repeat"
            + (f", {gap:.1f}s gap, {block_total * repeats + gap_total:.1f}s total" if repeats > 1 else f", {block_total:.1f}s total")
        )
        self.multi_sin_worker.start()

    def _stop_multi_sin(self):
        if self.multi_sin_worker:
            self.multi_sin_worker.stop()
            self._log("Sequence stop requested")

    def _multi_sin_repeat_started(self, rep: int, total: int):
        if total > 1:
            self._log(f"--- Repetition {rep}/{total} ---")

    def _multi_sin_block_started(self, idx: int):
        b = self.sin_blocks[idx]
        self._log(f"  Block {idx + 1}/{len(self.sin_blocks)} — "
                  f"{b['freq'].value():.2f} Hz, amp {b['amp'].value():.2f} V, "
                  f"offset {b['offset'].value():.2f} V, {b['dur'].value():.1f}s")

    def _multi_sin_done(self, completed: int):
        self.led.off()
        self.run_multi_sin_btn.setEnabled(True)
        self.stop_multi_sin_btn.setEnabled(False)
        total = self.multi_sin_repeats_spin.value()
        if completed < total:
            self._log(f"Sequence stopped — {completed}/{total} repetition(s) completed")
        else:
            self._log(f"Sequence complete — {completed} repetition(s)")

    def _build_log_panel(self):
        box = QGroupBox("Log")
        layout = QVBoxLayout(box)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(140)
        self.log_box.setFont(QFont("Courier New", 9))
        layout.addWidget(self.log_box)
        return box

    # ── Theme ─────────────────────────────────────────────────────────────
    def _apply_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1a1d23;
                color: #e0e4ef;
                font-family: 'Segoe UI', sans-serif;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #2e3340;
                border-radius: 6px;
                margin-top: 10px;
                padding: 10px;
                font-weight: bold;
                color: #7eb8f7;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QPushButton {
                background-color: #2a2f3d;
                border: 1px solid #3a4155;
                border-radius: 5px;
                padding: 6px 14px;
                color: #e0e4ef;
            }
            QPushButton:hover { background-color: #323849; }
            QPushButton:pressed { background-color: #1e2230; }
            QPushButton:disabled { color: #555; border-color: #2a2f3d; }
            QPushButton#start { background-color: #1a3a2a; border-color: #2d7a4f; color: #5ecf8a; }
            QPushButton#stop  { background-color: #3a1a1a; border-color: #7a2d2d; color: #cf5e5e; }
            QLineEdit, QDoubleSpinBox, QSpinBox {
                background-color: #22262f;
                border: 1px solid #3a4155;
                border-radius: 4px;
                padding: 4px 8px;
                color: #e0e4ef;
            }
            QTextEdit {
                background-color: #12141a;
                border: 1px solid #2e3340;
                border-radius: 4px;
                color: #8fbf6f;
            }
            QSlider::groove:horizontal {
                height: 4px;
                background: #2e3340;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #7eb8f7;
                width: 14px; height: 14px;
                margin: -5px 0;
                border-radius: 7px;
            }
            QSlider::sub-page:horizontal { background: #7eb8f7; border-radius: 2px; }
        """)
        # Color start/stop buttons
        self.start_rec_btn.setObjectName("start")
        self.stop_rec_btn.setObjectName("stop")
        self.run_ramp_btn.setObjectName("start")
        self.stop_ramp_btn.setObjectName("stop")
        self.run_sin_btn.setObjectName("start")
        self.stop_sin_btn.setObjectName("stop")
        self.run_multi_sin_btn.setObjectName("start")
        self.stop_multi_sin_btn.setObjectName("stop")

    # ── SpikeGLX Actions ──────────────────────────────────────────────────
    def _toggle_sglx_connection(self):
        if self.sglx.is_connected():
            self.sglx.disconnect()
            self.connect_btn.setText("Connect")
            self.conn_dot.setStyleSheet("color: #cf5e5e; font-size: 18px;")
            self._log("Disconnected from SpikeGLX")
        else:
            try:
                self.sglx.host = self.host_edit.text()
                self.sglx.port = int(self.port_edit.text())
                self.sglx.connect()
                ver = self.sglx.get_version()
                self.connect_btn.setText("Disconnect")
                self.conn_dot.setStyleSheet("color: #5ecf8a; font-size: 18px;")
                self._log(f"Connected to SpikeGLX — version: {ver}")
            except Exception as e:
                self._log(f"Connection failed: {e}")

    def _start_recording(self):
        if not self.sglx.is_connected():
            self._log("Not connected to SpikeGLX")
            return
        try:
            self.sglx.set_save_dir(self.savedir_edit.text())
            r = self.sglx.start_run(self.runname_edit.text())
            self.sglx.set_recording(True)
            self._log(f"Recording started — {r}")
            self.start_rec_btn.setEnabled(False)
            self.stop_rec_btn.setEnabled(True)
        except Exception as e:
            self._log(f"Start recording error: {e}")

    def _stop_recording(self):
        if not self.sglx.is_connected():
            return
        try:
            self.sglx.set_recording(False)
            self.sglx.stop_run()
            self._log("Recording stopped")
            self.start_rec_btn.setEnabled(True)
            self.stop_rec_btn.setEnabled(False)
        except Exception as e:
            self._log(f"Stop recording error: {e}")

    # ── LED Actions ───────────────────────────────────────────────────────
    def _log_daq_status(self):
        """Called via QTimer after exec_() starts — safe to call System.local() here."""
        ch = self.daq_chan_edit.text()
        if self.led.task:
            self._log(f"DAQ ready — {ch}")
        else:
            self._log(f"DAQ not connected — check channel name and click 'Reconnect DAQ'")
        channels = list_ao_channels()
        if channels:
            self._log(f"Available AO channels: {', '.join(channels)}")

    def _enumerate_and_init_daq(self):
        """Used only by Reconnect DAQ button."""
        channels = list_ao_channels()
        if not channels:
            self._log("No NI-DAQ AO channels found — check NI MAX and driver installation")
            return
        self._log(f"Available AO channels: {', '.join(channels)}")
        current = self.daq_chan_edit.text().strip()
        if current not in channels:
            self.daq_chan_edit.setText(channels[0])
            self._log(f"Channel '{current}' not found — defaulting to {channels[0]}")
        self._daq_reconnect()

    def _daq_reconnect(self):
        channel = self.daq_chan_edit.text().strip()
        if self.daq_worker and self.daq_worker.isRunning():
            return
        self.daq_worker = DAQWorker(self.led, channel)
        self.daq_worker.log_signal.connect(self._log)
        self.daq_worker.finished_signal.connect(self._daq_finished)
        self.daq_worker.start()

    def _daq_finished(self, success: bool):
        if success:
            self._log(f"DAQ task started — {self.daq_chan_edit.text()}")

    def _led_on(self):
        try:
            v = self.intensity_slider.value() / 100.0
            self.led.set_voltage(v)
            self._log(f"LED ON  →  {v:.2f} V")
        except Exception as e:
            self._log(f"LED ON error: {e}")

    def _led_off(self):
        try:
            self.led.off()
            self._log("LED OFF  →  0.00 V")
        except Exception as e:
            self._log(f"LED OFF error: {e}")

    def _slider_changed(self, val):
        v = val / 100.0
        self.intensity_label.setText(f"{v:.2f} V")

    # ── Ramp Actions ──────────────────────────────────────────────────────
    def _run_ramp(self):
        if not self.ramp_blocks:
            self._log("No ramp blocks defined — add at least one block")
            return
        blocks = [
            {'start': b['start'].value(), 'peak': b['peak'].value(),
             'duration': b['dur'].value()}
            for b in self.ramp_blocks
        ]
        repeats = self.ramp_repeats_spin.value()
        gap = self.ramp_gap_spin.value()
        self.ramp_worker = MultiBlockRampWorker(
            self.led, blocks, repeats=repeats, inter_repeat_gap=gap
        )
        self.ramp_worker.log_signal.connect(self._log)
        self.ramp_worker.repeat_signal.connect(self._ramp_repeat_started)
        self.ramp_worker.block_signal.connect(lambda _, idx: self._ramp_block_started(idx))
        self.ramp_worker.finished_signal.connect(self._ramp_done)
        self.run_ramp_btn.setEnabled(False)
        self.stop_ramp_btn.setEnabled(True)
        block_total = sum(b['duration'] for b in blocks)
        gap_total = gap * (repeats - 1)
        self._log(
            f"Ramp sequence started — {len(blocks)} block(s) × {repeats} repeat(s), "
            f"{block_total:.1f}s/repeat"
            + (f", {gap:.1f}s gap, {block_total * repeats + gap_total:.1f}s total"
               if repeats > 1 else f", {block_total:.1f}s total")
        )
        self.ramp_worker.start()

    def _stop_ramp(self):
        if self.ramp_worker:
            self.ramp_worker.stop()
            self._log("Ramp stop requested")

    def _ramp_repeat_started(self, rep: int, total: int):
        if total > 1:
            self._log(f"--- Ramp repetition {rep}/{total} ---")

    def _ramp_block_started(self, idx: int):
        b = self.ramp_blocks[idx]
        self._log(f"  Block {idx + 1}/{len(self.ramp_blocks)} — "
                  f"{b['start'].value():.2f} V → {b['peak'].value():.2f} V "
                  f"over {b['dur'].value():.1f}s")

    def _ramp_done(self, completed: int):
        self.run_ramp_btn.setEnabled(True)
        self.stop_ramp_btn.setEnabled(False)
        total = self.ramp_repeats_spin.value()
        if completed < total:
            self._log(f"Ramp stopped — {completed}/{total} repetition(s) completed, LED off")
        else:
            self._log(f"Ramp sequence complete — {completed} repetition(s), LED off")

    # ── Sinusoid Actions ──────────────────────────────────────────────────
    def _update_sin_preview(self):
        offset = self.sin_offset_spin.value()
        amp = self.sin_amp_spin.value()
        peak = min(5.0, offset + amp)
        warn = "  ⚠ clipped to 5 V" if offset + amp > 5.0 else ""
        self.sin_range_label.setText(f"Peak: {peak:.2f} V  |  Trough: {offset:.2f} V{warn}")

    def _run_sinusoid(self):
        freq = self.sin_freq_spin.value()
        amp = self.sin_amp_spin.value()
        offset = self.sin_offset_spin.value()
        self.sinusoid_worker = SinusoidWorker(self.led, freq, amp, offset)
        self.sinusoid_worker.log_signal.connect(self._log)
        self.sinusoid_worker.finished_signal.connect(self._sinusoid_done)
        self.run_sin_btn.setEnabled(False)
        self.stop_sin_btn.setEnabled(True)
        self._log(f"Sinusoid started — {freq:.2f} Hz, amp {amp:.2f} V, trough {offset:.2f} V")
        self.sinusoid_worker.start()

    def _stop_sinusoid(self):
        if self.sinusoid_worker:
            self.sinusoid_worker.stop()
            self._log("Sinusoid stop requested")

    def _sinusoid_done(self):
        self.led.off()
        self.run_sin_btn.setEnabled(True)
        self.stop_sin_btn.setEnabled(False)
        self._log("Sinusoid stopped")

    # ── Log ───────────────────────────────────────────────────────────────
    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_box.append(f"[{ts}]  {msg}")

    # ── Cleanup ───────────────────────────────────────────────────────────
    def closeEvent(self, event):
        if self.daq_worker:
            self.daq_worker.wait()
        if self.ramp_worker:
            self.ramp_worker.stop()
        if self.sinusoid_worker:
            self.sinusoid_worker.stop()
        if self.multi_sin_worker:
            self.multi_sin_worker.stop()
        self.led.stop()
        self.sglx.disconnect()
        event.accept()


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # NI-DAQmx must be fully initialised (System.local + Task) BEFORE
    # QApplication calls OleInitialize. After OleInitialize, any first-time
    # Task() call crashes with a null-pointer access violation because
    # NI-DAQmx's internal COM state is unset. Pre-running both calls here
    # locks in the correct state; subsequent Task() calls (led.start below,
    # and Reconnect DAQ) reuse the already-initialised library.
    _pre_channels: list = []
    if HAS_DAQ:
        _pre_channels = list_ao_channels()      # System.local() — before OleInitialize
        try:
            _t = nidaqmx.Task()                 # warm up task subsystem
            _t.close()
        except Exception as e:
            print(f"NI-DAQmx pre-warmup error: {e}", flush=True)

    app = QApplication(sys.argv)
    win = MainWindow()

    # led.start() here reuses the already-initialised NI-DAQmx state.
    _daq_error: str = ""
    if HAS_DAQ:
        ch = win.daq_chan_edit.text().strip()
        if _pre_channels and ch not in _pre_channels:
            ch = _pre_channels[0]
            win.daq_chan_edit.setText(ch)
        try:
            win.led.start(channel=ch)
        except Exception as e:
            _daq_error = str(e)
            print(f"DAQ init error: {e}", flush=True)

    # Log startup results once exec_() is running and the UI is live.
    def _startup_log():
        if _pre_channels:
            win._log(f"Available AO channels: {', '.join(_pre_channels)}")
        if _daq_error:
            win._log(f"DAQ error: {_daq_error}")
        elif win.led.task:
            win._log(f"DAQ ready — {win.daq_chan_edit.text()}")
        else:
            win._log("DAQ not connected — click 'Reconnect DAQ'")

    QTimer.singleShot(100, _startup_log)
    win.show()
    sys.exit(app.exec_())

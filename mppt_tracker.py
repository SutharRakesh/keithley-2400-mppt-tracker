from nicegui import ui, run

import os
import sys
import time
import subprocess
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
import re
from typing import Optional, Union, Tuple, List

import numpy as np
import pandas as pd
import pyvisa as visa
import plotly.graph_objects as go


# ============================================================
# Config
# ============================================================

DEFAULT_RESOURCE = os.environ.get('KEITHLEY_VISA', 'GPIB0::19::INSTR')
DEFAULT_TIMEOUT_MS = 50000

DEFAULT_OUTPUT_FOLDER = Path.home() / 'Documents' / 'Keithley_MPPT_Data'

APP_WIDTH = 1520
APP_HEIGHT = 900

PLOT_WIDTH = 670
PLOT_HEIGHT = 580

# Hidden/internal settling delay after every voltage step.
# You asked not to show this in the GUI. Change here if your device needs more/less settling.
INTERNAL_SETTLE_S = 0.05

# Auto MPPT tuning. The GUI's P&O step is now the INITIAL step.
# The tracker automatically grows/shrinks the step between these limits.
MPPT_MIN_STEP_FACTOR = 0.15
MPPT_MAX_STEP_FACTOR = 6.0
MPPT_STEP_GROW = 1.08
MPPT_STEP_SHRINK_ON_REVERSAL = 0.65
MPPT_STEP_SHRINK_ON_FLAT = 0.92
MPPT_ABS_POWER_DEADBAND_W = 1e-9
MPPT_REL_POWER_DEADBAND = 0.002  # 0.2 percent of previous power

COMPLIANCE_WARNING_FRACTION = 0.98


# ============================================================
# Data models
# ============================================================

@dataclass
class Readout:
    v: float
    i: float
    t: float
    status: int


@dataclass
class RunSettings:
    sample_name: str
    output_folder: str
    resource: str
    use_rear: bool
    area_cm2: float
    pin_mw_cm2: float
    compliance_A: float
    vstart: float
    vend: float
    nsteps: int
    sweep_mode: str
    track_seconds: float
    sample_dt: float
    live_every: int
    po_step_v: float


# ============================================================
# Instrument wrapper
# ============================================================

class Keithley2400:
    def __init__(self, resource: str, visa_backend=None, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        if visa_backend and str(visa_backend).strip():
            self.rm = visa.ResourceManager(visa_backend)
        else:
            try:
                self.rm = visa.ResourceManager()
            except Exception:
                self.rm = visa.ResourceManager('@py')

        self.sm = self.rm.open_resource(resource, timeout=timeout_ms)
        self.sm.read_termination = '\n'
        self.sm.write_termination = '\n'

    def write(self, cmd: str) -> None:
        self.sm.write(cmd)

    def query(self, cmd: str) -> str:
        return self.sm.query(cmd)

    def query_ascii_values(self, cmd: str) -> np.ndarray:
        raw = list(self.sm.query_ascii_values(cmd))
        if len(raw) < 4:
            raise ValueError(f'Expected at least 4 values from {cmd!r}, got {len(raw)}: {raw}')
        return np.array(raw[:4], dtype=float)

    def set_voltage(self, value: float) -> None:
        self.write(f':source:voltage {value:.6f}')

    def output_on(self) -> None:
        self.write(':output on')

    def output_off(self) -> None:
        self.write(':output off')

    def close(self) -> None:
        # Safer shutdown: move source to 0 V before output off.
        try:
            self.set_voltage(0.0)
            time.sleep(0.05)
        except Exception:
            pass

        try:
            self.output_off()
        except Exception:
            pass

        try:
            self.sm.close()
        except Exception:
            pass

        try:
            self.rm.close()
        except Exception:
            pass


# ============================================================
# Measurement helpers
# ============================================================

def read_once(sm: Keithley2400) -> Readout:
    vals = sm.query_ascii_values('READ?')
    v, i, t, status = vals[:4]
    return Readout(float(v), float(i), float(t), int(status))


def base_setup(sm: Keithley2400, use_rear: bool) -> None:
    try:
        sm.write('*RST')
    except Exception:
        pass

    sm.write(':system:azero on')
    sm.write(':sense:function:concurrent on')
    sm.write(':sense:function "current:dc", "voltage:dc"')
    sm.write(':format:elements voltage,current,time,status')

    if use_rear:
        sm.write(':rout:term rear')
    else:
        sm.write(':rout:term front')

    sm.write(':sense:voltage:range 10')
    sm.write(':sense:voltage:nplcycles 0.5')
    sm.write(':sense:current:nplcycles 0.5')
    sm.write(':source:function voltage')
    sm.write(':source:voltage:mode fixed')
    sm.write(':trigger:count 1')


def set_compliance(sm: Keithley2400, comp: float) -> float:
    comp = float(comp)
    sm.write(f':sense:current:protection {comp:.6f}')
    sm.write(f':sense:current:range {comp:.6f}')

    try:
        sm.write(f':source:voltage:ilim {comp:.6f}')
    except Exception:
        pass

    sm.output_on()
    return comp


def delivered_power_W(v, i) -> float:
    """Generated/delivered device power in W.

    For the common PV source-measure convention, useful generated power is when V*I < 0.
    Non-generating points are stored as 0 W for MPPT and P-V plots.
    """
    vi = float(v) * float(i)
    return -vi if vi < 0 else 0.0


def generated_current_density_mAcm2(i, area_cm2: float) -> float:
    return (-float(i) / max(1e-12, float(area_cm2))) * 1e3


def raw_current_density_mAcm2(i, area_cm2: float) -> float:
    return (float(i) / max(1e-12, float(area_cm2))) * 1e3


def compute_mpp(V: np.ndarray, I: np.ndarray, area_cm2: float) -> Tuple[float, float, float, float, int]:
    V = np.asarray(V, dtype=float)
    I = np.asarray(I, dtype=float)

    if len(V) == 0 or len(I) == 0:
        return np.nan, np.nan, np.nan, np.nan, -1

    VI = V * I
    gen_mask = VI < 0

    if np.any(gen_mask):
        Pdev = -VI[gen_mask]
        idx_local = int(np.argmax(Pdev))
        idx = int(np.flatnonzero(gen_mask)[idx_local])
        Pmpp_W = float(Pdev[idx_local])
    else:
        Pabs = np.abs(VI)
        idx = int(np.argmax(Pabs))
        Pmpp_W = float(Pabs[idx])

    Vmpp = float(V[idx])
    Impp = float(I[idx])
    Jmpp_Acm2_raw = Impp / max(1e-12, area_cm2)

    return Vmpp, Impp, Jmpp_Acm2_raw, Pmpp_W, idx


# ============================================================
# PV metric helpers
# ============================================================

def _interp_at_x(x0, x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)

    if len(x) == 0:
        return np.nan

    order = np.argsort(x)
    xs, ys = x[order], y[order]

    if x0 < xs[0] or x0 > xs[-1]:
        return float(ys[np.argmin(np.abs(xs - x0))])

    return float(np.interp(x0, xs, ys))


def _interp_x_at_y_zero(y0, x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)

    if len(x) == 0:
        return np.nan

    for idx in range(len(y) - 1):
        y1, y2 = y[idx], y[idx + 1]

        if y1 == y0:
            return float(x[idx])

        if (y1 - y0) * (y2 - y0) < 0:
            x1, x2 = x[idx], x[idx + 1]
            return float(x1 + (y0 - y1) * (x2 - x1) / (y2 - y1))

    return float(x[np.argmin(np.abs(y - y0))])


def _local_dVdJ(V, J, center_V, n_pts=7):
    V = np.asarray(V, dtype=float)
    J = np.asarray(J, dtype=float)

    if len(V) < 3:
        return np.nan

    idx = np.argsort(np.abs(V - center_V))[: max(3, min(n_pts, len(V)))]
    J_sub = J[idx]
    V_sub = V[idx]

    if np.std(J_sub) < 1e-15:
        return np.nan

    m, _b = np.polyfit(J_sub, V_sub, 1)
    return float(m)


def compute_metrics_df(V, I, T, settings: RunSettings, comp: float):
    V = np.asarray(V, dtype=float)
    I = np.asarray(I, dtype=float)

    if len(V) == 0 or len(I) == 0:
        empty_metrics = pd.DataFrame()
        empty_sweep = pd.DataFrame()
        return empty_metrics, empty_sweep, np.nan, np.nan, np.nan

    area = max(1e-12, settings.area_cm2)

    J_raw_Acm2 = I / area
    J_gen_Acm2 = -J_raw_Acm2

    Vmpp, Impp, Jmpp_Acm2_raw, Pmpp_W, _idx_mpp = compute_mpp(V, I, settings.area_cm2)

    Jmpp_raw_mAcm2 = Jmpp_Acm2_raw * 1e3
    Jmpp_gen_mAcm2 = -Jmpp_raw_mAcm2
    Pmpp_mWcm2 = (Pmpp_W * 1e3) / area

    # Positive Jsc for PV metrics, but save raw and generated signs in the data table.
    Jsc_raw_Acm2 = _interp_at_x(0.0, V, J_raw_Acm2)
    Jsc_gen_Acm2 = -Jsc_raw_Acm2
    Jsc_mAcm2 = abs(Jsc_gen_Acm2 * 1e3)

    # Voc is found using raw current-density zero crossing. Same zero crossing as generated J.
    Voc_V = _interp_x_at_y_zero(0.0, V, J_raw_Acm2)
    Voc_V_abs = abs(Voc_V)

    denom = Voc_V_abs * max(1e-12, Jsc_mAcm2)
    FF_pct = float((Pmpp_mWcm2 / denom) * 100.0) if denom > 0 else float('nan')

    if settings.pin_mw_cm2 > 0:
        PCE_pct = float((Pmpp_mWcm2 / max(1e-12, settings.pin_mw_cm2)) * 100.0)
    else:
        PCE_pct = float('nan')

    Rs_ohm_cm2 = abs(_local_dVdJ(V, J_raw_Acm2, center_V=Voc_V, n_pts=7))
    Rsh_ohm_cm2 = abs(_local_dVdJ(V, J_raw_Acm2, center_V=0.0, n_pts=7))

    df_metrics = pd.DataFrame({
        'Jsc (mA/cm2)': [Jsc_mAcm2],
        'Voc (V)': [Voc_V_abs],
        'Vmpp (V)': [abs(Vmpp)],
        'Jmpp generated (mA/cm2)': [abs(Jmpp_gen_mAcm2)],
        'Jmpp raw (mA/cm2)': [Jmpp_raw_mAcm2],
        'Pmpp (mW/cm2)': [Pmpp_mWcm2],
        'FF (%)': [FF_pct],
        'PCE (%)': [PCE_pct],
        'Rs (ohm cm2)': [Rs_ohm_cm2],
        'Rsh (ohm cm2)': [Rsh_ohm_cm2],
        'Pin (mW/cm2)': [settings.pin_mw_cm2],
        'Area (cm2)': [settings.area_cm2],
        'Compliance (A)': [comp],
        'Sweep mode': [settings.sweep_mode],
    })

    power_W = np.array([delivered_power_W(v, i) for v, i in zip(V, I)], dtype=float)

    df_sweep = pd.DataFrame({
        'voltage_V': V,
        'current_A': I,
        'time_s': np.asarray(T, dtype=float),
        'current_density_raw_mA_per_cm2': J_raw_Acm2 * 1e3,
        'current_density_generated_mA_per_cm2': J_gen_Acm2 * 1e3,
        'power_delivered_W': power_W,
        'power_density_mW_per_cm2': (power_W * 1e3) / area,
    })

    return df_metrics, df_sweep, Vmpp, Impp, Pmpp_mWcm2


def add_sweep_extra_columns(
    df_sweep: pd.DataFrame,
    status_values: Optional[List[int]] = None,
    direction_values: Optional[List[str]] = None,
) -> pd.DataFrame:
    df = df_sweep.copy()

    if not df.empty:
        n = len(df)
        if status_values is not None and len(status_values) == n:
            df['status'] = np.asarray(status_values, dtype=int)
        if direction_values is not None and len(direction_values) == n:
            df['scan_direction'] = list(direction_values)

    return df


# ============================================================
# MPPT summary helper
# ============================================================

def compute_mppt_summary_df(df_tracking: pd.DataFrame, settings: RunSettings, run_status: str) -> pd.DataFrame:
    if df_tracking is None or df_tracking.empty:
        return pd.DataFrame({
            'run_status': [run_status],
            'points': [0],
            'tracking_time_s': [0.0],
            'initial_power_mW_cm2': [np.nan],
            'final_power_mW_cm2': [np.nan],
            'mean_power_mW_cm2': [np.nan],
            'median_power_mW_cm2': [np.nan],
            'std_power_mW_cm2': [np.nan],
            'max_power_mW_cm2': [np.nan],
            'min_power_mW_cm2': [np.nan],
            'final_voltage_V': [np.nan],
            'mean_voltage_V': [np.nan],
            'initial_step_V': [settings.po_step_v],
            'auto_step_used': [True],
        })

    p = df_tracking['power_density_mW_per_cm2'].astype(float)
    v = df_tracking['voltage_V'].astype(float)
    t = df_tracking['time_s'].astype(float)

    return pd.DataFrame({
        'run_status': [run_status],
        'points': [len(df_tracking)],
        'tracking_time_s': [float(t.iloc[-1]) if len(t) else 0.0],
        'initial_power_mW_cm2': [float(p.iloc[0])],
        'final_power_mW_cm2': [float(p.iloc[-1])],
        'mean_power_mW_cm2': [float(p.mean())],
        'median_power_mW_cm2': [float(p.median())],
        'std_power_mW_cm2': [float(p.std()) if len(p) > 1 else 0.0],
        'max_power_mW_cm2': [float(p.max())],
        'min_power_mW_cm2': [float(p.min())],
        'final_voltage_V': [float(v.iloc[-1])],
        'mean_voltage_V': [float(v.mean())],
        'initial_step_V': [settings.po_step_v],
        'auto_step_used': [True],
    })


# ============================================================
# Save helpers
# ============================================================

def safe_name(text: str) -> str:
    text = str(text).strip()
    text = re.sub(r'[^A-Za-z0-9_.-]+', '_', text)
    text = text.strip('_')
    return text if text else 'sample'


def make_run_folder(settings: RunSettings) -> Path:
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    sample = safe_name(settings.sample_name)

    folder = (
        Path(settings.output_folder)
        .expanduser()
        .resolve()
        / sample
        / f'{sample}_{timestamp}'
    )

    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_run_files(
    run_folder: Path,
    settings: RunSettings,
    df_metrics: pd.DataFrame,
    df_sweep: pd.DataFrame,
    df_tracking: pd.DataFrame,
    idn: str,
    run_status: str = 'complete',
):
    sample = safe_name(settings.sample_name)
    stamp = run_folder.name.replace(f'{sample}_', '')
    suffix = '' if run_status == 'complete' else f'_{safe_name(run_status).upper()}'

    sweep_csv = run_folder / f'{sample}_{stamp}{suffix}_sweep.csv'
    tracking_csv = run_folder / f'{sample}_{stamp}{suffix}_mppt_tracking.csv'
    metrics_csv = run_folder / f'{sample}_{stamp}{suffix}_metrics.csv'
    mppt_summary_csv = run_folder / f'{sample}_{stamp}{suffix}_mppt_summary.csv'
    excel_path = run_folder / f'{sample}_{stamp}{suffix}_mppt_run.xlsx'
    settings_txt = run_folder / f'{sample}_{stamp}{suffix}_settings.txt'

    if df_metrics is None:
        df_metrics = pd.DataFrame()
    if df_sweep is None:
        df_sweep = pd.DataFrame()
    if df_tracking is None:
        df_tracking = pd.DataFrame()

    df_mppt_summary = compute_mppt_summary_df(df_tracking, settings, run_status)

    df_sweep.to_csv(sweep_csv, index=False)
    df_tracking.to_csv(tracking_csv, index=False)
    df_metrics.to_csv(metrics_csv, index=False)
    df_mppt_summary.to_csv(mppt_summary_csv, index=False)

    settings_df = pd.DataFrame({
        'parameter': [
            'sample_name',
            'run_status',
            'instrument_id',
            'visa_resource',
            'terminals',
            'area_cm2',
            'pin_mW_cm2',
            'compliance_A',
            'vstart_V',
            'vend_V',
            'points_per_direction',
            'sweep_mode',
            'mppt_seconds_after_sweep',
            'sample_dt_s',
            'live_every',
            'initial_po_step_V',
            'auto_mppt_enabled',
            'internal_settle_s_hidden',
            'run_folder',
        ],
        'value': [
            settings.sample_name,
            run_status,
            idn.strip(),
            settings.resource,
            'rear' if settings.use_rear else 'front',
            settings.area_cm2,
            settings.pin_mw_cm2,
            settings.compliance_A,
            settings.vstart,
            settings.vend,
            settings.nsteps,
            settings.sweep_mode,
            settings.track_seconds,
            settings.sample_dt,
            settings.live_every,
            settings.po_step_v,
            True,
            INTERNAL_SETTLE_S,
            str(run_folder),
        ],
    })

    with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
        settings_df.to_excel(writer, index=False, sheet_name='Settings')
        df_metrics.to_excel(writer, index=False, sheet_name='Metrics')
        df_sweep.to_excel(writer, index=False, sheet_name='Sweep')
        df_tracking.to_excel(writer, index=False, sheet_name='MPPT_Tracking')
        df_mppt_summary.to_excel(writer, index=False, sheet_name='MPPT_Summary')

    with open(settings_txt, 'w', encoding='utf-8') as f:
        f.write('Keithley 2400 MPPT run\n')
        f.write(f'Run status: {run_status}\n')
        f.write(f'Run folder: {run_folder}\n')
        f.write(f'Instrument ID: {idn.strip()}\n')
        f.write('\n')

        for _, row in settings_df.iterrows():
            f.write(f'{row["parameter"]}: {row["value"]}\n')

    return {
        'folder': run_folder,
        'excel': excel_path,
        'sweep_csv': sweep_csv,
        'tracking_csv': tracking_csv,
        'metrics_csv': metrics_csv,
        'mppt_summary_csv': mppt_summary_csv,
        'settings_txt': settings_txt,
    }


def open_folder(path: Union[str, Path]):
    folder = Path(path).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)

    if os.name == 'nt':
        os.startfile(folder)  # type: ignore[attr-defined]
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(folder)])
    else:
        subprocess.Popen(['xdg-open', str(folder)])


# ============================================================
# Plot helpers
# ============================================================

def empty_jv_plot():
    fig = go.Figure()

    fig.update_layout(
        title='J-V Sweep',
        template='plotly_white',
        width=PLOT_WIDTH,
        height=PLOT_HEIGHT,
        margin=dict(l=80, r=30, t=60, b=70),
        showlegend=False,
        paper_bgcolor='white',
        plot_bgcolor='rgba(245,245,245,0.55)',
    )

    fig.update_xaxes(
        title='Voltage (V)',
        showline=True,
        mirror=True,
        linewidth=1.4,
        linecolor='black',
        gridcolor='rgba(160,160,160,0.35)',
        zeroline=True,
        zerolinecolor='black',
    )

    fig.update_yaxes(
        title='Generated current density (mA/cm2)',
        showline=True,
        mirror=True,
        linewidth=1.4,
        linecolor='black',
        gridcolor='rgba(160,160,160,0.35)',
        zeroline=True,
        zerolinecolor='black',
    )

    return fig


def empty_pv_plot():
    fig = go.Figure()

    fig.update_layout(
        title='Power vs Voltage from J-V Sweep',
        template='plotly_white',
        width=PLOT_WIDTH,
        height=PLOT_HEIGHT,
        margin=dict(l=80, r=30, t=60, b=70),
        showlegend=False,
        paper_bgcolor='white',
        plot_bgcolor='rgba(245,245,245,0.55)',
    )

    fig.update_xaxes(
        title='Voltage (V)',
        showline=True,
        mirror=True,
        linewidth=1.4,
        linecolor='black',
        gridcolor='rgba(160,160,160,0.35)',
        zeroline=True,
        zerolinecolor='black',
    )

    fig.update_yaxes(
        title='Delivered power density (mW/cm2)',
        showline=True,
        mirror=True,
        linewidth=1.4,
        linecolor='black',
        gridcolor='rgba(160,160,160,0.35)',
        rangemode='tozero',
    )

    return fig


def _iter_sweep_groups(df_sweep: pd.DataFrame):
    if 'scan_direction' in df_sweep.columns and df_sweep['scan_direction'].nunique(dropna=True) > 1:
        for name, group in df_sweep.groupby('scan_direction', sort=False):
            yield str(name), group
    else:
        yield 'sweep', df_sweep


def build_jv_plot(df_sweep: Optional[pd.DataFrame], Vmpp=None, Impp=None, area_cm2=1.0):
    fig = empty_jv_plot()

    if df_sweep is None or df_sweep.empty:
        return fig

    for name, group in _iter_sweep_groups(df_sweep):
        V = group['voltage_V'].to_numpy(dtype=float)
        if 'current_density_generated_mA_per_cm2' in group.columns:
            J = group['current_density_generated_mA_per_cm2'].to_numpy(dtype=float)
        else:
            J = -group['current_density_mA_per_cm2'].to_numpy(dtype=float)

        marker_mode = 'lines+markers' if len(V) <= 200 else 'lines'

        fig.add_trace(
            go.Scatter(
                x=V,
                y=J,
                mode=marker_mode,
                name=f'J-V {name}',
                line=dict(width=2.5),
                marker=dict(size=5),
                hovertemplate='V = %{x:.5g} V<br>Jgen = %{y:.5g} mA/cm2<extra></extra>',
            )
        )

    if Vmpp is not None and Impp is not None and not pd.isna(Vmpp) and not pd.isna(Impp):
        Jmpp = generated_current_density_mAcm2(float(Impp), float(area_cm2))

        fig.add_trace(
            go.Scatter(
                x=[float(Vmpp)],
                y=[Jmpp],
                mode='markers+text',
                name='MPP',
                text=['MPP'],
                textposition='top right',
                marker=dict(size=11, symbol='diamond'),
                hovertemplate='MPP<br>Vmpp = %{x:.5g} V<br>Jgen,mpp = %{y:.5g} mA/cm2<extra></extra>',
            )
        )

    fig.update_layout(
        showlegend=True,
        legend=dict(
            orientation='h',
            y=-0.18,
            x=0.5,
            xanchor='center',
        ),
    )

    return fig


def build_pv_plot(df_sweep: Optional[pd.DataFrame], Vmpp=None, Pmpp_mWcm2=None):
    fig = empty_pv_plot()

    if df_sweep is None or df_sweep.empty:
        return fig

    for name, group in _iter_sweep_groups(df_sweep):
        V = group['voltage_V'].to_numpy(dtype=float)
        if 'power_density_mW_per_cm2' in group.columns:
            P = group['power_density_mW_per_cm2'].to_numpy(dtype=float)
        else:
            P = np.array(
                [delivered_power_W(v, i) for v, i in zip(group['voltage_V'], group['current_A'])],
                dtype=float,
            )

        marker_mode = 'lines+markers' if len(V) <= 200 else 'lines'

        fig.add_trace(
            go.Scatter(
                x=V,
                y=P,
                mode=marker_mode,
                name=f'P-V {name}',
                line=dict(width=2.5),
                marker=dict(size=5),
                hovertemplate='V = %{x:.5g} V<br>P = %{y:.5g} mW/cm2<extra></extra>',
            )
        )

    if Vmpp is not None and Pmpp_mWcm2 is not None and not pd.isna(Vmpp) and not pd.isna(Pmpp_mWcm2):
        fig.add_trace(
            go.Scatter(
                x=[float(Vmpp)],
                y=[float(Pmpp_mWcm2)],
                mode='markers+text',
                name='MPP',
                text=['MPP'],
                textposition='top right',
                marker=dict(size=11, symbol='diamond'),
                hovertemplate='MPP<br>Vmpp = %{x:.5g} V<br>Pmpp = %{y:.5g} mW/cm2<extra></extra>',
            )
        )

    fig.update_layout(
        showlegend=True,
        legend=dict(
            orientation='h',
            y=-0.18,
            x=0.5,
            xanchor='center',
        ),
    )

    return fig


def empty_power_plot():
    fig = go.Figure()

    fig.update_layout(
        title='MPPT Power Tracking',
        template='plotly_white',
        width=PLOT_WIDTH,
        height=PLOT_HEIGHT,
        margin=dict(l=80, r=30, t=60, b=70),
        showlegend=False,
        paper_bgcolor='white',
        plot_bgcolor='rgba(245,245,245,0.55)',
    )

    fig.update_xaxes(
        title='Time (s)',
        showline=True,
        mirror=True,
        linewidth=1.4,
        linecolor='black',
        gridcolor='rgba(160,160,160,0.35)',
    )

    fig.update_yaxes(
        title='Power density (mW/cm2)',
        showline=True,
        mirror=True,
        linewidth=1.4,
        linecolor='black',
        gridcolor='rgba(160,160,160,0.35)',
        rangemode='tozero',
    )

    return fig


def build_power_plot(df_tracking: Optional[pd.DataFrame]):
    fig = empty_power_plot()

    if df_tracking is None or df_tracking.empty:
        return fig

    fig.add_trace(
        go.Scatter(
            x=df_tracking['time_s'],
            y=df_tracking['power_density_mW_per_cm2'],
            mode='lines',
            name='Power density',
            line=dict(width=2.5),
            hovertemplate='t = %{x:.3f} s<br>P = %{y:.5g} mW/cm2<extra></extra>',
        )
    )

    return fig


def build_sweep_plot_for_current_mode():
    try:
        mode = sweep_plot_mode_select.value
    except Exception:
        mode = 'J-V'

    if mode == 'P-V':
        return build_pv_plot(current_sweep_df, current_Vmpp, current_Pmpp_mWcm2)

    return build_jv_plot(current_sweep_df, current_Vmpp, current_Impp, current_area_cm2)


# ============================================================
# VISA helpers
# ============================================================

def list_visa_resources():
    try:
        try:
            rm = visa.ResourceManager()
        except Exception:
            rm = visa.ResourceManager('@py')

        resources = list(rm.list_resources())

        try:
            rm.close()
        except Exception:
            pass

    except Exception as e:
        raise RuntimeError(f'Could not list VISA resources: {e}') from e

    gpib = [r for r in resources if 'GPIB' in r.upper()]
    return gpib or resources


# ============================================================
# App state
# ============================================================

stop_requested = False
last_run_folder = None
last_paths = {}

current_sweep_df = pd.DataFrame()
current_tracking_df = pd.DataFrame()
current_metrics_df = pd.DataFrame()
current_Vmpp = np.nan
current_Impp = np.nan
current_Pmpp_mWcm2 = np.nan
current_area_cm2 = 1.0


# ============================================================
# GUI actions
# ============================================================

def hide_settings_panel():
    try:
        controls_card.set_visibility(False)
        compact_status_card.set_visibility(True)
    except Exception:
        pass


def show_settings_panel():
    try:
        controls_card.set_visibility(True)
        compact_status_card.set_visibility(False)
    except Exception:
        pass


def sync_progress(value: float):
    value = max(0.0, min(1.0, float(value)))
    percent_text = f'{value * 100:.1f}%'

    progress_bar.value = value
    progress_percent_label.set_text(percent_text)

    try:
        compact_progress.value = value
        compact_progress_percent_label.set_text(percent_text)
    except Exception:
        pass


def set_running_state(running: bool):
    run_button.set_enabled(not running)
    stop_button.set_enabled(running)
    scan_button.set_enabled(not running)

    try:
        compact_run_button.set_enabled(not running)
        compact_stop_button.set_enabled(running)
    except Exception:
        pass


def request_stop():
    global stop_requested
    stop_requested = True
    status_label.set_text('Stop requested. Finishing current point and saving collected data...')


def validate_settings(settings: RunSettings):
    if not settings.sample_name.strip():
        raise ValueError('Enter a sample name.')

    if not settings.resource.strip():
        raise ValueError('Enter a VISA resource.')

    if settings.area_cm2 <= 0:
        raise ValueError('Active area must be > 0.')

    if settings.pin_mw_cm2 < 0:
        raise ValueError('Illumination Pin must be >= 0.')

    if settings.compliance_A <= 0:
        raise ValueError('Compliance must be > 0.')

    if settings.nsteps < 2:
        raise ValueError('Points must be at least 2.')

    if settings.sweep_mode not in {'Forward', 'Reverse', 'Both'}:
        raise ValueError('Sweep mode must be Forward, Reverse, or Both.')

    if settings.track_seconds < 0:
        raise ValueError('MPPT seconds cannot be negative.')

    if settings.sample_dt <= 0:
        raise ValueError('Sample interval must be > 0.')

    if settings.live_every < 1:
        raise ValueError('Plot update every N points must be >= 1.')

    if settings.po_step_v <= 0:
        raise ValueError('Initial P&O step must be > 0.')


def read_settings() -> RunSettings:
    return RunSettings(
        sample_name=str(sample_input.value or '').strip(),
        output_folder=str(output_folder_input.value or DEFAULT_OUTPUT_FOLDER).strip(),
        resource=str(resource_input.value or '').strip() or DEFAULT_RESOURCE,
        use_rear=(terminal_select.value == 'Rear'),
        area_cm2=float(area_input.value),
        pin_mw_cm2=float(pin_input.value),
        compliance_A=float(compliance_input.value),
        vstart=float(vstart_input.value),
        vend=float(vend_input.value),
        nsteps=int(float(points_input.value)),
        sweep_mode=str(sweep_mode_select.value or 'Forward'),
        track_seconds=float(track_seconds_input.value),
        sample_dt=float(sample_dt_input.value),
        live_every=max(1, int(float(live_every_input.value))),
        po_step_v=float(po_step_input.value),
    )


def build_voltage_sequence(settings: RunSettings) -> Tuple[np.ndarray, List[str]]:
    forward = np.linspace(settings.vstart, settings.vend, settings.nsteps)
    reverse = np.linspace(settings.vend, settings.vstart, settings.nsteps)

    if settings.sweep_mode == 'Forward':
        return forward, ['forward'] * len(forward)

    if settings.sweep_mode == 'Reverse':
        return reverse, ['reverse'] * len(reverse)

    voltage_points = np.concatenate([forward, reverse])
    direction_labels = ['forward'] * len(forward) + ['reverse'] * len(reverse)
    return voltage_points, direction_labels


def update_metric_cards(df_metrics: Optional[pd.DataFrame]):
    if df_metrics is None or df_metrics.empty:
        jsc_card.set_text('---')
        voc_card.set_text('---')
        ff_card.set_text('---')
        pce_card.set_text('---')
        vmpp_card.set_text('---')
        pmpp_card.set_text('---')
        rs_card.set_text('---')
        rsh_card.set_text('---')
        return

    m = df_metrics.iloc[0]

    jsc_card.set_text(f'{m["Jsc (mA/cm2)"]:.3f}')
    voc_card.set_text(f'{m["Voc (V)"]:.4f}')
    ff_card.set_text(f'{m["FF (%)"]:.2f}')
    pce_card.set_text(f'{m["PCE (%)"]:.2f}')
    vmpp_card.set_text(f'{m["Vmpp (V)"]:.4f}')
    pmpp_card.set_text(f'{m["Pmpp (mW/cm2)"]:.3f}')

    try:
        rs_card.set_text(f'{m["Rs (ohm cm2)"]:.2f}')
    except Exception:
        rs_card.set_text('nan')

    try:
        rsh_card.set_text(f'{m["Rsh (ohm cm2)"]:.2f}')
    except Exception:
        rsh_card.set_text('nan')


def refresh_sweep_plot():
    try:
        jv_plot.figure = build_sweep_plot_for_current_mode()
        jv_plot.update()
    except Exception:
        pass


def clear_run_display():
    global current_sweep_df
    global current_tracking_df
    global current_metrics_df
    global current_Vmpp
    global current_Impp
    global current_Pmpp_mWcm2
    global current_area_cm2

    current_sweep_df = pd.DataFrame()
    current_tracking_df = pd.DataFrame()
    current_metrics_df = pd.DataFrame()
    current_Vmpp = np.nan
    current_Impp = np.nan
    current_Pmpp_mWcm2 = np.nan
    current_area_cm2 = 1.0

    jv_plot.figure = empty_jv_plot()
    power_plot.figure = empty_power_plot()
    jv_plot.update()
    power_plot.update()

    update_metric_cards(pd.DataFrame())
    sync_progress(0)

    live_status_label.set_text('No active run.')


async def scan_visa_action():
    try:
        status_label.set_text('Scanning VISA resources...')

        resources = await run.io_bound(list_visa_resources)

        resource_select.options = resources
        resource_select.update()

        if resources:
            resource_select.value = resources[0]
            resource_input.value = resources[0]
            resource_select.update()
            resource_input.update()
            status_label.set_text(f'Found {len(resources)} resource(s).')
        else:
            status_label.set_text('No VISA resources found. You can type the VISA address manually.')
            ui.notify('No VISA resources found.', type='warning')

    except Exception as e:
        status_label.set_text(f'VISA scan error: {e}')
        ui.notify(str(e), type='negative')


def on_resource_selected():
    if resource_select.value:
        resource_input.value = str(resource_select.value)
        resource_input.update()


def open_output_folder_action():
    try:
        folder = output_folder_input.value or str(DEFAULT_OUTPUT_FOLDER)
        open_folder(folder)
    except Exception as e:
        ui.notify(f'Could not open folder: {e}', type='negative')


def open_last_run_folder_action():
    try:
        if last_run_folder is None:
            ui.notify('No saved run folder yet.', type='warning')
            return

        open_folder(last_run_folder)

    except Exception as e:
        ui.notify(f'Could not open last run folder: {e}', type='negative')


async def settle_after_voltage_step():
    if INTERNAL_SETTLE_S > 0:
        await run.io_bound(time.sleep, INTERNAL_SETTLE_S)


async def save_partial_sweep(
    run_folder: Path,
    settings: RunSettings,
    V_list: List[float],
    I_list: List[float],
    T_list: List[float],
    Status_list: List[int],
    Direction_list: List[str],
    comp: float,
    idn: str,
    run_status: str,
):
    global last_paths
    global current_sweep_df
    global current_metrics_df
    global current_Vmpp
    global current_Impp
    global current_Pmpp_mWcm2

    if len(V_list) >= 1:
        current_metrics_df, current_sweep_df, current_Vmpp, current_Impp, current_Pmpp_mWcm2 = compute_metrics_df(
            np.asarray(V_list, dtype=float),
            np.asarray(I_list, dtype=float),
            np.asarray(T_list, dtype=float),
            settings,
            comp,
        )
        current_sweep_df = add_sweep_extra_columns(current_sweep_df, Status_list, Direction_list)
    else:
        current_metrics_df = pd.DataFrame()
        current_sweep_df = pd.DataFrame()
        current_Vmpp = np.nan
        current_Impp = np.nan
        current_Pmpp_mWcm2 = np.nan

    last_paths = await run.io_bound(
        save_run_files,
        run_folder,
        settings,
        current_metrics_df,
        current_sweep_df,
        current_tracking_df,
        idn,
        run_status,
    )

    save_location_label.set_text(f'Saved partial Excel: {last_paths["excel"]}')


async def run_measurement_action():
    global stop_requested
    global last_run_folder
    global last_paths
    global current_sweep_df
    global current_tracking_df
    global current_metrics_df
    global current_Vmpp
    global current_Impp
    global current_Pmpp_mWcm2
    global current_area_cm2

    stop_requested = False
    set_running_state(True)
    clear_run_display()

    total_start_time = time.monotonic()

    instrument = None
    idn = ''

    try:
        settings = read_settings()
        validate_settings(settings)
        current_area_cm2 = settings.area_cm2

        hide_settings_panel()

        run_folder = make_run_folder(settings)
        last_run_folder = run_folder
        last_paths = {'folder': run_folder}

        save_location_label.set_text(f'Saving to: {run_folder}')
        status_label.set_text(f'Connecting to {settings.resource} ...')
        sync_progress(0)

        instrument = await run.io_bound(
            Keithley2400,
            settings.resource,
            None,
            DEFAULT_TIMEOUT_MS,
        )

        try:
            idn = await run.io_bound(instrument.query, '*IDN?')
        except Exception:
            idn = ''

        status_label.set_text('Connected. Stage 1/2: J-V sweep...')

        await run.io_bound(base_setup, instrument, settings.use_rear)
        comp = await run.io_bound(set_compliance, instrument, settings.compliance_A)

        # ====================================================
        # Stage 1: J-V sweep
        # ====================================================

        voltage_points, direction_labels = build_voltage_sequence(settings)

        V_list: List[float] = []
        I_list: List[float] = []
        T_list: List[float] = []
        Status_list: List[int] = []
        Direction_list: List[str] = []
        warned_compliance = False

        for k, (Vset, scan_direction) in enumerate(zip(voltage_points, direction_labels), start=1):
            if stop_requested:
                break

            await run.io_bound(instrument.set_voltage, float(Vset))
            await settle_after_voltage_step()

            readout = await run.io_bound(read_once, instrument)

            V_list.append(readout.v)
            I_list.append(readout.i)
            T_list.append(readout.t)
            Status_list.append(readout.status)
            Direction_list.append(scan_direction)

            if (not warned_compliance) and abs(readout.i) >= COMPLIANCE_WARNING_FRACTION * settings.compliance_A:
                warned_compliance = True
                ui.notify('Warning: current is near the compliance limit.', type='warning')

            sync_progress(50.0 * k / len(voltage_points) / 100.0)

            total_elapsed = time.monotonic() - total_start_time

            live_status_label.set_text(
                f'Total={total_elapsed:.1f} s | '
                f'Sweep {k}/{len(voltage_points)} ({scan_direction}) | '
                f'V={readout.v:.4g} V | I={readout.i:.3e} A'
            )

            if k % settings.live_every == 0 or k == 1 or k == len(voltage_points):
                temp_power_W = np.array(
                    [delivered_power_W(v, i) for v, i in zip(V_list, I_list)],
                    dtype=float,
                )
                temp_df = pd.DataFrame({
                    'voltage_V': np.asarray(V_list, dtype=float),
                    'current_A': np.asarray(I_list, dtype=float),
                    'time_s': np.asarray(T_list, dtype=float),
                    'current_density_raw_mA_per_cm2': (
                        np.asarray(I_list, dtype=float) / max(1e-12, settings.area_cm2)
                    ) * 1e3,
                    'current_density_generated_mA_per_cm2': (
                        -np.asarray(I_list, dtype=float) / max(1e-12, settings.area_cm2)
                    ) * 1e3,
                    'power_delivered_W': temp_power_W,
                    'power_density_mW_per_cm2': (
                        temp_power_W * 1e3
                    ) / max(1e-12, settings.area_cm2),
                    'status': np.asarray(Status_list, dtype=int),
                    'scan_direction': Direction_list,
                })

                # During the partial sweep there is no stable final MPP marker yet.
                if sweep_plot_mode_select.value == 'P-V':
                    jv_plot.figure = build_pv_plot(temp_df, None, None)
                else:
                    jv_plot.figure = build_jv_plot(temp_df, None, None, settings.area_cm2)
                jv_plot.update()

        if stop_requested:
            status_label.set_text('Stopped during sweep. Saving partial data...')
            await save_partial_sweep(
                run_folder,
                settings,
                V_list,
                I_list,
                T_list,
                Status_list,
                Direction_list,
                comp,
                idn,
                'stopped_during_sweep',
            )
            refresh_sweep_plot()
            update_metric_cards(current_metrics_df)
            sync_progress(1.0)
            ui.notify(f'Partial sweep saved to {run_folder}', type='warning')
            return

        V = np.asarray(V_list, dtype=float)
        I = np.asarray(I_list, dtype=float)
        T = np.asarray(T_list, dtype=float)

        current_metrics_df, current_sweep_df, current_Vmpp, current_Impp, current_Pmpp_mWcm2 = compute_metrics_df(
            V,
            I,
            T,
            settings,
            comp,
        )
        current_sweep_df = add_sweep_extra_columns(current_sweep_df, Status_list, Direction_list)

        update_metric_cards(current_metrics_df)
        refresh_sweep_plot()

        # ====================================================
        # Stage 2: MPPT tracking
        # Always uses adaptive P&O.
        # Important change: the next step is based on commanded v_target,
        # not noisy measured r.v.
        # ====================================================

        tracking_seconds = max(0.0, float(settings.track_seconds))

        status_label.set_text(
            f'Stage 2/2: auto MPPT from Vmpp = {current_Vmpp:.4g} V '
            f'for {tracking_seconds:.1f} s'
        )

        await run.io_bound(instrument.write, ':source:voltage:mode fixed')

        v_low = min(settings.vstart, settings.vend)
        v_high = max(settings.vstart, settings.vend)

        v_target = float(np.clip(current_Vmpp, v_low, v_high))
        await run.io_bound(instrument.set_voltage, v_target)
        await settle_after_voltage_step()

        r = await run.io_bound(read_once, instrument)
        last_power_W = delivered_power_W(r.v, r.i)

        initial_step = max(float(settings.po_step_v), 1e-6)
        min_step = max(initial_step * MPPT_MIN_STEP_FACTOR, 1e-6)
        max_step = max(initial_step * MPPT_MAX_STEP_FACTOR, initial_step)
        dV_current = initial_step
        direction = +1.0

        tseries: List[float] = []
        vset_series: List[float] = []
        vseries: List[float] = []
        iseries: List[float] = []
        pseries_W: List[float] = []
        step_series: List[float] = []
        direction_series: List[float] = []
        status_series: List[int] = []

        tracking_start = time.monotonic()
        tracking_end = tracking_start + tracking_seconds
        next_sample_time = tracking_start
        k = 0

        while not stop_requested:
            now = time.monotonic()

            if now >= tracking_end:
                break

            sleep_s = next_sample_time - now

            if sleep_s > 0:
                await run.io_bound(time.sleep, sleep_s)

            now = time.monotonic()

            if now >= tracking_end:
                break

            k += 1

            candidate_v = v_target + direction * dV_current
            boundary_hit = candidate_v < v_low or candidate_v > v_high
            v_target = float(np.clip(candidate_v, v_low, v_high))

            if boundary_hit:
                direction *= -1.0
                dV_current = max(min_step, dV_current * MPPT_STEP_SHRINK_ON_REVERSAL)

            await run.io_bound(instrument.set_voltage, v_target)
            await settle_after_voltage_step()

            r = await run.io_bound(read_once, instrument)

            elapsed_s = time.monotonic() - tracking_start

            if elapsed_s > tracking_seconds:
                break

            Pnow_W = delivered_power_W(r.v, r.i)

            tseries.append(elapsed_s)
            vset_series.append(v_target)
            vseries.append(r.v)
            iseries.append(r.i)
            pseries_W.append(Pnow_W)
            step_series.append(dV_current)
            direction_series.append(direction)
            status_series.append(r.status)

            deadband_W = max(MPPT_ABS_POWER_DEADBAND_W, abs(last_power_W) * MPPT_REL_POWER_DEADBAND)
            deltaP = Pnow_W - last_power_W

            if deltaP < -deadband_W:
                # Power clearly got worse: reverse and shrink step.
                direction *= -1.0
                dV_current = max(min_step, dV_current * MPPT_STEP_SHRINK_ON_REVERSAL)
            elif deltaP > deadband_W:
                # Power clearly improved: keep direction and allow faster movement.
                dV_current = min(max_step, dV_current * MPPT_STEP_GROW)
            else:
                # Flat/noisy region near MPP: keep direction but soften step to reduce dithering.
                dV_current = max(min_step, dV_current * MPPT_STEP_SHRINK_ON_FLAT)

            last_power_W = Pnow_W

            current_tracking_df = pd.DataFrame({
                'time_s': np.asarray(tseries, dtype=float),
                'voltage_setpoint_V': np.asarray(vset_series, dtype=float),
                'voltage_V': np.asarray(vseries, dtype=float),
                'current_A': np.asarray(iseries, dtype=float),
                'current_density_raw_mA_per_cm2': (
                    np.asarray(iseries, dtype=float) / max(1e-12, settings.area_cm2)
                ) * 1e3,
                'current_density_generated_mA_per_cm2': (
                    -np.asarray(iseries, dtype=float) / max(1e-12, settings.area_cm2)
                ) * 1e3,
                'power_delivered_W': np.asarray(pseries_W, dtype=float),
                'power_density_mW_per_cm2': (
                    np.asarray(pseries_W, dtype=float) * 1e3
                ) / max(1e-12, settings.area_cm2),
                'auto_step_V': np.asarray(step_series, dtype=float),
                'po_direction': np.asarray(direction_series, dtype=float),
                'status': np.asarray(status_series, dtype=int),
            })

            total_elapsed = time.monotonic() - total_start_time

            if tracking_seconds > 0:
                mppt_fraction = min(elapsed_s / tracking_seconds, 1.0)
            else:
                mppt_fraction = 1.0

            sync_progress((50.0 + 50.0 * mppt_fraction) / 100.0)

            live_status_label.set_text(
                f'Total={total_elapsed:.1f} s | '
                f'MPPT={elapsed_s:.1f}/{tracking_seconds:.1f} s | '
                f'Vset={v_target:.4g} V | Vmeas={r.v:.4g} V | '
                f'P={current_tracking_df["power_density_mW_per_cm2"].iloc[-1]:.4g} mW/cm2 | '
                f'step={dV_current:.4g} V'
            )

            if k % settings.live_every == 0 or k == 1:
                power_plot.figure = build_power_plot(current_tracking_df)
                power_plot.update()

            next_sample_time += settings.sample_dt

            if next_sample_time < time.monotonic():
                next_sample_time = time.monotonic()

        if current_tracking_df is not None and not current_tracking_df.empty:
            power_plot.figure = build_power_plot(current_tracking_df)
            power_plot.update()

        run_status = 'stopped_during_mppt' if stop_requested else 'complete'

        if stop_requested:
            status_label.set_text('Stopped. Saving collected data...')

        # ====================================================
        # Save files, complete or partial after MPPT stop
        # ====================================================

        last_paths = await run.io_bound(
            save_run_files,
            run_folder,
            settings,
            current_metrics_df,
            current_sweep_df,
            current_tracking_df,
            idn,
            run_status,
        )

        save_location_label.set_text(f'Saved Excel: {last_paths["excel"]}')

        if run_status == 'complete':
            status_label.set_text('Done. Files saved.')
            ui.notify(f'Saved to {run_folder}', type='positive')
        else:
            status_label.set_text('Stopped. Partial files saved.')
            ui.notify(f'Partial data saved to {run_folder}', type='warning')

        sync_progress(1.0)

    except Exception as e:
        status_label.set_text(f'Error: {e}')
        ui.notify(str(e), type='negative')

    finally:
        try:
            if instrument is not None:
                await run.io_bound(instrument.close)
        except Exception:
            pass

        set_running_state(False)


# ============================================================
# Front end
# ============================================================

ui.colors(
    primary='#2563eb',
    secondary='#0891b2',
    positive='#16a34a',
    negative='#dc2626',
    warning='#f59e0b',
)

ui.add_head_html('''
<style>
/* Make number fields easier to read */
input[type=number]::-webkit-inner-spin-button,
input[type=number]::-webkit-outer-spin-button {
    -webkit-appearance: none;
    margin: 0;
}

input[type=number] {
    -moz-appearance: textfield;
}

.q-field__label {
    font-size: 12px !important;
}

.q-field__native {
    font-size: 17px !important;
    font-weight: 600;
}
body {
    overflow: hidden;
    background:
        radial-gradient(circle at top left, rgba(37,99,235,0.14), transparent 28%),
        radial-gradient(circle at bottom right, rgba(8,145,178,0.14), transparent 28%),
        linear-gradient(135deg, #eff6ff 0%, #f8fafc 50%, #ecfeff 100%);
    font-family: Inter, Arial, sans-serif;
}
.q-card {
    border-radius: 18px;
    box-shadow: 0 10px 28px rgba(15, 23, 42, 0.10);
}
.controls-panel {
    background: rgba(255,255,255,0.96);
    border: 1px solid rgba(37, 99, 235, 0.18);
}
.metrics-panel {
    background: rgba(255,255,255,0.96);
    border: 1px solid rgba(14, 116, 144, 0.18);
}
.plot-panel {
    background: rgba(255,255,255,0.96);
    border: 1px solid rgba(14, 116, 144, 0.18);
    overflow: hidden;
}
.title-chip {
    background: linear-gradient(90deg, #1d4ed8, #0891b2);
    color: white;
    padding: 10px 14px;
    border-radius: 16px;
}
.metric-box {
    background: white;
    border: 1px solid #dbeafe;
    border-radius: 14px;
    padding: 8px 10px;
}
.metric-title {
    color: #64748b;
    font-size: 11px;
    font-weight: 700;
}
.metric-value {
    color: #0f172a;
    font-size: 18px;
    font-weight: 800;
}
.small-muted {
    color: #64748b;
    font-size: 11px;
}
.status-line {
    color: #334155;
    font-size: 12px;
    font-weight: 600;
}
</style>
''')

with ui.column().classes('w-screen h-screen gap-2 p-3'):

    # ========================================================
    # Full settings panel
    # ========================================================

    controls_card = ui.card().classes('w-full controls-panel').style('padding: 10px;')
    with controls_card:
        with ui.row().classes('w-full items-center no-wrap gap-2'):

            with ui.column().classes('title-chip gap-0').style('width: 210px; min-width: 210px;'):
                ui.label('MPPT Tracker').classes('text-lg font-bold')
                ui.label('Keithley 2400 | NiceGUI').classes('text-xs')

            sample_input = ui.input(
                'Sample',
                value='sample_1',
            ).props('dense outlined color=primary').style('width: 145px;')

            output_folder_input = ui.input(
                'Output folder',
                value=str(DEFAULT_OUTPUT_FOLDER),
            ).props('dense outlined color=primary').classes('grow')

            ui.button(
                'Open output',
                on_click=open_output_folder_action,
            ).props('color=secondary').style('width: 115px;')

            ui.button(
                'Open last',
                on_click=open_last_run_folder_action,
            ).props('color=primary').style('width: 100px;')

            ui.button(
                'Hide settings',
                on_click=hide_settings_panel,
            ).props('color=primary flat').style('width: 115px;')

        with ui.row().classes('w-full items-center no-wrap gap-2 mt-2'):

            resource_select = ui.select(
                [],
                label='Detected VISA/GPIB',
                on_change=on_resource_selected,
            ).props('dense outlined color=primary').style('width: 210px;')

            scan_button = ui.button(
                'Scan',
                on_click=scan_visa_action,
            ).props('color=primary').style('width: 75px;')

            resource_input = ui.input(
                'VISA resource',
                value=DEFAULT_RESOURCE,
            ).props('dense outlined color=primary').style('width: 180px;')

            terminal_select = ui.select(
                ['Front', 'Rear'],
                value='Front',
                label='Terminals',
            ).props('dense outlined color=primary').style('width: 115px;')

            area_input = ui.number(
                'Area cm2',
                value=1.0,
                min=0.000001,
                format='%.6f',
            ).props('dense outlined color=primary').style('width: 120px;')

            pin_input = ui.number(
                'Pin mW/cm2',
                value=100.0,
                min=0.0,
                format='%.3f',
            ).props('dense outlined color=primary').style('width: 125px;')

            compliance_input = ui.number(
                'Compliance A',
                value=0.1,
                min=0.000001,
                format='%.6f',
            ).props('dense outlined color=primary').style('width: 130px;')

            vstart_input = ui.number(
                'Vstart',
                value=-0.02,
                format='%.4f',
            ).props('dense outlined color=primary').style('width: 105px;')

            vend_input = ui.number(
                'Vend',
                value=1.0,
                format='%.4f',
            ).props('dense outlined color=primary').style('width: 105px;')

            points_input = ui.number(
                'Points',
                value=121,
                min=2,
                format='%.0f',
            ).props('dense outlined color=primary').style('width: 100px;')

        with ui.row().classes('w-full items-center no-wrap gap-2 mt-2'):

            sweep_mode_select = ui.select(
                ['Forward', 'Reverse', 'Both'],
                value='Forward',
                label='Sweep mode',
            ).props('dense outlined color=primary').style('width: 135px;')

            track_seconds_input = ui.number(
                'MPPT time (s)',
                value=100.0,
                min=0.0,
                format='%.1f',
            ).props('dense outlined color=primary').style('width: 145px;')

            sample_dt_input = ui.number(
                'dt (s)',
                value=0.1,
                min=0.001,
                format='%.3f',
            ).props('dense outlined color=primary').style('width: 110px;')

            po_step_input = ui.number(
                'Initial P&O step',
                value=0.005,
                min=0.0001,
                format='%.4f',
            ).props('dense outlined color=primary').style('width: 145px;')

            live_every_input = ui.number(
                'Plot every',
                value=3,
                min=1,
                format='%.0f',
            ).props('dense outlined color=primary').style('width: 115px;')

            run_button = ui.button(
                'Run Sweep + Auto MPPT',
                on_click=run_measurement_action,
            ).props('color=positive glossy').style('width: 185px;')

            stop_button = ui.button(
                'Stop',
                on_click=request_stop,
            ).props('color=negative glossy').style('width: 90px;')

            stop_button.disable()

            progress_bar = ui.linear_progress(
                value=0,
                show_value=False,
            ).props('instant-feedback color=positive').classes('grow')

            progress_percent_label = ui.label('0.0%').classes(
                'text-sm font-bold text-green-800'
            ).style('width: 60px; text-align: right;')

            ui.label('Total = sweep + MPPT + save').classes('small-muted').style('width: 170px;')

    # ========================================================
    # Compact top bar shown after settings are hidden
    # ========================================================

    compact_status_card = ui.card().classes('w-full controls-panel').style('padding: 8px;')
    with compact_status_card:
        with ui.row().classes('w-full items-center no-wrap gap-2'):

            ui.label('Clean MPPT Tracker').classes('text-base font-bold text-blue-900').style(
                'width: 170px;'
            )

            ui.button(
                'Show settings',
                on_click=show_settings_panel,
            ).props('color=primary').style('width: 120px;')

            ui.button(
                'Open last',
                on_click=open_last_run_folder_action,
            ).props('color=secondary').style('width: 100px;')

            compact_run_button = ui.button(
                'Run',
                on_click=run_measurement_action,
            ).props('color=positive').style('width: 80px;')

            compact_stop_button = ui.button(
                'Stop',
                on_click=request_stop,
            ).props('color=negative').style('width: 80px;')

            compact_stop_button.disable()

            compact_progress = ui.linear_progress(
                value=0,
                show_value=False,
            ).props('instant-feedback color=positive').classes('grow')

            compact_progress_percent_label = ui.label('0.0%').classes(
                'text-sm font-bold text-green-800'
            ).style('width: 60px; text-align: right;')

    compact_status_card.set_visibility(False)

    # ========================================================
    # Metrics and status
    # ========================================================

    with ui.card().classes('w-full metrics-panel').style('padding: 8px;'):
        with ui.row().classes('w-full no-wrap items-center gap-2'):

            with ui.column().classes('metric-box gap-0').style('width: 115px;'):
                ui.label('Jsc').classes('metric-title')
                jsc_card = ui.label('---').classes('metric-value')
                ui.label('mA/cm2').classes('small-muted')

            with ui.column().classes('metric-box gap-0').style('width: 115px;'):
                ui.label('Voc').classes('metric-title')
                voc_card = ui.label('---').classes('metric-value')
                ui.label('V').classes('small-muted')

            with ui.column().classes('metric-box gap-0').style('width: 115px;'):
                ui.label('FF').classes('metric-title')
                ff_card = ui.label('---').classes('metric-value')
                ui.label('%').classes('small-muted')

            with ui.column().classes('metric-box gap-0').style('width: 115px;'):
                ui.label('PCE').classes('metric-title')
                pce_card = ui.label('---').classes('metric-value')
                ui.label('%').classes('small-muted')

            with ui.column().classes('metric-box gap-0').style('width: 115px;'):
                ui.label('Vmpp').classes('metric-title')
                vmpp_card = ui.label('---').classes('metric-value')
                ui.label('V').classes('small-muted')

            with ui.column().classes('metric-box gap-0').style('width: 130px;'):
                ui.label('Pmpp').classes('metric-title')
                pmpp_card = ui.label('---').classes('metric-value')
                ui.label('mW/cm2').classes('small-muted')

            with ui.column().classes('metric-box gap-0').style('width: 115px;'):
                ui.label('Rs').classes('metric-title')
                rs_card = ui.label('---').classes('metric-value')
                ui.label('ohm cm2').classes('small-muted')

            with ui.column().classes('metric-box gap-0').style('width: 115px;'):
                ui.label('Rsh').classes('metric-title')
                rsh_card = ui.label('---').classes('metric-value')
                ui.label('ohm cm2').classes('small-muted')

            with ui.column().classes('grow gap-1'):
                status_label = ui.label('Ready.').classes('status-line')
                live_status_label = ui.label('No active run.').classes('small-muted')
                save_location_label = ui.label(
                    'Saving to: Output folder / Sample name / Sample_timestamp'
                ).classes('small-muted')

    # ========================================================
    # Side-by-side plots
    # ========================================================

    with ui.row().classes('w-full grow no-wrap gap-3'):

        with ui.card().classes('h-full w-1/2 plot-panel').style('padding: 8px;'):
            with ui.row().classes('w-full items-center no-wrap px-2'):
                ui.label('J-V / P-V Sweep').classes('text-lg font-bold text-slate-800')
                ui.space()
                sweep_plot_mode_select = ui.select(
                    ['J-V', 'P-V'],
                    value='J-V',
                    label='View',
                    on_change=lambda: refresh_sweep_plot(),
                ).props('dense outlined color=primary').style('width: 120px;')
            with ui.row().classes('w-full justify-center'):
                jv_plot = ui.plotly(
                    empty_jv_plot()
                ).style(f'width: {PLOT_WIDTH}px; height: {PLOT_HEIGHT}px;')

        with ui.card().classes('h-full w-1/2 plot-panel').style('padding: 8px;'):
            ui.label('MPPT Power Tracking').classes('text-lg font-bold text-slate-800 px-2')
            with ui.row().classes('w-full justify-center'):
                power_plot = ui.plotly(
                    empty_power_plot()
                ).style(f'width: {PLOT_WIDTH}px; height: {PLOT_HEIGHT}px;')


ui.run(
    native=True,
    reload=False,
    title='Clean Keithley 2400 Auto MPPT Tracker',
    window_size=(APP_WIDTH, APP_HEIGHT),
    fullscreen=False,
)

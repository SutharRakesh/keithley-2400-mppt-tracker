# Keithley 2400 PV Measurement Tools

Python/NiceGUI applications for photovoltaic device measurement using a Keithley 2400 SourceMeter.

This repository contains two graphical measurement tools:

1. **MPPT Tracker**  
   Performs a J-V sweep and then runs adaptive maximum power point tracking.

2. **Dark / Light J-V Sweep**  
   Performs dark or light I-V sweeps, displays a live semilog I-V plot, and saves Excel and raw CSV data.

These apps are designed for lab use with a Keithley 2400 SMU connected through VISA/GPIB/USB (tested with K2400 with GIPB/USB cable).

---

## Repository Contents

```text
keithley-2400-pv-tools/
│
├── mppt_tracker.py
├── dark_light_jv_sweep.py
├── requirements.txt
├── README.md
└── .gitignore
```

---

## Apps Included

## 1. MPPT Tracker

File:

```bash
mppt_tracker.py
```

This app performs:

- Keithley 2400 control through PyVISA
- J-V sweep measurement
- MPP extraction from the J-V sweep
- Adaptive perturb-and-observe MPPT tracking
- Live J-V, P-V, and MPPT power plots
- CSV and Excel export
- Front/rear terminal selection
- VISA/GPIB resource scanning

Default output folder:

```text
Documents/Keithley_MPPT_Data
```

The default VISA resource is:

```text
GPIB0::19::INSTR
```

You can change this in the GUI or by setting the environment variable:

```bash
KEITHLEY_VISA=GPIB0::19::INSTR
```

Run the app with:

```bash
python mppt_tracker.py
```

---

## 2. Dark / Light J-V Sweep

File:

```bash
dark_light_jv_sweep.py
```

This app performs:

- Dark or light I-V sweep
- Semilog I-V plotting
- Keithley 2400 control through PyVISA
- Live voltage and current display
- Point-by-point raw CSV saving for crash recovery
- Excel workbook saving
- Hidden raw-data sheet with signed current and metadata
- Backup file creation before overwriting Excel files
- Compliance-current safety stop

The app saves:

- Excel workbook containing the main I-V data
- Hidden raw-data sheet inside the Excel file
- Raw CSV file for each run
- Backup Excel files

Run the app with:

```bash
python dark_light_jv_sweep.py
```

---

## Hardware Requirements

You need:

- Keithley 2400 SourceMeter
- Computer with Python installed
- VISA-compatible connection, for example:
  - GPIB
  - USB-GPIB adapter
  - USB-TMC, depending on your instrument setup
- Installed VISA backend:
  - NI-VISA, recommended for GPIB systems
  - or `pyvisa-py` for supported setups

---

## Software Requirements

Recommended:

- Python 3.9 or newer
- VS Code or another Python editor
- Git, if you want version control
- NI-VISA or another VISA backend

Python packages used by the apps:

```text
nicegui
numpy
pandas
pyvisa
pyvisa-py
plotly
openpyxl
```

---

## Installation

First, download or clone this repository.

Open a terminal inside the repository folder.

Create a virtual environment:

```bash
python -m venv .venv
```

Activate the environment.

On Windows:

```bash
.venv\Scripts\activate
```

On macOS or Linux:

```bash
source .venv/bin/activate
```

Install the required Python packages:

```bash
pip install -r requirements.txt
```

---

## requirements.txt

If you need to recreate the `requirements.txt` file, use this:

```text
nicegui
numpy
pandas
pyvisa
pyvisa-py
plotly
openpyxl
```

---

## Running the MPPT App

To start the MPPT tracker:

```bash
python mppt_tracker.py
```

The app window will open.

Basic workflow:

1. Enter sample name.
2. Select or type the VISA resource.
3. Choose front or rear terminals.
4. Enter device area.
5. Enter illumination power density.
6. Enter compliance current.
7. Set J-V sweep voltage range.
8. Set number of sweep points.
9. Set MPPT tracking time.
10. Click **Run Sweep + Auto MPPT**.

The app will:

1. Connect to the Keithley 2400.
2. Run the J-V sweep.
3. Calculate PV metrics.
4. Start MPPT from the measured MPP voltage.
5. Save CSV and Excel files.

---

## Running the Dark / Light J-V App

To start the dark/light J-V sweep app:

```bash
python dark_light_jv_sweep.py
```

Basic workflow:

1. Enter sample name.
2. Select **Dark** or **Light**.
3. Click **Load sample data** if previous data exists.
4. Enter start voltage.
5. Enter end voltage.
6. Enter voltage step.
7. Enter compliance current in mA.
8. Enter Keithley delay per point.
9. Enter NPLC and averaging settings.
10. Enter the VISA address.
11. Click **Run I-V**.

The app will:

1. Connect to the Keithley 2400.
2. Sweep voltage from start to end.
3. Measure current at each point.
4. Plot absolute current on a log scale.
5. Save data to Excel.
6. Save raw CSV point-by-point.
7. Stop safely if the current reaches the compliance limit.

---

## Important Configuration

## VISA Address

Each lab setup may use a different VISA address.

Common examples:

```text
GPIB0::15::INSTR
GPIB0::19::INSTR
USB0::0x05E6::0x2400::XXXXXXXX::INSTR
```

To find the correct address, use one of these methods:

- NI MAX
- PyVISA resource listing
- The **Scan** button in the MPPT app

---

## Data Folder

Before running the dark/light J-V app, check the data folder near the top of `dark_light_jv_sweep.py`.

Look for:

```python
BASE_DATA_FOLDER = Path(...)
```

Change it to a folder that exists on your computer.

Recommended portable version:

```python
BASE_DATA_FOLDER = Path.home() / 'Documents' / 'Keithley_IV_Data'
```

The MPPT app already saves to:

```python
Path.home() / 'Documents' / 'Keithley_MPPT_Data'
```

---

## Current Compliance

Be careful with compliance current.

In the dark/light J-V app:

```text
Compliance mA = 1.0000 means 1 mA
Compliance mA = 0.0010 means 1 µA
```

Do not confuse mA and A.

Example:

```text
1 mA  = 1.0000 mA
100 µA = 0.1000 mA
10 µA  = 0.0100 mA
1 µA   = 0.0010 mA
```

---

## Current Range

In the dark/light J-V app:

```text
Current range A = 0
```

means auto range.

Example fixed ranges:

```text
1e-3   = 1 mA range
1e-4   = 100 µA range
1e-5   = 10 µA range
1e-6   = 1 µA range
```

If the compliance current is higher than the fixed current range, the app will stop and ask you to increase the range or use auto range.

---

## Output Files

## MPPT Tracker Output

The MPPT tracker saves files such as:

```text
sample_timestamp_sweep.csv
sample_timestamp_mppt_tracking.csv
sample_timestamp_metrics.csv
sample_timestamp_mppt_summary.csv
sample_timestamp_mppt_run.xlsx
sample_timestamp_settings.txt
```

The Excel file contains:

- Settings
- Metrics
- Sweep data
- MPPT tracking data
- MPPT summary

---

## Dark / Light J-V Output

The dark/light J-V app saves an Excel file like:

```text
sample_Dark_IV.xlsx
sample_Light_IV.xlsx
```

It also saves raw CSV files in:

```text
raw_csv/
```

And backup files in:

```text
backups/
```

The Excel workbook contains:

- Main `Data` sheet
- Hidden `Raw_Data` sheet
- Semilog I-V chart

---

## Safety Notes

This software controls real laboratory hardware.

Before running any measurement:

- Check wiring.
- Check front/rear terminal selection.
- Check voltage range.
- Check current compliance.
- Check current range.
- Confirm the device can safely handle the selected voltage and current.
- Do not leave the instrument unattended during first tests.
- Use small voltage ranges and low compliance current when testing a new device.

The software attempts to turn the Keithley output off when a run stops or an error occurs, but the user is responsible for safe instrument operation.

---

## Troubleshooting

## Keithley does not connect

Check:

- Instrument is powered on.
- GPIB/USB cable is connected.
- Correct VISA address is entered.
- NI-VISA or another VISA backend is installed.
- The instrument is not being used by another program.

Try listing VISA resources with Python:

```python
import pyvisa

rm = pyvisa.ResourceManager()
print(rm.list_resources())
```

---

## App opens but measurement does not start

Check:

- Sample name is entered.
- Compliance current is positive.
- Voltage step is positive.
- Current range is compatible with compliance.
- VISA address is correct.

---

## Excel file cannot be saved

This usually happens when the Excel file is already open.

Close the Excel file and run again.

The dark/light J-V app creates backup files before overwriting the workbook.

---

## Current immediately reaches compliance

Possible reasons:

- Device is shorted.
- Voltage range is too high.
- Compliance current is too low.
- Current range is too low.
- Wrong wiring.
- Wrong terminal selection.
- Light intensity is too high for the selected range.

Reduce the voltage range and increase compliance only if it is safe for the device.

---

## GitHub Notes

Recommended `.gitignore`:

```gitignore
__pycache__/
*.pyc
.venv/
venv/

*.csv
*.xlsx
*.log

raw_csv/
backups/
Keithley_MPPT_Data/
Keithley_IV_Data/
data/
output/

.DS_Store
Thumbs.db
.vscode/
.idea/
```

Do not upload private measurement data unless you intentionally want it in the repository.

---

## License

MIT License

You may use, modify, and share this software. Use it at your own risk. The author is not responsible for hardware damage, sample damage, or data loss.
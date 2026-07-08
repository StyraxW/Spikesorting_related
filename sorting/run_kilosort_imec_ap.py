import spikeinterface.full as si
from spikeinterface.sorters import run_sorter
import numpy as np
from probeinterface import Probe
from pathlib import Path
import argparse
import datetime as dt
import os


def _default_run_name() -> str:
	job_id = os.getenv('SLURM_JOB_ID')
	if job_id:
		return f'job{job_id}'
	stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
	return f'run_{stamp}_pid{os.getpid()}'


parser = argparse.ArgumentParser(description='Run Kilosort4 on an IMEC AP stream')
parser.add_argument(
	'--data-folder',
	type=str,
	default=os.getenv('DATA_FOLDER', '/n/scratch/users/x/xiw507/spikesort/spikesort_data/20260625_exp1_g0_imec0'),
	help='Path to SpikeGLX folder containing IMEC AP data'
)
parser.add_argument(
	'--results-folder',
	type=str,
	default=os.getenv('RESULTS_FOLDER', '/n/scratch/users/x/xiw507/spikesort/results'),
	help='Base output folder where sorter results are stored'
)
parser.add_argument(
	'--run-name',
	type=str,
	default=os.getenv('RUN_NAME', _default_run_name()),
	help='Unique suffix for this run; output goes to kilosort4_imec_ap_<run-name>'
)
args = parser.parse_args()

# Load IMEC AP recording
data_folder = Path(args.data_folder).expanduser()
results_folder = Path(args.results_folder).expanduser()
run_name = args.run_name
output_folder = results_folder / f'kilosort4_imec_ap_{run_name}'

if output_folder.exists():
	raise FileExistsError(
		f'Output folder already exists: {output_folder}. '
		'Use a different --run-name or set RUN_NAME to avoid overwriting.'
	)

print(f'Data folder: {data_folder}')
print(f'Results folder: {results_folder}')
print(f'Run name: {run_name}')
print(f'Output folder: {output_folder}')

print('Loading IMEC AP recording...')
recording = si.read_spikeglx(
str(data_folder),
stream_name='imec0.ap'
)

n_channels = len(recording.get_channel_ids())
print(f'Using {n_channels} AP channels')

# Define probe geometry from snsShankMap in .meta file when the channel count matches
rows = [16,22,17,21,18,24,19,26,23,28,25,27,30,29,31,
10,8,6,4,0,2,3,1,12,5,13,7,14,9,15,11,32]
if n_channels <= len(rows):
	rows = rows[:n_channels]
	positions = np.array([[0, r * 25] for r in rows])
	probe = Probe(ndim=2, si_units='um')
	probe.set_contacts(positions=positions, shapes='circle',
	shape_params={'radius': 5})
	probe.set_device_channel_indices(np.arange(n_channels))
	probe.create_auto_shape()
	recording = recording.set_probe(probe)
	print('Probe geometry set')
else:
	print(
		f'Skipping manual probe geometry because the recording has {n_channels} channels '
		f'but the custom layout only defines {len(rows)} contacts.'
	)

# Preprocessing
print('Preprocessing...')
sampling_frequency = recording.get_sampling_frequency()
freq_max = min(5000, sampling_frequency / 2 - 1)
print(f'Using bandpass range 300-{freq_max:.1f} Hz at {sampling_frequency:.1f} Hz sampling rate')
recording = si.bandpass_filter(recording, freq_min=300, freq_max=freq_max)
recording = si.common_reference(recording,
reference='global', operator='median')

# Run Kilosort4
print('Running Kilosort4...')
sorting = run_sorter(
'kilosort4',
recording,
folder=str(output_folder),
remove_existing_folder=False,
verbose=True
)
print('Done! Units found:', sorting.get_num_units())
import spikeinterface.full as si
from pathlib import Path

data_folder = Path('C:\\SGL_DATA\\20260626_LGN_exp_g0\\20260626_LGN_exp_g0_imec0').expanduser()
output_folder = Path('kilosort4_imec_ap_20260626_LGN_exp_job46234680\\sorter_output').expanduser()
output_folder.mkdir(parents=True, exist_ok=True)

recording = si.read_spikeglx(
str(data_folder),
stream_name='imec0.ap'
)

n_channels = len(recording.get_channel_ids())
print(f'Using {n_channels} AP channels')

# Preprocess before exporting so the binary is usable for local waveform viewing
sampling_frequency = recording.get_sampling_frequency()
freq_max = min(5000, sampling_frequency / 2 - 1)
print(f'Using bandpass range 300-{freq_max:.1f} Hz at {sampling_frequency:.1f} Hz sampling rate')
recording = si.bandpass_filter(recording, freq_min=300, freq_max=freq_max)
recording = si.common_reference(recording, reference='global', operator='median')

si.write_binary_recording(
recording,
file_paths=[str(output_folder / 'recording.dat')]
)
print(f'Done! Wrote {output_folder / "recording.dat"}')
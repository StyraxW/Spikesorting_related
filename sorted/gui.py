import spikeinterface.full as si
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import os

import spikeinterface_gui

# 1. Load your recording and sorting outputs from your local folders
# (Point these to the exact paths where your downloaded data lives)
recording = si.read_spikeglx(r"C:\SGL_DATA\20260610_LGN_test3_d-and-v_g0\20260610_LGN_test3_d-and-v_g0_imec0", stream_name="imec0.ap")
sorting = si.read_kilosort(r"C:\Sorted_spikes\20260610_LGN_test3_d-and-v\sorter_output")

# Load the KSLabel from the TSV file to display alongside your curation
tsv_path = r"C:\Sorted_spikes\20260610_LGN_test3_d-and-v\sorter_output\cluster_KSLabel.tsv"
unit_ids = sorting.get_unit_ids()
if os.path.exists(tsv_path):
    print("Found cluster_KSLabel.tsv! Loading original Kilosort labels...")
    df_labels = pd.read_csv(tsv_path, sep='\t')
    label_map = dict(zip(df_labels['cluster_id'], df_labels['KSLabel']))
    ks_labels = [label_map.get(uid, "unlabeled") for uid in unit_ids]
    sorting.set_property('ks_label', ks_labels)
    print(f"Set ks_label property for {len(ks_labels)} units")
else:
    print("Warning: cluster_KSLabel.tsv not found in your output path.")

# Create an empty quality property for you to curate
sorting.set_property('quality', [""] * len(unit_ids))
print("Created empty 'quality' property for curation")

# 2. Preprocess the recording exactly like you did before sorting
# (The GUI needs the filtered data to display the raw waveforms correctly)
recording_filtered = si.bandpass_filter(recording, freq_min=300, freq_max=6000)
recording_cmr = si.common_reference(recording_filtered, reference="global", operator="median")

job_kwargs = dict(n_jobs=-1, progress_bar=True, chunk_duration="1s")

# load or create the SortingAnalyzer folder for this dataset
folder = r"C:\Sorted_spikes\20260610_LGN_test3_d-and-v\sorting_analyzer"
if os.path.exists(folder):
    print("Loading existing analyzer folder...")
    sorting_analyzer = si.SortingAnalyzer.load_from_binary_folder(folder, recording=recording_cmr)
    # Restore the unit properties (ks_label and quality) to the loaded sorting
    sorting_analyzer.sorting.set_property('ks_label', sorting.get_property('ks_label'))
    # Always reset quality to empty for a fresh curation session
    sorting_analyzer.sorting.set_property('quality', [""] * len(unit_ids))
else:
    print("Creating new analyzer folder...")
    sorting_analyzer = si.create_sorting_analyzer(
        sorting,
        recording_cmr,
        format="binary_folder",
        folder=folder,
        overwrite=False,
        **job_kwargs,
    )

extensions = [
    ("random_spikes", dict(method="uniform", max_spikes_per_unit=500)),
    ("waveforms", job_kwargs),
    ("templates", job_kwargs),
    ("noise_levels", {}),
    ("unit_locations", dict(method="monopolar_triangulation")),
    ("isi_histograms", {}),
    ("correlograms", dict(window_ms=100, bin_ms=5.)),
    ("principal_components", dict(n_components=3, mode='by_channel_global', whiten=True, **job_kwargs)),
    ("template_similarity", {}),
    ("spike_amplitudes", job_kwargs),
]

for ext_name, ext_kwargs in extensions:
    if not sorting_analyzer.has_extension(ext_name):
        print(f"Computing extension: {ext_name}")
        sorting_analyzer.compute(ext_name, **ext_kwargs)
    else:
        print(f"Reusing existing extension: {ext_name}")

# Recompute quality metrics without SNR to avoid the histogram crash.
print("Refreshing quality_metrics extension without snr...")
if sorting_analyzer.has_extension("quality_metrics"):
    sorting_analyzer.delete_extension("quality_metrics")
sorting_analyzer.compute("quality_metrics", metric_names=["firing_rate"])

# 5. Launch the GUI!
print("Launching GUI...")
from spikeinterface_gui import run_mainwindow
run_mainwindow(sorting_analyzer, curation=True, displayed_unit_properties=['ks_label'])

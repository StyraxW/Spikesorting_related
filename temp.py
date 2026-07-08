# --- Trial-averaged firing rate + BNC trace across cyclic trials ---
start_time = 2971.0
first_trial_start_s = start_time + np.array([7, 30, 76])  # nominal start time of trial 1 (search window; refined via chirp detection)
# [0, 48, 67] for 12hz
# [7, 30, 76] for 4+12hz
# [16, 39, 60] for 18+ 12hz
trial_period_s = np.array([90, 90, 90])       # nominal spacing between trials
n_trials = 10                # number of trials to average
lapse_s = 1.5               # window length per trial, from each trial's aligned start
bin_size_s = 0.01            # firing-rate bin width (s)
smooth_bins = 10              # moving-average window, in bins

trial_start_times = np.array([first_trial_start_s + i * trial_period_s for i in range(n_trials)]).flatten()

bin_edges = np.arange(0, lapse_s + bin_size_s, bin_size_s)
bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
n_units = len(lgn_spike_times)

show_bnc = bool(nidq_file_path)
if show_bnc:
    nidq_recording = se.read_spikeglx(folder_path=nidq_file_path, stream_id="nidq")
    nidq_fs = nidq_recording.get_sampling_frequency()
    bnc_channel_id = nidq_recording.channel_ids[0]

trial_rates = []
trial_bnc_traces = []
for nominal_start in trial_start_times:
    trial_start = nominal_start
    if show_bnc:
        # re-center each trial on its actual chirp onset, same logic as plot_raster,
        # since the ~90s cadence isn't perfectly accurate
        search_time, search_trace = _load_bnc_window(
            nidq_recording, nidq_fs, bnc_channel_id, nominal_start, nominal_start + lapse_s
        )
        onsets = _detect_chirp_onsets(search_time, search_trace, None, 30.0)
        if onsets.size:
            trial_start = onsets[0] - 0.5
    trial_end = trial_start + lapse_s

    pooled_spikes = np.concatenate([
        times[(times >= trial_start) & (times < trial_end)] - trial_start
        for times in lgn_spike_times.values()
    ]) if n_units else np.array([])
    counts, _ = np.histogram(pooled_spikes, bins=bin_edges)
    trial_rates.append(counts / (bin_size_s * n_units))

    if show_bnc:
        bnc_time, bnc_trace = _load_bnc_window(nidq_recording, nidq_fs, bnc_channel_id, trial_start, trial_end)
        trial_bnc_traces.append(np.interp(bin_centers, bnc_time - trial_start, bnc_trace))

trial_rates = np.array(trial_rates)
mean_rate = trial_rates.mean(axis=0)
smoothed_rate = np.convolve(mean_rate, np.ones(smooth_bins) / smooth_bins, mode="same")

if show_bnc:
    trial_bnc_traces = np.array(trial_bnc_traces)
    mean_bnc = trial_bnc_traces.mean(axis=0)
    fig, (bnc_ax, rate_ax) = plt.subplots(
        2, 1, sharex=True, figsize=(12, 6), gridspec_kw={"height_ratios": [1, 3]}
    )
    bnc_ax.plot(bin_centers, mean_bnc)
    bnc_ax.set_ylabel(f"BNC ({bnc_channel_id})\nmean of {n_trials*3} trials")
else:
    fig, rate_ax = plt.subplots(figsize=(12, 4))

rate_ax.plot(bin_centers, smoothed_rate)
rate_ax.set_xlabel("Time from trial onset (s)")
rate_ax.set_ylabel("Firing rate (Hz)")
rate_ax.set_title(f"Mean firing rate across {n_units} units, {n_trials*3} trials")
plt.tight_layout()
plt.show()
import sys
from datetime import datetime
from collections import OrderedDict
import numpy as np
import h5py

from .data import Experiment, SyncRecording, PatchClampRecording, Trace
from .test_pulse import PatchClampTestPulse


class MiesNwb(Experiment):
    """Class for accessing data from a MIES-generated NWB file.
    """
    def __init__(self, filename):
        Experiment.__init__(self)
        self.filename = filename
        self.hdf = None
        self._sweeps = None
        self._groups = None
        self._notebook = None
        self.open()
        
    def notebook(self):
        """Return compiled data from the lab notebook.

        The format is a dict like ``{sweep_number: [ch1, ch2, ...]}`` that contains one key:value
        pair per sweep. Each value is a list containing one metadata dict for each channel in the
        sweep. For example::

            nwb.notebook()[sweep_id][channel_id][metadata_key]
        """
        if self._notebook is None:
            # collect all lab notebook entries
            nb_entries = OrderedDict()
            device = self.hdf['general/devices'].keys()[0].split('_',1)[-1]
            nb_keys = self.hdf['general']['labnotebook'][device]['numericalKeys'][0]
            nb_fields = OrderedDict([(k, i) for i,k in enumerate(nb_keys)])
            nb = self.hdf['general']['labnotebook'][device]['numericalValues']

            # EntrySourceType field is needed to distinguish between records created by TP vs sweep
            entry_source_type_index = nb_fields.get('EntrySourceType', None)
            
            nb_iter = iter(range(nb.shape[0]))  # so we can skip multiple rows from within the loop
            for i in nb_iter:
                rec = nb[i]
                sweep_num = rec[0,0]

                # ignore records with no associated sweep
                if np.isnan(sweep_num):
                    continue

                # ignore records that were generated by test pulse
                # (note: entrySourceType is nan if an older pxp is re-exported to nwb using newer MIES)
                if entry_source_type_index is not None and not np.isnan(rec[entry_source_type_index][0]):
                    if rec[entry_source_type_index][0] != 0:
                        continue
                elif i < nb.shape[0] - 1:
                    # Older files may be missing EntrySourceType. In this case, we can identify TP blocks
                    # as two records containing a "TP Peak Resistance" value in the first record followed
                    # by a "TP Pulse Duration" value in the second record.
                    tp_peak = rec[nb_fields['TP Peak Resistance']]
                    if any(np.isfinite(tp_peak)):
                        tp_dur = nb[i+1][nb_fields['TP Pulse Duration']]
                        if any(np.isfinite(tp_dur)):
                            nb_iter.next()
                            continue

                sweep_num = int(sweep_num)
                # each sweep gets multiple nb records; for each field we use the last non-nan value in any record
                if sweep_num not in nb_entries:
                    nb_entries[sweep_num] = np.array(rec)
                else:
                    mask = ~np.isnan(rec)
                    nb_entries[sweep_num][mask] = rec[mask]

            for swid, entry in nb_entries.items():
                # last column is "global"; applies to all channels
                mask = ~np.isnan(entry[:,8])
                entry[mask] = entry[:,8:9][mask]
    
                # first 4 fields of first column apply to all channels
                entry[:4] = entry[:4, 0:1]

                # async AD fields (notably used to record temperature) appear
                # only in column 0, but might move to column 8 later? Since these
                # are not channel-specific, we'll copy them to all channels
                for i,k in enumerate(nb_keys):
                    if not k.startswith('Async AD '):
                        continue
                    entry[i] = entry[i, 0]

                # convert to list-o-dicts
                meta = []
                for i in range(entry.shape[1]):
                    tm = entry[:, i]
                    meta.append(OrderedDict([(nb_keys[j], (None if np.isnan(tm[j]) else tm[j])) for j in range(len(nb_keys))]))
                nb_entries[swid] = meta

            self._notebook = nb_entries
        return self._notebook

    @property
    def contents(self):
        """A list of all sweeps in this file.
        """
        if self._sweeps is None:
            sweeps = set()
            for k in self.hdf['acquisition/timeseries'].keys():
                a, b, c = k.split('_')[:3] #discard anything past AD# channel
                sweeps.add(b)
            self._sweeps = [self.create_sync_recording(int(sweep_id)) for sweep_id in sorted(list(sweeps))]
        return self._sweeps
    
    def create_sync_recording(self, sweep_id):
        return MiesSyncRecording(self, sweep_id)

    def close(self):
        self.hdf.close()
        self.hdf = None

    def open(self):
        if self.hdf is not None:
            return
        try:
            self.hdf = h5py.File(self.filename, 'r')
        except Exception:
            print("Error opening: %s" % self.filename)
            raise

    def __enter__(self):
        self.open()
        return self
    
    def __exit__(self, *args):
        self.close()

    #def sweep_groups(self, keys=('shape', 'stim_name', 'V-Clamp Holding Level', 'Clamp Mode')):
        #"""Return a list of sweep groups--each group contains one or more
        #contiguous sweeps with matching metadata.

        #The *keys* argument contains the set of metadata keys that are compared
        #to determine group boundaries.

        #This is used mainly for grouping together sweeps that were repeated or were 
        #part of a stim set.
        #"""
        #keys = list(keys)
        #if 'shape' in keys:
            #group_by_shape = True
            #keys.remove('shape')
        #else:
            #group_by_shape = False

        #if self._groups is None:
            #current_group = []
            #current_meta = None
            #groups = [current_group]
            #for sweep in self.sweeps():
                ## get selected metadata for grouping sweeps
                #meta = {}
                #for ch in sweep.devices:
                    #rec = sweep[ch]
                    #m = rec.meta()
                    #meta[ch] = {k:m.get(k) for k in keys}
                    #if group_by_shape:
                        #meta[ch]['shape'] = rec['primary'].shape[0]

                #if len(current_group) == 0:
                    #current_group.append(sweep)
                    #current_meta = meta
                #else:
                    #if meta == current_meta:
                        #current_group.append(sweep)
                    #else:
                        #current_group = [sweep]
                        #current_meta = meta
                        #groups.append(current_group)
            #self._groups = [SweepGroup(self, grp) for grp in groups]
        #return self._groups

    @staticmethod
    def pack_sweep_data(sweeps):
        """Return a single array containing all data from a list of sweeps.
        
        The array shape is (sweeps, channels, samples, 2), where the final axis
        contains recorded data at index 0 and the stimulus at index 1.

        All sweeps must have the same length and number of channels.
        """
        sweeps = [s.data() for s in sweeps]
        data = np.empty((len(sweeps),) + sweeps[0].shape, dtype=sweeps[0].dtype)
        for i in range(len(sweeps)):
            data[i] = sweeps[i]
        return data

    @staticmethod
    def igorpro_date(timestamp):
        """Convert an IgorPro timestamp (seconds since 1904-01-01) to a datetime
        object.
        """
        dt = datetime(1970,1,1) - datetime(1904,1,1)
        return datetime.utcfromtimestamp(timestamp) - dt

    @property
    def children(self):
        return self.contents
    
    def __repr__(self):
        return "<MiesNwb %s>" % self.filename


class MiesTrace(Trace):
    def __init__(self, recording, chan):
        start = recording._meta['start_time']
        
        # Note: this is also available in meta()['Minimum Sampling interval'],
        # but that key is missing in some older NWB files.
        dt = recording.primary_hdf.attrs['IGORWaveScaling'][1,0] / 1000.
        Trace.__init__(self, recording=recording, channel_id=chan, dt=dt, start_time=start)
    
    @property
    def data(self):
        if self._data is None:
            rec = self.recording
            chan = self.channel_id
            if chan == 'primary':
                scale = 1e-12 if rec.clamp_mode == 'vc' else 1e-3
                self._data = np.array(rec.primary_hdf) * scale
            elif chan == 'command':
                scale = 1e-3 if rec.clamp_mode == 'vc' else 1e-12
                self._data = np.array(rec.command_hdf) * scale
        return self._data
    
    
class MiesRecording(PatchClampRecording):
    """A single stimulus / recording made on a single channel.
    """
    def __init__(self, sweep, sweep_id, ad_chan):
        self._sweep = sweep
        self._nwb = sweep._nwb
        self._trace_id = (sweep_id, ad_chan)
        self._inserted_test_pulse = None
        self._hdf_group = self._nwb.hdf['acquisition/timeseries/data_%05d_AD%d' % self._trace_id]
        self._da_chan = None
        headstage_id = int(self._hdf_group['electrode_name'].value[0].split('_')[1])
        start = self._hdf_group['starting_time'].value[0]
        
        PatchClampRecording.__init__(self, device_type='MultiClamp 700', device_id=headstage_id,
                                     sync_recording=sweep, start_time=start)

        # update metadata
        nb = self._nwb.notebook()[int(self._trace_id[0])][headstage_id]
        self._meta['stim_name'] = self._hdf_group['stimulus_description'].value[0]
        self.meta['holding_potential'] = None if nb['V-Clamp Holding Level'] is None else nb['V-Clamp Holding Level'] * 1e-3
        self.meta['holding_current'] = None if nb['I-Clamp Holding Level'] is None else nb['I-Clamp Holding Level'] * 1e-12
        self._meta['notebook'] = nb
        if nb['Clamp Mode'] == 0:
            self._meta['clamp_mode'] = 'vc'
        else:
            self._meta['clamp_mode'] = 'ic'
            self._meta['bridge_balance'] = 0 if nb['Bridge Bal Enable'] == 0.0 else nb['Bridge Bal Value'] * 1e6
        self._meta['lpf_cutoff'] = nb['LPF Cutoff']
        self._meta['pipette_offset'] = nb['Pipette Offset'] * 1e-3

        self._channels['primary'] = MiesTrace(self, 'primary')
        self._channels['command'] = MiesTrace(self, 'command')
    
    @property
    def clamp_mode(self):
        return 'vc' if self.meta['notebook']['Clamp Mode'] == 0 else 'ic'

    @property
    def primary_hdf(self):
        """The raw HDF5 data containing the primary channel recording
        """
        return self._hdf_group['data']        

    @property
    def command_hdf(self):
        """The raw HDF5 data containing the stimulus command 
        """
        return self._nwb.hdf['stimulus/presentation/data_%05d_DA%d/data' % (self._trace_id[0], self.da_chan())]

    @property
    def nearest_test_pulse(self):
        """The test pulse that was acquired nearest to this recording.
        """
        # Maybe we could point to a nearby TP if there was none inserted?
        return self.inserted_test_pulse
    
    @property
    def inserted_test_pulse(self):
        """Return the test pulse inserted at the beginning of the recording,
        or None if no pulse was inserted.
        """
        if self._inserted_test_pulse is None:
            if self.meta['notebook']['TP Insert Checkbox'] != 1.0:
                return None
            
            # get start/stop indices of the test pulse region
            pdur = self.meta['notebook']['TP Pulse Duration'] / 1000.
            bdur = pdur / (1.0 - 2. * self.meta['notebook']['TP Baseline Fraction'])
            tdur = pdur + 2 * bdur
            start = 0
            stop = start + int(tdur / self['primary'].dt)
            
            tp = PatchClampTestPulse(self, indices=(start, stop))
            
            # Record amplitude as specified by MIES
            if self.clamp_mode == 'vc':
                amp = self.meta['notebook']['TP Amplitude VC'] * 1e-3
            else:
                amp = self.meta['notebook']['TP Amplitude IC'] * 1e-12
            tp._meta['mies_pulse_amplitude'] = amp
            
            self._inserted_test_pulse = tp
        return self._inserted_test_pulse

    def _get_stim_data(self):
        scale = 1e-3 if self.clamp_mode == 'vc' else 1e-12
        return np.array(self.command_hdf) * scale

    def da_chan(self):
        """Return the DA channel ID for this recording.
        """
        if self._da_chan is None:
            hdf = self._nwb.hdf['stimulus/presentation']
            stims = [k for k in hdf.keys() if k.startswith('data_%05d_'%self._trace_id[0])]
            for s in stims:
                elec = hdf[s]['electrode_name'].value[0]
                if elec == 'electrode_%d' % self.device_id:
                    self._da_chan = int(s.split('_')[-1][2:])
            if self._da_chan is None:
                raise Exception("Cannot find DA channel for headstage %d" % self.device_id)
        return self._da_chan

    def __repr__(self):
        mode = self.clamp_mode
        if mode == 'vc':
            extra = "mode=VC holding=%d" % int(np.round(self.holding_potential))
        elif mode == 'ic':
            extra = "mode=IC holding=%d" % int(np.round(self.holding_current))

        return "<%s %d.%d  stim=%s %s>" % (self.__class__.__name__, self._trace_id[0], self.device_id, self._meta['stim_name'], extra)


class MiesSyncRecording(SyncRecording):
    """Represents one recorded sweep with multiple channels.
    """
    def __init__(self, nwb, sweep_id):
        sweep_id = int(sweep_id)
        self._nwb = nwb
        self._sweep_id = sweep_id
        self._chan_meta = None
        self._traces = None
        self._notebook_entry = None

        # get list of all A/D channels in this sweep
        chans = []
        for k in self._nwb.hdf['acquisition/timeseries'].keys():
            if not k.startswith('data_%05d_' % sweep_id):
                continue
            chans.append(int(k.split('_')[2][2:]))
        self._ad_channels = sorted(chans)
        
        devs = OrderedDict()

        for ch in self._ad_channels:
            rec = self.create_recording(sweep_id, ch)
            devs[rec.device_id] = rec
        SyncRecording.__init__(self, devs, parent=nwb)
        self._meta['sweep_id'] = sweep_id

    def create_recording(self, sweep_id, ch):
        return MiesRecording(self, sweep_id, ch)

    def __repr__(self):
        return "<%s sweep=%d>" % (self.__class__.__name__, self._sweep_id)

    #def channel_meta(self, all_chans=False):
        #"""Return a dict containing the metadata key/value pairs that are shared
        #across all traces in this sweep.

        #If *all_chans* is True, then instead return a list of values for each meta key.
        #"""
        #if all_chans:
            #m = OrderedDict()
            #for dev in self.devices:
                #rec = self[dev]
                #for k,v in rec.meta().items():
                    #if k not in m:
                        #m[k] = []
                    #m[k].append(v)
            #return m

        #else:
            #if self._chan_meta is None:
                #traces = [self.traces()[chan] for chan in self.channels()]
                #m = traces[0].meta().copy()
                #for tr in traces[1:]:
                    #trm = tr.meta()
                    #rem = []
                    #for k in m:
                        #if k not in trm or trm[k] != m[k]:
                            #rem.append(k)
                #for k in rem:
                    #m.pop(k)

                #self._chan_meta = m
            #return self._chan_meta
        
    #def data(self):
        #"""Return a single array containing recorded data and stimuli from all channels recorded
        #during this sweep.
        
        #The array shape is (channels, samples, 2).
        #"""
        #traces = self.traces()
        #chan_data = [traces[ch].data() for ch in sorted(list(traces))]
        #arr = np.empty((len(chan_data),) + chan_data[0].shape, chan_data[0].dtype)
        #for i,data in enumerate(chan_data):
            #arr[i] = data
        #return arr

    #def shape(self):
        #return (len(self.channels()), len(self.traces().values()[0]))

    #def describe(self):
        #"""Return a string description of this sweep.
        #"""
        #return "\n".join(map(repr, self.traces().values()))

    #def start_time(self):
        #return MiesNwb.igorpro_date(self.meta()['TimeStamp'])


#class SweepGroup(object):
    #"""Represents a collection of Sweeps that were acquired contiguously and
    #all share the same stimulus parameters.
    #"""
    #def __init__(self, nwb, sweeps):
        #self._nwb = nwb
        #self.sweeps = sweeps
        
    #def meta(self):
        #"""Return metadata from the first sweep in this group.
        #"""
        #return self.sweeps[0].meta()
        
    #def data(self):
        #"""Return a single array containing all data from all sweeps in this
        #group.
        
        #The array shape is (sweeps, channels, samples, 2).
        #"""
        #return self._nwb.pack_sweep_data(self.sweeps)

    #def describe(self):
        #"""Return a string description of this group (taken from the first sweep).
        #"""
        #return self.sweeps[0].describe()

    #def __repr__(self):
        #ids = self.sweeps[0].sweep_id, self.sweeps[-1].sweep_id
        #return "<SweepGroup %d-%d>" % ids

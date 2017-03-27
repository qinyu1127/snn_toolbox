# -*- coding: utf-8 -*-
"""Building SNNs using INI simulator.

The modules in ``target_simulators`` package allow building a spiking network
and exporting it for use in a spiking simulator.

This particular module offers functionality for the INI simulator. Adding
another simulator requires implementing the class ``SNN_compiled`` with its
methods tailored to the specific simulator.

Created on Thu May 19 14:59:30 2016

@author: rbodo
"""

# For compatibility with python2
from __future__ import division, absolute_import
from __future__ import print_function, unicode_literals

import os
import numpy as np
from textwrap import dedent
import keras
from future import standard_library
from snntoolbox.config import settings, initialize_simulator
from snntoolbox.core.inisim import bias_relaxation

standard_library.install_aliases()

if settings['online_normalization']:
    lidx = 0

remove_classifier = False


class SNN:
    """
    The compiled spiking neural network, ready for testing in a spiking
    simulator.

    Attributes
    ----------

    sim: Simulator
        Module containing utility functions of spiking simulator. Result of
        calling ``snntoolbox.config.initialize_simulator()``. For instance, if
        using Brian simulator, this initialization would be equivalent to
        ``import pyNN.brian as sim``.

    snn: keras.models.Model
        Keras model. This is the output format of the compiled spiking model
        because INI simulator runs networks of layers that are derived from
        Keras layer base classes.

    Methods
    -------

    build:
        Convert an ANN to a spiking neural network, using layers derived from
        Keras base classes.
    run:
        Simulate a spiking network.
    save:
        Write model architecture and parameters to disk.
    load:
        Load model architecture and parameters from disk.
    end_sim:
        Clean up after simulation. Not needed in this simulator, so do a
        ``pass``.
    """

    def __init__(self, s=None):
        """Init function."""

        if s is None:
            s = settings

        self.sim = initialize_simulator(s['simulator'])
        self.snn = None
        self.parsed_model = None
        # Logging variables
        self.spiketrains_n_b_l_t = self.activations_n_b_l = None
        self.input_b_l_t = self.mem_n_b_l_t = self.top1err_b_t = None
        # ``rescale_fac`` globally scales spike probability when using Poisson
        # input.
        self.rescale_fac = 1
        self.num_classes = 0

    # noinspection PyUnusedLocal
    def build(self, parsed_model, verbose=True, **kwargs):
        """Compile a SNN to prepare for simulation with INI simulator.

        Convert an ANN to a spiking neural network, using layers derived from
        Keras base classes.

        Aims at simulating the network on a self-implemented Integrate-and-Fire
        simulator using a timestepped approach.

        Sets the ``snn`` attribute of this class.

        Parameters
        ----------

        parsed_model: Keras model
            Parsed input model; result of applying
            ``model_lib.extract(input_model)`` to the ``input model``.
        verbose: Optional[bool]
            Whether or not to print status messages.
        """

        if verbose:
            print("Building spiking model...")

        self.parsed_model = parsed_model

        if 'batch_size' in kwargs:
            batch_shape = [kwargs['batch_size']] + \
                          list(parsed_model.layers[0].batch_input_shape)[1:]
        else:
            batch_shape = list(parsed_model.layers[0].batch_input_shape)
        if batch_shape[0] is None:
            batch_shape[0] = settings['batch_size']

        input_images = keras.layers.Input(batch_shape=batch_shape)
        spiking_layers = {parsed_model.layers[0].name: input_images}

        # Iterate over layers to create spiking neurons and connections.
        for layer in parsed_model.layers[1:]:  # Skip input layer
            if verbose:
                print("Building layer: {}".format(layer.name))
            spike_layer = getattr(self.sim, 'Spike' + layer.__class__.__name__)
            inbound = [spiking_layers[inb.name] for inb in
                       layer.inbound_nodes[0].inbound_layers]
            spiking_layers[layer.name] = \
                spike_layer.from_config(layer.get_config())(inbound)

        if verbose:
            print("Compiling spiking model...\n")
        self.snn = keras.models.Model(
            input_images, spiking_layers[parsed_model.layers[-1].name])
        self.snn.compile('sgd', 'categorical_crossentropy',
                         metrics=['accuracy'])
        self.snn.set_weights(parsed_model.get_weights())
        for layer in self.snn.layers:
            if hasattr(layer, 'b'):
                # Adjust biases to time resolution of simulator.
                layer.b.set_value(layer.b.get_value() * settings['dt'])
                if bias_relaxation:  # Experimental
                    layer.b0.set_value(layer.b.get_value())

    def run(self, x_test=None, y_test=None, dataflow=None, **kwargs):
        """Simulate a SNN with LIF and Poisson input.

        Simulate a spiking network with leaky integrate-and-fire units and
        Poisson input, using mean pooling and a timestepped approach.

        If ``settings['verbose'] > 1``, the toolbox plots the spiketrains
        and spikerates of each neuron in each layer, for the first sample of
        the first batch of ``x_test``.

        This is somewhat costly in terms of memory and time, but can be useful
        for debugging the network's general functioning.

        Parameters
        ----------

        x_test: float32 array
            The input samples to test.
            With data of the form (channels, num_rows, num_cols),
            x_test has dimension (num_samples, channels*num_rows*num_cols)
            for a multi-layer perceptron, and
            (num_samples, channels, num_rows, num_cols) for a convolutional
            net.
        y_test: float32 array
            Ground truth of test data. Has dimension (num_samples, num_classes)
        dataflow : keras.DataFlowGenerator

        kwargs: Optional[dict]
            - s: Optional[dict]
                Settings. If not given, the ``snntoolobx.config.settings``
                dictionary is used.
            - path: Optional[str]
                Where to store the output plots. If no path given, this value is
                taken from the settings dictionary.

        Returns
        -------

        top1acc_total: float
            Number of correctly classified samples divided by total number of
            test samples.
        """

        # from ann_architectures.imagenet.utils import preprocess_input
        from snntoolbox.core.util import get_activations_batch, get_top5score
        from snntoolbox.core.util import echo
        from snntoolbox.io_utils.plotting import output_graphs
        from snntoolbox.io_utils.plotting import plot_confusion_matrix
        from snntoolbox.io_utils.plotting import plot_error_vs_time
        from snntoolbox.io_utils.plotting import plot_input_image

        s = kwargs['settings'] if 'settings' in kwargs else settings
        log_dir = kwargs['path'] if 'path' in kwargs \
            else s['log_dir_of_current_run']

        # Load neuron layers and connections if conversion was done during a
        # previous session.
        if self.snn is None:
            print("Restoring spiking network...\n")
            self.load()
            self.parsed_model = keras.models.load_model(os.path.join(
                s['path_wd'], s['filename_parsed_model']+'.h5'))

        si = s['sample_indices_to_test'] \
            if 'sample_indices_to_test' in s else []
        if not si == []:
            assert len(si) == s['batch_size'], dedent("""
                You attempted to test the SNN on a total number of samples that
                is not compatible with the batch size with which the SNN was
                converted. Either change the number of samples to test to be
                equal to the batch size, or convert the ANN again using the
                corresponding batch size.""")
            if x_test is None:
                # Probably need to turn off shuffling in ImageDataGenerator
                # for this to produce the desired samples.
                x_test, y_test = dataflow.next()
            x_test = np.array([x_test[i] for i in si])
            y_test = np.array([y_test[i] for i in si])

        # Divide the test set into batches and run all samples in a batch in
        # parallel.
        num_batches = int(1e9) if s['dvs_input'] else \
            int(np.floor(s['num_to_test'] / s['batch_size']))

        top5score_moving = 0
        truth_d = []  # Filled up with correct classes of all test samples.
        guesses_d = []  # Filled up with guessed classes of all test samples.
        guesses_b = np.zeros(s['batch_size'])  # Guesses of one batch.
        x_b_xaddr = x_b_yaddr = x_b_ts = None
        x_b = y_b = None
        dvs_gen = None
        if s['dvs_input']:
            dvs_gen = DVSIterator(
                os.path.join(s['dataset_path'], 'DVS'), s['batch_size'],
                s['label_dict'], s['subsample_facs'],
                s['num_dvs_events_per_sample'])

        # Prepare files to write moving accuracy and error to.
        path_log_vars = os.path.join(log_dir, 'log_vars')
        if not os.path.isdir(path_log_vars):
            os.makedirs(path_log_vars)
        path_acc = os.path.join(log_dir, 'accuracy.txt')
        if os.path.isfile(path_acc):
            os.remove(path_acc)

        self.init_log_vars()
        self.num_classes = self.snn.layers[-1].output_shape[-1]

        for batch_idx in range(num_batches):
            # Get a batch of samples
            if x_test is None:
                x_b, y_b = dataflow.next()
                imagenet = True
                if imagenet:  # Only for imagenet!
                    print("Preprocessing input for ImageNet")
                    x_b = np.add(np.multiply(x_b, 2. / 255.), - 1.)
                    # x_b = preprocess_input(x_b)
            elif not s['dvs_input']:
                batch_idxs = range(s['batch_size'] * batch_idx,
                                   s['batch_size'] * (batch_idx + 1))
                x_b = x_test[batch_idxs, :]
                y_b = y_test[batch_idxs, :]
            if s['dvs_input']:
                try:
                    x_b_xaddr, x_b_yaddr, x_b_ts, y_b = dvs_gen.__next__()
                except StopIteration:
                    break
            truth_b = np.argmax(y_b, axis=1)

            # Either use Poisson spiketrains as inputs to the SNN, or take the
            # original data.
            if s['poisson_input']:
                # This factor determines the probability threshold for cells in
                # the input layer to fire a spike. Increasing ``input_rate``
                # increases the firing rate of the input and subsequent layers.
                self.rescale_fac = np.max(x_b)*1000/s['input_rate']/s['dt']
            elif s['dvs_input']:
                pass
            else:
                # Simply use the analog values of the original data as input.
                inp = x_b * s['dt']
                # inp = np.random.random_sample(x_b.shape)

            # Reset network variables.
            self.reset()

            # Allocate variables to monitor during simulation
            output_b_l = np.zeros((s['batch_size'], self.num_classes), 'int32')

            input_spikecount = 0
            sim_step_int = 0
            print("Starting new simulation...\n")
            # Loop through simulation time.
            for sim_step in range(s['dt'], s['duration']+s['dt'], s['dt']):
                # Generate input, in case it changes with each simulation step:
                if s['poisson_input']:
                    if input_spikecount < s['num_poisson_events_per_sample'] \
                            or s['num_poisson_events_per_sample'] < 0:
                        spike_snapshot = np.random.random_sample(x_b.shape) \
                                         * self.rescale_fac
                        inp = (spike_snapshot <= np.abs(x_b)).astype('float32')
                        input_spikecount += \
                            np.count_nonzero(inp) / s['batch_size']
                        # For BinaryNets, with input that is not normalized and
                        # not all positive, we stimulate with spikes of the same
                        # size as the maximum activation, and the same sign as
                        # the corresponding activation. Is there a better
                        # solution?
                        # inp *= np.max(x_b) * np.sign(x_b)
                    else:
                        inp = np.zeros(x_b.shape)
                elif s['dvs_input']:
                    # print("Generating a batch of even-frames...")
                    inp = np.zeros(self.snn.layers[0].batch_input_shape,
                                   'float32')
                    for sample_idx in range(s['batch_size']):
                        # Buffer event sequence because we will be removing
                        # elements from original list:
                        xaddr_sample = list(x_b_xaddr[sample_idx])
                        yaddr_sample = list(x_b_yaddr[sample_idx])
                        ts_sample = list(x_b_ts[sample_idx])
                        first_ts_of_frame = ts_sample[0] if ts_sample else 0
                        for x, y, ts in zip(xaddr_sample, yaddr_sample,
                                            ts_sample):
                            if inp[sample_idx, 0, y, x] == 0:
                                inp[sample_idx, 0, y, x] = 1
                                # Can't use .popleft()
                                x_b_xaddr[sample_idx].remove(x)
                                x_b_yaddr[sample_idx].remove(y)
                                x_b_ts[sample_idx].remove(ts)
                            if ts - first_ts_of_frame > s['eventframe_width']:
                                break
                # Main step: Propagate input through network and record output
                # spikes.
                self.set_time(sim_step)
                out_spikes = self.snn.predict_on_batch(inp)
                if remove_classifier:
                    output_b_l += np.argmax(np.reshape(
                        out_spikes.astype('int32'), (out_spikes.shape[0], -1)),
                        axis=1)
                else:
                    output_b_l += out_spikes.astype('int32')
                # Get result by comparing the guessed class (i.e. the index
                # of the neuron in the last layer which spiked most) to the
                # ground truth.
                guesses_b = np.argmax(output_b_l, axis=1)
                # Find sample indices for which there was no output spike yet
                undecided = np.where(np.sum(output_b_l != 0, axis=1) == 0)
                # Assign negative value such that undecided samples count as
                # wrongly classified.
                guesses_b[undecided] = -1
                self.top1err_b_t[:, sim_step_int] = truth_b != guesses_b
                # Record neuron variables.
                i = j = 0
                for layer in self.snn.layers:
                    if hasattr(layer, 'spiketrain') \
                            and self.spiketrains_n_b_l_t is not None:
                        self.spiketrains_n_b_l_t[i][0][..., sim_step_int] = \
                            layer.spiketrain.get_value()
                        i += 1
                    if hasattr(layer, 'mem') and self.mem_n_b_l_t is not None:
                        self.mem_n_b_l_t[j][0][..., sim_step_int] = \
                            layer.mem.get_value()
                        j += 1
                if 'input_b_l_t' in s['log_vars']:
                    self.input_b_l_t[Ellipsis, sim_step_int] = inp
                top1err = np.around(np.mean(self.top1err_b_t[:, sim_step_int]),
                                    4)
                sim_step_int += 1
                if s['verbose'] > 0 and sim_step % 1 == 0:
                    echo('{:.2%}_'.format(1-top1err))

            num_samples_seen = (batch_idx + 1) * s['batch_size']
            truth_d += list(truth_b)
            guesses_d += list(guesses_b)
            top1acc_moving = np.mean(np.array(truth_d) == np.array(guesses_d))
            top5score_moving += get_top5score(truth_b, output_b_l)
            top5acc_moving = top5score_moving / num_samples_seen
            if s['verbose'] > 0:
                print("\nBatch {} of {} completed ({:.1%})".format(
                    batch_idx + 1, num_batches, (batch_idx + 1) / num_batches))
                print("Moving top-1 accuracy: {:.2%}.\n".format(top1acc_moving))
                print("Moving top-5 accuracy: {:.2%}.\n".format(top5acc_moving))
            with open(path_acc, 'a') as f_acc:
                f_acc.write("{} {:.2%} {:.2%}\n".format(
                    num_samples_seen, top1acc_moving, top5acc_moving))
            if 'input_image' in s['plot_vars'] and x_b is not None:
                plot_input_image(x_b[0], int(truth_b[0]), log_dir)
            if 'error_t' in s['plot_vars']:
                ann_err = self.ANN_err if hasattr(self, 'ANN_err') else None
                plot_error_vs_time(self.top1err_b_t, ann_err, log_dir)
            if 'confusion_matrix' in s['plot_vars']:
                plot_confusion_matrix(truth_d, guesses_d, log_dir,
                                      list(np.arange(self.num_classes)))
            if any({'activations', 'correlation', 'hist_spikerates_activations'}
                   & s['plot_vars']) or 'activations_n_b_l' in s['log_vars']:
                print("Calculating activations...")
                self.activations_n_b_l = get_activations_batch(
                    self.parsed_model, x_b)
            log_vars = {key: getattr(self, key) for key in s['log_vars']}
            log_vars['top1err_b_t'] = self.top1err_b_t
            np.savez_compressed(os.path.join(path_log_vars, str(batch_idx)),
                                **log_vars)
            plot_vars = {}
            if any({'activations', 'correlation',
                    'hist_spikerates_activations'} & s['plot_vars']):
                plot_vars['activations_n_b_l'] = self.activations_n_b_l
            if any({'spiketrains', 'spikerates', 'correlation', 'spikecounts',
                    'hist_spikerates_activations'} & s['plot_vars']):
                plot_vars['spiketrains_n_b_l_t'] = self.spiketrains_n_b_l_t
            output_graphs(plot_vars, log_dir, 0)
        # Compute average accuracy, taking into account number of samples per
        # class
        count = np.zeros(self.num_classes)
        match = np.zeros(self.num_classes)
        for gt, p in zip(truth_d, guesses_d):
            count[gt] += 1
            if gt == p:
                match[gt] += 1
        avg_acc = np.mean(match / count)
        top1acc_total = np.mean(np.array(truth_d) == np.array(guesses_d))
        if 'confusion_matrix' in s['plot_vars']:
            plot_confusion_matrix(truth_d, guesses_d, log_dir,
                                  list(np.arange(self.num_classes)))
        print("Simulation finished.\n\n")
        print("Total accuracy: {:.2%} on {} test samples.\n\n".format(
            top1acc_total, len(guesses_d)))
        print("Accuracy averaged over classes: {}".format(avg_acc))

        return top1acc_total

    def save(self, path=None, filename=None):
        """Write model architecture and parameters to disk.

        Parameters
        ----------

        path: string, optional
            Path to directory where to save model to. Defaults to
            ``settings['path']``.

        filename: string, optional
            Name of file to write model to. Defaults to
            ``settings['filename_snn']``.
        """

        if path is None:
            path = settings['path']
        if filename is None:
            filename = settings['filename_snn']
        filepath = os.path.join(path, filename + '.h5')

        print("Saving model to {}...\n".format(filepath))
        self.snn.save(filepath, settings['overwrite'])

    def load(self, path=None, filename=None):
        """Load model architecture and parameters from disk.

        Sets the ``snn`` attribute of this class.

        Parameters
        ----------

        path: string, optional
            Path to directory where to load model from. Defaults to
            ``settings['path']``.

        filename: string, optional
            Name of file to load model from. Defaults to
            ``settings['filename_snn']``.
        """

        from snntoolbox.core.inisim import custom_layers

        if path is None:
            path = settings['path_wd']
        if filename is None:
            filename = settings['filename_snn']
        filepath = os.path.join(path, filename + '.h5')

        self.snn = keras.models.load_model(filepath, custom_layers)

    @staticmethod
    def assert_batch_size(batch_size):
        """Check if batchsize is matched with configuration."""

        if batch_size != settings['batch_size']:
            msg = dedent("""\
                You attempted to use the SNN with a batch_size different than
                the one with which it was converted. This is not supported when
                using INI simulator: To change the batch size, convert the ANN
                from scratch with the desired batch size. For now, the batch
                size has been reset from {} to the original {}.\n""".format(
                settings['batch_size'], batch_size))
            # logging.warning(msg)
            print(msg)
            settings['batch_size'] = batch_size

    @staticmethod
    def end_sim():
        """Clean up after simulation.

        Clean up after simulation. Not needed in this simulator, so do a
        ``pass``.
        """

        pass

    def set_time(self, t):
        """Set the simulation time variable of all layers in the network.

        Parameters
        ----------

        t: float
            Current simulation time.
        """

        for layer in self.snn.layers[1:]:
            if self.sim.get_time(layer) is not None:  # Has time attribute
                self.sim.set_time(layer, np.float32(t))

    def reset(self):
        """Reset network variables."""

        for layer in self.snn.layers[1:]:  # Skip input layer
            layer.reset()

    def init_log_vars(self):
        """Initialize debug variables."""

        num_timesteps = int(settings['duration'] / settings['dt'])

        if 'input_b_l_t' in settings['log_vars']:
            self.input_b_l_t = np.empty(
                list(self.snn.input_shape) + [num_timesteps], 'int32')

        if any({'spiketrains', 'spikerates', 'correlation',
                'hist_spikerates_activations'} & settings['plot_vars']) \
                or 'spiketrains_n_b_l_t' in settings['log_vars']:
            self.spiketrains_n_b_l_t = []
            for layer in self.snn.layers:
                if not hasattr(layer, 'spiketrain'):
                    continue
                shape = list(layer.output_shape) + [num_timesteps]
                self.spiketrains_n_b_l_t.append((np.zeros(shape, 'float32'),
                                                 layer.name))

        if 'mem_n_b_l_t' in settings['log_vars'] \
                or 'mem' in settings['plot_vars']:
            self.mem_n_b_l_t = []
            for layer in self.snn.layers:
                if not hasattr(layer, 'mem'):
                    continue
                shape = list(layer.output_shape) + [num_timesteps]
                self.mem_n_b_l_t.append((np.zeros(shape, 'float32'),
                                         layer.name))

        self.top1err_b_t = np.empty((settings['batch_size'], num_timesteps),
                                    np.bool)


def remove_outliers(timestamps, xaddr, yaddr, pol, x_max=239, y_max=179):
    """Remove outliers from DVS data.

    Parameters
    ----------
    timestamps :
    xaddr :
    yaddr :
    pol :
    x_max :
    y_max :

    Returns
    -------

    """

    len_orig = len(timestamps)
    xaddr_valid = np.where(np.array(xaddr) <= x_max)
    yaddr_valid = np.where(np.array(yaddr) <= y_max)
    xy_valid = np.intersect1d(xaddr_valid[0], yaddr_valid[0], True)
    xaddr = np.array(xaddr)[xy_valid]
    yaddr = np.array(yaddr)[xy_valid]
    timestamps = np.array(timestamps)[xy_valid]
    pol = np.array(pol)[xy_valid]
    num_outliers = len_orig - len(timestamps)
    if num_outliers:
        print("Removed {} outliers.".format(num_outliers))
    return timestamps, xaddr, yaddr, pol


def load_dvs_sequence(filename, xyrange=None):
    """

    Parameters
    ----------

    filename:
    xyrange:

    Returns
    -------

    """

    from snntoolbox.io_utils.AedatTools import ImportAedat

    print("Loading DVS sample {}...".format(filename))
    events = ImportAedat.import_aedat({'filePathAndName':
                                       filename})['data']['polarity']
    timestamps = events['timeStamp']
    xaddr = events['x']
    yaddr = events['y']
    pol = events['polarity']

    # Remove events with addresses outside valid range
    if xyrange:
        timestamps, xaddr, yaddr, pol = remove_outliers(
            timestamps, xaddr, yaddr, pol, xyrange[0], xyrange[1])

    xaddr = xyrange[0] - xaddr
    yaddr = xyrange[1] - yaddr

    return xaddr, yaddr, timestamps


class DVSIterator(object):
    """

    Parameters
    ----------
    dataset_path :
    batch_size :
    scale:

    Returns
    -------

    """

    def __init__(self, dataset_path, batch_size, label_dict=None,
                 scale=None, num_events_per_sample=1000):
        self.dataset_path = dataset_path
        self.batch_size = batch_size
        self.batch_idx = 0
        self.scale = scale
        self.xaddr_sequence = None
        self.yaddr_sequence = None
        self.dvs_sample = None
        self.num_events_of_sample = 0
        self.dvs_sample_idx = -1
        self.num_events_per_sample = num_events_per_sample
        self.num_events_per_batch = batch_size * num_events_per_sample

        # Count the number of samples and classes
        classes = [subdir for subdir in sorted(os.listdir(dataset_path))
                   if os.path.isdir(os.path.join(dataset_path, subdir))]

        self.label_dict = dict(zip(classes, range(len(classes)))) \
            if not label_dict else label_dict
        self.num_classes = len(label_dict)
        assert self.num_classes == len(classes), \
            "The number of classes provided by label_dict {} does not match " \
            "the number of subdirectories found in dataset_path {}.".format(
                self.label_dict, self.dataset_path)

        self.filenames = []
        labels = []
        self.num_samples = 0
        for subdir in classes:
            for fname in sorted(os.listdir(os.path.join(dataset_path, subdir))):
                is_valid = False
                for extension in {'aedat'}:
                    if fname.lower().endswith('.' + extension):
                        is_valid = True
                        break
                if is_valid:
                    labels.append(self.label_dict[subdir])
                    self.filenames.append(os.path.join(subdir, fname))
                    self.num_samples += 1
        self.labels = np.array(labels, 'int32')
        print("Found {} samples belonging to {} classes.".format(
            self.num_samples, self.num_classes))

    def __next__(self):
        from snntoolbox.io_utils.common import to_categorical
        from collections import deque

        while self.num_events_per_batch * (self.batch_idx + 1) >= \
                self.num_events_of_sample:
            self.dvs_sample_idx += 1
            if self.dvs_sample_idx == len(self.filenames):
                raise StopIteration()
            filepath = os.path.join(self.dataset_path,
                                    self.filenames[self.dvs_sample_idx])
            self.dvs_sample = load_dvs_sequence(filepath, (239, 179))
            self.num_events_of_sample = len(self.dvs_sample[0])
            self.batch_idx = 0
            print("Total number of events of this sample: {}.".format(
                self.num_events_of_sample))
            print("Number of batches: {:d}.".format(
                int(self.num_events_of_sample / self.num_events_per_batch)))

        print("Extracting batch of samples à {} events from DVS sequence..."
              "".format(self.num_events_per_sample))
        x_b_xaddr = [deque() for _ in range(self.batch_size)]
        x_b_yaddr = [deque() for _ in range(self.batch_size)]
        x_b_ts = [deque() for _ in range(self.batch_size)]
        for sample_idx in range(self.batch_size):
            start_event = self.num_events_per_batch * self.batch_idx + \
                          self.num_events_per_sample * sample_idx
            event_idxs = range(start_event,
                               start_event + self.num_events_per_sample)
            event_sums = np.zeros((64, 64), 'int32')
            xaddr_sub = []
            yaddr_sub = []
            for x, y in zip(self.dvs_sample[0][event_idxs],
                            self.dvs_sample[1][event_idxs]):
                if self.scale:
                    # Subsample from 240x180 to e.g. 64x64
                    x = int(x / self.scale[0])
                    y = int(y / self.scale[1])
                event_sums[y, x] += 1
                xaddr_sub.append(x)
                yaddr_sub.append(y)
            sigma = np.std(event_sums)
            # Clip number of events per pixel to three-sigma
            np.clip(event_sums, 0, 3*sigma, event_sums)
            print("Discarded {} events during 3-sigma standardization.".format(
                self.num_events_per_sample - np.sum(event_sums)))
            ts_sample = self.dvs_sample[2][event_idxs]
            for x, y, ts in zip(xaddr_sub, yaddr_sub, ts_sample):
                if event_sums[y, x] > 0:
                    x_b_xaddr[sample_idx].append(x)
                    x_b_yaddr[sample_idx].append(y)
                    x_b_ts[sample_idx].append(ts)
                    event_sums[y, x] -= 1

        # Each sample in the batch has the same label because it is generated
        # from the same DVS sequence.
        y_b = np.broadcast_to(to_categorical(
            [self.labels[self.dvs_sample_idx]], self.num_classes),
            (self.batch_size, self.num_classes))

        self.batch_idx += 1

        return x_b_xaddr, x_b_yaddr, x_b_ts, y_b

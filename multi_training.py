import matplotlib
matplotlib.use('agg')# to avoid GUI request on clusters
import os
# os.environ['CUDA_VISIBLE_DEVICES'] = '0,1,2,3'
import socket
import absl
import json
import time
import contextlib
import datetime as dt
import numpy as np
import pickle as pkl
import tensorflow as tf

from packaging import version
# check the version of tensorflow, and do the right thing.
# if tf.__version__ < "2.4.0": # does not work wor 2.10.1.
if version.parse(tf.__version__) < version.parse("2.4.0"):
    from tensorflow.keras.mixed_precision import experimental as mixed_precision

from general_utils import file_management
from general_utils.other_utils import memory_tracer, timer
from v1_model_utils import load_sparse, models, other_v1_utils, toolkit
from v1_model_utils.plotting_utils import InputActivityFigure, LaminarPlot, LGN_sample_plot, PopulationActivity, RasterPlot
# import data_sets
import stim_dataset

from time import time



def main(_):
    # # Enable TensorFlow Profiler
    # tf.profiler.experimental.start('log_dir') 
    flags = absl.app.flags.FLAGS
    np.random.seed(flags.seed)
    tf.random.set_seed(flags.seed)

    results_dir = os.path.join(flags.results_dir, 'multi_training')
    os.makedirs(results_dir, exist_ok=True)
     # Save the flags configuration in a JSON file
    with open(os.path.join(results_dir, 'flags_config.json'), 'w') as fp:
        json.dump(flags.flag_values_dict(), fp)

    # Allow for memory growth (also to observe memory consumption)
    physical_devices = tf.config.list_physical_devices("GPU")
    try:
        for dev in physical_devices:
            print(dev)
            tf.config.experimental.set_memory_growth(dev, True)
    except:
        # Invalid device or cannot modify virtual devices once initialized.
        print("Invalid device or cannot modify virtual devices once initialized.")
        pass
    print("Num GPUs Available: ", len(tf.config.list_physical_devices('GPU')))

    # Can be used to try half precision training
    if flags.float16:
        # policy = mixed_precision.Policy("mixed_float16")
        # mixed_precision.set_policy(policy)
        if tf.__version__ < "2.4.0":
            policy = mixed_precision.Policy("mixed_float16")
            mixed_precision.set_policy(policy)
        else:
            tf.keras.mixed_precision.set_global_policy("mixed_float16")
        dtype = tf.float16
    else:
        dtype = tf.float32

    
    n_workers, n_gpus_per_worker = 1, 1
    # model is being run on multiple GPUs or CPUs, and the results are being reduced to a single CPU device. 
    # In this case, the reduce_to_device argument is set to "cpu:0", which means that the results are being reduced to the first CPU device.
    strategy = tf.distribute.MirroredStrategy(cross_device_ops=tf.distribute.ReductionToOneDevice(reduce_to_device="cpu:0")) 
    task_name = 'drifting_gratings_firing_rates_distr'  
    per_replica_batch_size = flags.batch_size
    global_batch_size = per_replica_batch_size * strategy.num_replicas_in_sync
    print(f'Global batch size: {global_batch_size}')

    # load column model of Billeh et al
    ### Load or create the network configuration
    t0 = time()
    if flags.caching:
        load_fn = load_sparse.cached_load_v1
    else:
        load_fn = load_sparse.load_v1

    network, lgn_input, bkg_input = load_fn(flags, flags.neurons)
    print(f"Model data loading, {time()-t0:.2f} seconds")

    # Define the scope in which the model training will be executed
    with strategy.scope():
        t0 = time()
        model = models.create_model(
            network,
            lgn_input,
            bkg_input,
            # seq_len=flags.seq_len,
            seq_len=flags.seq_len,
            n_input=flags.n_input,
            n_output=flags.n_output,
            cue_duration=flags.cue_duration,
            dtype=dtype,
            input_weight_scale=flags.input_weight_scale,
            dampening_factor=flags.dampening_factor,
            gauss_std=flags.gauss_std,
            lr_scale=flags.learning_rate,
            train_input=flags.train_input,
            train_recurrent=flags.train_recurrent,
            neuron_output=flags.neuron_output,
            recurrent_dampening_factor=flags.recurrent_dampening_factor,
            batch_size=flags.batch_size,
            pseudo_gauss=flags.pseudo_gauss,
            use_state_input=True,
            return_state=True,
        )

        del lgn_input, bkg_input

        model.build((flags.batch_size, flags.seq_len, flags.n_input))
        print(f"Model built in {time()-t0:.2f} seconds")

        # Extract outputs of intermediate keras layers to get access to
        # spikes and membrane voltages of the model
        rsnn_layer = model.get_layer("rsnn")
        rec_weight_regularizer = models.StiffRegularizer(flags.recurrent_weight_regularization,
                                                         rsnn_layer.cell.recurrent_weight_values)

        # Load the firing rates distribution as a regularizer that we have and generate target firing rates for every neuron type
        with open(os.path.join(flags.data_dir, 'np_gratings_firing_rates.pkl'), 'rb') as f:
            target_firing_rates = pkl.load(f)

        for key, value in target_firing_rates.items():
            # identify tne ids that are included in value["ids"]
            neuron_ids = np.where(np.isin(network["node_type_ids"], value["ids"]))[0]
            target_firing_rates[key]['neuron_ids'] = neuron_ids
            type_n_neurons = len(neuron_ids)
            target_firing_rates[key]['sorted_target_rates'] = models.sample_firing_rates(
                value["rates"], type_n_neurons, flags.seed)
            
        rate_distribution_regularizer = models.SpikeRateDistributionTarget(target_firing_rates, flags.rate_cost)
        # rate_distribution_regularizer = models.SpikeRateDistributionRegularization(target_firing_rates, flags.rate_cost)
        # rate_loss = rate_distribution_regularizer(rsnn_layer.output[0])
        rate_loss = rate_distribution_regularizer(rsnn_layer.output[0][0])

        voltage_regularizer = models.VoltageRegularization(rsnn_layer.cell, flags.voltage_cost)
        # voltage_loss = voltage_regularizer(rsnn_layer.output[2]) 
        voltage_loss = voltage_regularizer(rsnn_layer.output[0][1]) 

        model.add_loss(rate_loss)
        model.add_loss(voltage_loss)
        model.add_metric(rate_loss, name='rate_loss')
        model.add_metric(voltage_loss, name='voltage_loss')

        prediction_layer = model.get_layer('prediction')
        extractor_model = tf.keras.Model(inputs=model.inputs,
                                         outputs=[rsnn_layer.output, model.output, prediction_layer.output])
        # extractor_model = tf.keras.Model(inputs=model.inputs, outputs=[rsnn_layer.output])

        loss_object = tf.keras.losses.SparseCategoricalCrossentropy(
            from_logits=False, reduction=tf.keras.losses.Reduction.NONE)

        def compute_loss(_l, _p, _w):
            per_example_loss = loss_object(_l, _p, sample_weight=_w) * strategy.num_replicas_in_sync / tf.reduce_sum(_w)
            rec_weight_loss = rec_weight_regularizer(rsnn_layer.cell.recurrent_weight_values)
            return tf.nn.compute_average_loss(per_example_loss, global_batch_size=global_batch_size) + rec_weight_loss

        def calculate_delta_angle(stim_angle, tuning_angle):
            # angle unit is degrees.
            # this function calculates the difference between stim_angle and tuning_angle,
            # but it is fine to have the opposite direction.
            # so, delta angle is always between -90 and 90.
            # they are both vector, so dimension matche is needed.
            # stim_angle is a length of batch size
            # tuning_angle is a length of n_neurons

            # delta_angle = stim_angle - tuning_angle
            delta_angle = tf.expand_dims(stim_angle, axis=1) - tuning_angle
            delta_angle = tf.where(delta_angle > 90, delta_angle - 180, delta_angle)
            delta_angle = tf.where(delta_angle < -90, delta_angle + 180, delta_angle)
            # # do it twice to make sure
            delta_angle = tf.where(delta_angle > 90, delta_angle - 180, delta_angle)
            delta_angle = tf.where(delta_angle < -90, delta_angle + 180, delta_angle)

            return delta_angle

        def compute_loss_gratings(_y, _z):
            # I need to access the tuning angle. of all the neurons.
            delta_angle = calculate_delta_angle(_y, network["tuning_angle"])
            
            # sum spikes in _z, and multiply with delta_angle.
            sum_angle = tf.reduce_mean(_z, axis=[1]) * delta_angle

            # make a huber loss for this.
            # angle_loss = tf.keras.losses.Huber(delta=1, reduction=tf.keras.losses.Reduction.SUM)(sum_angle, tf.zeros_like(sum_angle))
            angle_loss = tf.reduce_mean(tf.abs(sum_angle))
            return angle_loss
        
        optimizer = tf.keras.optimizers.Adam(flags.learning_rate, epsilon=1e-11)    

        zero_state = rsnn_layer.cell.zero_state(flags.batch_size)
   
        state_variables = tf.nest.map_structure(lambda a: tf.Variable(
            a, trainable=False, synchronization=tf.VariableSynchronization.ON_READ
        ), zero_state)

        train_loss = tf.keras.metrics.Mean()
        val_loss = tf.keras.metrics.Mean()
        train_accuracy = tf.keras.metrics.SparseCategoricalAccuracy()
        val_accuracy = tf.keras.metrics.SparseCategoricalAccuracy()
        train_firing_rate = tf.keras.metrics.Mean()
        val_firing_rate = tf.keras.metrics.Mean()

        train_rate_loss = tf.keras.metrics.Mean()
        val_rate_loss = tf.keras.metrics.Mean()
        train_voltage_loss = tf.keras.metrics.Mean()
        val_voltage_loss = tf.keras.metrics.Mean()

        def reset_train_metrics():
            train_loss.reset_states(), train_accuracy.reset_states(), train_firing_rate.reset_states()
            train_rate_loss.reset_states(), train_voltage_loss.reset_states()

        def reset_validation_metrics():
            val_loss.reset_states(), val_accuracy.reset_states(), val_firing_rate.reset_states()
            val_rate_loss.reset_states(), val_voltage_loss.reset_states()

    def roll_out(_x, _y, _w):
        _initial_state = tf.nest.map_structure(lambda _a: _a.read_value(), state_variables)
        stt = time()
        dummy_zeros = tf.zeros((flags.batch_size, flags.seq_len, flags.neurons), dtype)
        _out, _p, _ = extractor_model((_x, dummy_zeros, _initial_state))
        # print('roll out time: ', time.time() - stt)

        _z, _v, _input_current = _out[0]
        voltage_32 = (tf.cast(_v, tf.float32) - rsnn_layer.cell.voltage_offset) / rsnn_layer.cell.voltage_scale
        v_pos = tf.square(tf.nn.relu(voltage_32 - 1.))
        v_neg = tf.square(tf.nn.relu(-voltage_32 + 1.))
        voltage_loss = tf.reduce_mean(tf.reduce_sum(v_pos + v_neg, -1)) * flags.voltage_cost
        rate_loss = rate_distribution_regularizer(_z)
        # classification loss is turned off for now.
        # classification_loss = compute_loss_gratings(_y, _z)

        _aux = dict(rate_loss=rate_loss, voltage_loss=voltage_loss)
        # _loss = classification_loss + rate_loss + voltage_loss
        _loss = rate_loss

        return _out, _p, _loss, _aux

    def train_step(_x, _y, _w):
        with tf.GradientTape() as tape:
            _out, _p, _loss, _aux = roll_out(_x, _y, _w)

        _op = train_accuracy.update_state(_y, _p, sample_weight=_w)
        with tf.control_dependencies([_op]):
            _op = train_loss.update_state(_loss)
        _rate = tf.reduce_mean(_out[0][0])
        with tf.control_dependencies([_op]):
            _op = train_firing_rate.update_state(_rate)
        with tf.control_dependencies([_op]):
            _op = train_rate_loss.update_state(_aux['rate_loss'])
        with tf.control_dependencies([_op]):
            _op = train_voltage_loss.update_state(_aux['voltage_loss'])

        grad = tape.gradient(_loss, model.trainable_variables)
        for g, v in zip(grad, model.trainable_variables):
            with tf.control_dependencies([_op]):
                _op = optimizer.apply_gradients([(g, v)])

    @tf.function
    def distributed_train_step(_x, _y, _w):
        strategy.run(train_step, args=(_x, _y, _w))

    def validation_step(_x, _y, _w):
        _out, _p, _loss, _aux = roll_out(_x, _y, _w)
        _op = val_accuracy.update_state(_y, _p, sample_weight=_w)
        with tf.control_dependencies([_op]):
            _op = val_loss.update_state(_loss)
        _rate = tf.reduce_mean(_out[0][0])
        with tf.control_dependencies([_op]):
            _op = val_firing_rate.update_state(_rate)
        with tf.control_dependencies([_op]):
            _op = val_rate_loss.update_state(_aux['rate_loss'])
        with tf.control_dependencies([_op]):
            _op = val_voltage_loss.update_state(_aux['voltage_loss'])
        # tf.nest.map_structure(lambda _a, _b: _a.assign(_b), list(state_variables), _out[1:])

    @tf.function
    def distributed_validation_step(_x, _y, _w):
        strategy.run(validation_step, args=(_x, _y, _w))

    def reset_state():
        tf.nest.map_structure(lambda a, b: a.assign(b), state_variables, zero_state)

    @tf.function
    def distributed_reset_state():
        strategy.run(reset_state)

    checkpoint = tf.train.Checkpoint(optimizer=optimizer, model=model)
    sim_name = toolkit.get_random_identifier('b_')
    logdir = os.path.join(results_dir, sim_name)
    print(f'> Results for {task_name} will be stored in {logdir}')

    if flags.restore_from != '':
        with strategy.scope():
            # checkpoint.restore(tf.train.latest_checkpoint(flags.restore_from))
            checkpoint.restore(flags.restore_from)
            print(f'Model parameters of {task_name} restored from {flags.restore_from}')

    def compose_str(_loss, _acc, _rate, _rate_loss, _voltage_loss):
        _s = f'Loss {_loss:.4f}, '
        _s += f'RLoss {_rate_loss:.4f}, '
        _s += f'VLoss {_voltage_loss:.4f}, '
        _s += f'Accuracy {_acc:.4f}, '
        _s += f'Rate {_rate:.4f}'
        return _s

    def get_dataset_fn():
        def _f(input_context):
            delays = [int(a) for a in flags.delays.split(',') if a != '']
            _data_set = stim_dataset.generate_drifting_grating_tuning(
                seq_len=flags.seq_len,
                pre_delay=delays[0],
                post_delay=delays[1],
            ).batch(
                    per_replica_batch_size)
                        
            return _data_set
        return _f
    
    # We define the dataset generates function under the strategy scope for a randomly selected orientation
    test_data_set = strategy.experimental_distribute_datasets_from_function(
        get_dataset_fn())
    train_data_set = strategy.experimental_distribute_datasets_from_function(
        get_dataset_fn())

    ### Training
    step_counter = tf.Variable(0,trainable=False)
    manager = tf.train.CheckpointManager(
        checkpoint, directory=logdir, max_to_keep=100,
        # keep_checkpoint_every_n_hours=2,
        checkpoint_interval = 1, # save ckpt for data analysis
        step_counter=step_counter
    )
    summary_writer = tf.summary.create_file_writer(logdir)

    def save_model():
        step_counter.assign_add(1)
        p = manager.save()
        print(f'Model saved in {p}')

    with open(os.path.join(logdir, 'config.json'), 'w') as f:
        json.dump(flags.flag_values_dict(), f, indent=4)

    stop = False
    t0 = time()
    
    for epoch in range(flags.n_epochs):
        if stop:
            break

        it = iter(train_data_set)
        date_str = dt.datetime.now().strftime('%d-%m-%Y %H:%M')
        print(f'Epoch {epoch + 1:2d}/{flags.n_epochs} @ {date_str}')
        # quit()
        distributed_reset_state()
        for step in range(flags.steps_per_epoch):
            step_t0 = time()
            x, y, _, w = next(it)
            distributed_train_step(x, y, w)

            if step % 1 == 0:
                print_str = f'  Step {step + 1:2d}/{flags.steps_per_epoch}: {time() - step_t0:.2f}s\n'
                print_str += compose_str(train_loss.result(), train_accuracy.result(),
                                            train_firing_rate.result(), train_rate_loss.result(), train_voltage_loss.result())
                print(print_str, end='\n')
            if 0 < flags.max_time < (time() - t0) / 3600:
                stop = True
                break

        if stop:
            print(f'[ Maximum optimization time of {flags.max_time:.2f}h reached ]')

        distributed_reset_state()
        test_it = iter(test_data_set)
        for step in range(flags.val_steps):
            x, y, _, w = next(test_it)
            distributed_validation_step(x, y, w) 

        print_str = '  Validation: ' + compose_str(
            val_loss.result(), val_accuracy.result(), val_firing_rate.result(),
            val_rate_loss.result(), val_voltage_loss.result())
        
        print(print_str)
        keys = ['train_accuracy', 'train_loss', 'train_firing_rate', 'train_rate_loss',
                'train_voltage_loss', 'val_accuracy', 'val_loss',
                'val_firing_rate', 'val_rate_loss', 'val_voltage_loss']
        values = [a.result().numpy() for a in [train_accuracy, train_loss, train_firing_rate, train_rate_loss,
                                                train_voltage_loss, val_accuracy, val_loss, val_firing_rate,
                                                val_rate_loss, val_voltage_loss]]
        if stop:
            result = dict(
                train_loss=float(train_loss.result().numpy()),
                train_accuracy=float(train_accuracy.result().numpy()),
                test_loss=float(val_loss.result().numpy()),
                test_accuracy=float(val_accuracy.result().numpy())
            )
        save_model()
        with summary_writer.as_default():
            for k, v in zip(keys, values):
                tf.summary.scalar(k, v, step=epoch)
        if stop:
            with open(os.path.join(cm.paths.results_path, 'result.json'), 'w') as f:
                json.dump(result, f)
        reset_train_metrics()
        reset_validation_metrics()


if __name__ == '__main__':
    hostname = socket.gethostname()
    if hostname.count('shinya') > 0:
        _data_dir = '/allen/programs/mindscope/workgroups/realistic-model/shinya.ito/tensorflow/GLIF_network'
        _results_dir = '/allen/programs/mindscope/workgroups/realistic-model/shinya.ito/tensorflow/results'
    else: 
        _data_dir = '/home/jgalvan/Desktop/Neurocoding/V1_GLIF_model/GLIF_network'
        _results_dir = '/home/jgalvan/Desktop/Neurocoding/V1_GLIF_model/GLIF_network/results'

    absl.app.flags.DEFINE_string('data_dir', _data_dir, '')
    absl.app.flags.DEFINE_string('results_dir', _results_dir, '')
    # absl.app.flags.DEFINE_string('restore_from', '', '')
    # absl.app.flags.DEFINE_string('restore_from', '../results/multi_training/b_53dw/results/ckpt-49', '')
    absl.app.flags.DEFINE_string('restore_from', '', '')
    absl.app.flags.DEFINE_string('comment', '', '')
    absl.app.flags.DEFINE_string('delays', '50,50', '')
    # absl.app.flags.DEFINE_string('neuron_model', 'GLIF3', '')
    absl.app.flags.DEFINE_string('scale', '2,2', '')

    absl.app.flags.DEFINE_float('learning_rate', .01, '')
    absl.app.flags.DEFINE_float('rate_cost', 10., '')
    absl.app.flags.DEFINE_float('voltage_cost', .00001, '')
    absl.app.flags.DEFINE_float('dampening_factor', .5, '')
    absl.app.flags.DEFINE_float('gauss_std', .28, '')
    absl.app.flags.DEFINE_float('recurrent_weight_regularization', 0., '')
    # absl.app.flags.DEFINE_float('p_reappear', .5, '')
    absl.app.flags.DEFINE_float('max_time', -1, '')
    # absl.app.flags.DEFINE_float('max_time', 0.05, '')
    # absl.app.flags.DEFINE_float('scale_w_e', -1, '')
    # absl.app.flags.DEFINE_float('sti_intensity', 2., '')
    absl.app.flags.DEFINE_float('input_weight_scale', 1., '')

    absl.app.flags.DEFINE_integer('n_epochs', 51, '')
    absl.app.flags.DEFINE_integer('batch_size', 1, '')
    absl.app.flags.DEFINE_integer('neurons', 66634, '')
    absl.app.flags.DEFINE_integer("n_input", 17400, "")  
    absl.app.flags.DEFINE_integer('seq_len', 600, '')
    # absl.app.flags.DEFINE_integer('im_slice', 100, '')
    absl.app.flags.DEFINE_integer('seed', 3000, '')
    # absl.app.flags.DEFINE_integer('port', 12778, '')
    absl.app.flags.DEFINE_integer("n_output", 2, "")
    absl.app.flags.DEFINE_integer('neurons_per_output', 30, '')
    absl.app.flags.DEFINE_integer('steps_per_epoch', 10, '')# EA and garret dose not need this many but pure classification needs 781 = int(50000/64)
    absl.app.flags.DEFINE_integer('val_steps', 25, '')# EA and garret dose not need this many but pure classification needs 156 = int(10000/64)
    # absl.app.flags.DEFINE_integer('max_delay', 5, '')
    # absl.app.flags.DEFINE_integer('n_plots', 1, '')

    # absl.app.flags.DEFINE_integer('pre_chunks', 3, '')
    # absl.app.flags.DEFINE_integer('post_chunks', 8, '') # the pure calssification task only need 1 but to make consistent with other tasks one has to make up here
    absl.app.flags.DEFINE_integer('pre_delay', 50, '')
    absl.app.flags.DEFINE_integer('post_delay', 450, '')

    # absl.app.flags.DEFINE_boolean('use_rand_connectivity', False, '')
    # absl.app.flags.DEFINE_boolean('use_uniform_neuron_type', False, '')
    # absl.app.flags.DEFINE_boolean('use_only_one_type', False, '')
    # absl.app.flags.DEFINE_boolean('use_dale_law', True, '')
    absl.app.flags.DEFINE_boolean('caching', False, '') # if one wants to use caching, remember to update the caching function
    absl.app.flags.DEFINE_boolean('core_only', False, '')
    # absl.app.flags.DEFINE_boolean('train_input', True, '')
    absl.app.flags.DEFINE_boolean('train_input', False, '')
    absl.app.flags.DEFINE_boolean('train_recurrent', True, '')
    absl.app.flags.DEFINE_boolean('connected_selection', True, '')
    absl.app.flags.DEFINE_boolean('neuron_output', False, '')
    # absl.app.flags.DEFINE_boolean('localized_readout', True, '')
    # absl.app.flags.DEFINE_boolean('current_input', True, '')
    # absl.app.flags.DEFINE_boolean('use_rand_ini_w', True, '')
    # absl.app.flags.DEFINE_boolean('use_decoded_noise', True, '')
    # absl.app.flags.DEFINE_boolean('from_lgn', True, '')
    absl.app.flags.DEFINE_boolean("float16", False, "")

    absl.app.flags.DEFINE_integer("cue_duration", 40, "")
    absl.app.flags.DEFINE_float("recurrent_dampening_factor", 0.5, "")
    absl.app.flags.DEFINE_boolean("hard_reset", True, "")
    absl.app.flags.DEFINE_boolean("pseudo_gauss", False, "")

    absl.app.run(main)

import numpy as np
import tensorflow as tf
import psutil

# Define a custom gradient for the spike function.
# Diverse functions can be used to define the gradient.
# Here we provide variations of this functions depending on
# the gradient type and the precision of the input tensor.


def gauss_pseudo(v_scaled, sigma, amplitude):
    return tf.math.exp(-tf.square(v_scaled) / tf.square(sigma)) * amplitude


def pseudo_derivative(v_scaled, dampening_factor):
    return dampening_factor * tf.maximum(1 - tf.abs(v_scaled), 0)


# def slayer_pseudo(v_scaled, sigma, amplitude):
#     return tf.math.exp(-sigma * tf.abs(v_scaled)) * amplitude


@tf.custom_gradient
def spike_gauss(v_scaled, sigma, amplitude):
    z_ = tf.greater(v_scaled, 0.0)
    z_ = tf.cast(z_, tf.float32)

    def grad(dy):
        de_dz = dy
        dz_dv_scaled = gauss_pseudo(v_scaled, sigma, amplitude)
        de_dv_scaled = de_dz * dz_dv_scaled

        return [de_dv_scaled, tf.zeros_like(sigma), tf.zeros_like(amplitude)]

    return tf.identity(z_, name="spike_gauss"), grad


@tf.custom_gradient
def spike_gauss_16(v_scaled, sigma, amplitude):
    z_ = tf.greater(v_scaled, 0.0)
    z_ = tf.cast(z_, tf.float16)

    def grad(dy):
        de_dz = dy
        dz_dv_scaled = gauss_pseudo(v_scaled, sigma, amplitude)

        de_dv_scaled = de_dz * dz_dv_scaled

        return [de_dv_scaled, tf.zeros_like(sigma), tf.zeros_like(amplitude)]

    return tf.identity(z_, name="spike_gauss"), grad


# @tf.custom_gradient
# def spike_slayer(v_scaled, sigma, amplitude):
#     z_ = tf.greater(v_scaled, 0.0)
#     z_ = tf.cast(z_, tf.float32)

#     def grad(dy):
#         de_dz = dy
#         dz_dv_scaled = slayer_pseudo(v_scaled, sigma, amplitude)

#         de_dv_scaled = de_dz * dz_dv_scaled

#         return [de_dv_scaled, tf.zeros_like(sigma), tf.zeros_like(amplitude)]

#     return tf.identity(z_, name="spike_slayer"), grad


@tf.custom_gradient
def spike_function(v_scaled, dampening_factor):
    z_ = tf.greater(v_scaled, 0.0)
    z_ = tf.cast(z_, tf.float32)

    def grad(dy):
        de_dz = dy
        dz_dv_scaled = pseudo_derivative(v_scaled, dampening_factor)

        de_dv_scaled = de_dz * dz_dv_scaled

        return [de_dv_scaled, tf.zeros_like(dampening_factor)]

    return tf.identity(z_, name="spike_function"), grad


@tf.custom_gradient
def spike_function_16(v_scaled, dampening_factor):
    z_ = tf.greater(v_scaled, 0.0)
    z_ = tf.cast(z_, tf.float16)

    def grad(dy):
        de_dz = dy
        dz_dv_scaled = pseudo_derivative(v_scaled, dampening_factor)

        de_dv_scaled = de_dz * dz_dv_scaled

        return [de_dv_scaled, tf.zeros_like(dampening_factor)]

    return tf.identity(z_, name="spike_function"), grad


@tf.custom_gradient
def spike_function_b16(v_scaled, dampening_factor):
    z_ = tf.greater(v_scaled, 0.0)
    z_ = tf.cast(z_, tf.bfloat16)

    def grad(dy):
        de_dz = dy
        dz_dv_scaled = pseudo_derivative(v_scaled, dampening_factor)

        de_dv_scaled = de_dz * dz_dv_scaled

        return [de_dv_scaled, tf.zeros_like(dampening_factor)]

    return tf.identity(z_, name="spike_function"), grad


def exp_convolve(tensor, decay=0.8, reverse=False, initializer=None, axis=0):
    rank = len(tensor.get_shape())
    perm = np.arange(rank)
    perm[0], perm[axis] = perm[axis], perm[0]
    tensor = tf.transpose(tensor, perm)

    if initializer is None:
        initializer = tf.zeros_like(tensor[0])

    def scan_fun(_acc, _t):
        return _acc * decay + _t

    filtered = tf.scan(scan_fun, tensor, reverse=reverse,
                       initializer=initializer)

    filtered = tf.transpose(filtered, perm)
    return filtered


class BackgroundNoiseLayer(tf.keras.layers.Layer):
    def __init__(self, indices, weights, dense_shape, tau_syn_weights, 
                 lr_scale=1., bkg_firing_rate=250, n_bkg_units=100, 
                 dtype=tf.float32, **kwargs):
        super().__init__(**kwargs)
        self._dtype = dtype
        self._bkg_weights = weights
        self._bkg_indices = indices
        self._dense_shape = dense_shape
        self._tau_syn_weights = tau_syn_weights
        self._n_syn_basis = self._tau_syn_weights.shape[1]
        self._lr_scale = lr_scale
        self._bkg_firing_rate = bkg_firing_rate
        self._n_bkg_units = n_bkg_units

    def call(self, inp): # inp only provides the shape   

        inp_shape = inp.get_shape().as_list()
        shp = [dim if dim is not None else tf.shape(inp)[i] for i, dim in enumerate(inp_shape)]
        shp[-1] = self._n_bkg_units
        # locomotion_inputs = (np.random.uniform(size=(flags.batch_size, flags.seq_len, flags.n_locomotion_input),
        #         low=0., high=1.) < locomotion_firing_rates * .001).astype(np.uint8)

        rest_of_brain = tf.cast(tf.random.uniform(
                (shp[0], shp[1], self._n_bkg_units)) < self._bkg_firing_rate * .001, self._compute_dtype)

        # (batch_size*sequence_length, input_dim)
        rest_of_brain = tf.reshape(rest_of_brain, (shp[0] * shp[1], shp[2]))

        print('>>>>>>>>>>>>>>>>> Background Noise layer')
        # print(rest_of_brain.shape, tf.reduce_sum(rest_of_brain))
        print(self._dense_shape)
        print(self._bkg_weights.shape)
        print(self._tau_syn_weights.shape)
        print(rest_of_brain)

        noise_input = tf.TensorArray(dtype=self._compute_dtype, size=self._n_syn_basis)
        for r_id in range(self._n_syn_basis):
            weights_syn_receptors = self._bkg_weights * self._tau_syn_weights[:, r_id]
            print(r_id)
            print(weights_syn_receptors.shape)
            sparse_w_in = tf.sparse.SparseTensor(
                self._bkg_indices, 
                tf.cast(weights_syn_receptors, self._dtype), 
                self._dense_shape
            )

            print(sparse_w_in.shape)
            print(rest_of_brain.shape)

            i_receptor = tf.sparse.sparse_dense_matmul(
            tf.cast(sparse_w_in, self._compute_dtype), 
            # rest_of_brain, 
            rest_of_brain,
            adjoint_b=True
            )

            noise_input = noise_input.write(r_id, i_receptor)

        noise_input = noise_input.stack()
        noise_input = tf.transpose(noise_input)


        # rest_of_brain = tf.cast(tf.random.uniform(
        #         (shp[0], shp[1], self._n_bkg_units)) < self._bkg_firing_rate, self._compute_dtype)

        print('Hey judeeeeeeeeeeeeeeeeeeeeeeeeeee')
        print((shp[0], shp[1], self._n_bkg_units, self._bkg_firing_rate))
        print(rest_of_brain.shape)
        print(noise_input.shape)
        noise_input = tf.reshape(noise_input, (shp[0], shp[1], -1))
        print(noise_input.shape)

        # noise_input = (
        #     tf.cast(self._bkg_weights[None, None], self._compute_dtype)
        #     * rest_of_brain[..., None, None]
        # )

        # # print the sum of all the elements in the noise_input tensor
        # print('Sum of all elements in noise_input tensor: ', tf.reduce_sum(noise_input))

        # input_current = tf.reshape(input_current + noise_input, (shp[0], shp[1], -1)) # + noise_input

        # print(input_current.shape)

        # # Compute the BKG noise input
        # rest_of_brain = tf.reduce_sum(tf.cast(
        #         tf.random.uniform((shp[0], shp[1], 10)) < .1, self._compute_dtype), -1) # (1, 2500)

        # noise_input = tf.cast(self._bkg_weights[None, None], self._compute_dtype) * rest_of_brain[..., None] / 10.    

        return noise_input


class SparseLayer(tf.keras.layers.Layer):
    def __init__(
        self,
        indices,
        weights,
        dense_shape,
        tau_syn_weights,
        lr_scale=1.0,
        dtype=tf.float32,
        **kwargs,
    ):
        super().__init__(**kwargs)
        # self._indices = indices
        # self._weights = weights
        # self._dense_shape = dense_shape
        self._dtype = dtype
        # self._bkg_weights = bkg_weights
        self._lr_scale = lr_scale
        # self._sparse_w_in = tf.sparse.SparseTensor(
        #     indices, tf.cast(weights, self._dtype), dense_shape
        # )

        self._indices = indices
        self._input_weights = weights
        self._tau_syn_weights = tau_syn_weights
        self._n_syn_basis = 5
        self._dense_shape = dense_shape

        # Define a threshold that determines whether to compute the sparse
        # matrix multiplication directly or split it into smaller batches.
        # The value is calculated as the maximum number of rows that can be used
        # in a sparse matrix multiplication operation without running into memory
        # limitations on the device. The value 2**32 represents 4GB the maximum number
        # of elements in a tensor that can be stored on most modern GPU devices, and
        # weights.shape[0] is the number of rows in the sparse matrix.
        self._max_batch = int(2**31 / weights.shape[0])
        print('The maximum batch size is: ', self._max_batch)

    def call(self, inp):
        # replace any None values in the shape of inp with the actual values obtained
        # from the input tensor at runtime (tf.shape(inp)).
        # This is necessary because the SparseTensor multiplication operation requires
        # a fully defined shape.
        inp_shape = inp.get_shape().as_list()
        shp = [dim if dim is not None else tf.shape(inp)[i] for i, dim in enumerate(inp_shape)]

        print('>>> Input layer')
        print('inp_shape, inp')
        print(inp_shape)
        print(shp)
        print(self._input_weights.shape)

        # cast the weights to dtype
        # self._weights = tf.cast(self._weights, self._dtype)
        # sparse_w_in = tf.sparse.SparseTensor(
        #     self._indices, self._weights, self._dense_shape
        # )  # (923696, 17400)
        inp = tf.cast(inp, self._compute_dtype)
        # (batch_size*sequence_length, input_dim)
        inp = tf.reshape(inp, (shp[0] * shp[1], shp[2]))
        # By setting self._max_batch to this value, the code ensures that the input
        # tensor is processed in smaller batches when its shape exceeds the maximum
        # number of elements in a tensor.
        # the sparse tensor multiplication can be directly performed


        # if shp[0] * shp[1] < self._max_batch:
        tf.print('Processing input tensor directly.')
        
        input_current = tf.TensorArray(dtype=self._compute_dtype, size=self._n_syn_basis)
        for r_id in range(self._n_syn_basis):
            weights_syn_receptors = self._input_weights * self._tau_syn_weights[:, r_id]
            print(r_id)
            sparse_w_in = tf.sparse.SparseTensor(
                self._indices, 
                tf.cast(weights_syn_receptors, self._dtype), 
                self._dense_shape
            )

            print(sparse_w_in.shape)
            print(inp.shape)

            i_receptor = tf.sparse.sparse_dense_matmul(
            tf.cast(sparse_w_in, self._compute_dtype), 
            inp, 
            adjoint_b=True
            )

            input_current = input_current.write(r_id, i_receptor)

        input_current = input_current.stack()
        input_current = tf.transpose(input_current)
        print(weights_syn_receptors.shape)
        print('input_current_shape', input_current.shape)

        #     self._sparse_w_in = tf.sparse.SparseTensor(
        #     indices, tf.cast(weights, self._dtype), dense_shape
        # )
        #     input_current = tf.sparse.sparse_dense_matmul(
        #         self._sparse_w_in,
        #         inp,
        #         adjoint_b=True
        #     )
        #     input_current = tf.transpose(input_current)




        # else: 
        #     tf.print('Chunking input tensor into smaller batches.')
        #     num_chunks = tf.cast(tf.math.ceil(
        #         tf.shape(inp)[0] / self._max_batch), tf.int32)
        #     num_pad_elements = num_chunks * self._max_batch - tf.shape(inp)[0]
        #     padded_input = tf.pad(inp, [(0, num_pad_elements), (0, 0)])
        #     # Initialize a tensor array to hold the partial results
        #     result_array = tf.TensorArray(
        #         dtype=self._compute_dtype, size=num_chunks)
        #     for i in tf.range(num_chunks):
        #         tf.print('Processing chunk:', i, 'of', num_chunks, '.')
        #         if tf.config.list_physical_devices('GPU'):
        #             print(tf.config.experimental.get_memory_usage('GPU:0'))
        #         # print the memory consumption at this point
        #         process = psutil.Process()
        #         mem = process.memory_info().rss / (1024**3)  # in GB
        #         tf.print("Memory consumption in GB:", mem)

        #         start_idx = i * self._max_batch
        #         end_idx = (i + 1) * self._max_batch
        #         chunk = padded_input[start_idx:end_idx]
        #         chunk = tf.cast(chunk, self._compute_dtype)
        #         partial_input_current = tf.sparse.sparse_dense_matmul(
        #             self._sparse_w_in, chunk, adjoint_b=True)
        #         partial_input_current = tf.transpose(partial_input_current)
        #         # Store the partial result in the tensor array
        #         # print the shape of result_array and partial_input_current
        #         result_array = result_array.write(i, partial_input_current)

        #     # Concatenate the partial results to get the final result
        #     result_array = result_array.concat()[:-num_pad_elements, :]

        #     input_current = tf.cast(result_array, self._compute_dtype)




        # Add background noise with 1-channel, 4kHz spike rate (with 1 kHz sampling rate)
        # bkg_spike_rate = 4
        # rest_of_brain = tf.reduce_sum(
        #     tf.cast(tf.random.uniform(
        #         (shp[0], shp[1], 10*bkg_spike_rate)) < 0.1, self._compute_dtype),
        #     -1,
        # )
        # noise_input = (
        #     tf.cast(self._bkg_weights[None, None], self._compute_dtype)
        #     * rest_of_brain[..., None]
        #     / 10.0
        # )

        # Add background noise with 1-channel, 4kHz spike rate without random sampling
        # rest_of_brain = tf.ones(
        #     shape=(shp[0], shp[1]), dtype=self._compute_dtype)
        # rest_of_brain = tf.multiply(rest_of_brain, bkg_spike_rate)
        # bkg_spike_rate = 4
        # rest_of_brain = tf.reduce_sum(
        #     tf.cast(tf.random.uniform(
        #         (shp[0], shp[1], bkg_spike_rate)) < 1, self._compute_dtype),
        #     -1,
        # )

        # print('Hey judeeeeeeeeeeeeeeeeeeeeeeeeeee')
        # print((shp[0], shp[1], bkg_spike_rate))
        # print(self._bkg_weights.shape)
        # print(rest_of_brain.shape)

        # # noise_input = (
        # #     tf.cast(self._bkg_weights[None, None], self._compute_dtype)
        # #     * rest_of_brain[..., None]
        # # )

        # noise_input = (
        #     tf.cast(self._bkg_weights[None, None], self._compute_dtype)
        #     * rest_of_brain[..., None, None]
        # )

        # # print the sum of all the elements in the noise_input tensor
        # print('Sum of all elements in noise_input tensor: ', tf.reduce_sum(noise_input))

        
        
        # print(shp)
        # print(noise_input.shape)
        # print((shp[0], shp[1], -1))

        # print('Hey jude 2!')
        # print(input_current.shape)
        # print(tf.reshape(input_current, (shp[0], shp[1], -1)).shape)

        input_current = tf.reshape(
            input_current, (shp[0], shp[1], -1)) #+ noise_input

        print('input_current_shape2', input_current.shape)

        # input_current = tf.reshape(input_current + noise_input, (shp[0], shp[1], -1)) # + noise_input

        # print(input_current.shape)
        return input_current


class SignedConstraint(tf.keras.constraints.Constraint):
    def __init__(self, positive):
        self._positive = positive

    def __call__(self, w):
        sign_corrected_w = tf.where(
            self._positive, tf.nn.relu(w), -tf.nn.relu(-w))
        return sign_corrected_w


class SparseSignedConstraint(tf.keras.constraints.Constraint):
    def __init__(self, mask, positive):
        self._mask = mask
        self._positive = positive

    def __call__(self, w):
        sign_corrected_w = tf.where(
            self._positive, tf.nn.relu(w), -tf.nn.relu(-w))
        return tf.where(self._mask, sign_corrected_w, tf.zeros_like(sign_corrected_w))


class StiffRegularizer(tf.keras.regularizers.Regularizer):
    def __init__(self, strength, initial_value):
        super().__init__()
        self._strength = strength
        self._initial_value = tf.Variable(initial_value, trainable=False)

    def __call__(self, x):
        return self._strength * tf.reduce_sum(tf.square(x - self._initial_value))


class V1Column(tf.keras.layers.Layer):
    def __init__(
        self,
        network,
        lgn_input,
        bkg_input,
        dt=1.0,
        gauss_std=0.5,
        dampening_factor=0.3,
        recurrent_dampening_factor=0.4,
        input_weight_scale=1.0,
        recurrent_weight_scale=1.0,
        lr_scale=1.0,
        spike_gradient=False,
        max_delay=5,
        pseudo_gauss=False,
        train_recurrent=True,
        train_input=True,
        hard_reset=True,
    ):
        super().__init__()
        self._params = network["node_params"]

        # Rescale the voltages to have them near 0, as we wanted the effective step size
        # for the weights to be normalized when learning (weights are scaled similarly)
        voltage_scale = self._params["V_th"] - self._params["E_L"]
        voltage_offset = self._params["E_L"]
        self._params["V_th"] = (self._params["V_th"] -
                                voltage_offset) / voltage_scale
        self._params["E_L"] = (self._params["E_L"] -
                               voltage_offset) / voltage_scale
        self._params["V_reset"] = (
            self._params["V_reset"] - voltage_offset
        ) / voltage_scale
        self._params["asc_amps"] = (
            self._params["asc_amps"] / voltage_scale[..., None]
        )  # _params['asc_amps'] has shape (111, 2)

        self._node_type_ids = network["node_type_ids"]
        # self._n_tau_syns = network["tau_syns"]
        # self._max_n_receptors = int(
        #     network["synapses"]["dense_shape"][0]
        #     / network["synapses"]["dense_shape"][1]
        # )
        self._n_syn_basis = 5
        self._dt = dt
        self._recurrent_dampening = recurrent_dampening_factor
        self._pseudo_gauss = pseudo_gauss
        self._lr_scale = lr_scale
        self._spike_gradient = spike_gradient
        self._hard_reset = hard_reset

        # n_receptors = network["node_params"]["tau_syn"].shape[
        #     1
        # ]
        # self._n_receptors = n_receptors
        self._n_neurons = network["n_nodes"]
        self._dampening_factor = tf.cast(dampening_factor, self._compute_dtype)
        self._gauss_std = tf.cast(gauss_std, self._compute_dtype)

        tau = (
            self._params["C_m"] / self._params["g"]
        )  # determine the membrane time decay constant
        self._decay = np.exp(-dt / tau)
        self._current_factor = 1 / \
            self._params["C_m"] * (1 - self._decay) * tau
        # replace with 0 values where the denominator is 0 for the following arrays
        # self._syn_decay = np.where(
        #     np.array(self._params["tau_syn"]) == 0,
        #     0,
        #     np.exp(-dt / np.array(self._params["tau_syn"])),
        # )
        # self._psc_initial = np.where(
        #     np.array(self._params["tau_syn"]) == 0,
        #     0,
        #     np.e / np.array(self._params["tau_syn"]),
        # )

        path='GLIF_network/synaptic_data/tau_basis.npy'
        self._tau_syn = np.load(path)
        self._syn_decay = np.exp(-dt / self._tau_syn)
        self._psc_initial = np.e / self._tau_syn

        # self._syn_decay = np.exp(-dt / np.array(self._params["tau_syn"]))
        # self._psc_initial = np.e / np.array(self._params["tau_syn"])

        # synapses: target_ids, source_ids, weights, delays
        # this are the axonal delays
        self.max_delay = int(
            np.round(
                np.min([np.max(network["synapses"]["delays"]), max_delay]))
        )

        self.state_size = (
            self._n_neurons * self.max_delay,  # z buffer
            self._n_neurons,  # v
            self._n_neurons,  # r
            self._n_neurons,  # asc 1
            self._n_neurons,  # asc 2
            self._n_neurons * self._n_syn_basis,  # psc rise
            self._n_neurons * self._n_syn_basis,  # psc
        )

        def _f(_v, trainable=False):
            return tf.Variable(
                tf.cast(self._gather(_v), self._compute_dtype), trainable=trainable
            )

        def inv_sigmoid(_x):
            return tf.math.log(_x / (1 - _x))

        def custom_val(_v, trainable=False):
            _v = tf.Variable(
                tf.cast(inv_sigmoid(self._gather(_v)), self._compute_dtype),
                trainable=trainable,
            )

            def _g():
                return tf.nn.sigmoid(_v.read_value())

            return _v, _g

        self.v_reset = _f(self._params["V_reset"])
        self.syn_decay = self._syn_decay
        self.psc_initial = self._psc_initial
        # self.syn_decay = _f(self._syn_decay)
        # self.psc_initial = _f(self._psc_initial)

        # create the syn_decay attribute by repeating it the number of nodes
        # self.syn_decay = tf.tile(
        #             tf.cast(tf.expand_dims(self._syn_decay, axis=1), self._compute_dtype),
        #             [1, self._n_neurons]
        #         )
        # # transpose it
        # self.syn_decay = tf.transpose(self.syn_decay)
        # self.psc_initial = tf.tile(
        #             tf.cast(tf.expand_dims(self._psc_initial, axis=1), self._compute_dtype),
        #             [1, self._n_neurons]
        #         )
        # self.psc_initial = tf.transpose(self.psc_initial)
        print('psc_initial_shape', self.psc_initial.shape)

        self.t_ref = _f(self._params["t_ref"])  # refractory time
        self.asc_amps = _f(self._params["asc_amps"], trainable=False)
        _k = self._params["k"]
        # inverse sigmoid of the adaptation rate constant (1/ms)
        self.param_k, self.param_k_read = custom_val(_k, trainable=False)
        self.v_th = _f(self._params["V_th"])
        self.e_l = _f(self._params["E_L"])
        self.param_g = _f(self._params["g"])
        self.decay = _f(self._decay)
        self.current_factor = _f(self._current_factor)
        self.voltage_scale = _f(voltage_scale)
        self.voltage_offset = _f(voltage_offset)
        self.recurrent_weights = None

        indices, weights, dense_shape, tau_syn_weights = (
            network["synapses"]["indices"],
            network["synapses"]["weights"],
            network["synapses"]["dense_shape"],
            network["synapses"]["tau_syn_weights_array"],
        )

        self.tau_syn_weights = tau_syn_weights

        print('Consulta')
        print(weights.shape)
        print(self._node_type_ids[indices[:, 0] // self._n_syn_basis].shape)
        print(voltage_scale[self._node_type_ids[indices[:, 0] // self._n_syn_basis]].shape)

        # weights = (
        #     weights
        #     / voltage_scale[self._node_type_ids[indices[:, 0] // self._n_syn_basis]]
        # )  # scale down the weights

        weights = (
            weights
            / voltage_scale[self._node_type_ids[indices[:, 0]]]
        )  # scale down the weights

        #print the range of values in each column of indices array

        print('Indices shape: ', indices.shape)
        for i in range(indices.shape[1]):
            print(f"Column {i}: {np.min(indices[:, i])}, {np.max(indices[:, i])}")

        # weights = (weights / voltage_scale[self._node_type_ids[indices[:, 0]]])  # scale down the weights

        delays = np.round(
            np.clip(network["synapses"]["delays"], dt, self.max_delay) / dt
        ).astype(np.int32)
        dense_shape = dense_shape[0], self.max_delay * dense_shape[1]
        # Notice that in dense_shape, the first column (presynaptic neuron) has size receptors*n_neurons
        # and the second column (postsynaptic neuron) has size max_delay*n_neurons

        # print(delays.shape) # (n_edges*n_syn_basis)

        indices[:, 1] = indices[:, 1] + self._n_neurons * (delays - 1)
        # print the range of values in each column of indices array
        print('Indices shape: ', indices.shape)
        for i in range(indices.shape[1]):
            print(f"Column {i}: {np.min(indices[:, i])}, {np.max(indices[:, i])}")

        weights = weights.astype(np.float32)
        print(f"> Recurrent synapses {len(indices)}")

        input_weights = lgn_input["weights"].astype(np.float32)
        input_indices = lgn_input["indices"]
        self.input_tau_syn_weights = lgn_input["tau_syn_weights_array"]
        input_weights = (input_weights/ voltage_scale[
            self._node_type_ids[input_indices[:, 0]]])
        
        # input_weights = (input_weights/ voltage_scale[
        #     self._node_type_ids[input_indices[:, 0] // self._n_syn_basis]])
        # input_weights = (input_weights/ voltage_scale[self._node_type_ids[input_indices[:, 0]]])

        print(f"> LGN input synapses {len(input_indices)}")

        # input_dense_shape = (
        #     self._n_receptors * self._n_neurons,
        #     input_population["n_inputs"],
        # )
        lgn_input_dense_shape = (
            self._n_neurons,
            lgn_input["n_inputs"],
        )
        # input_dense_shape = (
        #     self._n_syn_basis * self._n_neurons,
        #     input_population["n_inputs"],
        # )

        bkg_input_weights = bkg_input["weights"].astype(np.float32)
        bkg_input_indices = bkg_input["indices"]
        self.bkg_input_tau_syn_weights = bkg_input["tau_syn_weights_array"]
        bkg_input_weights = (bkg_input_weights/ voltage_scale[
            self._node_type_ids[bkg_input_indices[:, 0]]])
        
        bkg_input_dense_shape = (
            self._n_neurons,
            bkg_input["n_inputs"],
        )
        
        print(f"> LGN input synapses {len(bkg_input_indices)}")

        self.recurrent_weight_positive = tf.Variable(
            weights >= 0.0, name="recurrent_weights_sign", trainable=False
        )
        self.input_weight_positive = tf.Variable(
            input_weights >= 0.0, name="input_weights_sign", trainable=False
        )
        self.recurrent_weight_values = tf.Variable(
            weights * recurrent_weight_scale / lr_scale,
            name="sparse_recurrent_weights",
            # to ensure that the weights always keep their sign
            constraint=SignedConstraint(self.recurrent_weight_positive),
            trainable=train_recurrent,
        )
        self.recurrent_indices = tf.Variable(indices, trainable=False)
        self.recurrent_dense_shape = dense_shape

        self.input_weight_values = tf.Variable(
            input_weights * input_weight_scale / lr_scale,
            name="sparse_input_weights",
            constraint=SignedConstraint(self.input_weight_positive),
            trainable=train_input,
        )
        self.input_indices = tf.Variable(input_indices, trainable=False)
        self.lgn_input_dense_shape = lgn_input_dense_shape
        # bkg_weights = bkg_weights / np.repeat(
        #     voltage_scale[self._node_type_ids], self._max_n_receptors
        # )
        print('bkg_weights_shape', bkg_input_weights.shape)
        print('voltage_scale_shape', voltage_scale.shape)
        print(voltage_scale[self._node_type_ids].shape)

        self.bkg_input_weight_positive = tf.Variable(
            bkg_input_weights >= 0.0, name="bkg_input_weights_sign", trainable=False
        )

        self.bkg_input_weights = tf.Variable(
            bkg_input_weights * input_weight_scale / lr_scale, 
            name="rest_of_brain_weights", 
            constraint=SignedConstraint(self.bkg_input_weight_positive),
            trainable=train_input
        )

        self.bkg_input_indices = tf.Variable(bkg_input_indices, trainable=False)
        self.bkg_input_dense_shape = bkg_input_dense_shape

        

    def compute_input_current(self, inp):
        tf_shp = tf.unstack(tf.shape(inp))
        shp = inp.shape.as_list()
        for i, a in enumerate(shp):
            if a is None:
                shp[i] = tf_shp[i]

        sparse_w_in = tf.sparse.SparseTensor(
            self.input_indices, self.input_weight_values, self.lgn_input_dense_shape
        )
        inp = tf.reshape(inp, (shp[0] * shp[1], shp[2]))
        input_current = tf.sparse.sparse_dense_matmul(
            sparse_w_in, tf.cast(inp, tf.float32), adjoint_b=True
        )
        input_current = tf.transpose(input_current)

        input_current = tf.reshape(
            input_current, (shp[0], shp[1], 10 * self._n_neurons))
        return input_current

    def zero_state(self, batch_size, dtype=tf.float32):
        # The neurons membrane voltage start the simulation at their reset value
        v0 = tf.ones((batch_size, self._n_neurons), dtype) * tf.cast(
            self.v_th * 0.0 + 1.0 * self.v_reset, dtype
        )
        z0_buf = tf.zeros(
            (batch_size, self._n_neurons * self.max_delay), dtype)
        r0 = tf.zeros((batch_size, self._n_neurons), dtype)
        asc_10 = tf.zeros((batch_size, self._n_neurons), dtype)
        asc_20 = tf.zeros((batch_size, self._n_neurons), dtype)
        psc_rise0 = tf.zeros(
            (batch_size, self._n_neurons * self._n_syn_basis), dtype
        )
        psc0 = tf.zeros((batch_size, self._n_neurons *
                        self._n_syn_basis), dtype)
        return z0_buf, v0, r0, asc_10, asc_20, psc_rise0, psc0

    def _gather(self, prop):
        return tf.gather(prop, self._node_type_ids)

    def call(self, inputs, state, constants=None):
        print('inputs_shape', inputs.shape)
        batch_size = inputs.shape[0]
        if batch_size is None:
            batch_size = tf.shape(inputs)[0]
        external_current = inputs
        if self._spike_gradient:
            state_input = tf.zeros((1,))
        else:
            state_input = tf.zeros((4,))
        if constants is not None:
            if self._spike_gradient:
                external_current = inputs[:,
                                          : self._n_neurons * self._n_syn_basis]
                state_input = inputs[:, self._n_neurons *
                                     self._n_syn_basis:]
            else:
                external_current = inputs[:,
                                          : self._n_neurons * self._n_syn_basis]
                state_input = inputs[:, self._n_neurons *
                                     self._n_syn_basis:]
                state_input = tf.reshape(
                    state_input, (batch_size, self._n_neurons,
                                  self._n_syn_basis)
                )
                # state_input = tf.reshape(state_input, (batch_size, self._n_neurons, 4))

        z_buf, v, r, asc_1, asc_2, psc_rise, psc = state

        shaped_z_buf = tf.reshape(
            z_buf, (-1, self.max_delay, self._n_neurons)
        )  # shape (4, 50000)
        prev_z = shaped_z_buf[:, 0]  # previous spikes with shape (50000)
        dampened_z_buf = z_buf * self._recurrent_dampening  # dampened version of z_buf # no entiendo muy bien la utilidad de esto
        rec_z_buf = (
            tf.stop_gradient(z_buf - dampened_z_buf) + dampened_z_buf
        )  # here we use tf.stop_gradient to prevent the term (z_buf - dampened_z_buf) to be trained

        psc_rise = tf.reshape(
            psc_rise, (batch_size, self._n_neurons, self._n_syn_basis)
        )
        psc = tf.reshape(
            psc, (batch_size, self._n_neurons, self._n_syn_basis))

        # Calculate the recurrent input current
        # sparse_w_rec = tf.sparse.SparseTensor(
        #     self.recurrent_indices,
        #     self.recurrent_weight_values,
        #     self.recurrent_dense_shape,
        # )
        # i_rec = tf.sparse.sparse_dense_matmul(
        #     sparse_w_rec, tf.cast(rec_z_buf, tf.float32), adjoint_b=True
        # )


        # sparse_w_receptors = tf.sparse.SparseTensor(
        #     self.recurrent_indices,
        #     self.tau_syn_weights[:, 0],
        #     self.recurrent_dense_shape,
        # )

        # we need to multiply the sparse_w_rec tensor by the sparse_w_receptors element wise
        # to obtain the correct weights for each receptor
        print('Aqui estamos!!!')
        print((batch_size, self._n_neurons, self._n_syn_basis))
        print(self._compute_dtype)
        
        # i_rec = tf.zeros((self._n_neurons, self._n_syn_basis), dtype=self._compute_dtype)
        i_rec = tf.TensorArray(dtype=self._compute_dtype, size=self._n_syn_basis)
        
        for r_id in range(self._n_syn_basis):
            weights_syn_receptors = self.recurrent_weight_values * self.tau_syn_weights[:, r_id]
            sparse_w_rec = tf.sparse.SparseTensor(
                self.recurrent_indices,
                weights_syn_receptors,
                self.recurrent_dense_shape,
            )
            # print(r_id)
            # print(weights_syn_receptors.shape)
            # print(sparse_w_rec.shape) # (n_neurons, n_neurons*max_delay)
            # # print(sparse_w_rec.shape) # (n_neurons*n_syn_basis, n_neurons*max_delay)
            # print(rec_z_buf.shape) # (, n_neurons*max_delay))
            # rec_z_buf += 1
            i_receptor = tf.sparse.sparse_dense_matmul(
                tf.cast(sparse_w_rec, self._compute_dtype), tf.cast(rec_z_buf, self._compute_dtype), adjoint_b=True
            )

            # assign the i_receptor to a slide of i_rec
            # i_rec[:, r_id] = i_receptor
            i_rec = i_rec.write(r_id, i_receptor)

        i_rec = i_rec.stack()

        

        # i_rec = tf.sparse.sparse_dense_matmul(
        #     tf.cast(sparse_w_rec, self._compute_dtype), tf.cast(rec_z_buf, self._compute_dtype), adjoint_b=True
        # )

        # (batch_size, n_neurons, n_syn_basis)
        # create an empty tensor with the shape (batch_size, self._n_neurons, self._n_syn_basis)
        
    

        print(i_rec.shape)
        
        i_rec = tf.transpose(i_rec)
        rec_inputs = tf.cast(i_rec, self._compute_dtype)
        # Add the external current to the recurrent current
        print('Add the external current')
        print(rec_inputs.shape) # (batch_size, n_neurons*n_syn_basis)
        print(external_current.shape)
        # rec_inputs = tf.reshape(
        #     rec_inputs,
        #     (batch_size, self._n_neurons, self._n_syn_basis),
        # )
        external_current = tf.reshape(
            external_current,
            (batch_size, self._n_neurons, self._n_syn_basis),
        )
        rec_inputs = tf.reshape(
            rec_inputs,
            (batch_size, self._n_neurons, self._n_syn_basis),
        )
        rec_inputs = rec_inputs + external_current

        # rec_inputs = tf.reshape(
        #     rec_inputs + external_current,
        #     (batch_size, self._n_neurons, self._n_syn_basis),
        # )

        rec_inputs = rec_inputs * self._lr_scale
        print(rec_inputs)

        if constants is not None and not self._spike_gradient:
            rec_inputs = rec_inputs + state_input * self._lr_scale

        print(psc_rise.shape)
        print(rec_inputs.shape)
        print(self.psc_initial.shape)
        print(self.syn_decay.shape)
        print(rec_inputs[0, 0, :])
        print(self.psc_initial)
        print((rec_inputs * self.psc_initial)[0, 0, :])


        new_psc_rise = psc_rise * self.syn_decay + rec_inputs * self.psc_initial
        new_psc = psc * self.syn_decay + self._dt * self.syn_decay * psc_rise

        print(new_psc_rise.shape)

        # New r is a variable that accounts for the refractory period in which
        # a neuron cannot spike
        new_r = tf.nn.relu(
            r + prev_z * self.t_ref - self._dt
        )  # =max(r + prev_z * self.t_ref - self._dt, 0)
        # Calculate the ASC
        k = self.param_k_read()
        asc_amps = self.asc_amps
        new_asc_1 = tf.exp(-self._dt * k[:, 0]) * \
            asc_1 + prev_z * asc_amps[:, 0]
        new_asc_2 = tf.exp(-self._dt * k[:, 1]) * \
            asc_2 + prev_z * asc_amps[:, 1]

        # if constants is not None and self._spike_gradient:
        #     input_current = tf.reduce_sum(psc, -1) + state_input
        # else:
        #     input_current = tf.reduce_sum(psc, -1)

        input_current = tf.reduce_sum(psc, -1)

        decayed_v = self.decay * v
        gathered_g = self.param_g * self.e_l
        c1 = input_current + asc_1 + asc_2 + gathered_g
        # Update the voltage according to the LIF equation and the refractory period
        if self._hard_reset:
            # Here we keep the voltage at the reset value during the refractory period
            new_v = tf.where(
                new_r > 0.0, self.v_reset, decayed_v + self.current_factor * c1
            )
            # Here we make a hard reset and let the voltage freely evolve but we do not let the
            # neuron spike during the refractory period
            # new_v = tf.where(prev_z > 0., self.v_reset, decayed_v + self.current_factor * c1)
        else:
            reset_current = prev_z * (self.v_reset - self.v_th)
            new_v = decayed_v + self.current_factor * c1 + reset_current

        normalizer = self.v_th - self.e_l
        v_sc = (new_v - self.v_th) / normalizer

        if self._pseudo_gauss:
            if self._compute_dtype == tf.bfloat16:
                new_z = spike_function_b16(v_sc, self._dampening_factor)
            elif self._compute_dtype == tf.float16:
                new_z = spike_gauss_16(
                    v_sc, self._gauss_std, self._dampening_factor
                )
            else:
                new_z = spike_gauss(
                    v_sc, self._gauss_std, self._dampening_factor)
        else:
            if self._compute_dtype == tf.float16:
                new_z = spike_function_16(v_sc, self._dampening_factor)
            else:
                new_z = spike_function(v_sc, self._dampening_factor)

        # new_z = spike_slayer(
        #     v_sc, 5.0, 0.6
        # )  # If v_sc is greater than 0 then there is a spike

        new_z = tf.where(new_r > 0.0, tf.zeros_like(new_z), new_z)
        new_psc = tf.reshape(
            new_psc, (batch_size, self._n_neurons * self._n_syn_basis)
        )
        new_psc_rise = tf.reshape(
            new_psc_rise, (batch_size, self._n_neurons * self._n_syn_basis)
        )
        # Add current spikes to the buffer
        new_shaped_z_buf = tf.concat((new_z[:, None], shaped_z_buf[:, :-1]), 1)
        new_z_buf = tf.reshape(
            new_shaped_z_buf, (-1, self._n_neurons * self.max_delay))

        outputs = (
            new_z,
            new_v * self.voltage_scale + self.voltage_offset,
            (input_current + new_asc_1 + new_asc_2) * self.voltage_scale,
        )
        new_state = (
            new_z_buf,
            new_v,
            new_r,
            new_asc_1,
            new_asc_2,
            new_psc_rise,
            new_psc,
        )

        return outputs, new_state


def huber_quantile_loss(u, tau, kappa):
    branch_1 = tf.abs(tau - tf.cast(u <= 0, tf.float32)) / \
        (2 * kappa) * tf.square(u)
    branch_2 = tf.abs(tau - tf.cast(u <= 0, tf.float32)) * \
        (tf.abs(u) - 0.5 * kappa)
    return tf.where(tf.abs(u) <= kappa, branch_1, branch_2)


def compute_spike_rate_distribution_loss(_spikes, target_rate):
    _rate = tf.reduce_mean(_spikes, (0, 1))
    ind = tf.range(target_rate.shape[0])
    rand_ind = tf.random.shuffle(ind)
    _rate = tf.gather(_rate, rand_ind)
    sorted_rate = tf.sort(_rate)
    u = target_rate - sorted_rate
    tau = (tf.cast(tf.range(target_rate.shape[0]), tf.float32) + 1) / target_rate.shape[
        0
    ]
    loss = huber_quantile_loss(u, tau, 0.002)

    return loss


class SpikeRateDistributionRegularization:
    def __init__(self, target_rates, rate_cost=0.5):
        self._rate_cost = rate_cost
        self._target_rates = target_rates

    def __call__(self, spikes):
        reg_loss = (
            compute_spike_rate_distribution_loss(spikes, self._target_rates)
            * self._rate_cost
        )
        reg_loss = tf.reduce_sum(reg_loss)

        return reg_loss


class VoltageRegularization:
    def __init__(self, cell, voltage_cost=1e-5):
        self._voltage_cost = voltage_cost
        self._cell = cell

    def __call__(self, voltages):
        voltage_32 = (
            tf.cast(voltages, tf.float32) - self._cell.voltage_offset
        ) / self._cell.voltage_scale
        v_pos = tf.square(tf.nn.relu(voltage_32 - 1.0))
        v_neg = tf.square(tf.nn.relu(-voltage_32 + 1.0))
        voltage_loss = (
            tf.reduce_mean(tf.reduce_sum(v_pos + v_neg, -1)) *
            self._voltage_cost
        )
        return voltage_loss


def create_model(
    network,
    lgn_input,
    bkg_input,
    seq_len=100,
    n_input=10,
    n_output=2,
    cue_duration=20,
    dtype=tf.float32,
    input_weight_scale=1.0,
    gauss_std=0.5,
    dampening_factor=0.2,
    lr_scale=800.0,
    train_recurrent=True,
    train_input=True,
    neuron_output=False,
    recurrent_dampening_factor=0.5,
    use_state_input=False,
    return_state=False,
    return_sequences=False,
    down_sample=50,
    add_metric=True,
    max_delay=5,
    batch_size=None,
    pseudo_gauss=False,
    hard_reset=False,
):

    # Create the input layer of the model
    x = tf.keras.layers.Input(shape=(seq_len, n_input,))
    neurons = network["n_nodes"]

    # Create an input layer for the initial state of the RNN
    state_input_holder = tf.keras.layers.Input(shape=(seq_len, neurons))
    state_input = tf.cast(tf.identity(state_input_holder), dtype)  

    # If batch_size is not provided as an argument, it is automatically inferred from the
    # first dimension of x using tf.shape().
    if batch_size is None:
        batch_size = tf.shape(x)[0]
    else:
        batch_size = batch_size

    # Create the V1Column cell
    print('Creating the V1 column')

    cell = V1Column(
        network,
        lgn_input,
        bkg_input,
        gauss_std=gauss_std,
        dampening_factor=dampening_factor,
        input_weight_scale=input_weight_scale,
        lr_scale=lr_scale,
        spike_gradient=True,
        recurrent_dampening_factor=recurrent_dampening_factor,
        max_delay=max_delay,
        pseudo_gauss=pseudo_gauss,
        train_recurrent=train_recurrent,
        train_input=train_input,
        hard_reset=hard_reset,
    )

    print("V1Column created")

    # initialize the RNN state to zero using the zero_state() method of the V1Column class.
    zero_state = cell.zero_state(batch_size, dtype)

    if use_state_input:
        # The shape of each input tensor matches the shape of the corresponding
        # tensor in the zero_state tuple, except for the batch dimension. The batch
        # dimension is left unspecified, allowing the tensor to be fed variable-sized
        # batches of data.
        initial_state_holder = tf.nest.map_structure(lambda _x: tf.keras.layers.Input(shape=_x.shape[1:]), zero_state)
        # The code then copies the input tensors into the rnn_initial_state variable
        # using tf.nest.map_structure(). This creates a nested structure of tensors with
        # the same shape as the original zero_state structure.
        rnn_initial_state = tf.nest.map_structure(tf.identity, initial_state_holder)
        # In both cases, the code creates a constants tensor using tf.zeros_like() or
        # tf.zeros(). This tensor is used to provide constant input to the RNN during
        # computation. The shape of the constants tensor matches the batch_size.
        constants = tf.zeros_like(rnn_initial_state[0][:, 0], dtype)
    else:
        rnn_initial_state = zero_state
        constants = tf.zeros((batch_size,))

    # Create the recurrent layer of the model
    rnn_inputs = SparseLayer(
        cell.input_indices,
        cell.input_weight_values,
        cell.lgn_input_dense_shape,
        cell.input_tau_syn_weights,
        # cell.bkg_input_weights,
        lr_scale=lr_scale,
        dtype=dtype,
        name="input_layer",
    )(x)

    noise_inputs = BackgroundNoiseLayer(
        cell.bkg_input_indices,
        cell.bkg_input_weights,
        cell.bkg_input_dense_shape,
        cell.bkg_input_tau_syn_weights,
        lr_scale=lr_scale,
        dtype=dtype,
        name="noise_layer",
    )(x)

    print('Heyyy')
    print(x.shape)
    print(noise_inputs.shape)
    print(rnn_inputs.shape)
    print(state_input.shape)

    rnn_inputs = rnn_inputs + noise_inputs

    print("Sparse layer created")

    # Concatenate the input layer with the initial state of the RNN
    rnn_inputs = tf.cast(rnn_inputs, dtype)
    full_inputs = tf.concat((rnn_inputs, state_input), -1)
    print('Full inputs shape: ', full_inputs.shape)
    # Create the RNN layer of the model using the V1Column cell
    # The RNN layer returns the output of the RNN layer and the final state of the RNN
    # layer. The output of the RNN layer is a tensor of shape (batch_size, seq_len,
    # neurons). The final state of the RNN layer is a tuple of tensors, each of shape
    # (batch_size, neurons).
    process = psutil.Process()
    mem = process.memory_info().rss / (1024 * 1024)  # in MB
    tf.print("Memory consumption:", mem)

    rnn = tf.keras.layers.RNN(
        cell, return_sequences=True, return_state=return_state, name="rsnn"
    )

    print("RNN layer created")

    # Apply the rnn layer to the full_inputs tensor
    out = rnn(full_inputs, initial_state=rnn_initial_state, constants=constants)
    process = psutil.Process()
    mem = process.memory_info().rss / (1024 * 1024)  # in MB
    tf.print("Memory consumption:", mem)
    print("RNN layer applied")
    # Check if the return_state argument is True or False and assign the output of the
    # RNN layer to the hidden variable accordingly.
    if return_state:
        hidden = out[0]
        new_state = out[1:]
    else:
        hidden = out
    spikes = hidden[0]
    voltage = hidden[1]
    # computes the mean of the spikes tensor along the second and third dimensions
    # (which represent time and neurons),
    rate = tf.cast(tf.reduce_mean(spikes, (1, 2)), tf.float32)

    # The neuron output option selects only the output neurons from the spikes tensor
    if neuron_output:
        # The output_spikes tensor is computed by taking a linear combination
        # of the current spikes and the previous spikes, with the coefficients
        # determined by the dampening_factor. This serves to reduce the volatility
        # of the output spikes, making them more stable.
        output_spikes = 1 / dampening_factor * spikes + (
            1 - 1 / dampening_factor
        ) * tf.stop_gradient(spikes)
        # The output tensor is then computed by selecting the spikes from the
        # output neurons and scaling them by a learned factor. The scale factor
        # is computed using a softplus activation function applied to the output
        # of a dense layer, and the threshold is computed by passing the output
        # spikes through another dense layer.
        output = tf.gather(
            output_spikes, network["readout_neuron_ids"], axis=2)
        output = tf.reduce_mean(output, -1)
        scale = 1 + tf.nn.softplus(
            tf.keras.layers.Dense(1)(tf.zeros_like(output[:1, :1]))
        )
        thresh = tf.keras.layers.Dense(1)(tf.zeros_like(output))
        output = tf.stack([thresh[..., 0], output[..., -1]], -1) * scale
    # If neuron_output is False, then the output tensor is simply the result of
    # passing the spikes tensor through a dense layer with n_output units.
    else:
        output = tf.keras.layers.Dense(n_output, name="projection", trainable=True)(
            spikes
        )
    # Finally, the output tensor is passed through a lambda layer which simply
    # returns the tensor as is.
    output = tf.keras.layers.Lambda(lambda _a: _a, name="prediction")(output)

    # If return_sequences is True, then the mean_output tensor is computed by
    # averaging over sequences of length down_sample in the output tensor.
    # Otherwise, mean_output is simply the mean of the last cue_duration time steps
    # of the output tensor.
    if return_sequences:
        mean_output = tf.reshape(
            output, (-1, int(seq_len / down_sample), down_sample, n_output)
        )
        mean_output = tf.reduce_mean(mean_output, 2)
        mean_output = tf.nn.softmax(mean_output, axis=-1)
    else:
        mean_output = tf.reduce_mean(output[:, -cue_duration:], 1)
        mean_output = tf.nn.softmax(mean_output)

    if use_state_input:
        many_input_model = tf.keras.Model(
            inputs=[x, state_input_holder,
                    initial_state_holder], outputs=mean_output
        )
    else:
        many_input_model = tf.keras.Model(
            inputs=[x, state_input_holder], outputs=mean_output
        )

    if add_metric:
        # add the firing rate of the neurons as a metric to the model
        many_input_model.add_metric(rate, name="rate")

    return many_input_model


# if name is main run the create model function
if __name__ == "__main__":
    # load the network
    import load_sparse

    n_input = 17400
    n_neurons = 1574

    network, lgn_input, bkg_input = load_sparse.cached_load_v1(
        n_input,
        n_neurons,
        True,
        "GLIF_network",
        seed=3000,
        connected_selection=False,
        n_output=2,
        neurons_per_output=16,
    )
    # create the model
    model = create_model(
        network,
        lgn_input,
        bkg_input,
        seq_len=100,
        n_input=n_input,
        n_output=2,
        cue_duration=20,
        dtype=tf.float32,
        input_weight_scale=1.0,
        gauss_std=0.5,
        dampening_factor=0.2,
        lr_scale=800.0,
        train_recurrent=True,
        train_input=True,
        neuron_output=False,
        recurrent_dampening_factor=0.5,
        use_state_input=False,
        return_state=False,
        return_sequences=False,
        down_sample=50,
        add_metric=True,
        max_delay=5,
        batch_size=1,
        pseudo_gauss=False,
        hard_reset=True,
    )

    # print the model summary
    # model.summary()

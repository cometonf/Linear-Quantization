#written by Jinbae Park
#2019-04

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import cv2
import tensorflow as tf
import tensorflow.keras.backend as K

# tensorpack
from tensorpack import *
from tensorpack.tfutils.varreplace import remap_variables
from tensorpack.tfutils import optimizer, gradproc
from tensorpack.tfutils.summary import add_moving_summary, add_param_summary
from tensorpack.utils import logger
from tensorpack.tfutils.common import get_global_step_var

# custom
from .regularization import regularizers
from .optimization.optimizers import get_optimizer
from .activation.activation_funcs import get_activation_func
from .quantization.quantizers import quantize_weight, quantize_activation, quantize_gradient
from .callbacks import CkptModifier
from .callbacks import StatsChecker
from .callbacks import InitSaver


class Model(ModelDesc):
    def __init__(self, config={}, size=32, nb_classes=10):
        self.config = config

        self.size = size
        self.nb_classes = nb_classes
        self.load_config = config['load']
        self.initializer_config = config['initializer']
        self.activation = get_activation_func(config['activation'])
        self.regularizer_config = config['regularizer']
        self.quantizer_config = config['quantizer']
        self.optimizer_config = config['optimizer']
        
    def inputs(self):
        return [tf.TensorSpec([None, self.size, self.size, 3], tf.float32, 'input'),
                tf.TensorSpec([None], tf.int32, 'label')]

    def build_graph(self, image, label):
        # get quantization function
        # quantize weights
        qw = quantize_weight(int(self.quantizer_config['BITW']), self.quantizer_config['name'], self.quantizer_config['W_opts'], self.quantizer_config)
        # quantize activation
        if self.quantizer_config['BITA'] in ['32', 32]:
            qa = tf.identity
        else:
            qa = quantize_activation(int(self.quantizer_config['BITA']))
        # quantize gradient
        qg = quantize_gradient(int(self.quantizer_config['BITG']))

        def new_get_variable(v):
            name = v.op.name
            # don't quantize first and last layer
            if not name.endswith('/W') or 'conv1' in name or 'fct' in name:
                return v
            else:
                logger.info("Quantizing weight {}".format(v.op.name))
                return qw(v)

        def activate(x):
            return qa(self.activation(x))

        with remap_variables(new_get_variable), \
                argscope(BatchNorm, decay=0.9, epsilon=1e-4), \
                argscope(Conv2D, use_bias=False, nl=tf.identity,
                         kernel_initializer=tf.variance_scaling_initializer(scale=float(self.initializer_config['scale']),
                                                                            mode=self.initializer_config['mode'])):
            logits = (LinearWrap(image)
                      .Conv2D('conv1', 96, 3)
                      .BatchNorm('bn1')
                      .apply(activate)
                      .Conv2D('conv2', 256, 3, padding='SAME', split=2)
                      .BatchNorm('bn2')
                      .apply(activate)
                      .MaxPooling('pool2', 2, 2, padding='VALID')  # size=16

                      .Conv2D('conv3', 384, 3)
                      .BatchNorm('bn3')
                      .apply(activate)
                      .MaxPooling('pool2', 2, 2, padding='VALID')  # size=8

                      .Conv2D('conv4', 384, 3, split=2)
                      .BatchNorm('bn4')
                      .apply(activate)

                      .Conv2D('conv5', 256, 3, split=2)
                      .BatchNorm('bn5')
                      .apply(activate)
                      .MaxPooling('pool5', 2, 2, padding='VALID')  # size=4

                      .FullyConnected('fc1', 4096, use_bias=False)
                      .BatchNorm('bnfc1')
                      .apply(activate)

                      .FullyConnected('fc2', 4096, use_bias=False)
                      .BatchNorm('bnfc2')
                      .apply(activate)

                      .FullyConnected('fct', self.nb_classes, use_bias=True)())
        prob = tf.nn.softmax(logits, name='output')

        cost = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=label)
        cost = tf.reduce_mean(cost, name='cross_entropy_loss')

        # regularization
        if self.regularizer_config['name'] not in [None, 'None']:
            reg_func = getattr(regularizers, self.regularizer_config['name'])().get_func(self.regularizer_config)
            reg_cost = tf.multiply(float(self.regularizer_config['lmbd']), regularize_cost('.*/W', reg_func), name='reg_cost')
            total_cost = tf.add_n([cost, reg_cost], name='total_cost')
        else:
            total_cost = cost

        # summary
        def add_summary(logits, cost):
            err_top1 = tf.cast(tf.logical_not(tf.nn.in_top_k(logits, label, 1)), tf.float32, name='err_top1')
            add_moving_summary(tf.reduce_mean(err_top1, name='train_error_top1'))
            err_top5 = tf.cast(tf.logical_not(tf.nn.in_top_k(logits, label, 5)), tf.float32, name='err_top5')
            add_moving_summary(tf.reduce_mean(err_top5, name='train_error_top5'))

            add_moving_summary(cost)
            add_param_summary(('.*/W', ['histogram']))  # monitor W
        add_summary(logits, cost)
            
        return total_cost

    def add_centralizing_update(self):
        def func(x):
            param_name = x.op.name
            if '/W' in param_name and 'conv1' not in param_name and 'fct' not in param_name:
                name_scope, device_scope = x.op.name.split('/W')

                inBIT, exBIT = eval(self.quantizer_config['W_opts']['threshold_bit'])
                ratio = (1 / (1 + ((2 ** exBIT - 1) / (2 ** inBIT - 1))))

                with tf.variable_scope(name_scope, reuse=tf.AUTO_REUSE):
                    if eval(self.quantizer_config['W_opts']['fix_max']):
                        #max_x_name = 'post_op_internals/' + name_scope + '/maxW'
                        #max_x_name = name_scope + '/maxW'
                        max_x = tf.stop_gradient(tf.get_variable('maxW', shape=(), initializer=tf.ones_initializer, dtype=tf.float32))
                        max_x *= float(self.quantizer_config['W_opts']['max_scale'])
                    else:
                        max_x = tf.stop_gradient(tf.reduce_max(tf.abs(x)))

                    thresh = max_x * ratio * 0.999

                    mask_name = name_scope + '/maskW'
                    mask = tf.get_variable('maskW', shape=x.shape, initializer=tf.zeros_initializer, dtype=tf.float32)

                new_x = tf.where(tf.equal(1.0, mask), tf.clip_by_value(x, -thresh, thresh), x)
                return tf.assign(x, new_x, use_locking=False).op

        self.centralizing = func

    def add_stop_grad(self):
        def func(grad, val):
            val_name = val.op.name
            if '/W' in val_name and 'conv1' not in val_name and 'fct' not in val_name:
                name_scope, device_scope = val.op.name.split('/W')
                
                with tf.variable_scope(name_scope, reuse=tf.AUTO_REUSE):
                    mask_name = name_scope + '/maskW'
                    mask = tf.get_variable('maskW', shape=val.shape, initializer=tf.zeros_initializer, dtype=tf.float32)

                    #zero_grad = tf.zeros(shape=grad.shape)

                    new_grad = tf.where(tf.equal(1.0, mask), grad, grad * 0.1)
                    
                return new_grad

        self.stop_grad = func

    def add_clustering_update(self, n_ls):
        def func(grad, val):
            val_name = val.op.name
            if '/W' in val_name and 'conv1' not in val_name and 'fct' not in val_name:
                cluster_mask_name = val_name.split('/W')[0] + '/cluster_maskW'
                cluster_mask = tf.get_variable(cluster_mask_name, shape=grad.shape, initializer=tf.zeros_initializer, dtype=tf.float32)

                total_grad = tf.zeros(shape=grad.shape)
                sum_grads = []

                for n in n_ls:
                    sum_grads.append(
                        tf.reduce_sum(tf.where(tf.equal(np.float32(n), cluster_mask), grad, total_grad)) /
                        tf.reduce_sum(tf.where(tf.equal(np.float32(n), cluster_mask), tf.ones(grad.shape), tf.zeros(grad.shape))))

                for i in range(len(n_ls)):
                    total_grad = tf.where(tf.equal(np.float32(n_ls[i]), cluster_mask),
                                          tf.fill(grad.shape, sum_grads[i]), total_grad)
                return total_grad

        self.clustering = func

    def add_masking_update(self):
        gamma = 0.0001
        crate = 3.

        inBIT, exBIT = eval(self.quantizer_config['W_opts']['threshold_bit'])
        ratio = (1 / (1 + ((2 ** exBIT - 1) / (2 ** inBIT - 1))))

        def func(val):
            val_name = val.op.name
            if '/W' in val_name and 'conv1' not in val_name and 'fct' not in val_name:
                name_scope, device_scope = x.op.name.split('/W')

                with tf.variable_scope(name_scope, reuse=tf.AUTO_REUSE):
                    if eval(self.quantizer_config['W_opts']['fix_max']) ==True:
                        max_x = tf.stop_gradient(
                            tf.get_variable('maxW', shape=(), initializer=tf.ones_initializer, dtype=tf.float32))
                        max_x *= float(self.quantizer_config['W_opts']['max_scale'])
                    else:
                        max_x = tf.stop_gradient(tf.reduce_max(tf.abs(x)))
                    mask = tf.get_variable('maskW', shape=val.shape, initializer=tf.zeros_initializer, dtype=tf.float32)

                probThreshold = (1 + gamma * get_global_step_var()) ** -1

                # Determine which filters shall be updated this iteration
                random_number = K.random_uniform(shape=(1, 1, 1, int(mask.shape[-1])))
                random_number1 = K.cast(random_number < probThreshold, dtype='float32')
                random_number2 = K.cast(random_number < (probThreshold * 0.1), dtype='float32')

                thresh = max_x * ratio * 0.999

                # Incorporate hysteresis into the threshold
                alpha = thresh
                beta = 1.2 * thresh

                # Update the significant weight mask by applying the threshold to the unmasked weights
                abs_kernel = K.abs(x=val)
                new_mask = mask - K.cast(abs_kernel < alpha, dtype='float32') * random_number1
                new_mask = new_mask + K.cast(abs_kernel > beta, dtype='float32') * random_number2
                new_mask = K.clip(x=new_mask, min_value=0., max_value=1.)
                return tf.assign(mask, new_mask, use_locking=False).op

        self.masking = func

    def optimizer(self):
        opt = get_optimizer(self.optimizer_config)
        '''
        if self.optimizer_config['second'] != None:
            temp = {'name': self.optimizer_config['second']}
            opt2 = get_optimizer(temp)

            choose = tf.get_variable('select_opt', initializer=False, dtype=tf.bool)
            opt = tf.cond(choose, opt2, opt)
        '''
        
        if self.quantizer_config['name'] == 'linear' and eval(self.quantizer_config['W_opts']['stop_grad']):
            self.add_stop_grad()
            opt = optimizer.apply_grad_processors(opt, [gradproc.MapGradient(self.stop_grad)])
        if self.quantizer_config['name'] == 'linear' and eval(self.quantizer_config['W_opts']['centralized']):
            self.add_centralizing_update()
            opt = optimizer.PostProcessOptimizer(opt, self.centralizing)
        if self.quantizer_config['name'] == 'cent':
            self.add_centralizing_update()
            opt = optimizer.PostProcessOptimizer(opt, self.centralizing)
        if self.quantizer_config['name'] == 'cluster' and eval(self.load_config['clustering']):
            opt = optimizer.apply_grad_processors(opt, [gradproc.MapGradient(self.clustering)])
        if self.quantizer_config['name'] == 'linear' and eval(self.quantizer_config['W_opts']['pruning']):
            self.add_masking_update()
            opt = optimizer.PostProcessOptimizer(opt, self.masking)
        return opt

    def get_callbacks(self, ds_tst):
        callbacks=[
            ModelSaver(max_to_keep=1),
            InferenceRunner(ds_tst,
                            [ScalarStats('cross_entropy_loss'),
                             ClassificationError('err_top1', summary_name='validation_error_top1'),
                             ClassificationError('err_top5', summary_name='validation_error_top5')]),
            MinSaver('validation_error_top1'),
            CkptModifier('min-validation_error_top1'),
            StatsChecker()
        ]

        # scheduling learning rate
        if self.optimizer_config['lr_schedule'] not in [None, 'None']:
            if type(self.optimizer_config['lr_schedule']) == str:
                callbacks += [ScheduledHyperParamSetter('learning_rate', eval(self.optimizer_config['lr_schedule']))]
            else:
                callbacks += [ScheduledHyperParamSetter('learning_rate', self.optimizer_config['lr_schedule'])]
        else:
            callbacks += [ScheduledHyperParamSetter('learning_rate',
                                      [(1, 0.1), (82, 0.01), (123, 0.001), (300, 0.0002)])]

        if eval(self.config['save_init']):
            callbacks = [InitSaver()]

        max_epoch = int(self.optimizer_config['max_epoch'])
        return callbacks, max_epoch

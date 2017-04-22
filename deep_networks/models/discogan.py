import datetime
import functools
import math
import operator
import os

import tensorflow as tf

from .base import GANModel
from .gan import BasicGenerator, BasicDiscriminator
from ..ops import lrelu
from ..train import IncrementalAverage


def build_conv_resize_conv_generator(X,
                                     is_training,
                                     updates_collections,
                                     output_shape,
                                     name='generator',
                                     reuse=False,
                                     min_size=4,
                                     dim=32,
                                     num_layers=4,
                                     skip_first_batch=False,
                                     activation_fn=None):
    assert num_layers > 0
    target_h, target_w, target_c = output_shape
    initializer = tf.contrib.layers.xavier_initializer()

    with tf.variable_scope(name, reuse=reuse):
        outputs = tf.reshape(X, (-1, ) + output_shape)
        for i in range(num_layers):
            with tf.variable_scope('d_conv{}'.format(i + 1)):
                if i == 0:
                    normalizer_fn = normalizer_params = None
                else:
                    normalizer_fn = tf.contrib.layers.batch_norm
                    normalizer_params = {
                        'is_training': is_training,
                        'updates_collections': updates_collections
                    }
                outputs = tf.layers.conv2d(
                    inputs=outputs,
                    filters=dim,
                    kernel_size=4,
                    strides=2,
                    padding='SAME',
                    activation=None,
                    kernel_initializer=initializer)
                if normalizer_fn is not None:
                    outputs = normalizer_fn(outputs, **normalizer_params)
                outputs = lrelu(outputs)
                dim *= 2

        dim /= 4
        for i in range(num_layers):
            with tf.variable_scope('g_rs_conv{}'.format(i + 1)):
                if i == num_layers - 1:
                    normalizer_fn = normalizer_params = None
                else:
                    normalizer_fn = tf.contrib.layers.batch_norm
                    normalizer_params = {
                        'is_training': is_training,
                        'updates_collections': updates_collections
                    }

                h = max(min_size,
                        int(math.ceil(target_h / (2**(num_layers - 1 - i)))))
                w = max(min_size,
                        int(math.ceil(target_w / (2**(num_layers - 1 - i)))))
                c = dim if i != num_layers - 1 else target_c
                outputs = tf.image.resize_nearest_neighbor(outputs, (h, w))
                outputs = tf.layers.conv2d(
                    inputs=outputs,
                    filters=c,
                    kernel_size=4,
                    strides=1,
                    padding='SAME',
                    activation=None,
                    kernel_initializer=initializer)
                if normalizer_fn is not None:
                    outputs = normalizer_fn(outputs, **normalizer_params)
                    outputs = tf.nn.relu(outputs)
                dim /= 2
        return tf.nn.tanh(tf.contrib.layers.flatten(outputs))


class DiscoGAN(GANModel):
    def __init__(self,
                 sess,
                 X_real,
                 Y_real,
                 num_examples,
                 x_output_shape,
                 y_output_shape,
                 reg_const=5e-5,
                 stddev=None,
                 g_dim=32,
                 d_dim=32,
                 batch_size=128,
                 g_learning_rate=0.0002,
                 g_beta1=0.5,
                 d_learning_rate=0.0002,
                 d_beta1=0.5,
                 d_label_smooth=0.25,
                 generator_cls=BasicGenerator,
                 discriminator_cls=BasicDiscriminator,
                 image_summary=False,
                 name='DiscoGAN'):
        with tf.variable_scope(name):
            super().__init__(
                sess=sess,
                name=name,
                num_examples=num_examples,
                output_shape=None,
                reg_const=reg_const,
                stddev=stddev,
                batch_size=batch_size,
                image_summary=image_summary)

            self.x_output_shape = x_output_shape
            self.y_output_shape = y_output_shape
            x_output_size = functools.reduce(operator.mul, x_output_shape)
            y_output_size = functools.reduce(operator.mul, y_output_shape)

            self.g_dim = g_dim
            self.g_learning_rate = g_learning_rate
            self.g_beta1 = g_beta1

            self.d_dim = d_dim
            self.d_learning_rate = d_learning_rate
            self.d_beta1 = d_beta1
            self.d_label_smooth = d_label_smooth

            self.X = X_real
            self.X = tf.placeholder_with_default(self.X, [None, x_output_size])
            self.Y = Y_real
            self.Y = tf.placeholder_with_default(self.Y, [None, y_output_size])
            self.updates_collections_noop = 'updates_collections_noop'

            self._build_GAN(generator_cls, discriminator_cls)
            self._build_losses()
            self._build_optimizer()
            self._build_summary()

            self.saver = tf.train.Saver()
            sess.run(tf.global_variables_initializer())

    def _build_GAN(self, generator_cls, discriminator_cls):
        self.x_g = generator_cls(
            z=self.Y,
            is_training=self.is_training,
            output_shape=self.x_output_shape,
            regularizer=self.regularizer,
            initializer=self.initializer,
            dim=self.g_dim,
            skip_first_batch=True,
            name='x_generator')
        self.y_g = generator_cls(
            z=self.X,
            is_training=self.is_training,
            output_shape=self.y_output_shape,
            regularizer=self.regularizer,
            initializer=self.initializer,
            dim=self.g_dim,
            skip_first_batch=True,
            name='y_generator')

        self.x_g_recon = generator_cls(
            z=self.y_g.outputs,
            is_training=self.is_training,
            output_shape=self.x_output_shape,
            updates_collections=self.updates_collections_noop,
            regularizer=self.regularizer,
            initializer=self.initializer,
            dim=self.g_dim,
            reuse=True,
            skip_first_batch=True,
            name='x_generator')
        self.y_g_recon = generator_cls(
            z=self.x_g.outputs,
            is_training=self.is_training,
            output_shape=self.y_output_shape,
            updates_collections=self.updates_collections_noop,
            regularizer=self.regularizer,
            initializer=self.initializer,
            dim=self.g_dim,
            reuse=True,
            skip_first_batch=True,
            name='y_generator')

        self.x_d_real = discriminator_cls(
            X=self.X,
            is_training=self.is_training,
            input_shape=self.x_output_shape,
            regularizer=self.regularizer,
            initializer=self.initializer,
            dim=self.d_dim,
            name='x_discriminator')

        self.y_d_real = discriminator_cls(
            X=self.Y,
            is_training=self.is_training,
            input_shape=self.y_output_shape,
            regularizer=self.regularizer,
            initializer=self.initializer,
            dim=self.d_dim,
            name='y_discriminator')

        self.x_d_fake = discriminator_cls(
            X=self.x_g.outputs,
            is_training=self.is_training,
            input_shape=self.x_output_shape,
            regularizer=self.regularizer,
            initializer=self.initializer,
            dim=self.d_dim,
            reuse=True,
            name='x_discriminator')

        self.y_d_fake = discriminator_cls(
            X=self.y_g.outputs,
            is_training=self.is_training,
            input_shape=self.y_output_shape,
            regularizer=self.regularizer,
            initializer=self.initializer,
            dim=self.d_dim,
            reuse=True,
            name='y_discriminator')

        self.x_d_recon = discriminator_cls(
            X=self.x_g_recon.outputs,
            is_training=self.is_training,
            input_shape=self.x_output_shape,
            regularizer=self.regularizer,
            initializer=self.initializer,
            dim=self.d_dim,
            reuse=True,
            name='x_discriminator')

        self.y_d_recon = discriminator_cls(
            X=self.y_g_recon.outputs,
            is_training=self.is_training,
            input_shape=self.y_output_shape,
            regularizer=self.regularizer,
            initializer=self.initializer,
            dim=self.d_dim,
            reuse=True,
            name='y_discriminator')

        with tf.variable_scope('x_generator') as scope:
            self.x_g_vars = tf.get_collection(
                tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope.name)
        with tf.variable_scope('y_generator') as scope:
            self.y_g_vars = tf.get_collection(
                tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope.name)
        with tf.variable_scope('x_discriminator') as scope:
            self.x_d_vars = tf.get_collection(
                tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope.name)
        with tf.variable_scope('y_discriminator') as scope:
            self.y_d_vars = tf.get_collection(
                tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope.name)

    def _build_losses(self):
        with tf.variable_scope('x_generator') as scope:
            self.x_recon_loss = tf.reduce_sum(
                tf.losses.mean_squared_error(
                    self.X, self.x_g_recon.outputs)) + self.feats_loss(
                        self.x_d_real.features, self.x_d_recon.features)

            self.x_g_loss = tf.reduce_mean(
                tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=self.x_d_fake.outputs_d,
                    labels=tf.ones_like(self.x_d_fake.outputs_d))
            ) + self.feats_loss(self.x_d_real.features, self.x_d_fake.features)

            x_g_reg_ops = tf.get_collection(
                tf.GraphKeys.REGULARIZATION_LOSSES, scope=scope.name)
            self.x_g_reg_loss = tf.add_n(x_g_reg_ops) if x_g_reg_ops else 0.0

        with tf.variable_scope('y_generator') as scope:
            self.y_recon_loss = tf.reduce_sum(
                tf.losses.mean_squared_error(
                    self.Y, self.y_g_recon.outputs)) + self.feats_loss(
                        self.y_d_real.features, self.y_d_recon.features)

            self.y_g_loss = tf.reduce_mean(
                tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=self.y_d_fake.outputs_d,
                    labels=tf.ones_like(self.y_d_fake.outputs_d))
            ) + self.feats_loss(self.y_d_real.features, self.y_d_fake.features)

            y_g_reg_ops = tf.get_collection(
                tf.GraphKeys.REGULARIZATION_LOSSES, scope=scope.name)
            self.y_g_reg_loss = tf.add_n(y_g_reg_ops) if y_g_reg_ops else 0.0

        if self.d_label_smooth > 0.0:
            labels_real = tf.ones_like(
                self.x_d_real.outputs_d) - self.d_label_smooth
        else:
            labels_real = tf.ones_like(self.x_d_real.outputs_d)

        with tf.variable_scope('x_discriminator') as scope:
            self.x_d_loss_real = tf.reduce_mean(
                tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=self.x_d_real.outputs_d, labels=labels_real))
            self.x_d_loss_fake = tf.reduce_mean(
                tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=self.x_d_fake.outputs_d,
                    labels=tf.zeros_like(self.x_d_fake.outputs_d)))

            x_d_reg_ops = tf.get_collection(
                tf.GraphKeys.REGULARIZATION_LOSSES, scope=scope.name)
            self.x_d_reg_loss = tf.add_n(x_d_reg_ops) if x_d_reg_ops else 0.0
        with tf.variable_scope('y_discriminator') as scope:
            self.y_d_loss_real = tf.reduce_mean(
                tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=self.y_d_real.outputs_d, labels=labels_real))
            self.y_d_loss_fake = tf.reduce_mean(
                tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=self.y_d_fake.outputs_d,
                    labels=tf.zeros_like(self.y_d_fake.outputs_d)))

            y_d_reg_ops = tf.get_collection(
                tf.GraphKeys.REGULARIZATION_LOSSES, scope=scope.name)
            self.y_d_reg_loss = tf.add_n(y_d_reg_ops) if y_d_reg_ops else 0.0

        self.g_total_loss = (
            self.x_g_loss + self.y_g_loss + self.x_recon_loss +
            self.y_recon_loss + self.x_g_reg_loss + self.y_g_reg_loss)
        self.d_total_loss = (
            self.x_d_loss_real + self.x_d_loss_fake + self.y_d_loss_real +
            self.y_d_loss_fake + self.x_d_reg_loss + self.y_d_reg_loss)

    def _build_summary(self):
        with tf.variable_scope('summary') as scope:
            if self.image_summary:
                self.x_sum = tf.summary.image(
                    'x', tf.reshape(self.X, (-1, ) + self.x_output_shape))
                self.y_sum = tf.summary.image(
                    'y', tf.reshape(self.Y, (-1, ) + self.y_output_shape))
                self.x_g_sum = tf.summary.image(
                    'x_g',
                    tf.reshape(self.x_g.outputs, (-1, ) + self.x_output_shape))
                self.y_g_sum = tf.summary.image(
                    'y_g',
                    tf.reshape(self.y_g.outputs, (-1, ) + self.y_output_shape))
                self.x_g_recon_sum = tf.summary.image(
                    'x_g_recon',
                    tf.reshape(self.x_g_recon.outputs,
                               (-1, ) + self.x_output_shape))
                self.y_g_recon_sum = tf.summary.image(
                    'y_g_recon',
                    tf.reshape(self.y_g_recon.outputs,
                               (-1, ) + self.y_output_shape))
            else:
                self.x_sum = tf.summary.histogram('x', self.X)
                self.y_sum = tf.summary.histogram('y', self.Y)
                self.x_g_sum = tf.summary.histogram('x_g', self.x_g.outputs)
                self.y_g_sum = tf.summary.histogram('y_g', self.y_g.outputs)
                self.x_g_recon_sum = tf.summary.histogram(
                    'x_g_recon', self.x_g_recon.outputs)
                self.y_g_recon_sum = tf.summary.histogram(
                    'y_g_recon', self.y_g_recon.outputs)
            self.x_d_real_sum = tf.summary.histogram(
                'x_d_real', self.x_d_real.activations_d)
            self.y_d_real_sum = tf.summary.histogram(
                'y_d_real', self.y_d_real.activations_d)
            self.x_d_fake_sum = tf.summary.histogram(
                'x_d_fake', self.x_d_fake.activations_d)
            self.y_d_fake_sum = tf.summary.histogram(
                'y_d_fake', self.y_d_fake.activations_d)

            self.g_total_loss_sum = tf.summary.scalar('g_total_loss',
                                                      self.g_total_loss)
            self.d_total_loss_sum = tf.summary.scalar('d_total_loss',
                                                      self.d_total_loss)

            self.summary = tf.summary.merge(
                tf.get_collection(tf.GraphKeys.SUMMARIES, scope=scope.name))

    def _build_optimizer(self):
        with tf.variable_scope('x_generator') as scope:
            update_ops_x_g = tf.get_collection(
                tf.GraphKeys.UPDATE_OPS, scope=scope.name)
        with tf.variable_scope('y_generator') as scope:
            update_ops_y_g = tf.get_collection(
                tf.GraphKeys.UPDATE_OPS, scope=scope.name)
        update_ops_g = update_ops_x_g + update_ops_y_g
        with tf.control_dependencies(update_ops_g):
            self.g_optim = tf.train.AdamOptimizer(
                self.g_learning_rate, beta1=self.g_beta1).minimize(
                    self.g_total_loss, var_list=self.x_g_vars + self.y_g_vars)

        with tf.variable_scope('x_discriminator') as d_scope:
            update_ops_x_d = tf.get_collection(
                tf.GraphKeys.UPDATE_OPS, scope=d_scope.name)
        with tf.variable_scope('y_discriminator') as d_scope:
            update_ops_y_d = tf.get_collection(
                tf.GraphKeys.UPDATE_OPS, scope=d_scope.name)
        update_ops_d = update_ops_x_d + update_ops_y_d
        with tf.control_dependencies(update_ops_d):
            self.d_optim = tf.train.AdamOptimizer(
                self.d_learning_rate, beta1=self.d_beta1).minimize(
                    self.d_total_loss, var_list=self.x_d_vars + self.y_d_vars)

    def train(self,
              num_epochs,
              resume=True,
              resume_step=None,
              checkpoint_dir=None,
              save_step=500,
              sample_step=100,
              sample_fn=None,
              log_dir='logs'):
        with tf.variable_scope(self.name):
            if log_dir is not None:
                log_dir = os.path.join(log_dir, self.name)
                os.makedirs(log_dir, exist_ok=True)
                run_name = '{}_{}'.format(self.name,
                                          datetime.datetime.now().isoformat())
                log_path = os.path.join(log_dir, run_name)
                self.writer = tf.summary.FileWriter(log_path, self.sess.graph)
            else:
                self.writer = None

            num_batches = self.num_examples // self.batch_size

            success, step = False, 0
            if resume and checkpoint_dir:
                success, saved_step = self.load(checkpoint_dir, resume_step)

            if success:
                step = saved_step
                start_epoch = step // num_batches
            else:
                start_epoch = 0

            for epoch in range(start_epoch, num_epochs):
                start_idx = step % num_batches
                epoch_g_total_loss = IncrementalAverage()
                epoch_d_total_loss = IncrementalAverage()
                t = self._trange(
                    start_idx, num_batches, desc='Epoch #{}'.format(epoch + 1))
                for idx in t:
                    _, _, d_total_loss, g_total_loss, summary_str = self.sess.run(
                        [
                            self.d_optim, self.g_optim, self.d_total_loss,
                            self.g_total_loss, self.summary
                        ])
                    epoch_d_total_loss.add(d_total_loss)
                    epoch_g_total_loss.add(g_total_loss)

                    if self.writer:
                        self.writer.add_summary(summary_str, step)
                    step += 1

                    # Save checkpoint
                    if checkpoint_dir and save_step and step % save_step == 0:
                        self.save(checkpoint_dir, step)

                    # Sample
                    if sample_fn and sample_step and (
                        (isinstance(sample_step, int) and
                         step % sample_step == 0) or
                        (not isinstance(sample_step, int) and
                         step in sample_step)):
                        sample_fn(self, step)

                    t.set_postfix(
                        g_loss=epoch_g_total_loss.average,
                        d_loss=epoch_d_total_loss.average)

    def sample_x(self, y=None):
        if y is not None:
            return [y] + self.sess.run(
                [self.x_g.outputs, self.y_g_recon.outputs],
                feed_dict={self.is_training: False,
                           self.Y: y})
        else:
            return self.sess.run(
                [self.Y, self.x_g.outputs, self.y_g_recon.outputs],
                feed_dict={self.is_training: False})

    def sample_y(self, x=None):
        if x is not None:
            return [x] + self.sess.run(
                [self.y_g.outputs, self.x_g_recon.outputs],
                feed_dict={self.is_training: False,
                           self.X: x})
        else:
            return self.sess.run(
                [self.X, self.y_g.outputs, self.x_g_recon.outputs],
                feed_dict={self.is_training: False})

    def feats_loss(self, real_feats, fake_feats):
        losses = tf.constant(0.)

        for real_feat, fake_feat in zip(real_feats, fake_feats):
            losses += tf.reduce_mean(
                tf.losses.mean_squared_error(real_feat, fake_feat))
        return losses

# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

r"""Train and Eval DQN.

To run:

```bash
tf_agents/agents/dqn/examples/train_eval_rnn_gym \
 --root_dir=$HOME/tmp/rnn-dqn/gym/cart-pole/ \
 --alsologtostderr
```
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time
from absl import flags

import tensorflow as tf

from tf_agents.agents.dqn import dqn_agent
from tf_agents.drivers import dynamic_episode_driver
from tf_agents.environments import suite_gym
from tf_agents.environments import tf_py_environment
from tf_agents.environments.examples import masked_cartpole  # pylint: disable=unused-import
from tf_agents.metrics import metric_utils
from tf_agents.metrics import py_metrics
from tf_agents.metrics import tf_metrics
from tf_agents.networks import q_rnn_network
from tf_agents.policies import py_tf_policy
from tf_agents.policies import random_tf_policy
from tf_agents.replay_buffers import tf_uniform_replay_buffer
from tf_agents.utils import common as common_utils

nest = tf.contrib.framework.nest

flags.DEFINE_string('root_dir', os.getenv('TEST_UNDECLARED_OUTPUTS_DIR'),
                    'Root directory for writing logs/summaries/checkpoints.')
flags.DEFINE_integer('num_iterations', 100000,
                     'Total number train/eval iterations to perform.')
flags.DEFINE_integer('eval_interval', 1000,
                     'Total number train/eval iterations to perform.')
FLAGS = flags.FLAGS


def train_eval(
    root_dir,
    env_name='MaskedCartPole-v0',
    num_iterations=100000,
    input_fc_layer_params=(50,),
    lstm_size=(20,),
    output_fc_layer_params=(20,),
    train_sequence_length=10,
    # Params for collect
    initial_collect_steps=50,
    collect_episodes_per_iteration=1,
    epsilon_greedy=0.1,
    replay_buffer_capacity=100000,
    # Params for target update
    target_update_tau=0.05,
    target_update_period=5,
    # Params for train
    train_steps_per_iteration=10,
    batch_size=128,
    learning_rate=1e-3,
    gamma=0.99,
    reward_scale_factor=1.0,
    gradient_clipping=None,
    # Params for eval
    num_eval_episodes=10,
    eval_interval=1000,
    # Params for summaries and logging
    train_checkpoint_interval=10000,
    policy_checkpoint_interval=5000,
    rb_checkpoint_interval=20000,
    log_interval=100,
    summary_interval=1000,
    summaries_flush_secs=10,
    debug_summaries=False,
    summarize_grads_and_vars=False,
    eval_metrics_callback=None):
  """A simple train and eval for DQN."""
  root_dir = os.path.expanduser(root_dir)
  train_dir = os.path.join(root_dir, 'train')
  eval_dir = os.path.join(root_dir, 'eval')

  train_summary_writer = tf.contrib.summary.create_file_writer(
      train_dir, flush_millis=summaries_flush_secs * 1000)
  train_summary_writer.set_as_default()

  eval_summary_writer = tf.contrib.summary.create_file_writer(
      eval_dir, flush_millis=summaries_flush_secs * 1000)
  eval_metrics = [
      py_metrics.AverageReturnMetric(buffer_size=num_eval_episodes),
      py_metrics.AverageEpisodeLengthMetric(buffer_size=num_eval_episodes),
  ]

  with tf.contrib.summary.record_summaries_every_n_global_steps(
      summary_interval):

    eval_py_env = suite_gym.load(env_name)
    tf_env = tf_py_environment.TFPyEnvironment(suite_gym.load(env_name))

    q_net = q_rnn_network.QRnnNetwork(
        tf_env.time_step_spec().observation,
        tf_env.action_spec(),
        input_fc_layer_params=input_fc_layer_params,
        lstm_size=lstm_size,
        output_fc_layer_params=output_fc_layer_params)

    tf_agent = dqn_agent.DqnAgent(
        tf_env.time_step_spec(),
        tf_env.action_spec(),
        q_network=q_net,
        optimizer=tf.train.AdamOptimizer(learning_rate=learning_rate),
        # TODO(kbanoop): Decay epsilon based on global step, cf. cl/188907839
        epsilon_greedy=epsilon_greedy,
        target_update_tau=target_update_tau,
        target_update_period=target_update_period,
        td_errors_loss_fn=dqn_agent.element_wise_squared_loss,
        gamma=gamma,
        reward_scale_factor=reward_scale_factor,
        gradient_clipping=gradient_clipping,
        debug_summaries=debug_summaries,
        summarize_grads_and_vars=summarize_grads_and_vars)

    replay_buffer = tf_uniform_replay_buffer.TFUniformReplayBuffer(
        tf_agent.collect_data_spec(),
        batch_size=tf_env.batch_size,
        max_length=replay_buffer_capacity)

    eval_py_policy = py_tf_policy.PyTFPolicy(tf_agent.policy())

    train_metrics = [
        tf_metrics.NumberOfEpisodes(),
        tf_metrics.EnvironmentSteps(),
        tf_metrics.AverageReturnMetric(),
        tf_metrics.AverageEpisodeLengthMetric(),
    ]

    global_step = tf.train.get_or_create_global_step()

    initial_collect_policy = random_tf_policy.RandomTFPolicy(
        tf_env.time_step_spec(), tf_env.action_spec())
    initial_collect_op = dynamic_episode_driver.DynamicEpisodeDriver(
        tf_env,
        initial_collect_policy,
        observers=[replay_buffer.add_batch],
        num_episodes=initial_collect_steps).run()

    collect_policy = tf_agent.collect_policy()
    collect_op = dynamic_episode_driver.DynamicEpisodeDriver(
        tf_env,
        collect_policy,
        observers=[replay_buffer.add_batch] + train_metrics,
        num_episodes=collect_episodes_per_iteration).run()

    # Need extra step to generate transitions of train_sequence_length.
    # Dataset generates trajectories with shape [BxTx...]
    dataset = replay_buffer.as_dataset(
        num_parallel_calls=3,
        sample_batch_size=batch_size,
        num_steps=train_sequence_length + 1).prefetch(3)

    iterator = dataset.make_initializable_iterator()
    experience, _ = iterator.get_next()
    loss_info = tf_agent.train(
        experience=experience, train_step_counter=global_step)

    train_checkpointer = common_utils.Checkpointer(
        ckpt_dir=train_dir,
        agent=tf_agent,
        global_step=global_step,
        metrics=tf.contrib.checkpoint.List(train_metrics))
    policy_checkpointer = common_utils.Checkpointer(
        ckpt_dir=os.path.join(train_dir, 'policy'),
        policy=tf_agent.policy(),
        global_step=global_step)
    rb_checkpointer = common_utils.Checkpointer(
        ckpt_dir=os.path.join(train_dir, 'replay_buffer'),
        max_to_keep=1,
        replay_buffer=replay_buffer)

    for train_metric in train_metrics:
      train_metric.tf_summaries(step_metrics=train_metrics[:2])
    summary_op = tf.contrib.summary.all_summary_ops()

    with eval_summary_writer.as_default(), \
         tf.contrib.summary.always_record_summaries():
      for eval_metric in eval_metrics:
        eval_metric.tf_summaries()

    init_agent_op = tf_agent.initialize()

    with tf.Session() as sess:
      tf.contrib.summary.initialize(graph=tf.get_default_graph())
      # Initialize the graph.
      train_checkpointer.initialize_or_restore(sess)
      rb_checkpointer.initialize_or_restore(sess)
      sess.run(iterator.initializer)
      # TODO(sguada) Remove once Periodically can be saved.
      common_utils.initialize_uninitialized_variables(sess)

      sess.run(init_agent_op)
      tf.contrib.summary.initialize(session=sess)
      tf.logging.info('Collecting initial experience.')
      sess.run(initial_collect_op)

      # Compute evaluation metrics.
      global_step_val = sess.run(global_step)
      metric_utils.compute_summaries(
          eval_metrics,
          eval_py_env,
          eval_py_policy,
          num_episodes=num_eval_episodes,
          global_step=global_step_val,
          callback=eval_metrics_callback,
      )

      collect_call = sess.make_callable(collect_op)
      train_step_call = sess.make_callable([loss_info, summary_op, global_step])

      timed_at_step = sess.run(global_step)
      time_acc = 0
      steps_per_second_ph = tf.placeholder(
          tf.float32, shape=(), name='steps_per_sec_ph')
      steps_per_second_summary = tf.contrib.summary.scalar(
          name='global_steps/sec', tensor=steps_per_second_ph)

      for _ in range(num_iterations):
        # Train/collect/eval.
        start_time = time.time()
        collect_call()
        for _ in range(train_steps_per_iteration):
          loss_info_value, _, global_step_val = train_step_call()
        time_acc += time.time() - start_time

        if global_step_val % log_interval == 0:
          tf.logging.info(
              'step = %d, loss = %f', global_step_val, loss_info_value.loss)
          steps_per_sec = (global_step_val - timed_at_step) / time_acc
          tf.logging.info('%.3f steps/sec' % steps_per_sec)
          sess.run(
              steps_per_second_summary,
              feed_dict={steps_per_second_ph: steps_per_sec})
          timed_at_step = global_step_val
          time_acc = 0

        if global_step_val % train_checkpoint_interval == 0:
          train_checkpointer.save(global_step=global_step_val)

        if global_step_val % policy_checkpoint_interval == 0:
          policy_checkpointer.save(global_step=global_step_val)

        if global_step_val % rb_checkpoint_interval == 0:
          rb_checkpointer.save(global_step=global_step_val)

        if global_step_val % eval_interval == 0:
          metric_utils.compute_summaries(
              eval_metrics,
              eval_py_env,
              eval_py_policy,
              num_episodes=num_eval_episodes,
              global_step=global_step_val,
              log=True,
              callback=eval_metrics_callback,
          )


def main(_):
  tf.logging.set_verbosity(tf.logging.INFO)
  train_eval(
      FLAGS.root_dir,
      num_iterations=FLAGS.num_iterations,
      eval_interval=FLAGS.eval_interval)


if __name__ == '__main__':
  flags.mark_flag_as_required('root_dir')
  tf.app.run()

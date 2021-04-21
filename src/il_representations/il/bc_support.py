"""Support code for using imitation's BC implementation."""
import collections
import json
import logging
import os

import torch as th
import imitation.data.rollout as il_rollout


class MultiCallback:
    """Callback that allows multiple callbacks to be passed into `on_epoch_end`"""
    def __init__(self, callbacks):
        self.callbacks = callbacks

    def __call__(self, **kwargs):
        for callback in self.callbacks:
            callback(**kwargs)


class BCModelSaver:
    """Callback that saves BC policy every N epochs."""
    def __init__(self, policy, save_dir, epoch_length, save_interval_batches):
        self.policy = policy
        self.save_dir = save_dir
        self.last_save_batches = 0
        self.save_interval_batches = save_interval_batches
        self.batch_count = 0
        self.epoch_length = epoch_length

    def __call__(self, **kwargs):
        """It is assumed that this is called on epoch end."""
        self.batch_count += self.epoch_length
        if self.batch_count >= self.last_save_batches + self.save_interval_batches:
            os.makedirs(self.save_dir, exist_ok=True)
            save_fn = f'policy_{self.batch_count:08d}_batches.pt'
            save_path = os.path.join(self.save_dir, save_fn)
            th.save(self.policy, save_path)
            print(f"Saved policy to {save_path}!")
            self.last_save_batches = self.batch_count


class IntermediateRolloutEvaluator:
    """Callback that saves BC policy every N epochs."""
    def __init__(self, policy, vec_env, save_dir, epoch_length, evaluate_interval_batches, n_rollouts):
        self.policy = policy
        self.vec_env = vec_env
        self.save_dir = save_dir
        self.last_save_batches = 0
        self.evaluate_interval_batches = evaluate_interval_batches
        self.batch_count = 0
        self.epoch_length = epoch_length
        self.n_rollouts = n_rollouts

    def get_stats(self):
        # Stolen from il_test
        trajectories = il_rollout.generate_trajectories(
            self.policy, self.vec_env, il_rollout.min_episodes(self.n_rollouts))
        stats = il_rollout.rollout_stats(trajectories)
        stats = collections.OrderedDict([(key, stats[key])
                                         for key in sorted(stats)])
        return stats

    def __call__(self, **kwargs):
        """It is assumed that this is called on epoch end."""
        self.batch_count += self.epoch_length
        if self.batch_count >= self.last_save_batches + self.evaluate_interval_batches:
            stats = self.get_stats()
            kv_message = '\n'.join(f"  {key}={value}"
                                   for key, value in stats.items())
            logging.info(f"Evaluation stats at '{self.batch_count:08d}' batches: {kv_message}")

            os.makedirs(self.save_dir, exist_ok=True)
            save_filename = f'evaluation_{self.batch_count:08d}_batches.json'
            save_path = os.path.join(self.save_dir, save_filename)
            with open(save_path, 'w') as fp:
                json.dump(stats, fp, indent=2, sort_keys=False)
            print(f"Rolled out {self.n_rollouts} trajectories, saved stats to to {save_path}!")
            self.last_save_batches = self.batch_count

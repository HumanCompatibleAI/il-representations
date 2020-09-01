import copy
import os
import time
import os.path as osp
import numpy as np
import sacred
from sacred import Experiment
from sacred.observers import FileStorageObserver
import ray
from ray import tune
from il_representations.scripts.run_rep_learner import represent_ex
from il_representations.scripts.il_train import il_train_ex
from il_representations.scripts.il_test import il_test_ex
from il_representations.scripts.utils import sacred_copy, update, detect_ec2


chain_ex = Experiment('chain', ingredients=[represent_ex, il_train_ex, il_test_ex])
output_root = 'runs/chain_runs'
cwd = os.getcwd()


def run_single_exp(inner_ex_config, config, log_dir, exp_name):
    """
    Run a specified experiment. We could not pass each Sacred experiment in because they are not pickle serializable,
    which is not supported by Ray (when running this as a remote function).

    params:
        inner_ex_config: The current experiment's default config.
        config: The config generated by Ray tune for hyperparameter tuning
        log_dir: The log directory of current chain experiment.
        exp_name: Specify the experiment type in ['rep', 'il_train', 'il_test']
    """
    from il_representations.scripts.run_rep_learner import represent_ex
    from il_representations.scripts.il_train import il_train_ex
    from il_representations.scripts.il_test import il_test_ex

    assert exp_name in ['rep', 'il_train', 'il_test']
    if exp_name == 'rep':
        inner_ex = represent_ex
    elif exp_name == 'il_train':
        inner_ex = il_train_ex
    elif exp_name == 'il_test':
        inner_ex = il_test_ex
    else:
        raise

    inner_ex_dict = dict(inner_ex_config)
    merged_config = update(inner_ex_dict, config)
    observer = FileStorageObserver(osp.join(log_dir, exp_name))
    inner_ex.observers.append(observer)
    ret_val = inner_ex.run(config_updates=merged_config)
    return ret_val.result


def run_end2end_exp(rep_ex_config, il_train_ex_config, il_test_ex_config, config, log_dir):
    """
    Run representation learning, imitation learning's training and testing sequentially.

    Params:
        rep_ex_config: Config of represent_ex. It's the default config plus any modifications we might have made
                       in an macro_experiment config update.
        il_train_ex_config: Config of il_train_ex. It's the default config plus any modifications we might have made
                       in an macro_experiment config update.
        il_test_ex_config: Config of il_test_ex. It's the default config plus any modifications we might have made
                       in an macro_experiment config update.
        config: The config generated by Ray tune for hyperparameter tuning
        log_dir: The log directory of current chain experiment.
    """
    # generate a new random seed
    # TODO(sam): use the same seed for different configs, but different seeds
    # within each repeat of a single config (to reduce variance)
    rng = np.random.RandomState()

    # copy config so that we don't mutate in-place
    config = copy.deepcopy(config)

    # Run representation learning
    config['rep'].update({
        'seed': rng.randint(1 << 31),
    })
    pretrain_result = run_single_exp(rep_ex_config, config['rep'], log_dir, 'rep')

    # Run il train
    config['il_train'].update({
        'encoder_path': osp.join(cwd, pretrain_result['encoder_path']),
        'seed': rng.randint(1 << 31),
    })
    il_train_result = run_single_exp(il_train_ex_config, config['il_train'], log_dir, 'il_train')

    # Run il test
    config['il_test'].update({
        'policy_path': osp.join(cwd, il_train_result['model_path']),
        'seed': rng.randint(1 << 31),
    })
    il_test_result = run_single_exp(il_test_ex_config, config['il_test'], log_dir, 'il_test')

    tune.report(reward=il_test_result['reward_mean'])


@chain_ex.config
def base_config():
    exp_name = "grid_search"
    metric = 'reward_mean'
    assert metric in ['reward_mean']  # currently only supports one metric
    spec = {
        'rep': {
            'algo': tune.grid_search(['MoCo', 'SimCLR']),
        },
        'il_train': {
            'algo': tune.grid_search(['bc']),
            # 'freeze_encoder': tune.grid_search([True, False])
        },
        'il_test': {
        }
    }

    representation_learning = {
        'root_dir': cwd,
    }

    il_train = {
        'root_dir': cwd,
    }

    tune_run_kwargs = dict(
        num_samples=1,
        resources_per_trial=dict(
            cpu=5,
            gpu=0.32,
        ))

    _ = locals()
    del _


@chain_ex.main
def run(exp_name, metric, spec, representation_learning, il_train,
        il_test, tune_run_kwargs):
    rep_ex_config = sacred_copy(representation_learning)
    il_train_ex_config = sacred_copy(il_train)
    il_test_ex_config = sacred_copy(il_test)
    spec = sacred_copy(spec)
    log_dir = chain_ex.observers[0].dir

    def trainable_function(config):
        run_end2end_exp(rep_ex_config, il_train_ex_config, il_test_ex_config,
                        config, log_dir)

    if detect_ec2():
        ray.init(address="auto")
    else:
        ray.init()

    rep_run = tune.run(
        trainable_function,
        name=exp_name,
        config=spec,
        **tune_run_kwargs,
    )

    best_config = rep_run.get_best_config(metric=metric)
    print(f"Best config is: {best_config}")
    print("Results available at: ")
    print(rep_run._get_trial_paths())


def main():
    sacred.SETTINGS['CAPTURE_MODE'] = 'sys'
    observer = FileStorageObserver('runs/chain_runs')
    chain_ex.observers.append(observer)
    chain_ex.run_commandline()


if __name__ == '__main__':
    main()

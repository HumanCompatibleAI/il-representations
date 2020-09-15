import collections
import copy
import enum
import logging
import os
import os.path as osp
import weakref

import numpy as np
import ray
from ray import tune
from ray.tune.schedulers import FIFOScheduler
from ray.tune.suggest.skopt import SkOptSearch
import sacred
from sacred import Experiment
from sacred.observers import FileStorageObserver
from skopt.optimizer import Optimizer

from il_representations.envs.config import benchmark_ingredient
from il_representations.scripts.il_test import il_test_ex
from il_representations.scripts.il_train import il_train_ex
from il_representations.scripts.run_rep_learner import represent_ex
from il_representations.scripts.utils import detect_ec2, sacred_copy, update

sacred.SETTINGS['CAPTURE_MODE'] = 'sys'  # workaround for sacred issue#740
chain_ex = Experiment(
    'chain',
    ingredients=[
        # explicitly list every ingredient we want to configure
        represent_ex,
        il_train_ex,
        il_test_ex,
        benchmark_ingredient,
    ])
cwd = os.getcwd()


class StagesToRun(str, enum.Enum):
    """These enum flags are used to control whether the script tunes RepL, or
    IL, or both."""
    REPL_AND_IL = "REPL_AND_IL"
    REPL_ONLY = "REPL_ONLY"
    IL_ONLY = "IL_ONLY"


def get_stages_to_run(stages_to_run):
    """Convert a string (or enum) to StagesToRun object."""
    upper_str = stages_to_run.upper()
    try:
        stage = StagesToRun(upper_str)
    except ValueError as ex:
        options = [f"'{s.name}'" for s in StagesToRun]
        raise ValueError(
            f"Could not convert '{stages_to_run}' to StagesToRun ({ex}). "
            f"Available options are {', '.join(options)}")
    return stage


class CheckpointFIFOScheduler(FIFOScheduler):
    """Variant of FIFOScheduler that periodically saves the given search
    algorithm. Useful for, e.g., SkOptSearch, where it is helpful to be able to
    re-instantiate the search object later on."""

    # FIXME: this is a stupid hack, inherited from another project. There
    # should be a better way of saving skopt internals as part of Ray Tune.
    # Perhaps defining a custom trainable would do the trick?
    def __init__(self, search_alg):
        self.search_alg = weakref.proxy(search_alg)

    def on_trial_complete(self, trial_runner, trial, result):
        rv = super().on_trial_complete(trial_runner, trial, result)
        # references to _local_checkpoint_dir and _session_dir are a bit hacky
        checkpoint_path = os.path.join(
            trial_runner._local_checkpoint_dir,
            f'search-alg-{trial_runner._session_str}.pkl')
        self.search_alg.save(checkpoint_path + '.tmp')
        os.rename(checkpoint_path + '.tmp', checkpoint_path)
        return rv


def expand_dict_keys(config_dict):
    """Some Ray Tune hyperparameter search options do not supported nested
    dictionaries for configuration. To emulate nested dictionaries, we use a
    plain dictionary with keys of the form "level1:level2:…". . The colons are
    then separated out by this function into a nested dict (e.g. {'level1':
    {'level2': …}}). Example:

    >>> expand_dict_keys({'x:y': 42, 'z': 4, 'x:u:v': 5, 'w': {'s:t': 99}})
    {'x': {'y': 42, 'u': {'v': 5}}, 'z': 4, 'w': {'s': {'t': 99}}}
    """
    dict_type = type(config_dict)
    new_dict = dict_type()

    for key, value in config_dict.items():
        dest_dict = new_dict

        parts = key.split(':')
        for part in parts[:-1]:
            if part not in dest_dict:
                # create a new sub-dict if necessary
                dest_dict[part] = dict_type()
            else:
                assert isinstance(dest_dict[part], dict)
            dest_dict = dest_dict[part]
        if isinstance(value, dict):
            # recursively expand nested dicts
            value = expand_dict_keys(value)
        dest_dict[parts[-1]] = value

    return new_dict


def run_single_exp(inner_ex_config, benchmark_config, tune_config_updates,
                   log_dir, exp_name):
    """
    Run a specified experiment. We could not pass each Sacred experiment in because they are not pickle serializable,
    which is not supported by Ray (when running this as a remote function).

    params:
        inner_ex_config: The current experiment's default config.
        config: The config generated by Ray tune for hyperparameter tuning
        log_dir: The log directory of current chain experiment.
        exp_name: Specify the experiment type in ['repl', 'il_train', 'il_test']
    """
    # we need to run the workaround in each raylet, so we do it at the start of run_single_exp
    sacred.SETTINGS['CAPTURE_MODE'] = 'sys'  # workaround for sacred issue#740

    from il_representations.scripts.il_test import il_test_ex
    from il_representations.scripts.il_train import il_train_ex
    from il_representations.scripts.run_rep_learner import represent_ex

    if exp_name == 'repl':
        inner_ex = represent_ex
    elif exp_name == 'il_train':
        inner_ex = il_train_ex
    elif exp_name == 'il_test':
        inner_ex = il_test_ex
    else:
        raise ValueError(f"cannot process exp type '{exp_name}'")

    assert tune_config_updates.keys() <= {'repl', 'il_train', 'il_test', 'benchmark'}, \
            tune_config_updates.keys()

    inner_ex_dict = dict(inner_ex_config)
    # combine with benchmark config
    merged_config = update(inner_ex_dict, dict(benchmark=benchmark_config))
    # now combine with rest of config values, form Ray
    merged_config = update(merged_config,
                           tune_config_updates.get(exp_name, {}))
    tune_bench_updates = tune_config_updates.get('benchmark', {})
    merged_config = update(merged_config, dict(benchmark=tune_bench_updates))
    observer = FileStorageObserver(osp.join(log_dir, exp_name))
    inner_ex.observers.append(observer)
    ret_val = inner_ex.run(config_updates=merged_config)
    return ret_val.result


def setup_run(config):
    """To be run before an experiment"""

    # generate a new random seed
    # TODO(sam): use the same seed for different configs, but different seeds
    # within each repeat of a single config (to reduce variance)
    rng = np.random.RandomState()

    # copy config so that we don't mutate in-place
    config = copy.deepcopy(config)

    return rng, config


def report_experiment_result(sacred_result):
    """To be run after an experiment."""

    filtered_result = {
        k: v
        for k, v in sacred_result.items() if isinstance(v, (int, float))
    }
    logging.info(
        f"Got sacred result with keys {', '.join(filtered_result.keys())}")
    tune.report(**filtered_result)


def run_end2end_exp(rep_ex_config, il_train_ex_config, il_test_ex_config,
                    benchmark_config, config, log_dir):
    """
    Run representation learning, imitation learning's training and testing sequentially.

    Params:
        rep_ex_config: Config of represent_ex. It's the default config plus any modifications we might have made
                       in an macro_experiment config update.
        il_train_ex_config: Config of il_train_ex. It's the default config plus any modifications we might have made
                       in an macro_experiment config update.
        il_test_ex_config: Config of il_test_ex. It's the default config plus any modifications we might have made
                       in an macro_experiment config update.
        benchmark_config: Config of benchmark. Used for all experiments.
        config: The config generated by Ray tune for hyperparameter tuning
        log_dir: The log directory of current chain experiment.
    """
    rng, tune_config_updates = setup_run(config)
    del config  # I want a new name for it

    # Run representation learning
    tune_config_updates['repl'].update({
        'seed': rng.randint(1 << 31),
    })
    pretrain_result = run_single_exp(rep_ex_config, benchmark_config,
                                     tune_config_updates, log_dir, 'repl')

    # Run il train
    tune_config_updates['il_train'].update({
        'encoder_path':
        pretrain_result['encoder_path'],
        'seed':
        rng.randint(1 << 31),
    })
    il_train_result = run_single_exp(il_train_ex_config, benchmark_config,
                                     tune_config_updates, log_dir, 'il_train')

    # Run il test
    tune_config_updates['il_test'].update({
        'policy_path':
        il_train_result['model_path'],
        'seed':
        rng.randint(1 << 31),
    })
    il_test_result = run_single_exp(il_test_ex_config, benchmark_config,
                                    tune_config_updates, log_dir, 'il_test')

    report_experiment_result(il_test_result)


def run_repl_only_exp(rep_ex_config, benchmark_config, config, log_dir):
    """Experiment that runs only representation learning."""
    rng, tune_config_updates = setup_run(config)
    del config

    tune_config_updates['repl'].update({
        'seed': rng.randint(1 << 31),
    })

    pretrain_result = run_single_exp(rep_ex_config, benchmark_config,
                                     tune_config_updates, log_dir, 'repl')
    report_experiment_result(pretrain_result)


def run_il_only_exp(il_train_ex_config, il_test_ex_config, benchmark_config,
                    config, log_dir):
    """Experiment that runs only imitation learning."""
    rng, tune_config_updates = setup_run(config)
    del config

    tune_config_updates['il_train'].update({'seed': rng.randint(1 << 31)})
    il_train_result = run_single_exp(il_train_ex_config, benchmark_config,
                                     tune_config_updates, log_dir, 'il_train')
    tune_config_updates['il_test'].update({
        'policy_path':
        il_train_result['model_path'],
        'seed':
        rng.randint(1 << 31),
    })
    il_test_result = run_single_exp(il_test_ex_config, benchmark_config,
                                    tune_config_updates, log_dir, 'il_test')
    report_experiment_result(il_test_result)


@chain_ex.config
def base_config():
    exp_name = "grid_search"
    # Name of the metric to optimise. By default, this will be automatically
    # selected based on the value of stages_to_run.
    metric = None
    stages_to_run = StagesToRun.REPL_ONLY
    spec = {
        # DO NOT UPDATE THESE DEFAULTS WITHOUT ALSO UPDATING CHAIN_CONFIG IN
        # test_support/configuration.py. They will affect unit tests!
        'repl': {
            'algo':
            tune.grid_search([
                'MoCoWithProjection',
                # 'SimCLR',
                # 'ActionConditionedTemporalCPC',
                # 'CEB',
                # 'DynamicsPrediction',
                # 'BYOL',
                # 'ActionConditionedTemporalCPC',
            ]),
        },
        'il_train': {
            'algo': tune.grid_search(['bc']),
            'freeze_encoder': tune.grid_search([True, False])
        },
        'il_test': {},
        'benchmark': {},
        # Example grid search config for benchmark:
        # tune.grid_search(
        #     # MAGICAL configs
        #     [{
        #         'benchmark_name': 'magical',
        #         'magical_env_prefix': magical_env_name,
        #         'magical_remove_null_actions': True,
        #     } for magical_env_name in [
        #         'MoveToCorner',
        #         'MoveToRegion',
        #         'MatchRegions',
        #         'FixColour',
        #         'FindDupe',
        #         # 'MakeLine',
        #         # 'ClusterColour',
        #         # 'ClusterShape',
        #     ]]
        #     # dm_control configs
        #     +  # (+ on a separate line for ease of commenting-out)
        #     [{
        #         'benchmark_name': 'dm_control',
        #         'dm_control_env': dm_control_env_name
        #     } for dm_control_env_name in [
        #         'reacher-easy',
        #         'finger-spin',
        #         'cheetah-run',
        #         'walker-walk',
        #         'cartpole-swingup',
        #         'reacher-easy',
        #         'ball-in-cup-catch',
        #     ]]
        #     # ATARI configs (TODO)
        #     +  # separate line for + again
        #     [],
        # )
    }
    # "use_skopt" will use scikit-optimize. This will ignore the 'spec' dict
    # above; instead, you need to declare an appropriate skopt_space. Use this
    # mode for hyperparameter tuning.
    use_skopt = False
    skopt_search_mode = None
    skopt_space = collections.OrderedDict()
    skopt_ref_configs = []

    # no updates, just leaving these in as a reminder that it's possible to
    # supply more updates to these parts in config files
    repl = {}
    il_train = {}
    il_test = {}
    benchmark = {}

    tune_run_kwargs = dict(num_samples=1,
                           resources_per_trial=dict(
                               cpu=1,
                               gpu=0,
                           ))
                           # queue_trials=True)
    ray_init_kwargs = dict(
        num_cpus=2,
        memory=None,
        object_store_memory=None,
        include_dashboard=False,
    )

    _ = locals()
    del _


@chain_ex.named_config
def cfg_use_magical():
    # see il_representations/envs/config for examples of what should go here
    benchmark = {
        'benchmark_name': 'magical',
        # MatchRegions is of intermediate difficulty
        # (TODO(sam): allow MAGICAL to load data from _all_ tasks at once, so
        # we can try multi-task repL)
        'magical_env_prefix': 'MatchRegions',
        # we really need magical_remove_null_actions=True for BC; for RepL it
        # shouldn't matter so much (for action-based RepL methods)
        'magical_remove_null_actions': False,
    }

    _ = locals()
    del _


@chain_ex.named_config
def cfg_use_dm_control():
    benchmark = {
        'benchmark_name': 'dm_control',
        # walker-walk is difficult relative to other dm-control tasks that we
        # use, but RL solves it quickly. Plateaus around 850-900 reward (see
        # https://docs.google.com/document/d/1YrXFCmCjdK2HK-WFrKNUjx03pwNUfNA6wwkO1QexfwY/edit#).
        'dm_control_env': 'reacher-easy',
    }

    _ = locals()
    del _


@chain_ex.named_config
def cfg_tune_moco():
    # these settings will be the same for all rep learning tune runs
    use_skopt = True
    skopt_search_mode = 'min'
    metric = 'repl_loss'
    stages_to_run = StagesToRun.REPL_ONLY

    # the following settings are algorithm-specific
    repl = {
        'algo': 'MoCo',
        'use_random_rollouts': False,
        'ppo_finetune': False,
        # this isn't a lot of training, but should be enough to tell whether
        # loss goes down quickly
        'pretrain_epochs': 250,

    }
    # this MUST be an ordered dict; skopt only looks at values (not k/v
    # mappings), so we must preserve the order of both values and keys
    skopt_space = collections.OrderedDict([
        # Below are some examples of ways you can declare opt variables.
        #
        # Using a log-uniform prior between some bounds:
        # ('lr', (1e-4, 1e-2, 'log-uniform')),
        #
        # Using a uniform distribution over integers:
        # ('nrollouts', (30, 150)),
        #
        # Using just a single value (in this case a float):
        # ('l1reg', [0.0]),
        ('repl:algo_params:augmenter_kwargs:augmenter_spec', [
            "translate,rotate,gaussian_blur", "translate,rotate",
            "translate,rotate,flip_ud,flip_lr"
        ]),
        ('repl:algo_params:batch_size', (64, 512)),
        ('repl:algo_params:encoder_kwargs:momentum_weight', (0.95, 0.999,
                                                             'log-uniform')),
        # ('repl:algo_params:decoder_kwargs:momentum_weight', (0.95, 0.999,
        #                                                      'log-uniform')),
        ('repl:algo_params:optimizer_kwargs:lr', (1e-6, 1e-2, 'log-uniform')),
        # ('repl:algo_params:projection_dim', (64, 256)),
        ('repl:algo_params:representation_dim', (8, 512)),

        ('repl:encoder_kwargs:obs_encoder_cls', ['BasicCNN', 'MAGICALCNN']),
    ])
    skopt_ref_configs = [
        # we include the default config as a reference

        # MoCoWithProjection
        # {
        #     'repl:algo_params:augmenter_kwargs:augmenter_spec':
        #     "translate,rotate,gaussian_blur",
        #     'repl:algo_params:batch_size':
        #     512,
        #     'repl:algo_params:decoder_kwargs:momentum_weight':
        #     0.99,
        #     'repl:algo_params:encoder_kwargs:momentum_weight':
        #     0.999,
        #     'repl:algo_params:optimizer_kwargs:lr':
        #     3e-5,
        #     'repl:algo_params:projection_dim':
        #     64,
        #     'repl:algo_params:representation_dim':
        #     128,
        #     'repl:encoder_kwargs:obs_encoder_cls':
        #     'BasicCNN',
        # }

        # MoCo
        {
            'repl:algo_params:augmenter_kwargs:augmenter_spec':
                "translate,rotate,gaussian_blur",
            'repl:algo_params:batch_size':
                512,
            'repl:algo_params:encoder_kwargs:momentum_weight':
                0.98,
            'repl:algo_params:optimizer_kwargs:lr':
                3e-5,
            'repl:algo_params:representation_dim':
                128,
            'repl:encoder_kwargs:obs_encoder_cls':
                'BasicCNN',
        }
    ]
    # do up to 200 runs of hyperparameter tuning
    # WARNING: This may require customisation on the command line. You want the
    # number to be high enough that the script will keep running for at least a
    # few days.
    tune_run_kwargs = dict(num_samples=2)

    _ = locals()
    del _


@chain_ex.named_config
def cfg_tune_cpc():
    # these settings will be the same for all rep learning tune runs
    use_skopt = True
    skopt_search_mode = 'min'
    metric = 'repl_loss'
    stages_to_run = StagesToRun.REPL_ONLY

    # the following settings are algorithm-specific
    repl = {
        'algo': 'ActionConditionedTemporalCPC',
        'use_random_rollouts': False,
        'ppo_finetune': False,
        # this isn't a lot of training, but should be enough to tell whether
        # loss goes down quickly
        'pretrain_epochs': 250,
    }
    # this MUST be an ordered dict; skopt only looks at values (not k/v
    # mappings), so we must preserve the order of both values and keys
    skopt_space = collections.OrderedDict([
        ('repl:algo_params:batch_size', (64, 512)),
        ('repl:algo_params:optimizer_kwargs:lr', (1e-6, 1e-2, 'log-uniform')),
        ('repl:algo_params:representation_dim', (8, 512)),
        ('repl:encoder_kwargs:obs_encoder_cls', ['BasicCNN', 'MAGICALCNN']),

        # RecurrentCPC params:
        # ('repl:algo_params:encoder_kwargs:rnn_output_dim', (8, 256)),

        # ActionConditionedTemporalCPC params:
        ('repl:algo_params:decoder_kwargs:action_encoding_dim', (64, 512)),
        ('repl:algo_params:decoder_kwargs:action_embedding_dim', (5, 30)),

        ('repl:algo_params:augmenter_kwargs:augmenter_spec', [
            "translate,rotate,gaussian_blur", "translate,rotate",
            "translate,rotate,flip_ud,flip_lr"
        ]),
    ])
    skopt_ref_configs = [
        # we include the default config as a reference

        # TemporalCPC
        {
            'repl:algo_params:batch_size':
            64,
            'repl:algo_params:optimizer_kwargs:lr':
            0.0003,
            'repl:algo_params:representation_dim':
            512,
            'repl:encoder_kwargs:obs_encoder_cls':
            'BasicCNN',
            'repl:algo_params:augmenter_kwargs:augmenter_spec':
            "translate,rotate,gaussian_blur",
        },

        # ActionConditionedCPC
        # {
        #     'repl:algo_params:batch_size':
        #         128,
        #     'repl:algo_params:representation_dim':
        #         128,
        #     'repl:algo_params:optimizer_kwargs:lr':
        #         1e-3,
        #     'repl:encoder_kwargs:obs_encoder_cls':
        #         'BasicCNN',
        #     'repl:algo_params:decoder_kwargs:action_encoding_dim': 128,
        #     'repl:algo_params:decoder_kwargs:action_embedding_dim': 5,
        #     'repl:algo_params:augmenter_kwargs:augmenter_spec':
        #         "translate,rotate,gaussian_blur",
        # }

        # RecurrentCPC
        {
            'repl:algo_params:batch_size':
                128,
            'repl:algo_params:encoder_kwargs:rnn_output_dim':
                64,
            'repl:algo_params:optimizer_kwargs:lr':
                0.001,
            'repl:algo_params:representation_dim':
                128,
            'repl:encoder_kwargs:obs_encoder_cls':
                'BasicCNN',
            'repl:algo_params:augmenter_kwargs:augmenter_spec':
                "translate,rotate,gaussian_blur",
        }
    ]
    # do up to 200 runs of hyperparameter tuning
    # WARNING: This may require customisation on the command line. You want the
    # number to be high enough that the script will keep running for at least a
    # few days.
    tune_run_kwargs = dict(num_samples=200)

    _ = locals()
    del _


@chain_ex.main
def run(exp_name, metric, spec, repl, il_train, il_test, benchmark,
        tune_run_kwargs, ray_init_kwargs, stages_to_run, use_skopt,
        skopt_search_mode, skopt_ref_configs, skopt_space):
    rep_ex_config = sacred_copy(repl)
    il_train_ex_config = sacred_copy(il_train)
    il_test_ex_config = sacred_copy(il_test)
    benchmark_config = sacred_copy(benchmark)
    spec = sacred_copy(spec)
    stages_to_run = get_stages_to_run(stages_to_run)
    log_dir = os.path.abspath(chain_ex.observers[0].dir)

    if metric is None:
        # choose a default metric depending on whether we're running
        # representation learning, IL, or both
        metric = {
            # return_mean is returned by il_test.run()
            StagesToRun.REPL_AND_IL:
            'return_mean',
            StagesToRun.IL_ONLY:
            'return_mean',
            # repl_loss is returned by run_rep_learner.run()
            StagesToRun.REPL_ONLY:
            'repl_loss',
        }[stages_to_run]

    # We remove unnecessary keys from the "spec" that we pass to Ray Tune. This
    # ensures that Ray Tune doesn't try to tune over things that can't affect
    # the outcome.

    if stages_to_run == StagesToRun.IL_ONLY \
       and 'repl' in spec:
        logging.warning(
            "You only asked to tune IL, so I'm removing the representation "
            "learning config from the Tune spec.")
        del spec['repl']

    if stages_to_run == StagesToRun.REPL_ONLY \
       and 'il_train' in spec:
        logging.warning(
            "You only asked to tune RepL, so I'm removing the imitation "
            "learning config from the Tune spec.")
        del spec['il_train']

    # make Ray run from this directory
    ray_dir = os.path.join(log_dir)
    os.makedirs(ray_dir, exist_ok=True)
    # Ray Tune will change the directory when tuning; this next step ensures
    # that pwd-relative data_roots remain valid.
    benchmark_config['data_root'] = os.path.abspath(
        os.path.join(cwd, benchmark_config['data_root']))

    def trainable_function(config):
        # "config" argument is passed in by Ray Tune
        config = expand_dict_keys(config)
        if stages_to_run == StagesToRun.REPL_AND_IL:
            run_end2end_exp(rep_ex_config, il_train_ex_config,
                            il_test_ex_config, benchmark_config, config,
                            log_dir)
        if stages_to_run == StagesToRun.IL_ONLY:
            run_il_only_exp(il_train_ex_config, il_test_ex_config,
                            benchmark_config, config, log_dir)
        if stages_to_run == StagesToRun.REPL_ONLY:
            run_repl_only_exp(rep_ex_config, benchmark_config, config, log_dir)

    if detect_ec2():
        ray.init(address="auto", **ray_init_kwargs)
    else:
        ray.init(**ray_init_kwargs)

    if use_skopt:
        assert skopt_search_mode in {'min', 'max'}, \
            'skopt_search_mode must be "min" or "max", as appropriate for ' \
            'the metric being optmised'
        assert len(skopt_space) > 0, "was passed an empty skopt_space"

        # do some sacred_copy() calls to ensure that we don't accidentally put
        # a ReadOnlyDict or ReadOnlyList into our optimizer
        skopt_space = sacred_copy(skopt_space)
        skopt_search_mode = sacred_copy(skopt_search_mode)
        skopt_ref_configs = sacred_copy(skopt_ref_configs)
        metric = sacred_copy(metric)

        sorted_space = collections.OrderedDict([
            (key, value) for key, value in sorted(skopt_space.items())
        ])
        skopt_optimiser = Optimizer(list(sorted_space.values()),
                                    base_estimator='RF')
        algo = SkOptSearch(skopt_optimiser,
                           list(sorted_space.keys()),
                           metric=metric,
                           mode=skopt_search_mode,
                           points_to_evaluate=[[
                               ref_config_dict[k] for k in sorted_space.keys()
                           ] for ref_config_dict in skopt_ref_configs])
        tune_run_kwargs = {
            'search_alg': algo,
            'scheduler': CheckpointFIFOScheduler(algo),
            **tune_run_kwargs,
        }
        # completely remove 'spec'
        if spec:
            logging.warning("Will ignore everything in 'spec' argument")
        spec = {}
    else:
        algo = None

    rep_run = tune.run(
        trainable_function,
        name=exp_name,
        config=spec,
        local_dir=ray_dir,
        **tune_run_kwargs,
    )

    best_config = rep_run.get_best_config(metric=metric)
    logging.info(f"Best config is: {best_config}")
    logging.info("Results available at: ")
    logging.info(rep_run._get_trial_paths())


if __name__ == '__main__':
    chain_ex.observers.append(FileStorageObserver('runs/chain_runs'))
    chain_ex.run_commandline()
